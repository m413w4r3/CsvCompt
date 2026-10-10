"""Pure deterministic classification proposals, simulation and explicit rule use."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.classification.matcher import AMBIGUOUS_MARKETPLACES, key_metadata
from app.classification.memory import HistoryCandidate, history_candidates
from app.classification.rule_store import (
    RuleDraft,
    RuleRecord,
    action_parts,
    action_signature,
    get_rule,
    list_rules,
    matches_condition,
    validate_rule_draft,
)
from app.core.errors import AppError
from app.db.errors import VersionConflictError
from app.db.models import (
    Allocation,
    AuditEvent,
    ClassificationDecision,
    ClassificationSuggestion,
    Payee,
    Rule,
    RuleExecution,
    Transaction,
)
from app.db.session import session_scope, write_transaction
from app.domain.types import FlowKind
from app.importers.normalize import search_label
from app.services.allocations import AllocationState, allocate_in_session, get_allocation_state

MAX_SIMULATION_TRANSACTIONS = 10_000
MAX_PAGE_SIZE = 1_000
MAX_SELECTION_SIZE = 10_000

CandidateSource = Literal["human", "rule", "memory", "heuristic", "llm"]
EvaluationContext = Literal["existing", "future"]
ApplyScope = Literal["selection", "existing_and_future"]


class RuleEngineError(AppError):
    """A proposal or application cannot be evaluated safely."""


@dataclass(frozen=True, slots=True)
class ProposalCandidate:
    source: CandidateSource
    priority_rank: int
    category_id: str | None
    flow_kind: str | None
    amount_cents: int
    confidence: str
    source_transaction_ids: tuple[str, ...] = ()
    rule_id: str | None = None
    rule_priority: int | None = None
    suggestion_id: str | None = None
    allocation_parts: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class TransactionEvaluation:
    transaction_id: str
    transaction_version: int
    candidates: tuple[ProposalCandidate, ...]
    winner: ProposalCandidate | None
    conflict_rule_ids: tuple[str, ...] = ()
    human_decision_protected: bool = False
    context: EvaluationContext = "existing"


@dataclass(frozen=True, slots=True)
class SimulationScope:
    """Read-only filters and bounded pagination for existing transactions."""

    transaction_ids: tuple[str, ...] = ()
    account_ids: tuple[str, ...] = ()
    booked_from: date | None = None
    booked_through: date | None = None
    offset: int = 0
    page_size: int = MAX_PAGE_SIZE
    max_transactions: int = MAX_SIMULATION_TRANSACTIONS


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    transaction_count: int
    amount_cents: int
    amount_euros: str


@dataclass(frozen=True, slots=True)
class SimulationRow:
    transaction_id: str
    transaction_version: int
    booked_on: date
    account_id: str
    amount_cents: int
    amount_euros: str
    before: tuple[dict[str, str | int], ...]
    after: tuple[dict[str, str | int], ...]
    ambiguous: bool


@dataclass(frozen=True, slots=True)
class RuleSimulation:
    rule_id: str | None
    rule_version: int | None
    scanned_count: int
    matched_count: int
    rows: tuple[SimulationRow, ...]
    totals_by_category: dict[str, CategoryTotal]
    conflict_transaction_ids: tuple[str, ...]
    human_decision_ignored_ids: tuple[str, ...]
    already_classified_ids: tuple[str, ...]
    ambiguous_transaction_ids: tuple[str, ...]
    offset: int
    next_offset: int | None
    truncated: bool
    elapsed_ns: int


@dataclass(frozen=True, slots=True)
class RuleApplication:
    rule_id: str
    rule_version: int
    applied_ids: tuple[str, ...]
    skipped_manual_ids: tuple[str, ...]
    skipped_classified_ids: tuple[str, ...]
    conflict_ids: tuple[str, ...]
    allocation_states: tuple[AllocationState, ...]
    elapsed_ns: int
    batch_id: str


def _fail(code: str, message: str, status: int = 422) -> RuleEngineError:
    return RuleEngineError(code, status, message)


def _format_euros(amount_cents: int) -> str:
    sign = "-" if amount_cents < 0 else ""
    whole, fractional = divmod(abs(amount_cents), 100)
    grouped = f"{whole:,}".replace(",", "\u202f")
    return f"{sign}{grouped},{fractional:02d} €"


def _strict_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise _fail("RULE_SIMULATION_INVALID", f"{name} doit être un entier strict.")
    return value


def _check_simulation_scope(scope: SimulationScope) -> None:
    if len(scope.transaction_ids) > MAX_SELECTION_SIZE:
        raise _fail("RULE_SELECTION_TOO_LARGE", "La sélection dépasse 10000 transactions.")
    if len(scope.account_ids) > 500:
        raise _fail("RULE_SCOPE_TOO_LARGE", "La portée dépasse 500 comptes.")
    if len(set(scope.transaction_ids)) != len(scope.transaction_ids):
        raise _fail("RULE_SIMULATION_INVALID", "La sélection contient des IDs répétés.")
    offset = _strict_int(scope.offset, "offset")
    page_size = _strict_int(scope.page_size, "page_size")
    maximum = _strict_int(scope.max_transactions, "max_transactions")
    if offset < 0 or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise _fail("RULE_SIMULATION_INVALID", "La pagination est hors limites.")
    if not 1 <= maximum <= MAX_SIMULATION_TRANSACTIONS:
        raise _fail("RULE_SIMULATION_INVALID", "Le plafond doit être compris entre 1 et 10000.")
    if scope.booked_from and scope.booked_through and scope.booked_from > scope.booked_through:
        raise _fail("RULE_SIMULATION_INVALID", "La plage de dates est inversée.")


def _id_batches(transaction_ids: list[str]) -> tuple[tuple[str, ...], ...]:
    """Keep SQLite IN clauses below its historic 999-bind limit."""
    batch_size = 500
    return tuple(
        tuple(transaction_ids[offset : offset + batch_size])
        for offset in range(0, len(transaction_ids), batch_size)
    )


def _json_allocations(rows: list[Allocation]) -> tuple[dict[str, str | int], ...]:
    return tuple(
        {
            "allocation_id": row.allocation_id,
            "category_id": row.category_id,
            "flow_kind": row.flow_kind,
            "amount_cents": row.amount_cents,
            "origin": row.origin,
            "beneficiary": row.beneficiary,
            "cadence": row.cadence,
            "flexibility": row.flexibility,
            "acquisition": row.acquisition,
        }
        for row in rows
    )


def _decision_has_rule_origin(decision: ClassificationDecision) -> bool:
    """Rule writes encode their origin in snapshots; a human clear has none.

    A clear decision can have a rule-origin allocation in its ``before_json``.
    That describes what was removed, not who made the clear, so it must not
    make the explicit human decision look like another rule execution.
    """
    if decision.action in ("clear", "undo"):
        return False
    for serialized in (decision.before_json, decision.after_json):
        if not serialized:
            continue
        try:
            payload = json.loads(serialized)
        except json.JSONDecodeError:
            continue
        parts = payload if isinstance(payload, list) else [payload]
        if any(isinstance(part, dict) and part.get("origin") == "rule" for part in parts):
            return True
    return False


def _human_decision_protected(
    decisions: list[ClassificationDecision], active_allocations: list[Allocation]
) -> bool:
    if any(row.origin != "rule" for row in active_allocations):
        return True
    return any(not _decision_has_rule_origin(decision) for decision in decisions)


def _ambiguous_transaction(session: Session, transaction: Transaction) -> bool:
    if transaction.payee_id is None:
        return False
    payee = session.get(Payee, transaction.payee_id)
    if payee is None:
        return False
    metadata = key_metadata(payee.normalized_name)
    return metadata is not None and metadata.get("entity_kind") == "marketplace"


def _draft_record(draft: RuleDraft) -> RuleRecord:
    normalized = validate_rule_draft(draft)
    now = datetime.now(UTC)
    return RuleRecord(
        rule_id="draft",
        name=normalized.name,
        condition=normalized.condition,
        action=normalized.action,
        priority=normalized.priority,
        status="draft",
        application_scope=normalized.application_scope,
        version=0,
        created_by="simulation",
        updated_by="simulation",
        approved_by=None,
        created_at_utc=now,
        updated_at_utc=now,
        ambiguous_reason=normalized.ambiguous_reason,
    )


def _rule_eligible_for_context(rule: RuleRecord, context: EvaluationContext) -> bool:
    if rule.status != "approved":
        return False
    if context == "future":
        return rule.application_scope in ("future_only", "existing_and_future")
    return rule.application_scope == "existing_and_future"


def _matched_rule_records(
    session: Session,
    transaction: Transaction,
    rules: tuple[RuleRecord, ...],
    context: EvaluationContext,
) -> list[RuleRecord]:
    return [
        rule
        for rule in rules
        if _rule_eligible_for_context(rule, context)
        and matches_condition(session, rule, transaction)
    ]


def _rule_candidates(
    matches: list[RuleRecord], transaction: Transaction
) -> tuple[list[ProposalCandidate], tuple[str, ...], RuleRecord | None]:
    if not matches:
        return [], (), None
    ordered = sorted(matches, key=lambda rule: (rule.priority, rule.rule_id))
    highest = ordered[0].priority
    tied = [rule for rule in ordered if rule.priority == highest]
    signatures = {action_signature(rule.action) for rule in tied}
    if len(signatures) > 1:
        candidates = [
            ProposalCandidate(
                source="rule",
                priority_rank=2,
                category_id=(
                    rule.action.category_id
                    if rule.action.category_id is not None
                    else sorted(
                        rule.action.ratios, key=lambda part: (-part.basis_points, part.category_id)
                    )[0].category_id
                ),
                flow_kind=FlowKind(rule.action.flow_kind).value,
                amount_cents=transaction.amount_cents,
                confidence="conflict",
                rule_id=rule.rule_id,
                rule_priority=rule.priority,
                allocation_parts=tuple(
                    (part.category_id, part.amount_cents)
                    for part in action_parts(rule.action, transaction.amount_cents)
                ),
            )
            for rule in tied
        ]
        return candidates, tuple(rule.rule_id for rule in tied), None
    winner = tied[0]
    return (
        [
            ProposalCandidate(
                source="rule",
                priority_rank=2,
                category_id=(
                    winner.action.category_id
                    if winner.action.category_id is not None
                    else sorted(
                        winner.action.ratios,
                        key=lambda part: (-part.basis_points, part.category_id),
                    )[0].category_id
                ),
                flow_kind=FlowKind(winner.action.flow_kind).value,
                amount_cents=transaction.amount_cents,
                confidence="approved",
                rule_id=winner.rule_id,
                rule_priority=winner.priority,
                allocation_parts=tuple(
                    (part.category_id, part.amount_cents)
                    for part in action_parts(winner.action, transaction.amount_cents)
                ),
            )
        ],
        (),
        winner,
    )


def _memory_candidate(candidate: HistoryCandidate, amount_cents: int) -> ProposalCandidate | None:
    if (
        candidate.confidence != "exact"
        or candidate.ambiguous_history
        or candidate.has_historical_split
        or candidate.category_id is None
        or candidate.flow_kind is None
    ):
        return None
    return ProposalCandidate(
        source="memory",
        priority_rank=3,
        category_id=candidate.category_id,
        flow_kind=candidate.flow_kind,
        amount_cents=amount_cents,
        confidence=candidate.confidence,
        source_transaction_ids=candidate.source_transaction_ids,
        allocation_parts=((candidate.category_id, amount_cents),),
    )


def _suggestion_candidates(
    suggestions: list[ClassificationSuggestion], transaction_amount_cents: int
) -> list[ProposalCandidate]:
    rank_by_method = {"heuristic": 4, "llm": 5}
    candidates: list[ProposalCandidate] = []
    for suggestion in suggestions:
        rank = rank_by_method.get(suggestion.method)
        if rank is None:
            continue
        amount = (
            suggestion.suggested_amount_cents
            if suggestion.suggested_amount_cents is not None
            else transaction_amount_cents
        )
        candidates.append(
            ProposalCandidate(
                source=suggestion.method,  # type: ignore[arg-type]
                priority_rank=rank,
                category_id=suggestion.category_id,
                flow_kind=suggestion.proposed_flow_kind,
                amount_cents=amount,
                confidence=suggestion.confidence_label,
                suggestion_id=suggestion.suggestion_id,
                allocation_parts=(
                    ((suggestion.category_id, amount),)
                    if suggestion.category_id is not None
                    else ()
                ),
            )
        )
    return candidates


def evaluate_transaction(
    engine: Engine, transaction_id: str, *, context: EvaluationContext = "existing"
) -> TransactionEvaluation:
    """Return ordered, write-free candidates using the documented source order."""
    if context not in ("existing", "future"):
        raise _fail("RULE_CONTEXT_INVALID", "Le contexte doit être existing ou future.")
    with session_scope(engine) as session:
        transaction = session.get(Transaction, transaction_id)
        if transaction is None:
            raise _fail("TRANSACTION_NOT_FOUND", "Transaction introuvable.", 404)
        active = list(
            session.scalars(
                select(Allocation).where(
                    Allocation.transaction_id == transaction_id,
                    Allocation.superseded_by_allocation_id.is_(None),
                    Allocation.retracted_at_utc.is_(None),
                )
            )
        )
        decisions = list(
            session.scalars(
                select(ClassificationDecision).where(
                    ClassificationDecision.transaction_id == transaction_id
                )
            )
        )
        human_locked = _human_decision_protected(decisions, active)
        candidates: list[ProposalCandidate] = []
        if active:
            for allocation in active:
                candidates.append(
                    ProposalCandidate(
                        source="human" if allocation.origin != "rule" else "rule",
                        priority_rank=1 if allocation.origin != "rule" else 2,
                        category_id=allocation.category_id,
                        flow_kind=allocation.flow_kind,
                        amount_cents=allocation.amount_cents,
                        confidence="confirmed",
                        allocation_parts=((allocation.category_id, allocation.amount_cents),),
                    )
                )
        elif human_locked:
            candidates.append(
                ProposalCandidate(
                    source="human",
                    priority_rank=1,
                    category_id=None,
                    flow_kind=None,
                    amount_cents=transaction.amount_cents,
                    confidence="confirmed_clear_or_rejection",
                )
            )

        approved = list_rules(engine, approved_only=True)
        matched = _matched_rule_records(session, transaction, approved, context)
        rule_candidates, conflicts, _winner = _rule_candidates(matched, transaction)
        candidates.extend(rule_candidates)

        if not any(candidate.source == "human" for candidate in candidates):
            history = history_candidates(session, transaction)
            if history.candidates:
                memory = _memory_candidate(history.candidates[0], transaction.amount_cents)
                if memory is not None:
                    candidates.append(memory)

        if not active:
            suggestions = list(
                session.scalars(
                    select(ClassificationSuggestion)
                    .where(
                        ClassificationSuggestion.transaction_id == transaction_id,
                        ClassificationSuggestion.status == "review",
                    )
                    .order_by(
                        ClassificationSuggestion.created_at_utc,
                        ClassificationSuggestion.suggestion_id,
                    )
                )
            )
            candidates.extend(_suggestion_candidates(suggestions, transaction.amount_cents))

        ordered_candidates = tuple(
            sorted(
                candidates,
                key=lambda item: (
                    item.priority_rank,
                    item.rule_priority or 0,
                    item.rule_id or "",
                    item.suggestion_id or "",
                ),
            )
        )
        if human_locked:
            winner = next((item for item in ordered_candidates if item.source == "human"), None)
        else:
            winner = None if conflicts else ordered_candidates[0] if ordered_candidates else None
        return TransactionEvaluation(
            transaction_id=transaction_id,
            transaction_version=transaction.version,
            candidates=ordered_candidates,
            winner=winner,
            conflict_rule_ids=conflicts,
            human_decision_protected=human_locked,
            context=context,
        )


def _condition_is_ambiguous_for_transaction(session: Session, transaction: Transaction) -> bool:
    if transaction.payee_id is not None:
        payee = session.get(Payee, transaction.payee_id)
        if payee is not None:
            metadata = key_metadata(payee.normalized_name)
            if metadata is not None and metadata.get("entity_kind") == "marketplace":
                return True
    searchable_label = str(search_label(transaction.identity_label))
    return any(
        str(search_label(marketplace)) in searchable_label for marketplace in AMBIGUOUS_MARKETPLACES
    )


def _all_rule_matches_conflict(
    session: Session,
    draft_record: RuleRecord,
    transaction: Transaction,
    approved: tuple[RuleRecord, ...],
    exception_ids_by_rule: dict[str, frozenset[str]] | None = None,
) -> tuple[bool, bool]:
    """Return (conflict, shadowed) against already approved existing-scope rules."""
    matched = [
        rule
        for rule in approved
        if rule.rule_id != draft_record.rule_id
        and rule.application_scope in ("selection", "existing_and_future")
        and matches_condition(
            session,
            rule,
            transaction,
            exception_ids=(exception_ids_by_rule or {}).get(rule.rule_id),
        )
    ]
    same_priority = [rule for rule in matched if rule.priority == draft_record.priority]
    conflict = any(
        action_signature(rule.action) != action_signature(draft_record.action)
        for rule in same_priority
    )
    shadowed = any(rule.priority < draft_record.priority for rule in matched)
    return conflict, shadowed


def simulate_rule(
    engine: Engine,
    rule_draft: RuleDraft | RuleRecord,
    scope: SimulationScope,
) -> RuleSimulation:
    """Simulate a rule using read-only DB access; no execution/audit row is written."""
    _check_simulation_scope(scope)
    rule = _draft_record(rule_draft) if isinstance(rule_draft, RuleDraft) else rule_draft
    if rule.application_scope == "selection" and not scope.transaction_ids:
        raise _fail(
            "RULE_SELECTION_REQUIRED", "Une règle limitée à la sélection exige des IDs explicites."
        )

    started = time.perf_counter_ns()
    with session_scope(engine) as session:
        statement = select(Transaction)
        filters: list[ColumnElement[bool]] = []
        if scope.account_ids:
            filters.append(Transaction.account_id.in_(scope.account_ids))
        if scope.booked_from:
            filters.append(Transaction.booked_on >= scope.booked_from)
        if scope.booked_through:
            filters.append(Transaction.booked_on <= scope.booked_through)
        if scope.transaction_ids:
            selected: list[Transaction] = []
            for batch in _id_batches(list(scope.transaction_ids)):
                selected.extend(
                    session.scalars(
                        statement.where(*filters, Transaction.transaction_id.in_(batch))
                    )
                )
            selected.sort(key=lambda row: (row.booked_on, row.transaction_id))
            fetched = selected[: scope.max_transactions + 1]
        else:
            fetched = list(
                session.scalars(
                    statement.where(*filters)
                    .order_by(Transaction.booked_on, Transaction.transaction_id)
                    .limit(scope.max_transactions + 1)
                )
            )
        truncated = len(fetched) > scope.max_transactions
        transactions = fetched[: scope.max_transactions]
        transaction_ids = [transaction.transaction_id for transaction in transactions]
        id_set = set(transaction_ids)

        active_by_tx: dict[str, list[Allocation]] = defaultdict(list)
        decisions_by_tx: dict[str, list[ClassificationDecision]] = defaultdict(list)
        executions: set[tuple[str, str]] = set()
        if transaction_ids:
            for batch in _id_batches(transaction_ids):
                active_rows = session.scalars(
                    select(Allocation)
                    .where(
                        Allocation.transaction_id.in_(batch),
                        Allocation.superseded_by_allocation_id.is_(None),
                        Allocation.retracted_at_utc.is_(None),
                    )
                    .order_by(
                        Allocation.transaction_id,
                        Allocation.category_id,
                        Allocation.allocation_id,
                    )
                )
                for allocation in active_rows:
                    active_by_tx[allocation.transaction_id].append(allocation)
                for decision in session.scalars(
                    select(ClassificationDecision).where(
                        ClassificationDecision.transaction_id.in_(batch)
                    )
                ):
                    decisions_by_tx[decision.transaction_id].append(decision)
                executions.update(
                    (item.rule_id, item.transaction_id)
                    for item in session.scalars(
                        select(RuleExecution).where(
                            RuleExecution.rule_id == rule.rule_id,
                            RuleExecution.transaction_id.in_(batch),
                            RuleExecution.outcome == "applied",
                            RuleExecution.dry_run == 0,
                        )
                    )
                )

        approved = tuple(
            item
            for item in list_rules(engine, approved_only=True)
            if item.application_scope in ("selection", "existing_and_future")
        )
        exception_ids_by_rule = {
            item.rule_id: frozenset(exception.transaction_id for exception in item.exceptions)
            for item in (rule, *approved)
        }
        matched_rows: list[SimulationRow] = []
        category_values: dict[str, list[int]] = defaultdict(list)
        conflicts: list[str] = []
        human_ignored: list[str] = []
        classified: list[str] = []
        ambiguous_ids: list[str] = []

        for transaction in transactions:
            if transaction.transaction_id not in id_set or not matches_condition(
                session,
                rule,
                transaction,
                exception_ids=exception_ids_by_rule[rule.rule_id],
            ):
                continue
            if (rule.rule_id, transaction.transaction_id) in executions:
                classified.append(transaction.transaction_id)
                continue
            active = active_by_tx.get(transaction.transaction_id, [])
            decisions = decisions_by_tx.get(transaction.transaction_id, [])
            if _human_decision_protected(decisions, active):
                human_ignored.append(transaction.transaction_id)
                continue
            if active:
                classified.append(transaction.transaction_id)
                continue
            conflict, shadowed = _all_rule_matches_conflict(
                session, rule, transaction, approved, exception_ids_by_rule
            )
            if conflict:
                conflicts.append(transaction.transaction_id)
                continue
            if shadowed:
                classified.append(transaction.transaction_id)
                continue
            ambiguous = _condition_is_ambiguous_for_transaction(session, transaction)
            if ambiguous:
                ambiguous_ids.append(transaction.transaction_id)
            after_parts: list[dict[str, str | int]] = []
            for part in action_parts(rule.action, transaction.amount_cents):
                after_parts.append(
                    {
                        "category_id": part.category_id,
                        "flow_kind": str(getattr(part.flow_kind, "value", part.flow_kind)),
                        "amount_cents": part.amount_cents,
                        "origin": "rule",
                        "beneficiary": str(getattr(part.beneficiary, "value", part.beneficiary)),
                        "cadence": str(getattr(part.cadence, "value", part.cadence)),
                        "flexibility": str(getattr(part.flexibility, "value", part.flexibility)),
                        "acquisition": str(getattr(part.acquisition, "value", part.acquisition)),
                    }
                )
            after = tuple(after_parts)
            for after_item in after:
                category_id = after_item["category_id"]
                amount_cents = after_item["amount_cents"]
                assert isinstance(category_id, str) and type(amount_cents) is int
                category_values[category_id].append(amount_cents)
            matched_rows.append(
                SimulationRow(
                    transaction_id=transaction.transaction_id,
                    transaction_version=transaction.version,
                    booked_on=transaction.booked_on,
                    account_id=transaction.account_id,
                    amount_cents=transaction.amount_cents,
                    amount_euros=_format_euros(transaction.amount_cents),
                    before=_json_allocations(active),
                    after=after,
                    ambiguous=ambiguous,
                )
            )

    end = scope.offset + scope.page_size
    rows = tuple(matched_rows[scope.offset : end])
    next_offset = end if end < len(matched_rows) else None
    totals = {
        category_id: CategoryTotal(
            transaction_count=len(values),
            amount_cents=sum(values),
            amount_euros=_format_euros(sum(values)),
        )
        for category_id, values in sorted(category_values.items())
    }
    # Keep the measured duration available in performance diagnostics without
    # storing or logging transaction text; normal callers can measure externally.
    elapsed = time.perf_counter_ns() - started
    return RuleSimulation(
        rule_id=rule.rule_id if rule.rule_id != "draft" else None,
        rule_version=rule.version if rule.rule_id != "draft" else None,
        scanned_count=len(transactions),
        matched_count=len(matched_rows),
        rows=rows,
        totals_by_category=totals,
        conflict_transaction_ids=tuple(conflicts),
        human_decision_ignored_ids=tuple(human_ignored),
        already_classified_ids=tuple(classified),
        ambiguous_transaction_ids=tuple(ambiguous_ids),
        offset=scope.offset,
        next_offset=next_offset,
        truncated=truncated,
        elapsed_ns=elapsed,
    )


def _simulate_all_pages(
    engine: Engine, rule: RuleRecord, scope: SimulationScope
) -> tuple[RuleSimulation, tuple[SimulationRow, ...]]:
    first = simulate_rule(engine, rule, scope)
    all_rows = list(first.rows)
    offset = first.next_offset
    while offset is not None:
        page = simulate_rule(engine, rule, replace(scope, offset=offset))
        all_rows.extend(page.rows)
        offset = page.next_offset
    return first, tuple(all_rows)


def _trace_execution(
    session: Session,
    *,
    rule: RuleRecord,
    transaction_id: str,
    actor: str,
    batch_id: str,
    batch_scope_sha256: str,
    outcome: Literal["applied", "skipped_manual", "skipped_scope"],
    before: list[dict[str, str | int]] | None,
    after: list[dict[str, str | int]] | None,
    classification_decision_id: str | None = None,
) -> None:
    event_id = str(uuid.uuid4())
    before_value = {
        "rule_version": rule.version,
        "allocations": before,
    }
    after_value = {
        "rule_version": rule.version,
        "outcome": outcome,
        "allocations": after,
    }
    before_json = json.dumps(
        before_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    after_json = json.dumps(after_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    existing = session.scalar(
        select(RuleExecution.rule_execution_id)
        .where(
            RuleExecution.rule_id == rule.rule_id,
            RuleExecution.transaction_id == transaction_id,
            RuleExecution.outcome.in_(("applied", "skipped_manual", "skipped_scope")),
            RuleExecution.dry_run == 0,
        )
        .limit(1)
    )
    if existing is not None:
        return
    session.add(
        AuditEvent(
            audit_event_id=event_id,
            target_type="transaction",
            target_id=transaction_id,
            action=f"rule.{outcome}",
            actor=actor,
            before_json=before_json,
            after_json=after_json,
        )
    )
    session.flush()
    session.add(
        RuleExecution(
            rule_execution_id=str(uuid.uuid4()),
            rule_id=rule.rule_id,
            transaction_id=transaction_id,
            outcome=outcome,
            before_json=before_json,
            after_json=after_json,
            dry_run=0,
            audit_event_id=event_id,
            batch_id=batch_id,
            rule_version=rule.version,
            batch_scope_sha256=batch_scope_sha256,
            classification_decision_id=classification_decision_id,
        )
    )


def _revalidate_rule_for_write(session: Session, rule_id: str, expected_version: int) -> None:
    current_rule = session.get(Rule, rule_id)
    if current_rule is None or current_rule.version != expected_version:
        raise VersionConflictError()
    if current_rule.is_approved != 1 or current_rule.is_active != 1:
        raise _fail("RULE_NOT_APPROVED", "Seule une règle approuvée peut être appliquée.", 409)


def apply_rule(
    engine: Engine,
    rule_id: str,
    *,
    scope: ApplyScope,
    expected_version: int,
    actor: str,
    selection: tuple[str, ...] | list[str] = (),
    batch_id: str | None = None,
) -> RuleApplication:
    """Explicitly apply an approved rule to selected/current existing transactions.

    Each transaction's allocation, decision audit, and rule-execution trace
    commit together through the canonical allocation service. A multi-
    transaction application can commit earlier transactions before a later
    one fails.
    """
    started = time.perf_counter_ns()
    if type(expected_version) is not int:
        raise _fail("RULE_INVALID", "expected_version doit être un entier strict.")
    if not isinstance(actor, str) or not actor.strip():
        raise _fail("RULE_INVALID", "Un acteur non vide est requis.")
    if scope not in ("selection", "existing_and_future"):
        raise _fail(
            "RULE_SCOPE_INVALID",
            "Cette portée ne peut pas être appliquée aux opérations existantes.",
        )
    rule = get_rule(engine, rule_id)
    if rule.version != expected_version:
        raise VersionConflictError()
    if rule.status != "approved":
        raise _fail("RULE_NOT_APPROVED", "Seule une règle approuvée peut être appliquée.", 409)
    if scope == "existing_and_future" and rule.application_scope != "existing_and_future":
        raise _fail(
            "RULE_RETROACTIVITY_NOT_ALLOWED",
            "La règle n'autorise pas l'application aux opérations existantes.",
            409,
        )
    if rule.application_scope == "future_only":
        raise _fail(
            "RULE_FUTURE_ONLY",
            "Une règle future_only ne peut pas être appliquée à l'existant.",
            409,
        )
    if scope == "selection":
        if not selection:
            raise _fail("RULE_SELECTION_REQUIRED", "La sélection explicite est vide.")
        if len(selection) > MAX_SELECTION_SIZE:
            raise _fail("RULE_SELECTION_TOO_LARGE", "La sélection dépasse 10000 transactions.")
    elif selection:
        raise _fail(
            "RULE_SCOPE_INVALID", "Une portée complète ne doit pas inclure de sélection partielle."
        )

    sim_scope = SimulationScope(
        transaction_ids=tuple(selection),
        page_size=MAX_PAGE_SIZE,
        max_transactions=MAX_SIMULATION_TRANSACTIONS,
    )
    simulation, all_rows = _simulate_all_pages(engine, rule, sim_scope)
    if simulation.truncated:
        raise _fail(
            "RULE_SIMULATION_LIMIT", "La portée dépasse le plafond de 10000 transactions.", 409
        )

    row_by_id = {row.transaction_id: row for row in all_rows}
    manual_ids = set(simulation.human_decision_ignored_ids)
    classified_ids = set(simulation.already_classified_ids)
    conflict_ids = set(simulation.conflict_transaction_ids)
    if scope == "selection":
        target_ids = tuple(dict.fromkeys(selection))
    else:
        with session_scope(engine) as session:
            target_ids = tuple(
                session.scalars(
                    select(Transaction.transaction_id).order_by(
                        Transaction.booked_on, Transaction.transaction_id
                    )
                )
            )
    if batch_id is None:
        batch_id_value = str(uuid.uuid4())
    else:
        try:
            parsed_batch_id = uuid.UUID(batch_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise _fail("RULE_BATCH_INVALID", "batch_id doit être un UUIDv4 canonique.") from error
        if str(parsed_batch_id) != batch_id or parsed_batch_id.version != 4:
            raise _fail("RULE_BATCH_INVALID", "batch_id doit être un UUIDv4 canonique.")
        batch_id_value = batch_id
    scope_document = json.dumps(
        {
            "actor": actor.strip(),
            "rule_id": rule_id,
            "rule_version": expected_version,
            "scope": scope,
            "transaction_ids": sorted(target_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    batch_scope_sha256 = hashlib.sha256(scope_document.encode("utf-8")).hexdigest()
    with session_scope(engine) as session:
        batch_rows = tuple(
            session.execute(
                select(
                    RuleExecution.rule_id,
                    RuleExecution.transaction_id,
                    RuleExecution.outcome,
                    RuleExecution.rule_version,
                    RuleExecution.batch_scope_sha256,
                    RuleExecution.undone_by_decision_id,
                ).where(
                    RuleExecution.batch_id == batch_id_value,
                    RuleExecution.dry_run == 0,
                )
            ).all()
        )
    if batch_rows:
        if any(
            row.rule_id != rule_id
            or row.rule_version != expected_version
            or row.batch_scope_sha256 != batch_scope_sha256
            for row in batch_rows
        ):
            raise _fail("RULE_BATCH_CONFLICT", "batch_id est déjà associé à une autre portée.", 409)
        if any(row.undone_by_decision_id is not None for row in batch_rows):
            raise _fail("RULE_BATCH_ALREADY_UNDONE", "Ce lot a déjà été annulé.", 409)
    allocations: list[AllocationState] = []
    prior_applied = {row.transaction_id for row in batch_rows if row.outcome == "applied"}
    applied_ids: list[str] = [
        transaction_id for transaction_id in target_ids if transaction_id in prior_applied
    ]
    for transaction_id in applied_ids:
        allocations.append(get_allocation_state(engine, transaction_id))
    skipped_manual: list[str] = []
    skipped_classified: list[str] = []

    for transaction_id in target_ids:
        if transaction_id in prior_applied:
            continue
        if transaction_id in manual_ids:
            skipped_manual.append(transaction_id)
            with write_transaction(engine) as session:
                _revalidate_rule_for_write(session, rule_id, expected_version)
                _trace_execution(
                    session,
                    rule=rule,
                    transaction_id=transaction_id,
                    actor=actor.strip(),
                    batch_id=batch_id_value,
                    batch_scope_sha256=batch_scope_sha256,
                    outcome="skipped_manual",
                    before=None,
                    after=None,
                )
            continue
        if transaction_id in conflict_ids:
            continue
        row = row_by_id.get(transaction_id)
        if row is None:
            if transaction_id in classified_ids:
                skipped_classified.append(transaction_id)
            continue

        # Rule version, allocation and trace share one write transaction. A
        # failed trace rolls back the classification instead of leaving an
        # untraceable applied allocation behind.
        with write_transaction(engine) as session:
            _revalidate_rule_for_write(session, rule_id, expected_version)
            prior_execution = session.scalar(
                select(RuleExecution.rule_execution_id)
                .where(
                    RuleExecution.rule_id == rule_id,
                    RuleExecution.transaction_id == transaction_id,
                    RuleExecution.outcome == "applied",
                    RuleExecution.dry_run == 0,
                )
                .limit(1)
            )
            if prior_execution is not None:
                skipped_classified.append(transaction_id)
                continue
            state = allocate_in_session(
                session,
                transaction_id,
                action_parts(rule.action, row.amount_cents),
                mode="complete",
                origin="rule",
                actor=actor.strip(),
                expected_version=row.transaction_version,
            )
            after: list[dict[str, str | int]] = [
                {
                    "category_id": item.category_id,
                    "flow_kind": item.flow_kind,
                    "amount_cents": item.amount_cents,
                    "origin": item.origin,
                    "beneficiary": item.beneficiary,
                    "cadence": item.cadence,
                    "flexibility": item.flexibility,
                    "acquisition": item.acquisition,
                }
                for item in state.allocations
            ]
            _trace_execution(
                session,
                rule=rule,
                transaction_id=transaction_id,
                actor=actor.strip(),
                batch_id=batch_id_value,
                batch_scope_sha256=batch_scope_sha256,
                outcome="applied",
                before=[],
                after=after,
                classification_decision_id=state.decision_id,
            )
        allocations.append(state)
        applied_ids.append(transaction_id)

    return RuleApplication(
        rule_id=rule_id,
        rule_version=rule.version,
        applied_ids=tuple(applied_ids),
        skipped_manual_ids=tuple(skipped_manual),
        skipped_classified_ids=tuple(skipped_classified),
        conflict_ids=tuple(sorted(conflict_ids)),
        allocation_states=tuple(allocations),
        elapsed_ns=time.perf_counter_ns() - started,
        batch_id=batch_id_value,
    )
