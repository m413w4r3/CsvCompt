"""Canonical, integer-only financial metric definitions and read model."""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, aliased

from app.analytics.quality import coverage_summary, load_quality_snapshot
from app.db.models import (
    Account,
    Allocation,
    Category,
    RefundLinkAllocation,
    Transaction,
    TransactionLink,
)
from app.services.balance_checks import calculate_balance_checks

MAX_SAFE_FRONTEND_INTEGER = 2**53 - 1
RELEVANT_UNCLASSIFIED_FLOWS = frozenset(
    {
        "expense",
        "income_recurring",
        "income_other",
        "allocation_personal",
        "refund",
        "cash_withdrawal_pending",
        "intermediary_pending",
    }
)


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    metric_definition_id: str
    label: str
    formula: str
    inclusions: tuple[str, ...]
    exclusions: tuple[str, ...]
    unit: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_definition_id": self.metric_definition_id,
            "label": self.label,
            "formula": self.formula,
            "inclusions": list(self.inclusions),
            "exclusions": list(self.exclusions),
            "unit": self.unit,
        }


METRIC_DEFINITIONS: dict[str, MetricDefinition] = {
    definition.metric_definition_id: definition
    for definition in (
        MetricDefinition(
            "bank_net_flow.v1",
            "Flux bancaire net",
            "Σ amount_cents des transactions sélectionnées, une fois chacune",
            ("Tous les flow_kind", "Montant bancaire complet d'une transaction retenue"),
            ("Aucune allocation ni lien n'est substitué au montant bancaire",),
            "EUR-cent",
        ),
        MetricDefinition(
            "regular_income.v1",
            "Revenus réguliers",
            "Σ allocations confirmées income_recurring admissibles",
            ("Salaire et revenu régulier confirmé",),
            ("Aide/autre, remboursement, prêt reçu et transfert interne",),
            "EUR-cent",
        ),
        MetricDefinition(
            "consumption_net.v1",
            "Consommation nette",
            "−Σ expense − remboursements refund_of confirmés attribués au jour du crédit",
            ("Dépenses expense du périmètre personnel sélectionné", "Parts refund matérialisées"),
            ("Transferts, prêts, capital, espèces en attente, remboursements non liés",),
            "EUR-cent",
        ),
        MetricDefinition(
            "personal_allowances.v1",
            "Allocations personnelles",
            "−Σ allocations allocation_personal confirmées du compte non personnel",
            ("Enveloppes personnelles en mode envelope",),
            ("Dépenses observées du compte personnel; valeur nulle en mode observed",),
            "EUR-cent",
        ),
        MetricDefinition(
            "savings_net_transferred.v1",
            "Épargne nette transférée",
            "−Σ allocations transfer_savings confirmées des comptes current sélectionnés",
            ("Jambe portée par un compte current",),
            ("Jambe du compte savings et transferts owned",),
            "EUR-cent",
        ),
        MetricDefinition(
            "operational_saving_capacity.v1",
            "Capacité d'épargne opérationnelle",
            (
                "revenu régulier − consommation nette − allocations personnelles "
                "− engagements sélectionnés"
            ),
            ("Mois complet et flux sélectionnés ventilés",),
            ("Engagements non sélectionnés et résidus non nuls",),
            "EUR-cent",
        ),
        MetricDefinition(
            "operational_saving_rate.v1",
            "Taux d'épargne opérationnel",
            "arrondi demi-loin-de-zéro(10000 × capacité / revenu régulier)",
            ("Basis points entiers",),
            ("Revenu ≤ 0, couverture incomplète ou capacité indisponible",),
            "basis-point-of-percent",
        ),
        MetricDefinition(
            "unclassified_amount_share.v1",
            "Part des montants non ventilés",
            "arrondi(10000 × Σ|résidu par opération| / Σ|allocations pertinentes et résidus|)",
            ("Résidus transactionnels et allocations de flux pertinents",),
            ("Prêts, épargne, capital et régularisation confirmés au dénominateur",),
            "basis-point-of-percent",
        ),
        MetricDefinition(
            "operation_coverage.v1",
            "Couverture de classification des opérations",
            "arrondi(10000 × opérations entièrement ventilées / opérations sélectionnées)",
            ("Toutes les transactions sélectionnées",),
            ("Les suggestions ne sont pas des allocations confirmées",),
            "basis-point-of-percent",
        ),
        MetricDefinition(
            "monthly_delta.v1",
            "Écart mensuel",
            "valeur brute du mois complet − même métrique du mois précédent complet",
            ("Mêmes filtres et métrique de base",),
            ("Mois partiel, précédent absent, couverture différente ou inconnue",),
            "EUR-cent-or-base-unit",
        ),
        MetricDefinition(
            "month_completeness.v1",
            "Complétude de la période",
            "État de l'union journalière des plages complètes par compte",
            ("Sections commitées et contrôle de solde recalculé",),
            ("Le jour provisoire ne valide pas la complétude",),
            "state",
        ),
    )
}


@dataclass(frozen=True, slots=True)
class AnalyticsFilters:
    period_start: date
    period_end: date
    account_ids: tuple[str, ...]
    category_ids: tuple[str, ...] = ()
    flow_kinds: tuple[str, ...] = ()
    mode: str = "treasury"
    personal_mode: str = "envelope"
    commitment_transaction_ids: tuple[str, ...] = ()
    as_of: date | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "account_ids": list(self.account_ids),
            "category_ids": list(self.category_ids) or None,
            "flow_kinds": list(self.flow_kinds) or None,
            "mode": self.mode,
            "personal_mode": self.personal_mode,
            "commitment_transaction_ids": list(self.commitment_transaction_ids) or None,
        }


@dataclass(frozen=True, slots=True)
class MetricAllocation:
    allocation_id: str
    transaction_id: str
    category_id: str
    flow_kind: str
    amount_cents: int
    beneficiary: str = "unknown"


@dataclass(frozen=True, slots=True)
class MetricTransaction:
    transaction_id: str
    account_id: str
    booked_on: date
    amount_cents: int
    account_role: str
    payee_id: str | None = None
    raw_label: str = ""


@dataclass(frozen=True, slots=True)
class RefundPart:
    transaction_link_id: str
    source_transaction_id: str
    target_transaction_id: str
    target_account_id: str
    target_allocation_id: str
    category_id: str
    amount_cents: int
    target_booked_on: date
    beneficiary: str = "unknown"


@dataclass(slots=True)
class AnalyticsDataset:
    filters: AnalyticsFilters
    account_roles: dict[str, str]
    transactions: tuple[MetricTransaction, ...]
    allocations_by_transaction: dict[str, tuple[MetricAllocation, ...]]
    refund_parts: tuple[RefundPart, ...]
    categories: dict[str, dict[str, Any]]
    coverage: dict[str, Any]
    transfer_pairs: tuple[dict[str, Any], ...] = ()
    _expanded_categories: tuple[str, ...] = field(default_factory=tuple)
    _quality_sections: tuple[dict[str, Any], ...] = ()
    _quality_transactions: tuple[dict[str, Any], ...] = ()
    _balance_checks: tuple[dict[str, Any], ...] = ()


def rounded_basis_points(numerator: int, denominator: int) -> int | None:
    """Integer-only half-away-from-zero ratio; denominator must be positive."""
    if denominator <= 0:
        return None
    sign = -1 if numerator < 0 else 1
    magnitude = abs(numerator)
    return sign * ((2 * magnitude * 10_000 + denominator) // (2 * denominator))


def _category_scope(
    filters: AnalyticsFilters, categories: Mapping[str, Mapping[str, Any]]
) -> tuple[str, ...]:
    if not filters.category_ids:
        return ()
    unknown = set(filters.category_ids) - set(categories)
    if unknown:
        raise ValueError("unknown_category")
    children: dict[str, list[str]] = defaultdict(list)
    for category_id, category in categories.items():
        parent_id = category.get("parent_category_id")
        if isinstance(parent_id, str):
            children[parent_id].append(category_id)
    selected = set(filters.category_ids)
    pending = list(selected)
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in selected:
                selected.add(child)
                pending.append(child)
    return tuple(sorted(selected))


def _filter_allocation(
    allocation: MetricAllocation, filters: AnalyticsFilters, expanded: Sequence[str]
) -> bool:
    return (not expanded or allocation.category_id in expanded) and (
        not filters.flow_kinds or allocation.flow_kind in filters.flow_kinds
    )


def selected_transaction_ids(dataset: AnalyticsDataset) -> set[str]:
    filters = dataset.filters
    expanded = dataset._expanded_categories
    by_source_refund: dict[str, list[RefundPart]] = defaultdict(list)
    for part in dataset.refund_parts:
        by_source_refund[part.source_transaction_id].append(part)
    result: set[str] = set()
    for transaction in dataset.transactions:
        allocations = dataset.allocations_by_transaction.get(transaction.transaction_id, ())
        residual = transaction.amount_cents - sum(item.amount_cents for item in allocations)
        matched = False
        if not filters.category_ids and not filters.flow_kinds:
            matched = True
        else:
            if "unknown" in filters.flow_kinds and residual != 0:
                matched = not filters.category_ids
            if not matched:
                matched = any(
                    _filter_allocation(item, filters, expanded)
                    for item in allocations
                    if item.flow_kind != "unknown"
                )
            if not matched and filters.category_ids:
                matched = any(
                    part.category_id in expanded
                    and (
                        not filters.flow_kinds
                        or bool({"expense", "refund"} & set(filters.flow_kinds))
                    )
                    for part in by_source_refund.get(transaction.transaction_id, ())
                )
        if matched:
            result.add(transaction.transaction_id)
    return result


def _selected_transactions(dataset: AnalyticsDataset) -> list[MetricTransaction]:
    ids = selected_transaction_ids(dataset)
    return [item for item in dataset.transactions if item.transaction_id in ids]


def _matching_allocations(
    dataset: AnalyticsDataset,
    transaction: MetricTransaction,
    flow_kinds: Iterable[str],
) -> list[MetricAllocation]:
    allowed = set(flow_kinds)
    return [
        allocation
        for allocation in dataset.allocations_by_transaction.get(transaction.transaction_id, ())
        if allocation.flow_kind in allowed
        and _filter_allocation(allocation, dataset.filters, dataset._expanded_categories)
    ]


def _family_expense_included(dataset: AnalyticsDataset, transaction: MetricTransaction) -> bool:
    return dataset.filters.personal_mode == "observed" or transaction.account_role != "personal"


def _refund_category_parts(dataset: AnalyticsDataset, selected_ids: set[str]) -> list[RefundPart]:
    allowed_categories = set(dataset._expanded_categories)
    if dataset.filters.flow_kinds and not {"expense", "refund"}.intersection(
        dataset.filters.flow_kinds
    ):
        return []
    result: list[RefundPart] = []
    for part in dataset.refund_parts:
        if (
            part.source_transaction_id not in selected_ids
            or part.target_account_id not in dataset.filters.account_ids
        ):
            continue
        # The credit reduces the purchase it is attributed to: it follows the role of the
        # purchase account, so an envelope view never nets a refund against an expense it
        # excluded (KPI-08).
        target_role = dataset.account_roles.get(part.target_account_id)
        if dataset.filters.personal_mode != "observed" and target_role == "personal":
            continue
        if allowed_categories and part.category_id not in allowed_categories:
            continue
        result.append(part)
    return result


def _matching_expenses(
    dataset: AnalyticsDataset, selected: Sequence[MetricTransaction]
) -> list[tuple[MetricTransaction, MetricAllocation]]:
    if dataset.filters.flow_kinds and "expense" not in dataset.filters.flow_kinds:
        return []
    result: list[tuple[MetricTransaction, MetricAllocation]] = []
    for transaction in selected:
        if not _family_expense_included(dataset, transaction):
            continue
        for allocation in _matching_allocations(dataset, transaction, ("expense",)):
            result.append((transaction, allocation))
    return result


def _residual(transaction: MetricTransaction, allocations: Sequence[MetricAllocation]) -> int:
    return transaction.amount_cents - sum(item.amount_cents for item in allocations)


def _known_amount(dataset: AnalyticsDataset) -> bool:
    return dataset.coverage.get("status") not in {"unknown", "not_started"}


def calculate_metrics(
    dataset: AnalyticsDataset, *, selected_commitments_cents: int = 0
) -> dict[str, Any]:
    """Calculate all stable metrics and reconcile their transaction/allocation detail."""
    filters = dataset.filters
    selected = _selected_transactions(dataset)
    selected_ids = {item.transaction_id for item in selected}
    allocations = dataset.allocations_by_transaction
    expense_rows = _matching_expenses(dataset, selected)
    refund_rows = _refund_category_parts(dataset, selected_ids)
    category_gross: dict[str, int] = defaultdict(int)
    category_refunds: dict[str, int] = defaultdict(int)
    for _transaction, allocation in expense_rows:
        category_gross[allocation.category_id] += -allocation.amount_cents
    for part in refund_rows:
        category_refunds[part.category_id] += part.amount_cents
    category_net = {
        category_id: category_gross.get(category_id, 0) - category_refunds.get(category_id, 0)
        for category_id in sorted(set(category_gross) | set(category_refunds))
    }
    gross_expense = sum(category_gross.values())
    matched_refunds = sum(category_refunds.values())
    consumption_net = sum(category_net.values())

    bank_net_flow = sum(item.amount_cents for item in selected)
    regular_income = sum(
        allocation.amount_cents
        for transaction in selected
        for allocation in _matching_allocations(dataset, transaction, ("income_recurring",))
    )
    personal_allowances = 0
    if filters.personal_mode == "envelope":
        personal_allowances = -sum(
            allocation.amount_cents
            for transaction in selected
            if transaction.account_role != "personal"
            for allocation in _matching_allocations(dataset, transaction, ("allocation_personal",))
        )
    savings_net = -sum(
        allocation.amount_cents
        for transaction in selected
        if transaction.account_role == "current"
        for allocation in _matching_allocations(dataset, transaction, ("transfer_savings",))
    )
    residuals = {
        transaction.transaction_id: _residual(
            transaction, allocations.get(transaction.transaction_id, ())
        )
        for transaction in selected
    }
    fully_classified = sum(value == 0 for value in residuals.values())
    residual_signed = sum(value for value in residuals.values() if value != 0)
    residual_absolute = sum(abs(value) for value in residuals.values() if value != 0)
    relevant_allocations_cents = sum(
        abs(allocation.amount_cents)
        for transaction in selected
        for allocation in allocations.get(transaction.transaction_id, ())
        if allocation.flow_kind in RELEVANT_UNCLASSIFIED_FLOWS
        and _filter_allocation(allocation, filters, dataset._expanded_categories)
    )
    relevant_residuals = [
        residuals[item.transaction_id] for item in selected if residuals[item.transaction_id] != 0
    ]
    ratio_denominator = relevant_allocations_cents + sum(abs(item) for item in relevant_residuals)
    unclassified_bps = rounded_basis_points(
        sum(abs(item) for item in relevant_residuals), ratio_denominator
    )
    operation_bps = rounded_basis_points(fully_classified, len(selected))

    in_scope_for_capacity = [
        transaction
        for transaction in selected
        if filters.personal_mode == "observed" or transaction.account_role != "personal"
    ]
    blocking_residual = any(
        _residual(item, allocations.get(item.transaction_id, ())) != 0
        for item in in_scope_for_capacity
    )
    period_complete = dataset.coverage.get("status") == "complete"
    commitment_ids = set(filters.commitment_transaction_ids)
    if commitment_ids:
        selected_ids = {item.transaction_id for item in selected}
        if not commitment_ids.issubset(selected_ids):
            raise ValueError("invalid_commitment")
        commitments_by_id = {item.transaction_id: item for item in selected}
        for transaction_id in commitment_ids:
            transaction = commitments_by_id[transaction_id]
            commitment_allocations = allocations.get(transaction_id, ())
            if transaction.amount_cents >= 0 or not any(
                item.flow_kind == "loan_repayment" for item in commitment_allocations
            ):
                raise ValueError("invalid_commitment")
            selected_commitments_cents += -transaction.amount_cents
    capacity: int | None = None
    if period_complete and not blocking_residual and _known_amount(dataset):
        capacity = (
            regular_income - consumption_net - personal_allowances - selected_commitments_cents
        )
    rate = (
        rounded_basis_points(capacity, regular_income)
        if capacity is not None and regular_income > 0 and period_complete
        else None
    )

    loan_principal_outflow = sum(
        -allocation.amount_cents
        for transaction in selected
        for allocation in _matching_allocations(dataset, transaction, ("loan_repayment",))
        if allocation.category_id == "flow_loan_principal"
    )
    loan_unsplit_outflow = sum(
        -allocation.amount_cents
        for transaction in selected
        for allocation in _matching_allocations(dataset, transaction, ("loan_repayment",))
        if allocation.category_id == "flow_loan_payment_unknown_split"
    )
    cash_pending = sum(
        -allocation.amount_cents
        for transaction in selected
        for allocation in _matching_allocations(dataset, transaction, ("cash_withdrawal_pending",))
    )
    intermediary_pending = sum(
        -allocation.amount_cents
        for transaction in selected
        for allocation in _matching_allocations(dataset, transaction, ("intermediary_pending",))
    )

    known = _known_amount(dataset)
    common = {
        "bank_net_flow": {
            "metric_definition_id": "bank_net_flow.v1",
            "raw_cents": bank_net_flow if known else None,
            "unit": "EUR-cent",
        },
        "regular_income": {
            "metric_definition_id": "regular_income.v1",
            "raw_cents": regular_income if known else None,
            "unit": "EUR-cent",
        },
        "consumption_net": {
            "metric_definition_id": "consumption_net.v1",
            "raw_cents": consumption_net if known else None,
            "unit": "EUR-cent",
        },
        "personal_allowances": {
            "metric_definition_id": "personal_allowances.v1",
            "raw_cents": personal_allowances if known else None,
            "unit": "EUR-cent",
        },
        "savings_net_transferred": {
            "metric_definition_id": "savings_net_transferred.v1",
            "raw_cents": savings_net if known else None,
            "unit": "EUR-cent",
        },
        "operational_saving_capacity": {
            "metric_definition_id": "operational_saving_capacity.v1",
            "raw_cents": capacity if known and capacity is not None else None,
            "unit": "EUR-cent",
            "unavailable_reason": None
            if capacity is not None
            else _capacity_unavailable_reason(dataset, blocking_residual),
        },
        "operational_saving_rate": {
            "metric_definition_id": "operational_saving_rate.v1",
            "raw_cents": None,
            "raw_basis_points": rate,
            "unit": "basis-point-of-percent",
            "unavailable_reason": None
            if rate is not None
            else _rate_unavailable_reason(dataset, capacity, regular_income),
        },
        "unclassified_amount_share": {
            "metric_definition_id": "unclassified_amount_share.v1",
            "raw_cents": None,
            "raw_basis_points": unclassified_bps if known else None,
            "unit": "basis-point-of-percent",
            "unclassified_absolute_residual_cents": residual_absolute if known else None,
            "unclassified_signed_residual_cents": residual_signed if known else None,
            "denominator_cents": ratio_denominator if known else None,
        },
        "operation_coverage": {
            "metric_definition_id": "operation_coverage.v1",
            "raw_cents": None,
            "raw_basis_points": operation_bps if known else None,
            "raw_count": {"fully_classified": fully_classified, "selected": len(selected)},
            "unit": "basis-point-of-percent",
        },
        "month_completeness": {
            "metric_definition_id": "month_completeness.v1",
            "raw_cents": None,
            "state_value": dataset.coverage.get("status", "unknown"),
            "unit": "state",
        },
    }
    return {
        "filters": filters.as_dict(),
        "period": {
            **_period_payload(filters),
            "completeness": dataset.coverage.get("status", "unknown"),
        },
        "accounts": list(filters.account_ids),
        "coverage": {
            **dataset.coverage,
            "operation_count": len(selected),
            "fully_classified_count": fully_classified,
        },
        "definitions": {key: value.as_dict() for key, value in METRIC_DEFINITIONS.items()},
        "metrics": common,
        "totals": {
            "bank_net_flow_cents": bank_net_flow if known else None,
            "gross_expense_cents": gross_expense if known else None,
            "linked_refunds_cents": matched_refunds if known else None,
            "consumption_net_cents": consumption_net if known else None,
            "personal_allowances_cents": personal_allowances if known else None,
            "regular_income_cents": regular_income if known else None,
            "savings_net_transferred_cents": savings_net if known else None,
            "loan_principal_outflow_cents": loan_principal_outflow if known else None,
            "loan_unsplit_outflow_cents": loan_unsplit_outflow if known else None,
            "loan_interest_cents": None,
            "cash_withdrawal_pending_cents": cash_pending if known else None,
            "intermediary_pending_cents": intermediary_pending if known else None,
            "selected_commitments_cents": selected_commitments_cents if known else None,
            "unclassified_signed_residual_cents": residual_signed if known else None,
            "unclassified_absolute_residual_cents": residual_absolute if known else None,
        },
        "category_totals": category_net,
        "category_gross_totals": dict(category_gross),
        "category_refund_totals": dict(category_refunds),
        "selected_transactions": selected,
        "residuals": residuals,
        "refund_parts_used": refund_rows,
        "transfer_pairs": list(dataset.transfer_pairs),
        "personal_unspent_cents": _personal_unspent(dataset, selected),
        "selected_commitments": [
            {
                "transaction_id": transaction_id,
                "amount_cents": -next(
                    item.amount_cents for item in selected if item.transaction_id == transaction_id
                ),
                "capital_interest_status": "unknown_if_unsplit"
                if any(
                    allocation.category_id == "flow_loan_payment_unknown_split"
                    for allocation in allocations.get(transaction_id, ())
                )
                else "principal_classified",
            }
            for transaction_id in sorted(commitment_ids)
        ],
    }


def _capacity_unavailable_reason(dataset: AnalyticsDataset, blocking_residual: bool) -> str:
    if dataset.coverage.get("status") != "complete":
        return "period_not_complete"
    if blocking_residual:
        return "relevant_residual"
    return "coverage_unknown"


def _rate_unavailable_reason(dataset: AnalyticsDataset, capacity: int | None, income: int) -> str:
    if capacity is None:
        return "capacity_unavailable"
    if income <= 0:
        return "regular_income_not_positive"
    return "period_not_complete"


def _personal_unspent(
    dataset: AnalyticsDataset, selected: Sequence[MetricTransaction]
) -> int | None:
    if dataset.filters.personal_mode != "observed":
        return None
    envelope = -sum(
        allocation.amount_cents
        for transaction in selected
        if transaction.account_role != "personal"
        for allocation in dataset.allocations_by_transaction.get(transaction.transaction_id, ())
        if allocation.flow_kind == "allocation_personal"
    )
    observed = -sum(
        allocation.amount_cents
        for transaction in selected
        if transaction.account_role == "personal"
        for allocation in dataset.allocations_by_transaction.get(transaction.transaction_id, ())
        if allocation.flow_kind == "expense"
    )
    return envelope - observed


def _period_payload(filters: AnalyticsFilters) -> dict[str, Any]:
    start, end = filters.period_start, filters.period_end
    calendar_month = start.day == 1 and end.day == calendar.monthrange(end.year, end.month)[1]
    if calendar_month and (start.year, start.month) == (end.year, end.month):
        kind = "calendar_month"
    else:
        kind = "custom"
    return {
        "start": start,
        "end": end,
        "kind": kind,
        "completeness": "unknown",
        "as_of": filters.as_of or date.today(),
        "generated_on": filters.as_of or date.today(),
    }


def _query_refund_parts(
    session: Session,
    filters: AnalyticsFilters,
    account_ids: Sequence[str],
    current_allocations: Mapping[str, Sequence[MetricAllocation]],
) -> tuple[RefundPart, ...]:
    source = aliased(Transaction)
    target = aliased(Transaction)
    active = and_(
        TransactionLink.link_kind == "refund_of",
        TransactionLink.status == "confirmed",
        TransactionLink.superseded_by_transaction_link_id.is_(None),
        source.account_id.in_(account_ids),
        source.booked_on >= filters.period_start,
        source.booked_on <= filters.period_end,
    )
    link_rows = session.execute(
        select(
            TransactionLink.transaction_link_id,
            TransactionLink.source_transaction_id,
            TransactionLink.target_transaction_id,
            TransactionLink.source_allocation_id,
            TransactionLink.target_allocation_id,
            TransactionLink.amount_cents,
            target.account_id,
            target.booked_on,
        )
        .join(source, source.transaction_id == TransactionLink.source_transaction_id)
        .join(target, target.transaction_id == TransactionLink.target_transaction_id)
        .where(active)
    ).all()
    parts: list[RefundPart] = []
    active_source_ids = {
        allocation.allocation_id
        for values in current_allocations.values()
        for allocation in values
        if allocation.flow_kind == "refund"
    }
    direct_rows = [row for row in link_rows if row[4] is not None]
    direct_ids = [str(row[4]) for row in direct_rows]
    direct_allocations: dict[str, Any] = {}
    for offset in range(0, len(direct_ids), 500):
        direct_chunk = direct_ids[offset : offset + 500]
        direct_allocations.update(
            {
                str(item[0]): item
                for item in session.execute(
                    select(
                        Allocation.allocation_id,
                        Allocation.category_id,
                        Allocation.flow_kind,
                        Allocation.amount_cents,
                        Allocation.decision,
                        Allocation.superseded_by_allocation_id,
                        Allocation.retracted_at_utc,
                        Allocation.beneficiary,
                    ).where(Allocation.allocation_id.in_(direct_chunk))
                ).all()
            }
        )
    for row in direct_rows:
        link_id = str(row[0])
        source_transaction_id = str(row[1])
        allocation = direct_allocations.get(str(row[4]))
        if (
            row[3] not in active_source_ids
            or allocation is None
            or allocation.flow_kind != "expense"
            or allocation.amount_cents >= 0
            or allocation.decision != "confirmed"
            or allocation.superseded_by_allocation_id is not None
            or allocation.retracted_at_utc is not None
        ):
            continue
        parts.append(
            RefundPart(
                link_id,
                source_transaction_id,
                str(row[2]),
                str(row[6]),
                str(allocation.allocation_id),
                str(allocation.category_id),
                int(row[5]),
                row[7],
                str(allocation.beneficiary),
            )
        )
    prorated_rows = session.execute(
        select(
            TransactionLink.transaction_link_id,
            TransactionLink.source_transaction_id,
            TransactionLink.target_transaction_id,
            TransactionLink.source_allocation_id,
            target.account_id,
            target.booked_on,
            RefundLinkAllocation.allocation_id,
            RefundLinkAllocation.amount_cents,
            Allocation.category_id,
            Allocation.flow_kind,
            Allocation.amount_cents,
            Allocation.decision,
            Allocation.superseded_by_allocation_id,
            Allocation.retracted_at_utc,
            Allocation.beneficiary,
        )
        .join(source, source.transaction_id == TransactionLink.source_transaction_id)
        .join(target, target.transaction_id == TransactionLink.target_transaction_id)
        .join(
            RefundLinkAllocation,
            RefundLinkAllocation.transaction_link_id == TransactionLink.transaction_link_id,
        )
        .join(Allocation, Allocation.allocation_id == RefundLinkAllocation.allocation_id)
        .where(active, TransactionLink.target_allocation_id.is_(None))
    ).all()
    for prorated_row in prorated_rows:
        row_values = cast(Sequence[Any], prorated_row)
        link_id = str(row_values[0])
        source_transaction_id = str(row_values[1])
        if (
            row_values[3] not in active_source_ids
            or row_values[9] != "expense"
            or row_values[10] >= 0
            or row_values[11] != "confirmed"
            or row_values[12] is not None
            or row_values[13] is not None
        ):
            continue
        parts.append(
            RefundPart(
                link_id,
                source_transaction_id,
                str(row_values[2]),
                str(row_values[4]),
                str(row_values[6]),
                str(row_values[8]),
                int(row_values[7]),
                row_values[5],
                str(row_values[14]),
            )
        )
    return tuple(parts)


def load_analytics_dataset(session: Session, filters: AnalyticsFilters) -> AnalyticsDataset:
    """Load a query-only, period-scoped read model using existing indexes."""
    account_ids = tuple(sorted(set(filters.account_ids)))
    if not account_ids:
        raise ValueError("account_required")
    accounts = list(
        session.scalars(select(Account).where(Account.account_id.in_(account_ids))).all()
    )
    account_roles = {row.account_id: row.account_role for row in accounts}
    if len(account_roles) != len(account_ids):
        raise ValueError("unknown_account")
    category_rows = session.execute(
        select(
            Category.category_id,
            Category.parent_category_id,
            Category.label,
            Category.sort_order,
        ).where(Category.is_active == 1)
    ).all()
    categories = {
        str(row[0]): {
            "category_id": str(row[0]),
            "parent_category_id": row[1],
            "label": str(row[2]),
            "sort_order": int(row[3]),
        }
        for row in category_rows
    }
    expanded = _category_scope(filters, categories)
    transaction_rows = session.execute(
        select(
            Transaction.transaction_id,
            Transaction.account_id,
            Transaction.booked_on,
            Transaction.amount_cents,
            Transaction.payee_id,
            Transaction.raw_label,
            Account.account_role,
        )
        .join(Account, Account.account_id == Transaction.account_id)
        .where(
            Transaction.account_id.in_(account_ids),
            Transaction.booked_on >= filters.period_start,
            Transaction.booked_on <= filters.period_end,
        )
        .order_by(Transaction.booked_on, Transaction.transaction_id)
    ).all()
    transactions = tuple(
        MetricTransaction(
            str(row[0]),
            str(row[1]),
            row[2],
            int(row[3]),
            str(row[6]),
            str(row[4]) if row[4] is not None else None,
            str(row[5]),
        )
        for row in transaction_rows
    )
    allocation_rows = session.execute(
        select(
            Allocation.allocation_id,
            Allocation.transaction_id,
            Allocation.category_id,
            Allocation.flow_kind,
            Allocation.amount_cents,
            Allocation.beneficiary,
        )
        .join(Transaction, Transaction.transaction_id == Allocation.transaction_id)
        .where(
            Transaction.account_id.in_(account_ids),
            Transaction.booked_on >= filters.period_start,
            Transaction.booked_on <= filters.period_end,
            Allocation.decision == "confirmed",
            Allocation.superseded_by_allocation_id.is_(None),
            Allocation.retracted_at_utc.is_(None),
        )
        .order_by(Allocation.transaction_id, Allocation.allocation_id)
    ).all()
    allocations_by_transaction: dict[str, list[MetricAllocation]] = defaultdict(list)
    for row in allocation_rows:
        allocation = MetricAllocation(
            str(row[0]), str(row[1]), str(row[2]), str(row[3]), int(row[4]), str(row[5])
        )
        allocations_by_transaction[allocation.transaction_id].append(allocation)
    refund_parts = _query_refund_parts(session, filters, account_ids, allocations_by_transaction)
    transfer_pairs = _load_transfer_pairs(session, filters, account_ids)
    sections, all_quality_transactions = load_quality_snapshot(session, account_ids)
    balance_checks = [
        check.as_dict()
        for account_id in account_ids
        for check in calculate_balance_checks(
            account_id,
            sections,
            all_quality_transactions,
            period_start=filters.period_start,
            period_end=filters.period_end,
        )
    ]
    provisional_dataset = AnalyticsDataset(
        filters=filters,
        account_roles=account_roles,
        transactions=transactions,
        allocations_by_transaction={
            key: tuple(value) for key, value in allocations_by_transaction.items()
        },
        refund_parts=refund_parts,
        categories=categories,
        coverage={},
        transfer_pairs=transfer_pairs,
        _expanded_categories=expanded,
        _quality_sections=tuple(sections),
        _quality_transactions=tuple(all_quality_transactions),
        _balance_checks=tuple(balance_checks),
    )
    selected = _selected_transactions(provisional_dataset)
    selected_quality_transactions = [
        {
            "transaction_id": item.transaction_id,
            "account_id": item.account_id,
            "booked_on": item.booked_on,
            "amount_cents": item.amount_cents,
        }
        for item in selected
    ]
    summary = coverage_summary(
        account_ids,
        (filters.period_start, filters.period_end),
        sections,
        selected_quality_transactions,
        balance_checks,
    )
    balance_status_by_account: dict[str, str] = {}
    for account_id in account_ids:
        statuses = [
            str(check["status"])
            for check in balance_checks
            if check.get("account_id") == account_id
        ]
        if "mismatch" in statuses:
            balance_status_by_account[account_id] = "mismatch"
        elif "match" in statuses:
            balance_status_by_account[account_id] = "match"
        else:
            balance_status_by_account[account_id] = "not_comparable"
    summary["balance_check_by_account"] = balance_status_by_account
    mark_running_month_partial(
        summary, filters.period_start, filters.period_end, filters.as_of or date.today()
    )
    provisional_dataset.coverage = summary
    return provisional_dataset


def mark_running_month_partial(
    coverage: dict[str, Any], start: date, end: date, as_of: date
) -> None:
    """A calendar month that has not ended yet is never complete (INV-10)."""
    if (
        start.year == as_of.year
        and start.month == as_of.month
        and start.day == 1
        and end.day == calendar.monthrange(end.year, end.month)[1]
        and end > as_of
        and coverage.get("status") == "complete"
    ):
        coverage["status"] = "partial"


def _load_transfer_pairs(
    session: Session, filters: AnalyticsFilters, account_ids: Sequence[str]
) -> tuple[dict[str, Any], ...]:
    source = aliased(Transaction)
    target = aliased(Transaction)
    rows = session.execute(
        select(
            TransactionLink.transaction_link_id,
            TransactionLink.source_transaction_id,
            TransactionLink.target_transaction_id,
            TransactionLink.amount_cents,
            source.account_id,
            source.booked_on,
            target.account_id,
            target.booked_on,
        )
        .join(source, source.transaction_id == TransactionLink.source_transaction_id)
        .join(target, target.transaction_id == TransactionLink.target_transaction_id)
        .where(
            TransactionLink.link_kind == "transfer_pair",
            TransactionLink.status == "confirmed",
            TransactionLink.superseded_by_transaction_link_id.is_(None),
            or_(
                and_(
                    source.account_id.in_(account_ids),
                    source.booked_on.between(filters.period_start, filters.period_end),
                ),
                and_(
                    target.account_id.in_(account_ids),
                    target.booked_on.between(filters.period_start, filters.period_end),
                ),
            ),
        )
        .order_by(TransactionLink.transaction_link_id)
    ).all()
    return tuple(
        {
            "transaction_link_id": str(row[0]),
            "source_transaction_id": str(row[1]),
            "target_transaction_id": str(row[2]),
            "amount_cents": int(row[3]),
            "source_account_id": str(row[4]),
            "source_booked_on": row[5],
            "target_account_id": str(row[6]),
            "target_booked_on": row[7],
        }
        for row in rows
    )


def metric_detail_rows(
    dataset: AnalyticsDataset, metric_definition_id: str
) -> list[dict[str, Any]]:
    """Return one deterministic contribution row per transaction for a metric."""
    if metric_definition_id not in METRIC_DEFINITIONS:
        raise ValueError("unknown_metric")
    selected = _selected_transactions(dataset)
    selected_ids = {item.transaction_id for item in selected}
    selected_commitment_ids = set(dataset.filters.commitment_transaction_ids)
    rows: dict[str, dict[str, Any]] = {}

    def ensure(transaction: MetricTransaction) -> dict[str, Any]:
        return rows.setdefault(
            transaction.transaction_id,
            {
                "transaction_id": transaction.transaction_id,
                "booked_on": transaction.booked_on,
                "account_id": transaction.account_id,
                "amount_cents": transaction.amount_cents,
                "active_allocation_ids": sorted(
                    allocation.allocation_id
                    for allocation in dataset.allocations_by_transaction.get(
                        transaction.transaction_id, ()
                    )
                ),
                "metric_contribution_cents": 0,
                "transaction_links": [],
            },
        )

    for transaction in selected:
        item = ensure(transaction)
        allocations = dataset.allocations_by_transaction.get(transaction.transaction_id, ())
        residual = _residual(transaction, allocations)
        if metric_definition_id == "bank_net_flow.v1":
            item["metric_contribution_cents"] = transaction.amount_cents
        elif metric_definition_id == "regular_income.v1":
            item["metric_contribution_cents"] = sum(
                allocation.amount_cents
                for allocation in _matching_allocations(dataset, transaction, ("income_recurring",))
            )
        elif metric_definition_id == "consumption_net.v1":
            if _family_expense_included(dataset, transaction):
                item["metric_contribution_cents"] = sum(
                    -allocation.amount_cents
                    for allocation in _matching_allocations(dataset, transaction, ("expense",))
                )
        elif metric_definition_id == "personal_allowances.v1":
            if (
                transaction.account_role != "personal"
                and dataset.filters.personal_mode == "envelope"
            ):
                item["metric_contribution_cents"] = -sum(
                    allocation.amount_cents
                    for allocation in _matching_allocations(
                        dataset, transaction, ("allocation_personal",)
                    )
                )
        elif metric_definition_id == "savings_net_transferred.v1":
            if transaction.account_role == "current":
                item["metric_contribution_cents"] = -sum(
                    allocation.amount_cents
                    for allocation in _matching_allocations(
                        dataset, transaction, ("transfer_savings",)
                    )
                )
        elif metric_definition_id == "operational_saving_capacity.v1":
            contribution = sum(
                allocation.amount_cents
                for allocation in _matching_allocations(dataset, transaction, ("income_recurring",))
            )
            if _family_expense_included(dataset, transaction):
                contribution -= sum(
                    -allocation.amount_cents
                    for allocation in _matching_allocations(dataset, transaction, ("expense",))
                )
            if (
                dataset.filters.personal_mode == "envelope"
                and transaction.account_role != "personal"
            ):
                contribution -= -sum(
                    allocation.amount_cents
                    for allocation in _matching_allocations(
                        dataset, transaction, ("allocation_personal",)
                    )
                )
            if transaction.transaction_id in selected_commitment_ids:
                contribution -= -transaction.amount_cents
            item["metric_contribution_cents"] = contribution
        elif metric_definition_id == "operational_saving_rate.v1":
            item["numerator_contribution_cents"] = 0
            item["denominator_contribution_cents"] = sum(
                allocation.amount_cents
                for allocation in _matching_allocations(dataset, transaction, ("income_recurring",))
            )
            numerator_contribution = (
                item["denominator_contribution_cents"]
                - (
                    sum(
                        -allocation.amount_cents
                        for allocation in _matching_allocations(dataset, transaction, ("expense",))
                    )
                    if _family_expense_included(dataset, transaction)
                    else 0
                )
                - (
                    -sum(
                        allocation.amount_cents
                        for allocation in _matching_allocations(
                            dataset, transaction, ("allocation_personal",)
                        )
                    )
                    if dataset.filters.personal_mode == "envelope"
                    and transaction.account_role != "personal"
                    else 0
                )
            )
            if transaction.transaction_id in selected_commitment_ids:
                numerator_contribution -= -transaction.amount_cents
            item["numerator_contribution_cents"] = numerator_contribution
            item["metric_contribution_cents"] = None
        elif metric_definition_id == "unclassified_amount_share.v1":
            denominator = sum(
                abs(allocation.amount_cents)
                for allocation in allocations
                if allocation.flow_kind in RELEVANT_UNCLASSIFIED_FLOWS
                and _filter_allocation(allocation, dataset.filters, dataset._expanded_categories)
            ) + abs(residual)
            item["unclassified_numerator_cents"] = abs(residual)
            item["unclassified_denominator_cents"] = denominator
            item["metric_contribution_cents"] = None
        elif metric_definition_id == "operation_coverage.v1":
            item["fully_classified_count"] = int(residual == 0)
            item["selected_operation_count"] = 1
            item["metric_contribution_cents"] = None
        elif metric_definition_id == "month_completeness.v1":
            item["metric_contribution_cents"] = None
        elif metric_definition_id == "monthly_delta.v1":
            item["metric_contribution_cents"] = transaction.amount_cents

    if metric_definition_id == "consumption_net.v1":
        transactions_by_id = {item.transaction_id: item for item in dataset.transactions}
        for part in _refund_category_parts(dataset, selected_ids):
            source = transactions_by_id[part.source_transaction_id]
            ensure(source)["metric_contribution_cents"] -= part.amount_cents
            link_item = {
                "transaction_link_id": part.transaction_link_id,
                "link_kind": "refund_of",
                "target_transaction_id": part.target_transaction_id,
                "target_allocation_id": part.target_allocation_id,
                "amount_cents": part.amount_cents,
                "attribution_date": source.booked_on,
            }
            ensure(source)["transaction_links"].append(link_item)
    elif metric_definition_id in {
        "operational_saving_capacity.v1",
        "operational_saving_rate.v1",
    }:
        transactions_by_id = {item.transaction_id: item for item in dataset.transactions}
        for part in _refund_category_parts(dataset, selected_ids):
            source = transactions_by_id[part.source_transaction_id]
            item = ensure(source)
            if metric_definition_id == "operational_saving_capacity.v1":
                item["metric_contribution_cents"] += part.amount_cents
            else:
                item["numerator_contribution_cents"] += part.amount_cents
            item["transaction_links"].append(
                {
                    "transaction_link_id": part.transaction_link_id,
                    "link_kind": "refund_of",
                    "target_transaction_id": part.target_transaction_id,
                    "target_allocation_id": part.target_allocation_id,
                    "amount_cents": part.amount_cents,
                    "attribution_date": source.booked_on,
                }
            )
    for row in rows.values():
        row["transaction_links"].sort(
            key=lambda link: (link["transaction_link_id"], link["target_allocation_id"])
        )
    return sorted(
        rows.values(), key=lambda item: (item["booked_on"], item["transaction_id"]), reverse=True
    )


def metric_contribution_sum(
    metric_definition_id: str, rows: Sequence[Mapping[str, Any]]
) -> int | None:
    if metric_definition_id == "operation_coverage.v1":
        return rounded_basis_points(
            sum(int(row.get("fully_classified_count", 0)) for row in rows), len(rows)
        )
    if metric_definition_id == "month_completeness.v1":
        return None
    if metric_definition_id == "operational_saving_rate.v1":
        numerator = sum(int(row.get("numerator_contribution_cents", 0)) for row in rows)
        denominator = sum(int(row.get("denominator_contribution_cents", 0)) for row in rows)
        return rounded_basis_points(numerator, denominator)
    if metric_definition_id == "unclassified_amount_share.v1":
        numerator = sum(int(row.get("unclassified_numerator_cents", 0)) for row in rows)
        denominator = sum(int(row.get("unclassified_denominator_cents", 0)) for row in rows)
        return rounded_basis_points(numerator, denominator)
    return sum(int(row.get("metric_contribution_cents", 0) or 0) for row in rows)


def category_rows(dataset: AnalyticsDataset, calculated: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flat category rows with parent edges; summing each row equals net consumption."""
    return [
        {
            **dataset.categories.get(
                category_id, {"category_id": category_id, "label": category_id}
            ),
            "gross_expense_cents": calculated["category_gross_totals"].get(category_id, 0),
            "linked_refunds_cents": calculated["category_refund_totals"].get(category_id, 0),
            "raw_cents": amount,
            "metric_definition_id": "consumption_net.v1",
        }
        for category_id, amount in calculated["category_totals"].items()
    ]


def flow_totals(dataset: AnalyticsDataset) -> dict[str, int]:
    """Reconcile every active allocation and transaction residual into bank flow."""
    selected = _selected_transactions(dataset)
    totals: dict[str, int] = defaultdict(int)
    for transaction in selected:
        allocations = dataset.allocations_by_transaction.get(transaction.transaction_id, ())
        for allocation in allocations:
            totals[allocation.flow_kind] += allocation.amount_cents
        residual = _residual(transaction, allocations)
        if residual:
            totals["unknown"] += residual
    return dict(sorted(totals.items()))


INCOME_COMPONENT_FLOWS = frozenset(
    {
        "income_recurring",
        "income_other",
        "loan_disbursement",
        "transfer_owned",
        "transfer_savings",
        "refund",
    }
)


def income_breakdown(
    dataset: AnalyticsDataset, selected: Sequence[MetricTransaction]
) -> dict[str, Any]:
    """Credits by component: salary stays apart from loans, aid, transfers and refunds."""
    by_flow: dict[str, int] = defaultdict(int)
    by_category: dict[tuple[str, str], int] = defaultdict(int)
    unknown_credit_cents = 0
    for transaction in selected:
        allocations = dataset.allocations_by_transaction.get(transaction.transaction_id, ())
        for allocation in _matching_allocations(
            dataset, transaction, tuple(INCOME_COMPONENT_FLOWS)
        ):
            if allocation.amount_cents <= 0:
                continue
            by_flow[allocation.flow_kind] += allocation.amount_cents
            by_category[(allocation.flow_kind, allocation.category_id)] += allocation.amount_cents
        residual = _residual(transaction, allocations)
        if residual > 0:
            unknown_credit_cents += residual
    return {
        "by_flow": dict(by_flow),
        "items": [
            {"flow_kind": flow_kind, "category_id": category_id, "amount_cents": amount}
            for (flow_kind, category_id), amount in sorted(by_category.items())
        ],
        "unknown_credit_residual_cents": unknown_credit_cents,
        "bank_credits_cents": sum(item.amount_cents for item in selected if item.amount_cents > 0),
    }


def family_breakdown(
    dataset: AnalyticsDataset,
    selected: Sequence[MetricTransaction],
    refund_parts: Sequence[RefundPart],
) -> list[dict[str, Any]]:
    """Consumption and personal envelopes per beneficiary; sums equal the global metrics."""
    grouped: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "gross_expense_cents": 0,
            "linked_refunds_cents": 0,
            "personal_allowances_cents": 0,
        }
    )
    envelope = dataset.filters.personal_mode == "envelope"
    for transaction in selected:
        if _family_expense_included(dataset, transaction):
            for allocation in _matching_allocations(dataset, transaction, ("expense",)):
                grouped[allocation.beneficiary]["gross_expense_cents"] += -allocation.amount_cents
        if envelope and transaction.account_role != "personal":
            for allocation in _matching_allocations(dataset, transaction, ("allocation_personal",)):
                grouped[allocation.beneficiary][
                    "personal_allowances_cents"
                ] += -allocation.amount_cents
    for part in refund_parts:
        grouped[part.beneficiary]["linked_refunds_cents"] += part.amount_cents
    return [
        {
            "beneficiary": beneficiary,
            "gross_expense_cents": values["gross_expense_cents"],
            "linked_refunds_cents": values["linked_refunds_cents"],
            "consumption_net_cents": values["gross_expense_cents"] - values["linked_refunds_cents"],
            "personal_allowances_cents": values["personal_allowances_cents"],
        }
        for beneficiary, values in sorted(grouped.items())
    ]


def savings_breakdown(
    dataset: AnalyticsDataset, selected: Sequence[MetricTransaction]
) -> dict[str, int]:
    """Net transferred savings by category, from `current` accounts only (ADR-020)."""
    by_category: dict[str, int] = defaultdict(int)
    for transaction in selected:
        if transaction.account_role != "current":
            continue
        for allocation in _matching_allocations(dataset, transaction, ("transfer_savings",)):
            by_category[allocation.category_id] += -allocation.amount_cents
    return dict(sorted(by_category.items()))


def category_tree(dataset: AnalyticsDataset, calculated: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Category → sub-category rollup; the roots sum exactly to net consumption."""
    gross: Mapping[str, int] = calculated["category_gross_totals"]
    refunds: Mapping[str, int] = calculated["category_refund_totals"]
    own_ids = set(gross) | set(refunds)
    parent_of: dict[str, str | None] = {}
    for category_id in own_ids:
        parent = dataset.categories.get(category_id, {}).get("parent_category_id")
        parent_of[category_id] = parent if isinstance(parent, str) else None
    nodes: dict[str, dict[str, Any]] = {}

    def node(category_id: str) -> dict[str, Any]:
        if category_id not in nodes:
            meta = dataset.categories.get(category_id, {})
            nodes[category_id] = {
                "category_id": category_id,
                "label": meta.get("label", category_id),
                "parent_category_id": meta.get("parent_category_id"),
                "sort_order": meta.get("sort_order", 0),
                "own_gross_expense_cents": 0,
                "own_linked_refunds_cents": 0,
                "gross_expense_cents": 0,
                "linked_refunds_cents": 0,
                "raw_cents": 0,
                "metric_definition_id": "consumption_net.v1",
                "children": [],
            }
        return nodes[category_id]

    for category_id in sorted(own_ids):
        item = node(category_id)
        item["own_gross_expense_cents"] = gross.get(category_id, 0)
        item["own_linked_refunds_cents"] = refunds.get(category_id, 0)
        cursor: str | None = category_id
        seen: set[str] = set()
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            ancestor = node(cursor)
            ancestor["gross_expense_cents"] += gross.get(category_id, 0)
            ancestor["linked_refunds_cents"] += refunds.get(category_id, 0)
            ancestor["raw_cents"] += gross.get(category_id, 0) - refunds.get(category_id, 0)
            parent = dataset.categories.get(cursor, {}).get("parent_category_id")
            cursor = parent if isinstance(parent, str) else None
    roots: list[dict[str, Any]] = []
    for category_id, item in nodes.items():
        parent = dataset.categories.get(category_id, {}).get("parent_category_id")
        if isinstance(parent, str) and parent in nodes:
            nodes[parent]["children"].append(item)
        else:
            roots.append(item)

    def order(items: list[dict[str, Any]]) -> None:
        items.sort(key=lambda entry: (entry["sort_order"], entry["category_id"]))
        for entry in items:
            order(entry["children"])

    order(roots)
    return roots


def period_bucket(start: date, bucket: str) -> tuple[date, date]:
    if bucket == "day":
        return start, start
    if bucket == "week":
        bucket_start = start - timedelta(days=start.weekday())
        return bucket_start, bucket_start + timedelta(days=6)
    month_start = start.replace(day=1)
    return month_start, month_start.replace(
        day=calendar.monthrange(month_start.year, month_start.month)[1]
    )
