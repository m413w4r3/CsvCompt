"""Read-only API projections over the canonical metrics engine."""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.analytics.definitions import (
    METRIC_DEFINITIONS,
    AnalyticsDataset,
    AnalyticsFilters,
    MetricTransaction,
    RefundPart,
    calculate_metrics,
    category_rows,
    category_tree,
    family_breakdown,
    flow_totals,
    income_breakdown,
    load_analytics_dataset,
    mark_running_month_partial,
    metric_contribution_sum,
    metric_detail_rows,
    period_bucket,
    rounded_basis_points,
    savings_breakdown,
    selected_transaction_ids,
)
from app.analytics.quality import coverage_summary

OVERVIEW_METRICS = (
    "bank_net_flow.v1",
    "regular_income.v1",
    "consumption_net.v1",
    "personal_allowances.v1",
    "savings_net_transferred.v1",
    "operational_saving_capacity.v1",
    "operational_saving_rate.v1",
    "unclassified_amount_share.v1",
    "operation_coverage.v1",
    "month_completeness.v1",
)
SERIESABLE_METRICS = frozenset(OVERVIEW_METRICS)
DELTA_METRICS = (
    "bank_net_flow.v1",
    "regular_income.v1",
    "consumption_net.v1",
    "personal_allowances.v1",
    "savings_net_transferred.v1",
    "operational_saving_capacity.v1",
    "operational_saving_rate.v1",
)
# Selected commitments are transactions of the requested month: they cannot be replayed on
# the previous month, so a delta of a commitment-dependent metric is unavailable, not false.
COMMITMENT_DEPENDENT_METRICS = frozenset(
    {"operational_saving_capacity.v1", "operational_saving_rate.v1"}
)
BASIS_POINT_METRICS = frozenset(
    {
        "operational_saving_rate.v1",
        "unclassified_amount_share.v1",
        "operation_coverage.v1",
    }
)


def _metric_payload(calculated: Mapping[str, Any], metric_id: str) -> dict[str, Any]:
    for value in calculated["metrics"].values():
        if value["metric_definition_id"] == metric_id:
            return dict(value)
    raise ValueError("unknown_metric")


def _metric_raw(calculated: Mapping[str, Any], metric_id: str) -> tuple[str, int | str | None]:
    item = _metric_payload(calculated, metric_id)
    if item.get("unit") == "state":
        value = item.get("state_value")
        return "state_value", value if isinstance(value, str) else None
    if metric_id in BASIS_POINT_METRICS:
        value = item.get("raw_basis_points")
        return "raw_basis_points", value if type(value) is int else None
    value = item.get("raw_cents")
    return "raw_cents", value if type(value) is int else None


def _base_response(calculated: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "period": calculated["period"],
        "accounts": calculated["accounts"],
        "coverage": calculated["coverage"],
        "filters": calculated["filters"],
    }


def _metric_with_definition(calculated: Mapping[str, Any], metric_id: str) -> dict[str, Any]:
    item = _metric_payload(calculated, metric_id)
    definition = METRIC_DEFINITIONS[metric_id]
    return {
        **item,
        "definition": definition.as_dict(),
        "drilldown": {
            "path": "/api/v1/analytics/drilldown",
            "metric_definition_id": metric_id,
        },
    }


def _previous_month(start: date, end: date) -> tuple[date, date] | None:
    if start.day != 1 or end.day != calendar.monthrange(end.year, end.month)[1]:
        return None
    if start.year != end.year or start.month != end.month or (start.year, start.month) <= (2026, 1):
        return None
    previous_end = start - timedelta(days=1)
    return previous_end.replace(day=1), previous_end


def monthly_deltas(
    session: Session,
    dataset: AnalyticsDataset,
    calculated: Mapping[str, Any],
    metric_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """`monthly_delta.v1` per base metric; unavailable rather than wrong (ADR-003, INV-10)."""
    for metric_id in metric_ids:
        if metric_id not in METRIC_DEFINITIONS or metric_id in {
            "monthly_delta.v1",
            "month_completeness.v1",
        }:
            raise ValueError("invalid_delta_metric")
    filters = dataset.filters
    previous_period = _previous_month(filters.period_start, filters.period_end)
    shared_reason: str | None = None
    previous_calculated: Mapping[str, Any] | None = None
    if previous_period is None:
        shared_reason = "previous_period_not_available"
    elif dataset.coverage.get("status") != "complete":
        shared_reason = "period_not_complete"
    else:
        previous_dataset = load_analytics_dataset(
            session,
            replace(
                filters,
                period_start=previous_period[0],
                period_end=previous_period[1],
                commitment_transaction_ids=(),
            ),
        )
        if previous_dataset.coverage.get("status") != "complete":
            shared_reason = "previous_period_not_complete"
        else:
            previous_calculated = calculate_metrics(previous_dataset)
    deltas: list[dict[str, Any]] = []
    for metric_id in metric_ids:
        field, current_value = _metric_raw(calculated, metric_id)
        reason = shared_reason
        previous_value: int | None = None
        if (
            reason is None
            and filters.commitment_transaction_ids
            and metric_id in COMMITMENT_DEPENDENT_METRICS
        ):
            reason = "commitments_not_comparable"
        if reason is None and previous_calculated is not None:
            _previous_field, raw_previous = _metric_raw(previous_calculated, metric_id)
            if type(current_value) is not int or type(raw_previous) is not int:
                reason = "metric_unavailable"
            else:
                previous_value = raw_previous
        delta = (
            current_value - previous_value
            if reason is None and type(current_value) is int and previous_value is not None
            else None
        )
        deltas.append(
            {
                "metric_definition_id": "monthly_delta.v1",
                "base_metric_definition_id": metric_id,
                "raw_cents": delta if field == "raw_cents" else None,
                "raw_basis_points": delta if field == "raw_basis_points" else None,
                "unit": METRIC_DEFINITIONS[metric_id].unit,
                "status": "available" if delta is not None else "unavailable",
                "unavailable_reason": reason,
                "previous_period": {"start": previous_period[0], "end": previous_period[1]}
                if previous_period
                else None,
                "previous_raw_cents": previous_value if field == "raw_cents" else None,
                "previous_raw_basis_points": previous_value
                if field == "raw_basis_points"
                else None,
            }
        )
    return deltas


def overview(session: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    dataset = load_analytics_dataset(session, filters)
    calculated = calculate_metrics(dataset)
    return {
        **_base_response(calculated),
        "metric_definition_id": "bank_net_flow.v1",
        "raw_cents": calculated["metrics"]["bank_net_flow"]["raw_cents"],
        "unit": "EUR-cent",
        "definition": calculated["definitions"]["bank_net_flow.v1"],
        "metrics": [_metric_with_definition(calculated, item) for item in OVERVIEW_METRICS],
        "monthly_deltas": monthly_deltas(session, dataset, calculated, DELTA_METRICS),
        "breakdown": calculated["totals"],
    }


def cashflow(session: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    dataset = load_analytics_dataset(session, filters)
    calculated = calculate_metrics(dataset)
    selected = calculated["selected_transactions"]
    bank_net = calculated["metrics"]["bank_net_flow"]["raw_cents"]
    known = bank_net is not None
    credits = sum(row.amount_cents for row in selected if row.amount_cents > 0)
    debits = -sum(row.amount_cents for row in selected if row.amount_cents < 0)
    return {
        **_base_response(calculated),
        "metric_definition_id": "bank_net_flow.v1",
        "raw_cents": bank_net,
        "unit": "EUR-cent",
        "definition": METRIC_DEFINITIONS["bank_net_flow.v1"].as_dict(),
        "credits_cents": credits if known else None,
        "debits_cents": debits if known else None,
        "by_flow_kind": [
            {"flow_kind": flow_kind, "raw_cents": amount if known else None}
            for flow_kind, amount in flow_totals(dataset).items()
        ],
        "loan_breakdown": {
            "principal_outflow_cents": calculated["totals"]["loan_principal_outflow_cents"],
            "unsplit_outflow_cents": calculated["totals"]["loan_unsplit_outflow_cents"],
            "interest_cents": None,
            "interest_status": "not_classified_in_current_taxonomy",
        },
        "pending": {
            "cash_withdrawal_cents": calculated["totals"]["cash_withdrawal_pending_cents"],
            "intermediary_cents": calculated["totals"]["intermediary_pending_cents"],
            "unknown_residual_cents": calculated["totals"]["unclassified_signed_residual_cents"],
        },
    }


def spending_hierarchy(session: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    dataset = load_analytics_dataset(session, filters)
    calculated = calculate_metrics(dataset)
    items = category_rows(dataset, calculated)
    return {
        **_base_response(calculated),
        "metric_definition_id": "consumption_net.v1",
        "raw_cents": calculated["metrics"]["consumption_net"]["raw_cents"],
        "unit": "EUR-cent",
        "definition": METRIC_DEFINITIONS["consumption_net.v1"].as_dict(),
        "gross_expense_cents": calculated["totals"]["gross_expense_cents"],
        "linked_refunds_cents": calculated["totals"]["linked_refunds_cents"],
        "unclassified_absolute_residual_cents": calculated["totals"][
            "unclassified_absolute_residual_cents"
        ],
        "items": items,
        "item_sum_cents": sum(row["raw_cents"] for row in items),
        "tree": category_tree(dataset, calculated),
    }


def income(session: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    """Expose salary separately from loans, other income, transfers and refunds."""
    dataset = load_analytics_dataset(session, filters)
    calculated = calculate_metrics(dataset)
    breakdown = income_breakdown(dataset, calculated["selected_transactions"])
    by_flow: Mapping[str, int] = breakdown["by_flow"]
    field_by_flow = {
        "income_other": "other_income_cents",
        "loan_disbursement": "loan_disbursement_cents",
        "transfer_owned": "owned_transfers_cents",
        "transfer_savings": "savings_returns_cents",
        "refund": "refunds_cents",
    }
    return {
        **_base_response(calculated),
        "metric_definition_id": "regular_income.v1",
        "raw_cents": calculated["metrics"]["regular_income"]["raw_cents"],
        "unit": "EUR-cent",
        "definition": METRIC_DEFINITIONS["regular_income.v1"].as_dict(),
        "bank_credits_cents": breakdown["bank_credits_cents"],
        "regular_income_cents": calculated["totals"]["regular_income_cents"],
        **{name: by_flow.get(flow, 0) for flow, name in field_by_flow.items()},
        "unknown_credit_residual_cents": breakdown["unknown_credit_residual_cents"],
        "items": breakdown["items"],
    }


class _SeriesIndex:
    """Per-day and per-refund-source indexes so each bucket costs only its own rows."""

    def __init__(self, dataset: AnalyticsDataset) -> None:
        self.dataset = dataset
        self.by_date: dict[date, list[MetricTransaction]] = defaultdict(list)
        for row in dataset.transactions:
            self.by_date[row.booked_on].append(row)
        self.parts_by_source: dict[str, list[RefundPart]] = defaultdict(list)
        for part in dataset.refund_parts:
            self.parts_by_source[part.source_transaction_id].append(part)

    def bucket(self, start: date, end: date) -> AnalyticsDataset:
        dataset = self.dataset
        transactions: list[MetricTransaction] = []
        day = start
        while day <= end:
            transactions.extend(self.by_date.get(day, ()))
            day += timedelta(days=1)
        ids = {row.transaction_id for row in transactions}
        bucket = replace(
            dataset,
            filters=replace(dataset.filters, period_start=start, period_end=end),
            transactions=tuple(transactions),
            allocations_by_transaction={
                transaction_id: dataset.allocations_by_transaction[transaction_id]
                for transaction_id in ids
                if transaction_id in dataset.allocations_by_transaction
            },
            refund_parts=tuple(
                part
                for transaction_id in ids
                for part in self.parts_by_source.get(transaction_id, ())
            ),
        )
        selected_ids = selected_transaction_ids(bucket)
        bucket.coverage = coverage_summary(
            list(dataset.filters.account_ids),
            (start, end),
            dataset._quality_sections,
            [
                {
                    "transaction_id": item.transaction_id,
                    "account_id": item.account_id,
                    "booked_on": item.booked_on,
                    "amount_cents": item.amount_cents,
                }
                for item in transactions
                if item.transaction_id in selected_ids
            ],
            dataset._balance_checks,
        )
        bucket.coverage["balance_check_by_account"] = dataset.coverage.get(
            "balance_check_by_account", {}
        )
        mark_running_month_partial(
            bucket.coverage, start, end, dataset.filters.as_of or date.today()
        )
        return bucket


def series(
    session: Session, filters: AnalyticsFilters, metric_definition_id: str, bucket: str
) -> dict[str, Any]:
    if metric_definition_id not in SERIESABLE_METRICS:
        raise ValueError("metric_not_seriesable")
    if bucket not in {"day", "week", "month"}:
        raise ValueError("invalid_bucket")
    dataset = load_analytics_dataset(session, filters)
    calculated = calculate_metrics(dataset)
    index = _SeriesIndex(dataset)
    points: list[dict[str, Any]] = []
    cursor = filters.period_start
    while cursor <= filters.period_end:
        natural_start, natural_end = period_bucket(cursor, bucket)
        start = max(natural_start, filters.period_start)
        end = min(natural_end, filters.period_end)
        bucket_dataset = index.bucket(start, end)
        bucket_metrics = calculate_metrics(bucket_dataset)
        value_key, raw_value = _metric_raw(bucket_metrics, metric_definition_id)
        item = _metric_payload(bucket_metrics, metric_definition_id)
        point: dict[str, Any] = {
            "period_start": start,
            "period_end": end,
            "metric_definition_id": metric_definition_id,
            "completeness": bucket_dataset.coverage.get("status", "unknown"),
            "operation_count": bucket_metrics["coverage"]["operation_count"],
            "provisional_operation_count": bucket_dataset.coverage.get(
                "provisional_operation_count", 0
            ),
            value_key: raw_value,
            "unavailable_reason": item.get("unavailable_reason"),
        }
        if metric_definition_id == "operation_coverage.v1":
            point["raw_count"] = bucket_metrics["coverage"]["fully_classified_count"]
        points.append(point)
        cursor = end + timedelta(days=1)
    return {
        **_base_response(calculated),
        "metric_definition_id": metric_definition_id,
        "raw_cents": None,
        "unit": METRIC_DEFINITIONS[metric_definition_id].unit,
        "definition": METRIC_DEFINITIONS[metric_definition_id].as_dict(),
        "bucket": bucket,
        "series": points,
    }


def merchants(session: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    dataset = load_analytics_dataset(session, filters)
    calculated = calculate_metrics(dataset)
    detail = metric_detail_rows(dataset, "consumption_net.v1")
    payee_by_transaction = {row.transaction_id: row.payee_id for row in dataset.transactions}
    grouped: dict[str | None, dict[str, Any]] = {}
    for row in detail:
        amount = row.get("metric_contribution_cents")
        if type(amount) is not int or amount == 0:
            continue
        payee_id = payee_by_transaction.get(row["transaction_id"])
        item = grouped.setdefault(
            payee_id,
            {"payee_id": payee_id, "operation_count": 0, "amount_cents": 0},
        )
        item["operation_count"] += 1
        item["amount_cents"] += amount
    items = sorted(
        grouped.values(),
        key=lambda item: (-item["amount_cents"], item["payee_id"] or ""),
    )
    return {
        **_base_response(calculated),
        "metric_definition_id": "consumption_net.v1",
        "raw_cents": calculated["metrics"]["consumption_net"]["raw_cents"],
        "unit": "EUR-cent",
        "definition": METRIC_DEFINITIONS["consumption_net.v1"].as_dict(),
        "items": items,
        "item_sum_cents": sum(item["amount_cents"] for item in items),
    }


def family(session: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    dataset = load_analytics_dataset(session, filters)
    calculated = calculate_metrics(dataset)
    items = family_breakdown(
        dataset, calculated["selected_transactions"], calculated["refund_parts_used"]
    )
    return {
        **_base_response(calculated),
        "metric_definition_id": "consumption_net.v1",
        "raw_cents": calculated["metrics"]["consumption_net"]["raw_cents"],
        "unit": "EUR-cent",
        "definition": METRIC_DEFINITIONS["consumption_net.v1"].as_dict(),
        "metrics": [
            _metric_with_definition(calculated, "consumption_net.v1"),
            _metric_with_definition(calculated, "personal_allowances.v1"),
        ],
        "gross_expense_cents": calculated["totals"]["gross_expense_cents"],
        "linked_refunds_cents": calculated["totals"]["linked_refunds_cents"],
        "personal_unspent_cents": calculated["personal_unspent_cents"],
        "items": items,
        "item_sum_cents": sum(item["consumption_net_cents"] for item in items),
        "personal_allowances_sum_cents": sum(item["personal_allowances_cents"] for item in items),
    }


def savings(session: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    dataset = load_analytics_dataset(session, filters)
    calculated = calculate_metrics(dataset)
    by_category = savings_breakdown(dataset, calculated["selected_transactions"])
    return {
        **_base_response(calculated),
        "metric_definition_id": "savings_net_transferred.v1",
        "raw_cents": calculated["metrics"]["savings_net_transferred"]["raw_cents"],
        "unit": "EUR-cent",
        "definition": METRIC_DEFINITIONS["savings_net_transferred.v1"].as_dict(),
        "by_category": [
            {"category_id": category_id, "raw_cents": amount}
            for category_id, amount in by_category.items()
        ],
        "by_category_sum_cents": sum(by_category.values()),
        "transfer_pairs": dataset.transfer_pairs,
        "selected_commitments_cents": calculated["totals"]["selected_commitments_cents"],
        "selected_commitments": calculated["selected_commitments"],
    }


def data_quality(session: Session, filters: AnalyticsFilters) -> dict[str, Any]:
    dataset = load_analytics_dataset(session, filters)
    calculated = calculate_metrics(dataset)
    unclassified = _metric_with_definition(calculated, "unclassified_amount_share.v1")
    classification = _metric_with_definition(calculated, "operation_coverage.v1")
    return {
        **_base_response(calculated),
        "metric_definition_id": "unclassified_amount_share.v1",
        "raw_cents": None,
        "raw_basis_points": unclassified["raw_basis_points"],
        "unit": "basis-point-of-percent",
        "definition": unclassified["definition"],
        "unclassified_absolute_residual_cents": unclassified[
            "unclassified_absolute_residual_cents"
        ],
        "unclassified_signed_residual_cents": unclassified["unclassified_signed_residual_cents"],
        "operation_coverage": classification,
        "provisional_operation_count": dataset.coverage.get("provisional_operation_count", 0),
        "missing_days": dataset.coverage.get("missing_days", []),
    }


def drilldown(
    dataset: AnalyticsDataset,
    metric_definition_id: str,
    *,
    last_key: tuple[date, str] | None = None,
    limit: int = 50,
    include_labels: bool = False,
) -> dict[str, Any]:
    if metric_definition_id == "monthly_delta.v1":
        raise ValueError("delta_base_metric_required")
    calculated = calculate_metrics(dataset)
    all_rows = metric_detail_rows(dataset, metric_definition_id)
    if metric_definition_id in {"operation_coverage.v1", "month_completeness.v1"}:
        listed = all_rows
    else:
        listed = [row for row in all_rows if _detail_row_contributes(metric_definition_id, row)]
    if last_key is not None:
        listed = [row for row in listed if (row["booked_on"], row["transaction_id"]) < last_key]
    page_rows = listed[: limit + 1]
    has_more = len(page_rows) > limit
    page_rows = page_rows[:limit]
    raw_key, recalculated = _drilldown_recalculated(metric_definition_id, all_rows, calculated)
    if include_labels:
        labels = {row.transaction_id: row.raw_label for row in dataset.transactions}
        for item in page_rows:
            item["raw_label"] = labels.get(item["transaction_id"], "")
    next_key = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_key = (last["booked_on"], last["transaction_id"])
    metric = _metric_with_definition(calculated, metric_definition_id)
    aggregate = metric.get(raw_key)
    return {
        **_base_response(calculated),
        "metric_definition_id": metric_definition_id,
        "raw_cents": metric.get("raw_cents"),
        "raw_basis_points": metric.get("raw_basis_points"),
        "raw_count": metric.get("raw_count"),
        "state_value": metric.get("state_value"),
        "unit": metric["unit"],
        "definition": metric["definition"],
        "unavailable_reason": metric.get("unavailable_reason"),
        "detail_summary": {
            "recalculated_raw_cents": recalculated if raw_key == "raw_cents" else None,
            "recalculated_raw_basis_points": recalculated
            if raw_key == "raw_basis_points"
            else None,
            "recalculated_raw_count": recalculated if raw_key == "raw_count" else None,
            # `None` means the aggregate itself is unavailable (INV-10): nothing to reconcile.
            "reconciles": None if aggregate is None else aggregate == recalculated,
            "operation_count": len(all_rows),
        },
        "items": page_rows,
        "page": {
            "limit": limit,
            "has_more": has_more,
            "next_key": next_key,
        },
    }


def _drilldown_recalculated(
    metric_id: str, rows: Sequence[Mapping[str, Any]], calculated: Mapping[str, Any]
) -> tuple[str, int | str | None]:
    if metric_id == "month_completeness.v1":
        return "state_value", calculated["coverage"].get("status")
    if metric_id == "operation_coverage.v1":
        counts = calculated["metrics"]["operation_coverage"]["raw_count"]
        value = rounded_basis_points(counts["fully_classified"], counts["selected"])
        return "raw_basis_points", value
    if metric_id in BASIS_POINT_METRICS:
        return "raw_basis_points", metric_contribution_sum(metric_id, rows)
    return "raw_cents", metric_contribution_sum(metric_id, rows)


def _detail_row_contributes(metric_id: str, row: Mapping[str, Any]) -> bool:
    if metric_id == "operational_saving_rate.v1":
        return bool(
            row.get("numerator_contribution_cents") or row.get("denominator_contribution_cents")
        )
    if metric_id == "unclassified_amount_share.v1":
        return bool(row.get("unclassified_denominator_cents"))
    value = row.get("metric_contribution_cents")
    return type(value) is int and value != 0
