"""Versioned deterministic rules stored in the existing v1 rule tables.

Rule JSON is application data, never executable code. Label matching accepts a
small, bounded glob-like language where ``*`` separates literal fragments.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Literal

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.classification.matcher import AMBIGUOUS_MARKETPLACES, key_metadata
from app.core.errors import AppError
from app.db.errors import VersionConflictError
from app.db.models import Account, AuditEvent, Category, Payee, Rule, Transaction
from app.db.session import session_scope, write_transaction
from app.domain.flow_kinds import expected_sign, is_category_allowed
from app.domain.types import Acquisition, Beneficiary, Cadence, Flexibility, FlowKind
from app.services.allocations import AllocationPart

RuleStatus = Literal["draft", "approved", "disabled"]
ApplicationScope = Literal["selection", "future_only", "existing_and_future"]
Direction = Literal["debit", "credit"]
MAX_LABEL_PATTERN_LENGTH = 128
MAX_LABEL_WILDCARDS = 4
MAX_RULE_NAME_LENGTH = 120
MAX_SCOPE_IDS = 500
MAX_RULE_EXCEPTIONS = 10_000


class RuleStoreError(AppError):
    """A rule was invalid or could not be changed safely."""


@dataclass(frozen=True, slots=True)
class RuleCondition:
    """Explicit predicates; amounts are absolute integer cents."""

    payee_id: str | None = None
    label_pattern: str | None = None
    account_ids: tuple[str, ...] = ()
    direction: Direction | None = None
    min_amount_cents: int | None = None
    max_amount_cents: int | None = None
    booked_from: date | None = None
    booked_through: date | None = None
    flow_kind: FlowKind | str | None = None


@dataclass(frozen=True, slots=True)
class RatioPart:
    category_id: str
    basis_points: int


@dataclass(frozen=True, slots=True)
class RuleAction:
    flow_kind: FlowKind | str
    category_id: str | None = None
    beneficiary: Beneficiary | str = Beneficiary.UNKNOWN
    cadence: Cadence | str = Cadence.UNKNOWN
    flexibility: Flexibility | str = Flexibility.UNKNOWN
    acquisition: Acquisition | str = Acquisition.UNKNOWN
    ratios: tuple[RatioPart, ...] = ()


@dataclass(frozen=True, slots=True)
class RuleDraft:
    name: str
    condition: RuleCondition
    action: RuleAction
    priority: int = 100
    application_scope: ApplicationScope = "selection"
    ambiguous_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RuleException:
    transaction_id: str
    reason: str
    actor: str
    created_at_utc: datetime


@dataclass(frozen=True, slots=True)
class RuleRecord:
    rule_id: str
    name: str
    condition: RuleCondition
    action: RuleAction
    priority: int
    status: RuleStatus
    application_scope: ApplicationScope
    version: int
    created_by: str
    updated_by: str
    approved_by: str | None
    created_at_utc: datetime
    updated_at_utc: datetime
    ambiguous_reason: str | None = None
    exceptions: tuple[RuleException, ...] = ()


def _fail(code: str, message: str, status: int = 422) -> RuleStoreError:
    return RuleStoreError(code, status, message)


def _strict_int(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise _fail("RULE_INVALID", f"{field_name} doit être un entier strict.")
    return value


def _actor(actor: str) -> str:
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 160:
        raise _fail("RULE_INVALID", "Un acteur non vide et borné est requis.")
    return actor.strip()


def _validate_pattern(pattern: str | None) -> None:
    if pattern is None:
        return
    if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_LABEL_PATTERN_LENGTH:
        raise _fail("RULE_PATTERN_INVALID", "Le motif de libellé est vide ou trop long.")
    if sum(character == "*" for character in pattern) > MAX_LABEL_WILDCARDS:
        raise _fail("RULE_PATTERN_TOO_COMPLEX", "Le motif dépasse la limite de jokers.")
    if not any(character != "*" for character in pattern):
        raise _fail(
            "RULE_PATTERN_INVALID", "Le motif doit contenir au moins un caractère littéral."
        )
    if any(ord(character) < 32 for character in pattern):
        raise _fail("RULE_PATTERN_INVALID", "Le motif contient un caractère de contrôle.")
    _compiled_pattern(pattern)


@lru_cache(maxsize=256)
def _compiled_pattern(pattern: str) -> tuple[re.Pattern[str], ...]:
    """Compile only escaped literal fragments; user input is never regex syntax."""
    return tuple(
        re.compile(re.escape(fragment), re.IGNORECASE)
        for fragment in pattern.split("*")
        if fragment
    )


def label_pattern_matches(pattern: str, label: str) -> bool:
    """Match literal segments in order, with ``*`` matching any intervening text."""
    fragments = _compiled_pattern(pattern)
    cursor = 0
    for fragment in fragments:
        match = fragment.search(label, cursor)
        if match is None:
            return False
        cursor = match.end()
    return True


def _validate_condition(condition: RuleCondition) -> None:
    if not isinstance(condition, RuleCondition):
        raise _fail("RULE_INVALID", "La condition doit être explicite.")
    if condition.payee_id is not None and (
        not isinstance(condition.payee_id, str) or not condition.payee_id.strip()
    ):
        raise _fail("RULE_INVALID", "L'identifiant de contrepartie est invalide.")
    _validate_pattern(condition.label_pattern)
    if len(condition.account_ids) > MAX_SCOPE_IDS:
        raise _fail("RULE_SCOPE_TOO_LARGE", "La portée dépasse le nombre maximal de comptes.")
    if any(not isinstance(value, str) or not value.strip() for value in condition.account_ids):
        raise _fail("RULE_INVALID", "Un identifiant de compte est invalide.")
    if len(set(condition.account_ids)) != len(condition.account_ids):
        raise _fail("RULE_INVALID", "Un compte apparaît plusieurs fois dans la condition.")
    if condition.direction not in (None, "debit", "credit"):
        raise _fail("RULE_INVALID", "La direction doit être debit ou credit.")
    for name, value in (
        ("min_amount_cents", condition.min_amount_cents),
        ("max_amount_cents", condition.max_amount_cents),
    ):
        if value is not None and _strict_int(value, name) < 0:
            raise _fail("RULE_INVALID", f"{name} doit être positif ou nul.")
    if (
        condition.min_amount_cents is not None
        and condition.max_amount_cents is not None
        and condition.min_amount_cents > condition.max_amount_cents
    ):
        raise _fail("RULE_INVALID", "La plage de montants est inversée.")
    if condition.booked_from is not None and not isinstance(condition.booked_from, date):
        raise _fail("RULE_INVALID", "La date de début est invalide.")
    if condition.booked_through is not None and not isinstance(condition.booked_through, date):
        raise _fail("RULE_INVALID", "La date de fin est invalide.")
    if (
        condition.booked_from is not None
        and condition.booked_through is not None
        and condition.booked_from > condition.booked_through
    ):
        raise _fail("RULE_INVALID", "La plage de dates est inversée.")
    if condition.flow_kind is not None:
        try:
            FlowKind(condition.flow_kind)
        except (TypeError, ValueError) as error:
            raise _fail("RULE_INVALID", "Le flux de condition est invalide.") from error


def _validate_action(action: RuleAction) -> RuleAction:
    if not isinstance(action, RuleAction):
        raise _fail("RULE_INVALID", "L'action de règle est invalide.")
    try:
        flow = FlowKind(action.flow_kind)
        beneficiary = Beneficiary(action.beneficiary)
        cadence = Cadence(action.cadence)
        flexibility = Flexibility(action.flexibility)
        acquisition = Acquisition(action.acquisition)
    except (TypeError, ValueError) as error:
        raise _fail("RULE_INVALID", "Une valeur de flux ou d'attribut est invalide.") from error
    if flow is FlowKind.UNKNOWN:
        raise _fail("RULE_INVALID", "Le flux unknown ne peut pas être alloué.")
    if action.ratios:
        if action.category_id is not None:
            raise _fail(
                "RULE_INVALID", "Une action ratio ne doit pas définir une catégorie unique."
            )
        if len(action.ratios) < 2 or len(action.ratios) > 8:
            raise _fail("RULE_INVALID", "Une ventilation ratio exige de 2 à 8 parts.")
        if any(
            type(part.basis_points) is not int or part.basis_points <= 0 for part in action.ratios
        ):
            raise _fail(
                "RULE_INVALID", "Chaque ratio doit être un entier positif en points de base."
            )
        if sum(part.basis_points for part in action.ratios) != 10_000:
            raise _fail(
                "RULE_RATIO_TOTAL_INVALID", "La somme des ratios doit valoir 10000 points de base."
            )
        if len({part.category_id for part in action.ratios}) != len(action.ratios):
            raise _fail("RULE_INVALID", "Une catégorie apparaît plusieurs fois dans le ratio.")
        if any(not is_category_allowed(flow, part.category_id) for part in action.ratios):
            raise _fail(
                "RULE_CATEGORY_FLOW_INVALID", "Une catégorie du ratio est interdite pour ce flux."
            )
    else:
        if not isinstance(action.category_id, str) or not is_category_allowed(
            flow, action.category_id
        ):
            raise _fail("RULE_CATEGORY_FLOW_INVALID", "La catégorie est interdite pour ce flux.")
    return RuleAction(
        flow_kind=flow,
        category_id=action.category_id,
        beneficiary=beneficiary,
        cadence=cadence,
        flexibility=flexibility,
        acquisition=acquisition,
        ratios=tuple(action.ratios),
    )


def validate_rule_draft(draft: RuleDraft) -> RuleDraft:
    if not isinstance(draft, RuleDraft):
        raise _fail("RULE_INVALID", "La règle doit être un brouillon typé.")
    if (
        not isinstance(draft.name, str)
        or not draft.name.strip()
        or len(draft.name) > MAX_RULE_NAME_LENGTH
    ):
        raise _fail("RULE_INVALID", "Le nom de règle est vide ou trop long.")
    if type(draft.priority) is not int:
        raise _fail("RULE_INVALID", "La priorité doit être un entier strict.")
    if draft.application_scope not in ("selection", "future_only", "existing_and_future"):
        raise _fail("RULE_INVALID", "La portée d'application est invalide.")
    _validate_condition(draft.condition)
    action = _validate_action(draft.action)
    if (
        draft.condition.flow_kind is not None
        and FlowKind(draft.condition.flow_kind) != action.flow_kind
    ):
        raise _fail(
            "RULE_FLOW_CONDITION_MISMATCH",
            "Le flux de condition doit correspondre au flux de l'action.",
        )
    if draft.ambiguous_reason is not None and (
        not isinstance(draft.ambiguous_reason, str) or len(draft.ambiguous_reason.strip()) > 300
    ):
        raise _fail("RULE_INVALID", "Le motif de contrepartie ambiguë dépasse la limite autorisée.")
    return RuleDraft(
        name=draft.name.strip(),
        condition=draft.condition,
        action=action,
        priority=draft.priority,
        application_scope=draft.application_scope,
        ambiguous_reason=draft.ambiguous_reason.strip() if draft.ambiguous_reason else None,
    )


def _ratio_order(action: RuleAction) -> tuple[RatioPart, ...]:
    return tuple(sorted(action.ratios, key=lambda part: (-part.basis_points, part.category_id)))


def _top_category(action: RuleAction) -> str:
    if action.ratios:
        return _ratio_order(action)[0].category_id
    assert action.category_id is not None
    return action.category_id


def _condition_document(condition: RuleCondition) -> dict[str, object]:
    return {
        "payee_id": condition.payee_id,
        "label_pattern": condition.label_pattern,
        "account_ids": list(condition.account_ids),
        "direction": condition.direction,
        "min_amount_cents": condition.min_amount_cents,
        "max_amount_cents": condition.max_amount_cents,
        "booked_from": condition.booked_from.isoformat() if condition.booked_from else None,
        "booked_through": condition.booked_through.isoformat()
        if condition.booked_through
        else None,
        "flow_kind": FlowKind(condition.flow_kind).value
        if condition.flow_kind is not None
        else None,
    }


def _effect_document(action: RuleAction) -> dict[str, object]:
    return {
        "beneficiary": Beneficiary(action.beneficiary).value,
        "cadence": Cadence(action.cadence).value,
        "flexibility": Flexibility(action.flexibility).value,
        "acquisition": Acquisition(action.acquisition).value,
        "ratios": [
            {"category_id": part.category_id, "basis_points": part.basis_points}
            for part in _ratio_order(action)
        ],
    }


def _scope_kind(condition: RuleCondition) -> str:
    if condition.label_pattern is not None and condition.payee_id is None:
        return "label_pattern"
    context = any(
        (
            condition.label_pattern,
            condition.account_ids,
            condition.direction,
            condition.min_amount_cents is not None,
            condition.max_amount_cents is not None,
            condition.booked_from,
            condition.booked_through,
            condition.flow_kind,
        )
    )
    return "payee_plus_context" if condition.payee_id and context else "exact_payee"


def _scope_document(
    draft: RuleDraft,
    *,
    created_by: str,
    updated_by: str,
    exceptions: Sequence[RuleException] = (),
) -> dict[str, object]:
    return {
        "condition": _condition_document(draft.condition),
        "application_scope": draft.application_scope,
        "exceptions": [
            {
                "transaction_id": item.transaction_id,
                "reason": item.reason,
                "actor": item.actor,
                "created_at_utc": item.created_at_utc.isoformat(),
            }
            for item in sorted(exceptions, key=lambda item: item.transaction_id)
        ],
        "provenance": {
            "created_by": created_by,
            "updated_by": updated_by,
            "ambiguous_reason": draft.ambiguous_reason,
        },
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_ambiguous_marketplace(payee: Payee | None, pattern: str | None) -> bool:
    if payee is not None:
        metadata = key_metadata(payee.normalized_name)
        if metadata is not None and metadata.get("entity_kind") == "marketplace":
            return True
        searchable = payee.display_name.casefold()
    else:
        searchable = ""
    if pattern:
        searchable += " " + pattern.casefold()
    return any(token.casefold() in searchable for token in AMBIGUOUS_MARKETPLACES)


def _has_additional_constraint(condition: RuleCondition) -> bool:
    return bool(
        condition.account_ids
        or condition.direction
        or condition.min_amount_cents is not None
        or condition.max_amount_cents is not None
        or condition.booked_from
        or condition.booked_through
        or condition.flow_kind
    )


def _validate_match_scope(draft: RuleDraft) -> None:
    if draft.application_scope != "selection" and not (
        draft.condition.payee_id or draft.condition.label_pattern
    ):
        raise _fail(
            "RULE_MATCH_SCOPE_REQUIRED",
            "Une règle future doit viser une contrepartie exacte ou un motif de libellé borné.",
        )


def _guard_ambiguous_global_rule(
    session: Session, draft: RuleDraft, *, scope: ApplicationScope
) -> None:
    payee = session.get(Payee, draft.condition.payee_id) if draft.condition.payee_id else None
    if draft.condition.payee_id and payee is None:
        raise _fail("RULE_PAYEE_NOT_FOUND", "La contrepartie exacte n'existe pas.", 404)
    if payee is not None and payee.is_active != 1:
        raise _fail("RULE_PAYEE_UNAVAILABLE", "La contrepartie exacte est désactivée.", 409)
    ambiguous = _is_ambiguous_marketplace(payee, draft.condition.label_pattern)
    if not ambiguous or scope == "selection":
        return
    if (
        not draft.ambiguous_reason
        or not draft.condition.label_pattern
        or not _has_additional_constraint(draft.condition)
    ):
        raise _fail(
            "AMBIGUOUS_MARKETPLACE_GLOBAL_SCOPE",
            "Une règle globale sur une marketplace ambiguë exige un motif de libellé, "
            "une contrainte additionnelle et une justification.",
            409,
        )


def _validate_accounts_exist(session: Session, condition: RuleCondition) -> None:
    missing = [
        account_id
        for account_id in condition.account_ids
        if session.get(Account, account_id) is None
    ]
    if missing:
        raise _fail("RULE_ACCOUNT_NOT_FOUND", "Un compte de la condition n'existe pas.", 404)


def _validate_categories_exist(session: Session, action: RuleAction) -> None:
    category_ids = (
        [part.category_id for part in action.ratios] if action.ratios else [action.category_id]
    )
    for category_id in category_ids:
        category = session.get(Category, category_id)
        if category is None or category.is_active != 1:
            raise _fail(
                "RULE_CATEGORY_UNAVAILABLE", "Une catégorie de la règle est inconnue ou désactivée."
            )


def _record(rule: Rule) -> RuleRecord:
    try:
        scope = json.loads(rule.scope_json)
        effect = json.loads(rule.effect_json)
        condition_payload = scope["condition"]
        provenance = scope["provenance"]
        ratios_payload = effect["ratios"]
        condition = RuleCondition(
            payee_id=condition_payload.get("payee_id"),
            label_pattern=condition_payload.get("label_pattern"),
            account_ids=tuple(condition_payload.get("account_ids", [])),
            direction=condition_payload.get("direction"),
            min_amount_cents=condition_payload.get("min_amount_cents"),
            max_amount_cents=condition_payload.get("max_amount_cents"),
            booked_from=date.fromisoformat(condition_payload["booked_from"])
            if condition_payload.get("booked_from")
            else None,
            booked_through=date.fromisoformat(condition_payload["booked_through"])
            if condition_payload.get("booked_through")
            else None,
            flow_kind=condition_payload.get("flow_kind"),
        )
        action = RuleAction(
            flow_kind=rule.flow_kind,
            category_id=None if ratios_payload else rule.category_id,
            beneficiary=effect["beneficiary"],
            cadence=effect["cadence"],
            flexibility=effect["flexibility"],
            acquisition=effect["acquisition"],
            ratios=tuple(
                RatioPart(category_id=item["category_id"], basis_points=item["basis_points"])
                for item in ratios_payload
            ),
        )
        exceptions = tuple(
            RuleException(
                transaction_id=item["transaction_id"],
                reason=item["reason"],
                actor=item.get("actor", provenance["updated_by"]),
                created_at_utc=datetime.fromisoformat(
                    item.get("created_at_utc", rule.updated_at_utc.isoformat())
                ),
            )
            for item in scope.get("exceptions", [])
        )
        scope_value = scope["application_scope"]
        status: RuleStatus = (
            "disabled" if not rule.is_active else "approved" if rule.is_approved else "draft"
        )
        return RuleRecord(
            rule_id=rule.rule_id,
            name=rule.name,
            condition=condition,
            action=action,
            priority=rule.priority,
            status=status,
            application_scope=scope_value,
            version=rule.version,
            created_by=provenance["created_by"],
            updated_by=provenance["updated_by"],
            approved_by=rule.approved_by,
            created_at_utc=rule.created_at_utc,
            updated_at_utc=rule.updated_at_utc,
            ambiguous_reason=provenance.get("ambiguous_reason"),
            exceptions=exceptions,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise _fail(
            "RULE_STORAGE_INVALID", "Le document d'une règle stockée est invalide.", 500
        ) from error


def _audit(
    session: Session,
    *,
    rule_id: str,
    actor: str,
    action: str,
    before: dict[str, object] | None,
    after: dict[str, object],
) -> None:
    session.add(
        AuditEvent(
            target_type="rule",
            target_id=rule_id,
            action=action,
            actor=actor,
            before_json=_canonical_json(before) if before is not None else None,
            after_json=_canonical_json(after),
        )
    )


def _rule_snapshot(record: RuleRecord) -> dict[str, object]:
    pattern = record.condition.label_pattern
    return {
        "rule_id": record.rule_id,
        "version": record.version,
        "status": record.status,
        "label_pattern_length": None if pattern is None else len(pattern),
        "label_pattern_sha256": (
            None if pattern is None else hashlib.sha256(pattern.encode("utf-8")).hexdigest()
        ),
        "effect": _effect_document(record.action),
        "flow_kind": str(record.action.flow_kind),
        "priority": record.priority,
        "application_scope": record.application_scope,
        "exceptions": [
            {
                "transaction_id": item.transaction_id,
                "reason_length": len(item.reason),
                "reason_sha256": hashlib.sha256(item.reason.encode("utf-8")).hexdigest(),
                "actor": item.actor,
                "created_at_utc": item.created_at_utc.isoformat(),
            }
            for item in record.exceptions
        ],
    }


def create_rule(engine: Engine, draft: RuleDraft, *, actor: str) -> RuleRecord:
    """Create a draft rule and audit its provenance in the same SQL transaction."""
    normalized = validate_rule_draft(draft)
    actor_value = _actor(actor)
    _validate_match_scope(normalized)
    rule_id = str(uuid.uuid4())
    with write_transaction(engine) as session:
        _guard_ambiguous_global_rule(session, normalized, scope=normalized.application_scope)
        _validate_accounts_exist(session, normalized.condition)
        _validate_categories_exist(session, normalized.action)
        row = Rule(
            rule_id=rule_id,
            name=normalized.name,
            scope_kind=_scope_kind(normalized.condition),
            scope_json=_canonical_json(
                _scope_document(normalized, created_by=actor_value, updated_by=actor_value)
            ),
            flow_kind=FlowKind(normalized.action.flow_kind).value,
            category_id=_top_category(normalized.action),
            effect_json=_canonical_json(_effect_document(normalized.action)),
            priority=normalized.priority,
            is_approved=0,
            is_active=1,
        )
        session.add(row)
        session.flush()
        _audit(
            session,
            rule_id=rule_id,
            actor=actor_value,
            action="rule.created",
            before=None,
            after=_rule_snapshot(_record(row)),
        )
        session.flush()
        return _record(row)


def update_rule(
    engine: Engine,
    rule_id: str,
    draft: RuleDraft,
    *,
    expected_version: int,
    actor: str,
) -> RuleRecord:
    """Replace a rule optimistically; any edit returns it to draft status."""
    normalized = validate_rule_draft(draft)
    expected = _strict_int(expected_version, "expected_version")
    actor_value = _actor(actor)
    _validate_match_scope(normalized)
    with write_transaction(engine) as session:
        row = session.get(Rule, rule_id)
        if row is None:
            raise _fail("RULE_NOT_FOUND", "Règle introuvable.", 404)
        if row.version != expected:
            raise VersionConflictError()
        _guard_ambiguous_global_rule(session, normalized, scope=normalized.application_scope)
        _validate_accounts_exist(session, normalized.condition)
        _validate_categories_exist(session, normalized.action)
        before_record = _record(row)
        current_scope = json.loads(row.scope_json)
        provenance = current_scope["provenance"]
        scope = _scope_document(
            normalized,
            created_by=provenance["created_by"],
            updated_by=actor_value,
            exceptions=before_record.exceptions,
        )
        row.name = normalized.name
        row.scope_kind = _scope_kind(normalized.condition)
        row.scope_json = _canonical_json(scope)
        row.flow_kind = FlowKind(normalized.action.flow_kind).value
        row.category_id = _top_category(normalized.action)
        row.effect_json = _canonical_json(_effect_document(normalized.action))
        row.priority = normalized.priority
        row.is_approved = 0
        row.is_active = 1
        row.approved_by = None
        row.version += 1
        now = datetime.now(UTC)
        row.updated_at_utc = now.replace(microsecond=(now.microsecond // 1000) * 1000)
        session.flush()
        after_record = _record(row)
        _audit(
            session,
            rule_id=rule_id,
            actor=actor_value,
            action="rule.updated",
            before=_rule_snapshot(before_record),
            after=_rule_snapshot(after_record),
        )
        return after_record


def approve_rule(engine: Engine, rule_id: str, *, expected_version: int, actor: str) -> RuleRecord:
    """Approve the current exact version. Reapproval is idempotent."""
    expected = _strict_int(expected_version, "expected_version")
    actor_value = _actor(actor)
    with write_transaction(engine) as session:
        row = session.get(Rule, rule_id)
        if row is None:
            raise _fail("RULE_NOT_FOUND", "Règle introuvable.", 404)
        if row.version != expected:
            raise VersionConflictError()
        current = _record(row)
        _guard_ambiguous_global_rule(
            session,
            RuleDraft(
                name=current.name,
                condition=current.condition,
                action=current.action,
                priority=current.priority,
                application_scope=current.application_scope,
                ambiguous_reason=current.ambiguous_reason,
            ),
            scope=current.application_scope,
        )
        _validate_accounts_exist(session, current.condition)
        _validate_categories_exist(session, current.action)
        if row.is_approved and row.is_active and row.approved_by == actor_value:
            return _record(row)
        before = _rule_snapshot(_record(row))
        row.is_approved = 1
        row.is_active = 1
        row.approved_by = actor_value
        row.version += 1
        now = datetime.now(UTC)
        row.updated_at_utc = now.replace(microsecond=(now.microsecond // 1000) * 1000)
        session.flush()
        after = _record(row)
        _audit(
            session,
            rule_id=rule_id,
            actor=actor_value,
            action="rule.approved",
            before=before,
            after=_rule_snapshot(after),
        )
        return after


def disable_rule(engine: Engine, rule_id: str, *, expected_version: int, actor: str) -> RuleRecord:
    expected = _strict_int(expected_version, "expected_version")
    actor_value = _actor(actor)
    with write_transaction(engine) as session:
        row = session.get(Rule, rule_id)
        if row is None:
            raise _fail("RULE_NOT_FOUND", "Règle introuvable.", 404)
        if row.version != expected:
            raise VersionConflictError()
        if row.is_active == 0:
            return _record(row)
        before = _rule_snapshot(_record(row))
        row.is_active = 0
        row.version += 1
        now = datetime.now(UTC)
        row.updated_at_utc = now.replace(microsecond=(now.microsecond // 1000) * 1000)
        session.flush()
        after = _record(row)
        _audit(
            session,
            rule_id=rule_id,
            actor=actor_value,
            action="rule.disabled",
            before=before,
            after=_rule_snapshot(after),
        )
        return after


def exclude_transaction_from_rule(
    engine: Engine,
    rule_id: str,
    transaction_id: str,
    *,
    expected_version: int,
    actor: str,
    reason: str,
) -> RuleRecord:
    """Persist a transaction-specific exception inside versioned ``scope_json``."""
    expected = _strict_int(expected_version, "expected_version")
    actor_value = _actor(actor)
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 300:
        raise _fail("RULE_EXCEPTION_REASON_REQUIRED", "Une justification courte est requise.")
    with write_transaction(engine) as session:
        row = session.get(Rule, rule_id)
        if row is None:
            raise _fail("RULE_NOT_FOUND", "Règle introuvable.", 404)
        if row.version != expected:
            raise VersionConflictError()
        if session.get(Transaction, transaction_id) is None:
            raise _fail("TRANSACTION_NOT_FOUND", "Transaction introuvable.", 404)
        before_record = _record(row)
        existing = {item.transaction_id: item for item in before_record.exceptions}
        prior = existing.get(transaction_id)
        if prior is not None and prior.reason == reason.strip():
            return before_record
        if prior is None and len(existing) >= MAX_RULE_EXCEPTIONS:
            raise _fail(
                "RULE_EXCEPTION_LIMIT",
                "La règle a atteint le plafond d'exceptions par transaction.",
                409,
            )
        now = datetime.now(UTC)
        exception = RuleException(
            transaction_id=transaction_id,
            reason=reason.strip(),
            actor=actor_value,
            created_at_utc=now.replace(microsecond=(now.microsecond // 1000) * 1000),
        )
        existing[transaction_id] = exception
        scope = json.loads(row.scope_json)
        scope["exceptions"] = [
            {
                "transaction_id": item.transaction_id,
                "reason": item.reason,
                "actor": item.actor,
                "created_at_utc": item.created_at_utc.isoformat(),
            }
            for item in sorted(existing.values(), key=lambda item: item.transaction_id)
        ]
        provenance = scope["provenance"]
        provenance["updated_by"] = actor_value
        row.scope_json = _canonical_json(scope)
        row.version += 1
        row.updated_at_utc = now.replace(microsecond=(now.microsecond // 1000) * 1000)
        session.flush()
        after = _record(row)
        _audit(
            session,
            rule_id=rule_id,
            actor=actor_value,
            action="rule.transaction_excluded",
            before=_rule_snapshot(before_record),
            after=_rule_snapshot(after),
        )
        return after


def get_rule(engine: Engine, rule_id: str) -> RuleRecord:
    with session_scope(engine) as session:
        row = session.get(Rule, rule_id)
        if row is None:
            raise _fail("RULE_NOT_FOUND", "Règle introuvable.", 404)
        return _record(row)


def list_rules(engine: Engine, *, approved_only: bool = False) -> tuple[RuleRecord, ...]:
    with session_scope(engine) as session:
        statement = select(Rule).order_by(Rule.priority, Rule.rule_id)
        if approved_only:
            statement = statement.where(Rule.is_approved == 1, Rule.is_active == 1)
        return tuple(_record(row) for row in session.scalars(statement))


def matches_condition(
    session: Session,
    rule: RuleRecord,
    transaction: Transaction,
    *,
    exception_ids: frozenset[str] | None = None,
) -> bool:
    """Evaluate the safe, exact predicates without mutating ORM state."""
    condition = rule.condition
    excluded_ids = (
        exception_ids
        if exception_ids is not None
        else frozenset(item.transaction_id for item in rule.exceptions)
    )
    if transaction.transaction_id in excluded_ids:
        return False
    if condition.payee_id and transaction.payee_id != condition.payee_id:
        return False
    if condition.account_ids and transaction.account_id not in condition.account_ids:
        return False
    if condition.direction == "debit" and transaction.amount_cents >= 0:
        return False
    if condition.direction == "credit" and transaction.amount_cents <= 0:
        return False
    magnitude = abs(transaction.amount_cents)
    if condition.min_amount_cents is not None and magnitude < condition.min_amount_cents:
        return False
    if condition.max_amount_cents is not None and magnitude > condition.max_amount_cents:
        return False
    if condition.booked_from and transaction.booked_on < condition.booked_from:
        return False
    if condition.booked_through and transaction.booked_on > condition.booked_through:
        return False
    if condition.label_pattern and not label_pattern_matches(
        condition.label_pattern, transaction.identity_label
    ):
        return False
    if condition.flow_kind is not None:
        from app.db.models import ClassificationSuggestion

        known_flow = session.scalar(
            select(ClassificationSuggestion.suggestion_id)
            .where(
                ClassificationSuggestion.transaction_id == transaction.transaction_id,
                ClassificationSuggestion.status == "review",
                ClassificationSuggestion.proposed_flow_kind == FlowKind(condition.flow_kind).value,
            )
            .limit(1)
        )
        if known_flow is None:
            return False
    categories = (
        [part.category_id for part in rule.action.ratios]
        if rule.action.ratios
        else [rule.action.category_id]
    )
    for category_id in categories:
        if category_id is None:
            return False
        expected = expected_sign(rule.action.flow_kind, category_id)
        if expected is not None and (transaction.amount_cents > 0) != (expected > 0):
            return False
    return True


def action_parts(action: RuleAction, amount_cents: int) -> tuple[AllocationPart, ...]:
    """Expand a category or ADR-007 ratios into allocation service parts."""
    if type(amount_cents) is not int or amount_cents == 0:
        raise _fail(
            "RULE_INVALID_AMOUNT", "Le montant de transaction doit être non nul en centimes."
        )
    flow = FlowKind(action.flow_kind)
    if not action.ratios:
        assert action.category_id is not None
        return (
            AllocationPart(
                flow_kind=flow,
                category_id=action.category_id,
                amount_cents=amount_cents,
                beneficiary=Beneficiary(action.beneficiary),
                cadence=Cadence(action.cadence),
                flexibility=Flexibility(action.flexibility),
                acquisition=Acquisition(action.acquisition),
            ),
        )
    ratios = _ratio_order(action)
    sign = -1 if amount_cents < 0 else 1
    absolute_amount = abs(amount_cents)
    portions: list[int] = []
    for part in ratios[:-1]:
        numerator = absolute_amount * part.basis_points
        rounded = (numerator * 2 + 10_000) // 20_000
        portions.append(sign * rounded)
    portions.append(amount_cents - sum(portions))
    return tuple(
        AllocationPart(
            flow_kind=flow,
            category_id=ratio.category_id,
            amount_cents=portion,
            beneficiary=Beneficiary(action.beneficiary),
            cadence=Cadence(action.cadence),
            flexibility=Flexibility(action.flexibility),
            acquisition=Acquisition(action.acquisition),
        )
        for ratio, portion in zip(ratios, portions, strict=True)
        if portion != 0
    )


def action_signature(action: RuleAction) -> tuple[object, ...]:
    return (
        FlowKind(action.flow_kind).value,
        action.category_id,
        tuple((part.category_id, part.basis_points) for part in _ratio_order(action)),
        Beneficiary(action.beneficiary).value,
        Cadence(action.cadence).value,
        Flexibility(action.flexibility).value,
        Acquisition(action.acquisition).value,
    )
