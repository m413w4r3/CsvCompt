"""Transactional allocation writes with audit and human-decision protection."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import wraps
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.errors import VersionConflictError
from app.db.models import (
    Allocation,
    AuditEvent,
    ClassificationDecision,
    Transaction,
    TransactionLink,
)
from app.db.session import session_scope, write_transaction
from app.domain.flow_kinds import expected_sign, is_category_allowed
from app.domain.types import (
    Acquisition,
    Beneficiary,
    Cadence,
    Flexibility,
    FlowKind,
    Origin,
)

AllocationMode = Literal["complete", "partial"]
AllocationOrigin = Literal["manual", "rule", "history", "llm"]


@dataclass(frozen=True, slots=True)
class AllocationPart:
    """One confirmed allocation supplied to :func:`allocate`."""

    flow_kind: FlowKind | str
    category_id: str
    amount_cents: int
    beneficiary: Beneficiary | str = Beneficiary.UNKNOWN
    cadence: Cadence | str = Cadence.UNKNOWN
    flexibility: Flexibility | str = Flexibility.UNKNOWN
    acquisition: Acquisition | str = Acquisition.UNKNOWN


@dataclass(frozen=True, slots=True)
class AllocationRecord:
    allocation_id: str
    flow_kind: str
    category_id: str
    amount_cents: int
    origin: str
    beneficiary: str
    cadence: str
    flexibility: str
    acquisition: str


@dataclass(frozen=True, slots=True)
class AllocationState:
    transaction_id: str
    transaction_amount_cents: int
    version: int
    allocations: tuple[AllocationRecord, ...]
    residual_cents: int
    state: Literal["unclassified", "partial", "complete"]
    derived_flow_kinds: tuple[str, ...]
    decision_id: str | None = None


@dataclass(frozen=True, slots=True)
class _NormalizedPart:
    flow_kind: FlowKind
    category_id: str
    amount_cents: int
    beneficiary: Beneficiary
    cadence: Cadence
    flexibility: Flexibility
    acquisition: Acquisition

    def business_key(self) -> tuple[str, str, int, str, str, str, str]:
        return (
            self.flow_kind.value,
            self.category_id,
            self.amount_cents,
            self.beneficiary.value,
            self.cadence.value,
            self.flexibility.value,
            self.acquisition.value,
        )

    def amount_key(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.flow_kind.value,
            self.category_id,
            self.beneficiary.value,
            self.cadence.value,
            self.flexibility.value,
            self.acquisition.value,
        )

    def as_json(self, origin: str) -> dict[str, str | int]:
        return {
            "flow_kind": self.flow_kind.value,
            "category_id": self.category_id,
            "amount_cents": self.amount_cents,
            "origin": origin,
            "beneficiary": self.beneficiary.value,
            "cadence": self.cadence.value,
            "flexibility": self.flexibility.value,
            "acquisition": self.acquisition.value,
        }


def _fail(code: str, status: int, message: str) -> AppError:
    return AppError(code, status, message)


def _translate_integrity_errors[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Keep raw SQLite constraint failures inside the typed service contract."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except IntegrityError as error:
            raise _fail(
                "ALLOCATION_CONFLICT",
                409,
                "La mutation des allocations entre en conflit avec une contrainte métier.",
            ) from error

    return wrapped


def _strict_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise _fail("ALLOCATION_INVALID", 422, f"{field} doit être un entier strict.")
    return value


def _enum_value(enum_type: type[Any], value: object, field: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        raise _fail("ALLOCATION_INVALID", 422, f"Dimension invalide : {field}.") from error


def _part_value(part: AllocationPart | Mapping[str, object]) -> AllocationPart:
    if isinstance(part, AllocationPart):
        return part
    if isinstance(part, Mapping):
        try:
            return AllocationPart(
                flow_kind=part["flow_kind"],  # type: ignore[arg-type]
                category_id=part["category_id"],  # type: ignore[arg-type]
                amount_cents=part["amount_cents"],  # type: ignore[arg-type]
                beneficiary=part.get("beneficiary", Beneficiary.UNKNOWN),  # type: ignore[arg-type]
                cadence=part.get("cadence", Cadence.UNKNOWN),  # type: ignore[arg-type]
                flexibility=part.get("flexibility", Flexibility.UNKNOWN),  # type: ignore[arg-type]
                acquisition=part.get("acquisition", Acquisition.UNKNOWN),  # type: ignore[arg-type]
            )
        except KeyError as error:
            raise _fail("ALLOCATION_INVALID", 422, "Une part est incomplète.") from error
    raise _fail("ALLOCATION_INVALID", 422, "Format de part invalide.")


def _normalize_parts(
    parts: Sequence[AllocationPart | Mapping[str, object]],
    *,
    transaction_amount_cents: int,
    mode: AllocationMode,
) -> tuple[_NormalizedPart, ...]:
    if mode not in ("complete", "partial"):
        raise _fail("ALLOCATION_INVALID", 422, "Le mode doit être complete ou partial.")
    if not parts:
        raise _fail("ALLOCATION_INVALID", 422, "Au moins une part est requise.")

    merged: dict[tuple[str, str, str, str, str, str], _NormalizedPart] = {}
    for raw_part in parts:
        part = _part_value(raw_part)
        if not isinstance(part.category_id, str) or not part.category_id:
            raise _fail("ALLOCATION_INVALID", 422, "Une catégorie est requise pour chaque part.")
        amount_cents = _strict_int(part.amount_cents, "amount_cents")
        if amount_cents == 0:
            raise _fail("ALLOCATION_INVALID", 422, "Une allocation nulle est interdite.")
        try:
            flow_kind = FlowKind(part.flow_kind)
        except (TypeError, ValueError) as error:
            raise _fail("ALLOCATION_INVALID", 422, "Le flux de la part est invalide.") from error
        if flow_kind is FlowKind.UNKNOWN or not is_category_allowed(flow_kind, part.category_id):
            raise _fail(
                "ALLOCATION_CATEGORY_FLOW_INVALID",
                422,
                "La catégorie n'est pas admissible pour ce flux.",
            )
        if (amount_cents > 0) != (transaction_amount_cents > 0):
            raise _fail(
                "ALLOCATION_SIGN_INVALID",
                422,
                "Le signe de chaque allocation doit correspondre au mouvement bancaire.",
            )
        direction = expected_sign(flow_kind, part.category_id)
        if direction is not None and (amount_cents > 0) != (direction > 0):
            raise _fail(
                "ALLOCATION_SIGN_INVALID",
                422,
                "Le signe de l'allocation est interdit par la direction de sa catégorie.",
            )
        normalized = _NormalizedPart(
            flow_kind=flow_kind,
            category_id=part.category_id,
            amount_cents=amount_cents,
            beneficiary=_enum_value(Beneficiary, part.beneficiary, "beneficiary"),
            cadence=_enum_value(Cadence, part.cadence, "cadence"),
            flexibility=_enum_value(Flexibility, part.flexibility, "flexibility"),
            acquisition=_enum_value(Acquisition, part.acquisition, "acquisition"),
        )
        key = normalized.amount_key()
        existing = merged.get(key)
        if existing is None:
            merged[key] = normalized
        else:
            merged[key] = _NormalizedPart(
                flow_kind=normalized.flow_kind,
                category_id=normalized.category_id,
                amount_cents=existing.amount_cents + normalized.amount_cents,
                beneficiary=normalized.beneficiary,
                cadence=normalized.cadence,
                flexibility=normalized.flexibility,
                acquisition=normalized.acquisition,
            )

    normalized_parts = tuple(sorted(merged.values(), key=lambda item: item.business_key()))
    if any(part.amount_cents == 0 for part in normalized_parts):
        raise _fail("ALLOCATION_INVALID", 422, "La fusion produit une part nulle.")
    total_cents = sum(part.amount_cents for part in normalized_parts)
    if mode == "complete" and total_cents != transaction_amount_cents:
        raise _fail(
            "ALLOCATION_TOTAL_MISMATCH",
            422,
            "Une ventilation complete doit égaler le montant bancaire au centime près.",
        )
    if mode == "partial" and abs(total_cents) > abs(transaction_amount_cents):
        raise _fail(
            "ALLOCATION_TOTAL_EXCEEDED",
            422,
            "Une ventilation partial ne peut pas dépasser le montant bancaire.",
        )
    return normalized_parts


def _origin_value(origin: AllocationOrigin | Origin | str) -> Origin:
    try:
        value = Origin(origin)
    except (TypeError, ValueError) as error:
        raise _fail("ALLOCATION_INVALID", 422, "L'origine de l'allocation est invalide.") from error
    if value is Origin.UNCLASSIFIED:
        raise _fail("ALLOCATION_INVALID", 422, "Une allocation confirmée doit avoir une origine.")
    return value


def _actor_value(actor: str) -> str:
    if not isinstance(actor, str) or not actor.strip():
        raise _fail("ALLOCATION_INVALID", 422, "Un acteur non vide est requis.")
    return actor


def _record(row: Allocation) -> AllocationRecord:
    return AllocationRecord(
        allocation_id=row.allocation_id,
        flow_kind=row.flow_kind,
        category_id=row.category_id,
        amount_cents=row.amount_cents,
        origin=row.origin,
        beneficiary=row.beneficiary,
        cadence=row.cadence,
        flexibility=row.flexibility,
        acquisition=row.acquisition,
    )


def _state_in_session(
    session: Session, transaction: Transaction, rows: Sequence[Allocation] | None = None
) -> AllocationState:
    active = (
        list(rows) if rows is not None else _active_allocations(session, transaction.transaction_id)
    )
    records = tuple(_record(row) for row in active)
    residual = transaction.amount_cents - sum(row.amount_cents for row in active)
    if not active:
        state: Literal["unclassified", "partial", "complete"] = "unclassified"
    elif residual == 0:
        state = "complete"
    else:
        state = "partial"
    flows = {row.flow_kind for row in active}
    if not active or residual != 0:
        flows.add(FlowKind.UNKNOWN.value)
    return AllocationState(
        transaction_id=transaction.transaction_id,
        transaction_amount_cents=transaction.amount_cents,
        version=transaction.version,
        allocations=records,
        residual_cents=residual,
        state=state,
        derived_flow_kinds=tuple(sorted(flows)),
    )


def _active_allocations(session: Session, transaction_id: str) -> list[Allocation]:
    return list(
        session.scalars(
            select(Allocation)
            .where(
                Allocation.transaction_id == transaction_id,
                Allocation.superseded_by_allocation_id.is_(None),
                Allocation.retracted_at_utc.is_(None),
            )
            .order_by(Allocation.category_id, Allocation.flow_kind, Allocation.allocation_id)
        )
    )


def _active_link_exists(session: Session, transaction_id: str) -> bool:
    statement = select(TransactionLink.transaction_link_id).where(
        TransactionLink.link_kind == "refund_of",
        TransactionLink.status == "confirmed",
        TransactionLink.superseded_by_transaction_link_id.is_(None),
        (TransactionLink.source_transaction_id == transaction_id)
        | (TransactionLink.target_transaction_id == transaction_id),
    )
    return session.scalar(statement.limit(1)) is not None


def _reject_proposed_links_after_allocation_change(
    session: Session, transaction_id: str, actor: str
) -> None:
    """Reject proposals tied to the allocation snapshot being replaced or cleared."""
    links = list(
        session.scalars(
            select(TransactionLink)
            .where(
                TransactionLink.link_kind == "refund_of",
                TransactionLink.status == "proposed",
                TransactionLink.superseded_by_transaction_link_id.is_(None),
                (TransactionLink.source_transaction_id == transaction_id)
                | (TransactionLink.target_transaction_id == transaction_id),
            )
            .order_by(TransactionLink.transaction_link_id)
        )
    )
    for link in links:
        before = {
            "transaction_link_id": link.transaction_link_id,
            "source_transaction_id": link.source_transaction_id,
            "source_allocation_id": link.source_allocation_id,
            "target_transaction_id": link.target_transaction_id,
            "target_allocation_id": link.target_allocation_id,
            "amount_cents": link.amount_cents,
            "status": "proposed",
        }
        after = {**before, "status": "rejected", "reason": "allocation_changed"}
        session.add(
            AuditEvent(
                target_type="transaction_link",
                target_id=link.transaction_link_id,
                action="refund_link.rejected",
                actor=actor,
                before_json=json.dumps(
                    before, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                after_json=json.dumps(
                    after, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            )
        )
        link.status = "rejected"
        link.version += 1
    if links:
        session.flush()


def _allocation_json(rows: Sequence[Allocation]) -> list[dict[str, str | int]]:
    return [
        {
            "allocation_id": row.allocation_id,
            "flow_kind": row.flow_kind,
            "category_id": row.category_id,
            "amount_cents": row.amount_cents,
            "origin": row.origin,
            "beneficiary": row.beneficiary,
            "cadence": row.cadence,
            "flexibility": row.flexibility,
            "acquisition": row.acquisition,
        }
        for row in rows
    ]


def _requested_json(
    parts: Sequence[_NormalizedPart], allocation_ids: Sequence[str], origin: str
) -> list[dict[str, str | int]]:
    return [
        {"allocation_id": allocation_id, **part.as_json(origin)}
        for allocation_id, part in zip(allocation_ids, parts, strict=True)
    ]


def _same_allocation_content(rows: Sequence[Allocation], parts: Sequence[_NormalizedPart]) -> bool:
    existing = sorted(
        (
            row.flow_kind,
            row.category_id,
            row.amount_cents,
            row.beneficiary,
            row.cadence,
            row.flexibility,
            row.acquisition,
        )
        for row in rows
    )
    requested = [part.business_key() for part in parts]
    return existing == requested


def _has_any_decision(session: Session, transaction_id: str) -> bool:
    statement = select(ClassificationDecision.decision_id).where(
        ClassificationDecision.transaction_id == transaction_id
    )
    return session.scalar(statement.limit(1)) is not None


def _write_decision_and_audit(
    session: Session,
    *,
    transaction_id: str,
    actor: str,
    action: str,
    transaction_version: int,
    audit_action: str,
    before: list[dict[str, str | int]] | None,
    after: list[dict[str, str | int]] | None,
    audit_event_id: str,
) -> str:
    before_json = (
        None
        if before is None
        else json.dumps(before, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    after_json = (
        None
        if after is None
        else json.dumps(after, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    decision_id = str(uuid.uuid4())
    session.add(
        ClassificationDecision(
            decision_id=decision_id,
            transaction_id=transaction_id,
            action=action,
            actor=actor,
            transaction_version=transaction_version,
            before_json=before_json,
            after_json=after_json,
        )
    )
    session.add(
        AuditEvent(
            audit_event_id=audit_event_id,
            target_type="transaction",
            target_id=transaction_id,
            action=audit_action,
            actor=actor,
            before_json=before_json,
            after_json=after_json,
        )
    )
    return decision_id


@_translate_integrity_errors
def allocate_in_session(
    session: Session,
    transaction_id: str,
    parts: Sequence[AllocationPart | Mapping[str, object]],
    *,
    mode: AllocationMode,
    origin: AllocationOrigin | Origin | str,
    actor: str,
    expected_version: int,
) -> AllocationState:
    """Confirm allocations within the caller's transaction.

    The caller owns commit/rollback. ``expected_version`` refers to the
    transaction's aggregate version; callers that combine this write with
    related records can keep the whole operation in one SQLite transaction.
    """
    actor_value = _actor_value(actor)
    origin_value = _origin_value(origin)
    expected = _strict_int(expected_version, "expected_version")
    audit_event_id = str(uuid.uuid4())

    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise _fail("TRANSACTION_NOT_FOUND", 404, "Transaction introuvable.")
    if transaction.version != expected:
        raise VersionConflictError()

    normalized_parts = _normalize_parts(
        parts,
        transaction_amount_cents=transaction.amount_cents,
        mode=mode,
    )
    current = _active_allocations(session, transaction_id)
    same_content = _same_allocation_content(current, normalized_parts)
    explicit_human_promotion = origin_value is Origin.MANUAL and any(
        row.origin != Origin.MANUAL.value for row in current
    )
    if same_content and not explicit_human_promotion:
        return _state_in_session(session, transaction, current)

    if (
        any(row.origin == Origin.MANUAL.value for row in current)
        and origin_value is not Origin.MANUAL
    ):
        raise _fail(
            "HUMAN_DECISION_PROTECTED",
            409,
            "Une allocation confirmée manuellement ne peut être remplacée que "
            "par une action manuelle.",
        )
    if origin_value is Origin.RULE and (current or _has_any_decision(session, transaction_id)):
        raise _fail(
            "RULE_REQUIRES_UNCLASSIFIED_TRANSACTION",
            409,
            "Une règle ne peut classer qu'une transaction sans décision préalable.",
        )
    if origin_value in (Origin.HISTORY, Origin.LLM) and current:
        raise _fail(
            "SUGGESTION_REQUIRES_REVIEW",
            409,
            "Une proposition historique ou LLM ne remplace pas une allocation confirmée.",
        )
    if current and _active_link_exists(session, transaction_id):
        raise _fail(
            "ALLOCATION_HAS_CONFIRMED_REFUND_LINK",
            409,
            "Cette transaction participe à un remboursement confirmé; revoyez d'abord le lien.",
        )

    if current:
        _reject_proposed_links_after_allocation_change(session, transaction_id, actor_value)

    new_allocation_ids = [str(uuid.uuid4()) for _ in normalized_parts]
    after_json_value = _requested_json(normalized_parts, new_allocation_ids, origin_value.value)
    before_json_value = _allocation_json(current)
    action = "confirm" if not current else ("split" if len(normalized_parts) > 1 else "edit")
    audit_action = "allocation.confirmed" if not current else "allocation.superseded"
    decision_id = _write_decision_and_audit(
        session,
        transaction_id=transaction_id,
        actor=actor_value,
        action=action,
        transaction_version=transaction.version + 1,
        audit_action=audit_action,
        before=before_json_value or None,
        after=after_json_value,
        audit_event_id=audit_event_id,
    )
    session.flush()

    if current:
        replacement_id = new_allocation_ids[0]
        for old in current:
            old.superseded_by_allocation_id = replacement_id
            old.superseded_by_audit_event_id = audit_event_id
            old.version += 1
        # n-11: make the old rows inactive before inserting their replacements.
        # Both supersession foreign keys are DEFERRABLE INITIALLY DEFERRED.
        session.flush()

    new_rows = [
        Allocation(
            allocation_id=allocation_id,
            transaction_id=transaction_id,
            category_id=part.category_id,
            flow_kind=part.flow_kind.value,
            amount_cents=part.amount_cents,
            origin=origin_value.value,
            decision="confirmed",
            beneficiary=part.beneficiary.value,
            cadence=part.cadence.value,
            flexibility=part.flexibility.value,
            acquisition=part.acquisition.value,
        )
        for allocation_id, part in zip(new_allocation_ids, normalized_parts, strict=True)
    ]
    session.add_all(new_rows)
    session.flush()

    # The row's bank-source columns remain untouched; its version is the
    # transaction-wide optimistic token shared with classification writes.
    transaction.version += 1
    session.flush()
    return replace(_state_in_session(session, transaction, new_rows), decision_id=decision_id)


@_translate_integrity_errors
def allocate(
    engine: Engine,
    transaction_id: str,
    parts: Sequence[AllocationPart | Mapping[str, object]],
    *,
    mode: AllocationMode,
    origin: AllocationOrigin | Origin | str,
    actor: str,
    expected_version: int,
) -> AllocationState:
    """Atomically confirm allocations and preserve prior human decisions.

    ``expected_version`` refers to the transaction's aggregate version. SQLite's
    ``BEGIN IMMEDIATE`` serializes competing writers before this value and all
    active allocations are read.
    """
    with write_transaction(engine) as session:
        state = allocate_in_session(
            session,
            transaction_id,
            parts,
            mode=mode,
            origin=origin,
            actor=actor,
            expected_version=expected_version,
        )
    return state


@_translate_integrity_errors
def clear_allocations(
    engine: Engine,
    transaction_id: str,
    *,
    actor: str,
    expected_version: int,
) -> AllocationState:
    """Audibly retract every active allocation without changing bank data."""
    actor_value = _actor_value(actor)
    expected = _strict_int(expected_version, "expected_version")
    audit_event_id = str(uuid.uuid4())
    with write_transaction(engine) as session:
        transaction = session.get(Transaction, transaction_id)
        if transaction is None:
            raise _fail("TRANSACTION_NOT_FOUND", 404, "Transaction introuvable.")
        if transaction.version != expected:
            raise VersionConflictError()
        current = _active_allocations(session, transaction_id)
        if not current:
            return get_allocation_state(engine, transaction_id)

        if _active_link_exists(session, transaction_id):
            raise _fail(
                "ALLOCATION_HAS_CONFIRMED_REFUND_LINK",
                409,
                "Retirez ou remplacez les liens de remboursement actifs avant de retirer "
                "les allocations.",
            )
        _reject_proposed_links_after_allocation_change(session, transaction_id, actor_value)
        before = _allocation_json(current)
        decision_id = _write_decision_and_audit(
            session,
            transaction_id=transaction_id,
            actor=actor_value,
            action="clear",
            transaction_version=transaction.version + 1,
            audit_action="allocation.retracted",
            before=before,
            after=[],
            audit_event_id=audit_event_id,
        )
        session.flush()

        retracted_at = datetime.now(UTC)
        for allocation in current:
            allocation.retracted_at_utc = retracted_at
            allocation.retracted_by_decision_id = decision_id
            allocation.version += 1
        transaction.version += 1
        session.flush()
    return replace(get_allocation_state(engine, transaction_id), decision_id=decision_id)


@_translate_integrity_errors
def get_allocation_state(engine: Engine, transaction_id: str) -> AllocationState:
    """Read confirmed active allocations, signed residual, state, and derived flow."""
    with session_scope(engine) as session:
        transaction = session.get(Transaction, transaction_id)
        if transaction is None:
            raise _fail("TRANSACTION_NOT_FOUND", 404, "Transaction introuvable.")
        return _state_in_session(session, transaction)
