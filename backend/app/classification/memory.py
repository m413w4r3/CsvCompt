"""Read-only proposals from repeated, human-confirmed payee history."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classification.matcher import derive_payee_key
from app.db.models import (
    Allocation,
    ClassificationSuggestion,
    Payee,
    Transaction,
)
from app.domain.types import Origin
from app.importers.normalize import parse_ca_label

HistoryScope = Literal["exact_payee", "payee_plus_context"]


@dataclass(frozen=True, slots=True)
class HistoryCandidate:
    """One non-applying category suggestion with complete source proof."""

    method: Literal["history"]
    scope: HistoryScope
    category_id: str | None
    flow_kind: str | None
    source_transaction_ids: tuple[str, ...]
    source_count: int
    category_distribution: dict[str, int]
    last_validated_at: datetime | None
    confidence: Literal["exact", "probable", "ambigu"]
    ambiguous_history: bool = False
    has_historical_split: bool = False


@dataclass(frozen=True, slots=True)
class HistoryResult:
    """Read-only review result; an empty candidate tuple means no suggestion."""

    candidates: tuple[HistoryCandidate, ...]
    needs_review: bool
    reason: str | None = None
    ambiguous_history: bool = False


def history_candidates(
    session: Session,
    transaction: Transaction,
    *,
    scope: HistoryScope = "exact_payee",
    amount_tolerance_cents: int | None = None,
) -> HistoryResult:
    """Suggest from prior manual confirmations of the same exact merchant key.

    This function only reads. A single example is insufficient; three agreeing
    validations are labeled `exact`, two agreeing validations `probable`. Any
    category conflict for the payee suppresses a unique category, even when a
    narrower context was requested. No split is returned as an applicable split.
    """

    if scope not in ("exact_payee", "payee_plus_context"):
        raise ValueError("portée mémoire invalide")
    if amount_tolerance_cents is not None and (
        type(amount_tolerance_cents) is not int or amount_tolerance_cents < 0
    ):
        raise ValueError("la fourchette de montant doit être un entier positif ou nul")
    if transaction.payee_id is None:
        return HistoryResult((), needs_review=False, reason="payee_unresolved")

    target_key = derive_payee_key(parse_ca_label(transaction.raw_label))
    payee = session.get(Payee, transaction.payee_id)
    if target_key is None or payee is None or payee.is_active != 1:
        return HistoryResult((), needs_review=False, reason="payee_unresolved")
    if payee.normalized_name != target_key.normalized_name:
        # An alias is an exact approved attachment but does not rewrite the
        # transaction's original merchant key or broaden its memory history.
        return HistoryResult((), needs_review=True, reason="alias_key_requires_review")
    if target_key.ambiguous:
        return HistoryResult((), needs_review=True, reason="ambiguous_marketplace")
    if target_key.is_intermediary:
        return HistoryResult((), needs_review=True, reason="intermediary_requires_review")

    confirmed_allocation_exists = session.scalar(
        select(Allocation.allocation_id)
        .join(Transaction, Transaction.transaction_id == Allocation.transaction_id)
        .where(
            Transaction.transaction_id == transaction.transaction_id,
            Allocation.decision == "confirmed",
            Allocation.superseded_by_allocation_id.is_(None),
            Allocation.retracted_at_utc.is_(None),
            Allocation.origin.in_((Origin.MANUAL.value, Origin.RULE.value)),
        )
        .limit(1)
    )
    if confirmed_allocation_exists is not None:
        # Human and approved-rule decisions outrank memory; do not create a
        # contradictory lower-priority suggestion for an already resolved row.
        return HistoryResult((), needs_review=False, reason="higher_priority_decision_exists")

    history = _manual_history(session, transaction, target_key.normalized_name)
    if not history:
        return HistoryResult((), needs_review=False, reason="insufficient_manual_history")

    all_distribution = _category_distribution(history)
    distinct_categories = set(all_distribution)
    distinct_flows = {
        (allocation.category_id, allocation.flow_kind)
        for _source, allocations in history
        for allocation in allocations
    }
    has_conflict = len(distinct_categories) > 1 or len(distinct_flows) > 1
    ambiguous = has_conflict

    scoped_history = history
    if scope == "payee_plus_context":
        target_operation_type = target_key.operation_type
        target_sign = _sign(transaction.amount_cents)
        target_abs_amount = abs(transaction.amount_cents)
        scoped_history = [
            item
            for item in history
            if parse_ca_label(item[0].raw_label).operation_type == target_operation_type
            and _sign(item[0].amount_cents) == target_sign
            and (
                amount_tolerance_cents is None
                or abs(abs(item[0].amount_cents) - target_abs_amount) <= amount_tolerance_cents
            )
        ]
    scoped_ids = tuple(sorted(source.transaction_id for source, _allocations in scoped_history))
    if len(scoped_history) < 2:
        return HistoryResult(
            (),
            needs_review=False,
            reason="insufficient_manual_history",
            ambiguous_history=ambiguous,
        )

    categories = {
        allocation.category_id
        for _source, allocations in scoped_history
        for allocation in allocations
    }
    flows = {
        (allocation.category_id, allocation.flow_kind)
        for _source, allocations in scoped_history
        for allocation in allocations
    }
    is_ambiguous = ambiguous or len(categories) != 1 or len(flows) != 1
    selected_category: str | None = None
    selected_flow: str | None = None
    if not is_ambiguous:
        selected_category, selected_flow = next(iter(flows))

    source_allocations = [allocation for _source, rows in scoped_history for allocation in rows]
    last_validated_at = max(
        (allocation.created_at_utc for allocation in source_allocations), default=None
    )
    scoped_distribution = _category_distribution(scoped_history)
    confidence: Literal["exact", "probable", "ambigu"] = (
        "ambigu" if is_ambiguous else "exact" if len(scoped_history) >= 3 else "probable"
    )
    candidate = HistoryCandidate(
        method="history",
        scope=scope,
        category_id=selected_category,
        flow_kind=selected_flow,
        source_transaction_ids=scoped_ids,
        source_count=len(scoped_history),
        category_distribution=scoped_distribution,
        last_validated_at=last_validated_at,
        confidence=confidence,
        ambiguous_history=is_ambiguous,
        has_historical_split=any(
            len({allocation.category_id for allocation in allocations}) > 1
            for _source, allocations in scoped_history
        ),
    )
    return HistoryResult(
        candidates=(candidate,),
        needs_review=is_ambiguous,
        reason="ambiguous_history" if is_ambiguous else None,
        ambiguous_history=is_ambiguous,
    )


def persist_suggestions(
    session: Session,
    transaction: Transaction,
    result: HistoryResult,
) -> list[ClassificationSuggestion]:
    """Idempotently persist reviewed history objects without changing decisions."""

    higher_priority_decision = session.scalar(
        select(Allocation.allocation_id)
        .where(
            Allocation.transaction_id == transaction.transaction_id,
            Allocation.origin.in_((Origin.MANUAL.value, Origin.RULE.value)),
            Allocation.decision == "confirmed",
            Allocation.superseded_by_allocation_id.is_(None),
            Allocation.retracted_at_utc.is_(None),
        )
        .limit(1)
    )
    if higher_priority_decision is not None:
        return []

    persisted: list[ClassificationSuggestion] = []
    for candidate in result.candidates:
        reason_payload = {
            "ambiguous_history": candidate.ambiguous_history,
            "category_distribution": candidate.category_distribution,
            "confidence": candidate.confidence,
            "scope": candidate.scope,
            "source_count": candidate.source_count,
            "source_transaction_ids": list(candidate.source_transaction_ids),
        }
        reason = json.dumps(
            reason_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        confidence_label = {
            "exact": "certain",
            "probable": "doute",
            "ambigu": "inconnu",
        }[candidate.confidence]
        flow_kind = candidate.flow_kind or "unknown"
        existing = session.scalar(
            select(ClassificationSuggestion).where(
                ClassificationSuggestion.transaction_id == transaction.transaction_id,
                ClassificationSuggestion.method == "history",
                ClassificationSuggestion.proposed_flow_kind == flow_kind,
                ClassificationSuggestion.category_id == candidate.category_id,
                ClassificationSuggestion.reason == reason,
                ClassificationSuggestion.status == "review",
            )
        )
        if existing is not None:
            persisted.append(existing)
            continue
        suggestion = ClassificationSuggestion(
            transaction_id=transaction.transaction_id,
            proposed_flow_kind=flow_kind,
            category_id=candidate.category_id,
            suggested_amount_cents=None,
            method="history",
            confidence_label=confidence_label,
            reason=reason,
            status="review",
        )
        session.add(suggestion)
        session.flush()
        persisted.append(suggestion)
    return persisted


def _manual_history(
    session: Session,
    target: Transaction,
    normalized_name: str,
) -> list[tuple[Transaction, list[Allocation]]]:
    rows = session.execute(
        select(Transaction, Allocation)
        .join(Allocation, Allocation.transaction_id == Transaction.transaction_id)
        .where(
            Transaction.payee_id == target.payee_id,
            Transaction.transaction_id != target.transaction_id,
            Allocation.origin == Origin.MANUAL.value,
            Allocation.decision == "confirmed",
            Allocation.superseded_by_allocation_id.is_(None),
            Allocation.retracted_at_utc.is_(None),
        )
        .order_by(Transaction.transaction_id, Allocation.allocation_id)
    )
    by_transaction: dict[str, tuple[Transaction, list[Allocation]]] = {}
    for source, allocation in rows:
        source_key = derive_payee_key(parse_ca_label(source.raw_label))
        if source_key is None or source_key.normalized_name != normalized_name:
            continue
        pair = by_transaction.setdefault(source.transaction_id, (source, []))
        pair[1].append(allocation)
    return [by_transaction[key] for key in sorted(by_transaction)]


def _category_distribution(
    rows: list[tuple[Transaction, list[Allocation]]],
) -> dict[str, int]:
    distribution: Counter[str] = Counter()
    for _transaction, allocations in rows:
        for category_id in {allocation.category_id for allocation in allocations}:
            distribution[category_id] += 1
    return dict(sorted(distribution.items()))


def _sign(amount_cents: int) -> int:
    if type(amount_cents) is not int or amount_cents == 0:
        raise ValueError("signe de transaction invalide")
    return 1 if amount_cents > 0 else -1
