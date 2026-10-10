"""Audited, version-checked undo operations for classification decisions."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.errors import VersionConflictError
from app.db.models import (
    Allocation,
    AuditEvent,
    CategoryFlowKind,
    ClassificationDecision,
    RuleExecution,
    Transaction,
    TransactionLink,
)
from app.db.session import write_transaction
from app.domain.flow_kinds import expected_sign, is_category_allowed
from app.domain.types import Acquisition, Beneficiary, Cadence, Flexibility, FlowKind, Origin
from app.services.allocations import (
    AllocationState,
    _reject_proposed_links_after_allocation_change,
    _state_in_session,
)
from app.services.snapshot_integrity import canonical_json, snapshot_sha256

_HUMAN_ACTIONS = frozenset({"confirm", "edit", "split", "reject", "clear"})
_ALLOCATION_ORIGINS = frozenset({"manual", "rule", "history", "llm"})
_ALLOCATION_FIELDS = (
    "flow_kind",
    "category_id",
    "amount_cents",
    "origin",
    "beneficiary",
    "cadence",
    "flexibility",
    "acquisition",
)


class ReviewServiceError(AppError):
    """A review decision cannot be undone safely under the supplied contract."""


@dataclass(frozen=True, slots=True)
class UndoResult:
    transaction_id: str
    decision_id: str
    undo_decision_id: str
    state: AllocationState
    idempotent: bool
    human_override: bool


@dataclass(frozen=True, slots=True)
class UndoBatchResult:
    batch_id: str
    undone_ids: tuple[str, ...]
    undo_decision_ids: tuple[str, ...]
    states: tuple[AllocationState, ...]
    idempotent: bool
    human_override_ids: tuple[str, ...]


def _fail(code: str, message: str, status: int = 409) -> ReviewServiceError:
    return ReviewServiceError(code, status, message)


def _strict_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise _fail("REVIEW_INVALID", f"{field} doit être un entier positif.", 422)
    return value


def _actor(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 160:
        raise _fail("REVIEW_INVALID", "Un acteur non vide et borné est requis.", 422)
    return value.strip()


def _uuid4(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise _fail("REVIEW_INVALID", f"{field} doit être un UUIDv4 canonique.", 422)
    try:
        parsed = uuid.UUID(value)
    except (TypeError, ValueError, AttributeError) as error:
        raise _fail("REVIEW_INVALID", f"{field} doit être un UUIDv4 canonique.", 422) from error
    if parsed.version != 4 or str(parsed) != value:
        raise _fail("REVIEW_INVALID", f"{field} doit être un UUIDv4 canonique.", 422)
    return value


def _json(value: object) -> str:
    return canonical_json(value)


def _parse_allocations(raw: str | None, *, wrapped: bool = False) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise _fail(
            "UNDO_SNAPSHOT_INVALID", "L'instantané de décision est illisible.", 500
        ) from error
    if isinstance(value, dict):
        if not wrapped or "allocations" not in value:
            raise _fail("UNDO_SNAPSHOT_INVALID", "La forme de l'instantané est invalide.", 500)
        value = value["allocations"]
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _fail("UNDO_SNAPSHOT_INVALID", "Les allocations de l'instantané sont invalides.", 500)
    parts: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise _fail(
                "UNDO_SNAPSHOT_INVALID", "Une allocation de l'instantané est invalide.", 500
            )
        part = {key: item.get(key) for key in _ALLOCATION_FIELDS}
        if (
            not isinstance(part["flow_kind"], str)
            or not isinstance(part["category_id"], str)
            or type(part["amount_cents"]) is not int
            or part["amount_cents"] == 0
            or not isinstance(part["origin"], str)
            or part["origin"] not in _ALLOCATION_ORIGINS
            or any(not isinstance(part[key], str) for key in _ALLOCATION_FIELDS[4:])
        ):
            raise _fail(
                "UNDO_SNAPSHOT_INVALID", "Une allocation de l'instantané est incomplète.", 500
            )
        parts.append(part)
    return tuple(parts)


def _current_allocations(session: Session, transaction_id: str) -> list[Allocation]:
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


def _current_json(rows: list[Allocation]) -> list[dict[str, str | int]]:
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


def _business_signature(
    parts: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted((tuple(part[key] for key in _ALLOCATION_FIELDS) for part in parts), key=repr)
    )


def _rows_signature(rows: list[Allocation]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            (
                (
                    row.flow_kind,
                    row.category_id,
                    row.amount_cents,
                    row.origin,
                    row.beneficiary,
                    row.cadence,
                    row.flexibility,
                    row.acquisition,
                )
                for row in rows
            ),
            key=repr,
        )
    )


def _previous_audited_decision(
    session: Session, decision: ClassificationDecision
) -> ClassificationDecision | None:
    statement = select(ClassificationDecision).where(
        ClassificationDecision.transaction_id == decision.transaction_id,
        ClassificationDecision.decision_id != decision.decision_id,
    )
    if decision.transaction_version is not None:
        statement = statement.where(
            ClassificationDecision.transaction_version < decision.transaction_version
        ).order_by(
            ClassificationDecision.transaction_version.desc(),
            ClassificationDecision.created_at_utc.desc(),
            ClassificationDecision.decision_id.desc(),
        )
    else:
        statement = statement.where(
            ClassificationDecision.created_at_utc < decision.created_at_utc
        ).order_by(
            ClassificationDecision.created_at_utc.desc(), ClassificationDecision.decision_id.desc()
        )
    return session.scalar(statement.limit(1))


def _verify_decision_snapshots(
    session: Session,
    transaction: Transaction,
    decision: ClassificationDecision,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Verify decision hashes and reconcile legacy snapshots with prior audit state."""
    before_hash = decision.before_json_sha256
    after_hash = decision.after_json_sha256
    if (before_hash is None) != (after_hash is None):
        raise _fail("UNDO_SNAPSHOT_INVALID", "Les empreintes de décision sont incomplètes.", 500)
    has_fingerprints = before_hash is not None and after_hash is not None
    if has_fingerprints:
        try:
            actual_before = snapshot_sha256(decision.before_json)
            actual_after = snapshot_sha256(decision.after_json)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise _fail(
                "UNDO_SNAPSHOT_INVALID", "L'instantané de décision est illisible.", 500
            ) from error
        if actual_before != before_hash or actual_after != after_hash:
            raise _fail("UNDO_SNAPSHOT_INVALID", "L'empreinte d'un instantané est invalide.", 500)

    before_parts = _parse_allocations(decision.before_json)
    after_parts = _parse_allocations(decision.after_json)
    previous = _previous_audited_decision(session, decision)
    previous_parts: tuple[dict[str, Any], ...] | None = None
    reconcile_previous = False
    if previous is not None:
        try:
            if (
                previous.before_json_sha256 is not None
                and snapshot_sha256(previous.before_json) != previous.before_json_sha256
            ):
                raise ValueError("empreinte précédente avant invalide")
            if (
                previous.after_json_sha256 is not None
                and snapshot_sha256(previous.after_json) != previous.after_json_sha256
            ):
                raise ValueError("empreinte précédente après invalide")
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise _fail(
                "UNDO_SNAPSHOT_INVALID", "L'état de la décision précédente est invalide.", 500
            ) from error
        previous_parts = _parse_allocations(previous.after_json)
        contiguous = (
            decision.transaction_version is None
            or previous.transaction_version is None
            or previous.transaction_version == decision.transaction_version - 1
        )
        reconcile_previous = contiguous or not has_fingerprints
        if contiguous and _business_signature(before_parts) != _business_signature(previous_parts):
            raise _fail(
                "UNDO_SNAPSHOT_INVALID",
                "L'instantané ne correspond pas à l'état audité précédent.",
                500,
            )
    elif not has_fingerprints:
        raise _fail(
            "UNDO_SNAPSHOT_INVALID",
            "Une décision historique sans empreinte n'a pas d'état audité précédent.",
            500,
        )

    if not has_fingerprints and (
        previous_parts is None
        or _business_signature(before_parts) != _business_signature(previous_parts)
    ):
        raise _fail(
            "UNDO_SNAPSHOT_INVALID",
            "Une décision historique sans empreinte ne concorde pas avec l'état précédent.",
            500,
        )

    _validate_restore_parts(session, transaction, before_parts)
    _validate_restore_parts(session, transaction, after_parts)
    restored_total = sum(part["amount_cents"] for part in before_parts)
    residual = transaction.amount_cents - restored_total
    if restored_total + residual != transaction.amount_cents:
        raise _fail(
            "UNDO_SNAPSHOT_INVALID",
            "Les allocations et le résidu ne couvrent pas l'opération.",
            500,
        )
    if previous_parts is not None and reconcile_previous:
        prior_total = sum(part["amount_cents"] for part in previous_parts)
        prior_residual = transaction.amount_cents - prior_total
        if (
            restored_total != prior_total
            or residual != prior_residual
            or restored_total + residual != transaction.amount_cents
            or prior_total + prior_residual != transaction.amount_cents
        ):
            raise _fail(
                "UNDO_SNAPSHOT_INVALID",
                "Le total restauré ne correspond pas à l'état audité précédent.",
                500,
            )
    return before_parts, after_parts


def _has_active_refund_link(session: Session, transaction_id: str) -> bool:
    return (
        session.scalar(
            select(TransactionLink.transaction_link_id)
            .where(
                TransactionLink.link_kind == "refund_of",
                TransactionLink.status == "confirmed",
                TransactionLink.superseded_by_transaction_link_id.is_(None),
                (TransactionLink.source_transaction_id == transaction_id)
                | (TransactionLink.target_transaction_id == transaction_id),
            )
            .limit(1)
        )
        is not None
    )


def _later_human_decisions(
    session: Session,
    transaction_id: str,
    *,
    exclude_decision_id: str | None,
    not_before: datetime,
) -> tuple[str, ...]:
    statement = select(ClassificationDecision).where(
        ClassificationDecision.transaction_id == transaction_id,
        ClassificationDecision.action.in_(_HUMAN_ACTIONS),
        ClassificationDecision.undone_by_decision_id.is_(None),
        ClassificationDecision.created_at_utc >= not_before,
    )
    if exclude_decision_id is not None:
        statement = statement.where(ClassificationDecision.decision_id != exclude_decision_id)
    return tuple(
        row.decision_id
        for row in session.scalars(statement.order_by(ClassificationDecision.created_at_utc))
    )


def _check_transaction_version(transaction: Transaction, expected_version: int) -> None:
    if transaction.version != expected_version:
        raise VersionConflictError()


def _check_state_for_undo(
    *,
    current: list[Allocation],
    expected_after: tuple[dict[str, Any], ...],
    recorded_transaction_version: int | None,
    current_transaction_version: int,
    later_human_decision_ids: tuple[str, ...],
    confirm_human_override: bool,
) -> bool:
    same_content = _rows_signature(current) == _business_signature(expected_after)
    same_version = (
        recorded_transaction_version is None
        or recorded_transaction_version == current_transaction_version
    )
    if later_human_decision_ids and not confirm_human_override:
        raise _fail(
            "UNDO_HUMAN_DECISION_CONFIRMATION_REQUIRED",
            "Une décision humaine postérieure exige une confirmation explicite.",
        )
    if not same_content or not same_version:
        if later_human_decision_ids and confirm_human_override:
            return True
        raise _fail(
            "UNDO_STATE_DIVERGED",
            "L'état ou la version de la transaction a changé depuis la décision.",
        )
    return bool(later_human_decision_ids and confirm_human_override)


def _validate_restore_parts(
    session: Session, transaction: Transaction, parts: tuple[dict[str, Any], ...]
) -> None:
    if len(parts) > 100:
        raise _fail("UNDO_SNAPSHOT_INVALID", "L'instantané dépasse le plafond d'allocations.", 500)
    allowed_pairs = set(
        session.execute(select(CategoryFlowKind.category_id, CategoryFlowKind.flow_kind))
    )
    total = 0
    for part in parts:
        try:
            flow_kind = FlowKind(part["flow_kind"])
            Origin(part["origin"])
            Beneficiary(part["beneficiary"])
            Cadence(part["cadence"])
            Flexibility(part["flexibility"])
            Acquisition(part["acquisition"])
        except (TypeError, ValueError) as error:
            raise _fail(
                "UNDO_SNAPSHOT_INVALID", "Une dimension de l'instantané est inconnue.", 500
            ) from error
        if (part["category_id"], flow_kind.value) not in allowed_pairs:
            raise _fail(
                "UNDO_SNAPSHOT_INVALID",
                "La catégorie et le flux de l'instantané sont incompatibles.",
                500,
            )
        if not is_category_allowed(flow_kind, part["category_id"]):
            raise _fail(
                "UNDO_SNAPSHOT_INVALID",
                "La catégorie et le flux de l'instantané sont incompatibles.",
                500,
            )
        amount = part["amount_cents"]
        if abs(amount) > 10**13:
            raise _fail(
                "UNDO_SNAPSHOT_INVALID", "Un montant de l'instantané dépasse la borne métier.", 500
            )
        if (amount > 0) != (transaction.amount_cents > 0):
            raise _fail("UNDO_SNAPSHOT_INVALID", "Le signe de l'instantané est incohérent.", 500)
        if (
            flow_kind
            in {
                FlowKind.EXPENSE,
                FlowKind.ALLOCATION_PERSONAL,
                FlowKind.LOAN_REPAYMENT,
                FlowKind.CAPITAL_EVENT,
                FlowKind.CASH_WITHDRAWAL_PENDING,
                FlowKind.INTERMEDIARY_PENDING,
            }
            and amount > 0
        ):
            raise _fail(
                "UNDO_SNAPSHOT_INVALID", "Le signe du flux de l'instantané est invalide.", 500
            )
        if (
            flow_kind
            in {
                FlowKind.INCOME_RECURRING,
                FlowKind.INCOME_OTHER,
                FlowKind.LOAN_DISBURSEMENT,
                FlowKind.REFUND,
            }
            and amount < 0
        ):
            raise _fail(
                "UNDO_SNAPSHOT_INVALID", "Le signe du flux de l'instantané est invalide.", 500
            )
        category_direction = expected_sign(flow_kind, part["category_id"])
        if category_direction is not None and (amount > 0) != (category_direction > 0):
            raise _fail(
                "UNDO_SNAPSHOT_INVALID", "Le signe de catégorie de l'instantané est invalide.", 500
            )
        total += amount
    if abs(total) > abs(transaction.amount_cents):
        raise _fail("UNDO_SNAPSHOT_INVALID", "L'instantané dépasse le montant bancaire.", 500)


def _apply_inverse_in_session(
    session: Session,
    transaction: Transaction,
    current: list[Allocation],
    restore_parts: tuple[dict[str, Any], ...],
    *,
    actor: str,
    target_type: str,
    target_id: str,
    human_override_ids: tuple[str, ...],
    target_decision: ClassificationDecision | None = None,
    execution: RuleExecution | None = None,
) -> tuple[str, AllocationState]:
    undo_decision_id = str(uuid.uuid4())
    audit_event_id = str(uuid.uuid4())
    new_ids = [str(uuid.uuid4()) for _ in restore_parts]
    before = _current_json(current)
    after: list[dict[str, str | int]] = []
    for allocation_id, part in zip(new_ids, restore_parts, strict=True):
        after.append({"allocation_id": allocation_id, **part})
    decision = ClassificationDecision(
        decision_id=undo_decision_id,
        transaction_id=transaction.transaction_id,
        action="undo",
        actor=actor,
        transaction_version=transaction.version + 1,
        before_json=_json(before),
        after_json=_json(after),
        before_json_sha256=snapshot_sha256(_json(before)),
        after_json_sha256=snapshot_sha256(_json(after)),
    )
    event = AuditEvent(
        audit_event_id=audit_event_id,
        target_type="transaction",
        target_id=transaction.transaction_id,
        action="classification.undo"
        if target_type == "classification_decision"
        else "rule.application_undone",
        actor=actor,
        before_json=_json(before),
        after_json=_json(
            {
                "allocations": after,
                "human_override_decision_ids": list(human_override_ids),
                "target_id": target_id,
                "target_type": target_type,
                "undo_decision_id": undo_decision_id,
            }
        ),
    )
    session.add_all((decision, event))
    session.flush()

    if target_decision is not None:
        target_decision.undone_by_decision_id = undo_decision_id
        target_decision.version += 1
    if execution is not None:
        execution.undone_by_decision_id = undo_decision_id

    _reject_proposed_links_after_allocation_change(session, transaction.transaction_id, actor)
    if current:
        if new_ids:
            for old in current:
                old.superseded_by_allocation_id = new_ids[0]
                old.superseded_by_audit_event_id = audit_event_id
                old.version += 1
        else:
            retracted_at = datetime.now(UTC)
            retracted_at = retracted_at.replace(
                microsecond=(retracted_at.microsecond // 1000) * 1000
            )
            for old in current:
                old.retracted_at_utc = retracted_at
                old.retracted_by_decision_id = undo_decision_id
                old.version += 1
        session.flush()

    restored = [
        Allocation(
            allocation_id=allocation_id,
            transaction_id=transaction.transaction_id,
            category_id=part["category_id"],
            flow_kind=part["flow_kind"],
            amount_cents=part["amount_cents"],
            origin=part["origin"],
            decision="confirmed",
            beneficiary=part["beneficiary"],
            cadence=part["cadence"],
            flexibility=part["flexibility"],
            acquisition=part["acquisition"],
        )
        for allocation_id, part in zip(new_ids, restore_parts, strict=True)
    ]
    session.add_all(restored)
    transaction.version += 1
    session.flush()
    return undo_decision_id, _state_in_session(session, transaction, restored)


def undo_decision(
    engine: Engine,
    decision_id: str,
    *,
    expected_version: int,
    actor: str,
    confirm_human_override: bool = False,
) -> UndoResult:
    """Undo one non-rule allocation decision with optimistic concurrency."""
    actor_value = _actor(actor)
    expected = _strict_int(expected_version, "expected_version")
    if not isinstance(confirm_human_override, bool):
        raise _fail("REVIEW_INVALID", "La confirmation doit être booléenne.", 422)
    with write_transaction(engine) as session:
        target = session.get(ClassificationDecision, decision_id)
        if target is None:
            raise _fail("DECISION_NOT_FOUND", "Décision introuvable.", 404)
        if target.action == "undo":
            raise _fail("UNDO_ACTION_NOT_SUPPORTED", "Une décision undo ne peut pas être annulée.")
        execution = session.scalar(
            select(RuleExecution).where(
                RuleExecution.classification_decision_id == target.decision_id,
                RuleExecution.outcome == "applied",
                RuleExecution.dry_run == 0,
            )
        )
        if execution is not None:
            raise _fail(
                "RULE_BATCH_UNDO_REQUIRED",
                "Cette décision doit être annulée avec son lot d'application de règle.",
            )
        transaction = session.get(Transaction, target.transaction_id)
        if transaction is None:
            raise _fail("DECISION_NOT_FOUND", "Transaction de décision introuvable.", 404)
        before_parts, after_parts = _verify_decision_snapshots(session, transaction, target)
        if target.undone_by_decision_id is not None:
            undo_decision = session.get(ClassificationDecision, target.undone_by_decision_id)
            if undo_decision is None:
                raise _fail("UNDO_SNAPSHOT_INVALID", "Le lien d'annulation est orphelin.", 500)
            return UndoResult(
                transaction_id=transaction.transaction_id,
                decision_id=target.decision_id,
                undo_decision_id=undo_decision.decision_id,
                state=_state_in_session(session, transaction),
                idempotent=True,
                human_override=False,
            )
        _check_transaction_version(transaction, expected)
        current = _current_allocations(session, transaction.transaction_id)
        if _has_active_refund_link(session, transaction.transaction_id):
            raise _fail(
                "UNDO_REFUND_LINK_ACTIVE",
                "Un lien de remboursement actif empêche cette annulation.",
            )
        later = _later_human_decisions(
            session,
            transaction.transaction_id,
            exclude_decision_id=target.decision_id,
            not_before=target.created_at_utc,
        )
        overridden = _check_state_for_undo(
            current=current,
            expected_after=after_parts,
            recorded_transaction_version=target.transaction_version,
            current_transaction_version=transaction.version,
            later_human_decision_ids=later,
            confirm_human_override=confirm_human_override,
        )
        undo_id, state = _apply_inverse_in_session(
            session,
            transaction,
            current,
            before_parts,
            actor=actor_value,
            target_type="classification_decision",
            target_id=target.decision_id,
            human_override_ids=later if overridden else (),
            target_decision=target,
        )
        return UndoResult(
            transaction_id=transaction.transaction_id,
            decision_id=target.decision_id,
            undo_decision_id=undo_id,
            state=replace(state, decision_id=undo_id),
            idempotent=False,
            human_override=overridden,
        )


def _undo_rule_execution_in_session(
    session: Session,
    execution: RuleExecution,
    *,
    expected_version: int,
    actor: str,
    confirm_human_override: bool,
) -> tuple[str, AllocationState, bool]:
    if execution.outcome != "applied" or execution.dry_run != 0:
        raise _fail("RULE_BATCH_NOT_REVERSIBLE", "L'exécution n'a pas appliqué de règle.")
    if execution.undone_by_decision_id is not None:
        raise _fail("RULE_BATCH_UNDO_INCOMPLETE", "L'exécution est déjà annulée.")
    transaction = session.get(Transaction, execution.transaction_id)
    if transaction is None:
        raise _fail("UNDO_SNAPSHOT_INVALID", "La transaction de l'exécution est absente.", 500)
    _check_transaction_version(transaction, expected_version)
    source_decision = (
        session.get(ClassificationDecision, execution.classification_decision_id)
        if execution.classification_decision_id
        else None
    )
    if source_decision is None or source_decision.action == "undo":
        raise _fail(
            "UNDO_SNAPSHOT_INVALID",
            "L'exécution historique n'est pas rapprochée d'une décision source.",
            500,
        )
    if source_decision.undone_by_decision_id is not None:
        raise _fail("RULE_BATCH_UNDO_INCOMPLETE", "La décision du lot est déjà annulée.")
    source_before_parts, source_after_parts = _verify_decision_snapshots(
        session, transaction, source_decision
    )
    execution_before_parts = _parse_allocations(execution.before_json, wrapped=True)
    execution_after_parts = _parse_allocations(execution.after_json, wrapped=True)
    if _business_signature(execution_before_parts) != _business_signature(
        source_before_parts
    ) or _business_signature(execution_after_parts) != _business_signature(source_after_parts):
        raise _fail(
            "UNDO_SNAPSHOT_INVALID",
            "Les snapshots d'exécution divergent de la décision auditée.",
            500,
        )
    before_parts = source_before_parts
    after_parts = source_after_parts
    if execution.rule_version is not None:
        for raw in (execution.before_json, execution.after_json):
            try:
                value = json.loads(raw) if raw is not None else None
            except json.JSONDecodeError as error:
                raise _fail(
                    "UNDO_SNAPSHOT_INVALID", "La version du lot est illisible.", 500
                ) from error
            if isinstance(value, dict) and value.get("rule_version") != execution.rule_version:
                raise _fail("UNDO_SNAPSHOT_INVALID", "La version du lot ne correspond pas.", 500)
    current = _current_allocations(session, transaction.transaction_id)
    cutoff = source_decision.created_at_utc
    later = _later_human_decisions(
        session,
        transaction.transaction_id,
        exclude_decision_id=source_decision.decision_id,
        not_before=cutoff,
    )
    recorded_version = source_decision.transaction_version
    if _has_active_refund_link(session, transaction.transaction_id):
        raise _fail(
            "UNDO_REFUND_LINK_ACTIVE",
            "Un lien de remboursement actif empêche cette annulation.",
        )
    overridden = _check_state_for_undo(
        current=current,
        expected_after=after_parts,
        recorded_transaction_version=recorded_version,
        current_transaction_version=transaction.version,
        later_human_decision_ids=later,
        confirm_human_override=confirm_human_override,
    )
    decision_id, state = _apply_inverse_in_session(
        session,
        transaction,
        current,
        before_parts,
        actor=actor,
        target_type="rule_execution",
        target_id=execution.rule_execution_id,
        human_override_ids=later if overridden else (),
        target_decision=source_decision,
        execution=execution,
    )
    return decision_id, replace(state, decision_id=decision_id), overridden


def undo_rule_application(
    engine: Engine,
    batch_id: str,
    *,
    expected_versions: Mapping[str, int],
    actor: str,
    confirm_human_override: bool = False,
) -> UndoBatchResult:
    """Undo all applied rows from a rule batch atomically and idempotently."""
    actor_value = _actor(actor)
    batch_id_value = _uuid4(batch_id, "batch_id")
    if not isinstance(confirm_human_override, bool):
        raise _fail("REVIEW_INVALID", "La confirmation doit être booléenne.", 422)
    if not isinstance(expected_versions, Mapping):
        raise _fail("REVIEW_INVALID", "expected_versions doit être un mapping.", 422)
    with write_transaction(engine) as session:
        executions = list(
            session.scalars(
                select(RuleExecution)
                .where(RuleExecution.batch_id == batch_id_value, RuleExecution.dry_run == 0)
                .order_by(RuleExecution.transaction_id, RuleExecution.rule_execution_id)
            )
        )
        if not executions:
            raise _fail("RULE_BATCH_NOT_FOUND", "Lot de règle introuvable.", 404)
        applied = [row for row in executions if row.outcome == "applied"]
        if not applied:
            raise _fail("RULE_BATCH_NOT_REVERSIBLE", "Le lot ne contient aucune application.")
        undone = [row for row in applied if row.undone_by_decision_id is not None]
        if len(undone) == len(applied):
            transactions = [session.get(Transaction, row.transaction_id) for row in applied]
            if any(row is None for row in transactions):
                raise _fail("UNDO_SNAPSHOT_INVALID", "Une transaction du lot est absente.", 500)
            current_states = tuple(
                _state_in_session(session, row) for row in transactions if row is not None
            )
            return UndoBatchResult(
                batch_id=batch_id_value,
                undone_ids=tuple(row.transaction_id for row in applied),
                undo_decision_ids=tuple(
                    row.undone_by_decision_id for row in applied if row.undone_by_decision_id
                ),
                states=current_states,
                idempotent=True,
                human_override_ids=(),
            )
        if undone:
            raise _fail("RULE_BATCH_UNDO_INCOMPLETE", "Le lot est partiellement annulé.")

        expected_ids = {row.transaction_id for row in applied}
        if set(expected_versions) != expected_ids:
            raise _fail(
                "REVIEW_INVALID",
                "expected_versions doit contenir exactement les transactions appliquées du lot.",
                422,
            )
        versions = {
            transaction_id: _strict_int(version, "expected_versions")
            for transaction_id, version in expected_versions.items()
        }
        states: list[AllocationState] = []
        undo_decision_ids: list[str] = []
        override_ids: list[str] = []
        for execution in applied:
            decision_id, state, overridden = _undo_rule_execution_in_session(
                session,
                execution,
                expected_version=versions[execution.transaction_id],
                actor=actor_value,
                confirm_human_override=confirm_human_override,
            )
            undo_decision_ids.append(decision_id)
            states.append(state)
            if overridden:
                override_ids.append(execution.transaction_id)
        return UndoBatchResult(
            batch_id=batch_id_value,
            undone_ids=tuple(row.transaction_id for row in applied),
            undo_decision_ids=tuple(undo_decision_ids),
            states=tuple(states),
            idempotent=False,
            human_override_ids=tuple(override_ids),
        )
