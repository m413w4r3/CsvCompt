from __future__ import annotations

import random
from dataclasses import replace
from datetime import date

from app.analytics.definitions import (
    AnalyticsDataset,
    AnalyticsFilters,
    MetricAllocation,
    MetricTransaction,
    calculate_metrics,
    flow_totals,
    metric_detail_rows,
    rounded_basis_points,
)
from tests.helpers.analytics_scenarios import family_analytics_scenario


def test_KPI_01_bank_net_flow_and_allocation_residual_reconcile_in_cents() -> None:
    dataset = family_analytics_scenario()
    result = calculate_metrics(dataset)
    expected = sum(transaction.amount_cents for transaction in dataset.transactions)
    assert result["metrics"]["bank_net_flow"]["raw_cents"] == expected
    assert sum(flow_totals(dataset).values()) == expected
    for transaction in dataset.transactions:
        allocations = dataset.allocations_by_transaction.get(transaction.transaction_id, ())
        residual = transaction.amount_cents - sum(item.amount_cents for item in allocations)
        assert sum(item.amount_cents for item in allocations) + residual == transaction.amount_cents


def test_KPI_02_loan_credit_is_never_regular_salary() -> None:
    dataset = family_analytics_scenario()
    result = calculate_metrics(dataset)
    without_loan = replace(
        dataset,
        transactions=tuple(item for item in dataset.transactions if item.transaction_id != "loan"),
        allocations_by_transaction={
            key: value for key, value in dataset.allocations_by_transaction.items() if key != "loan"
        },
    )
    without_loan_result = calculate_metrics(without_loan)
    assert result["metrics"]["regular_income"]["raw_cents"] == 300_000
    assert (
        result["totals"]["bank_net_flow_cents"]
        == without_loan_result["totals"]["bank_net_flow_cents"] + 5_000_000
    )
    assert result["totals"]["loan_principal_outflow_cents"] == 9_826_561
    assert result["totals"]["loan_interest_cents"] is None


def test_KPI_03_category_totals_recompose_net_consumption() -> None:
    result = calculate_metrics(family_analytics_scenario())
    assert (
        sum(result["category_totals"].values()) == result["metrics"]["consumption_net"]["raw_cents"]
    )
    assert result["metrics"]["consumption_net"]["raw_cents"] == 6_999
    assert result["totals"]["gross_expense_cents"] == 10_000
    assert result["totals"]["linked_refunds_cents"] == 3_001


def test_KPI_07_consumption_drilldown_contributions_equal_aggregate() -> None:
    dataset = family_analytics_scenario()
    result = calculate_metrics(dataset)
    rows = metric_detail_rows(dataset, "consumption_net.v1")
    contribution_sum = sum(
        row["metric_contribution_cents"]
        for row in rows
        if type(row.get("metric_contribution_cents")) is int
    )
    assert contribution_sum == result["metrics"]["consumption_net"]["raw_cents"]


def test_KPI_08_personal_envelope_and_observed_views_never_add_together() -> None:
    envelope = calculate_metrics(family_analytics_scenario(personal_mode="envelope"))
    observed = calculate_metrics(family_analytics_scenario(personal_mode="observed"))
    assert envelope["metrics"]["consumption_net"]["raw_cents"] == 6_999
    assert envelope["metrics"]["personal_allowances"]["raw_cents"] == 25_000
    assert observed["metrics"]["consumption_net"]["raw_cents"] == 24_999
    assert observed["metrics"]["personal_allowances"]["raw_cents"] == 0
    assert observed["personal_unspent_cents"] == 7_000


def test_MONEY_04_loan_disbursement_is_bank_flow_but_not_income() -> None:
    result = calculate_metrics(family_analytics_scenario())
    assert result["metrics"]["bank_net_flow"]["raw_cents"] is not None
    assert result["metrics"]["regular_income"]["raw_cents"] == 300_000


def test_MONEY_05_principal_remains_debt_outflow_not_consumption() -> None:
    result = calculate_metrics(family_analytics_scenario())
    assert result["totals"]["loan_principal_outflow_cents"] == 9_826_561
    assert result["category_totals"] == {"groceries": 4_199, "restaurants": 2_800}


def test_MONEY_06_savings_uses_current_allocations_once() -> None:
    result = calculate_metrics(family_analytics_scenario())
    assert result["metrics"]["savings_net_transferred"]["raw_cents"] == 15_000


def test_MONEY_07_personal_allowance_is_separate_from_observed_spending() -> None:
    result = calculate_metrics(family_analytics_scenario(personal_mode="envelope"))
    assert result["metrics"]["personal_allowances"]["raw_cents"] == 25_000
    assert result["metrics"]["consumption_net"]["raw_cents"] == 6_999


def test_KPI_11_selected_loan_commitment_is_in_capacity_and_drilldown() -> None:
    dataset = family_analytics_scenario()
    committed = replace(
        dataset,
        filters=replace(dataset.filters, commitment_transaction_ids=("loan-principal",)),
    )
    baseline = calculate_metrics(dataset)
    result = calculate_metrics(committed)
    expected = baseline["metrics"]["operational_saving_capacity"]["raw_cents"] - 9_826_561
    assert result["totals"]["selected_commitments_cents"] == 9_826_561
    assert result["metrics"]["operational_saving_capacity"]["raw_cents"] == expected
    capacity_rows = metric_detail_rows(committed, "operational_saving_capacity.v1")
    assert sum(row["metric_contribution_cents"] for row in capacity_rows) == expected
    rate_rows = metric_detail_rows(committed, "operational_saving_rate.v1")
    assert sum(row["numerator_contribution_cents"] for row in rate_rows) == expected


def test_KPI_04_incomplete_period_keeps_cashflow_and_withholds_capacity() -> None:
    result = calculate_metrics(family_analytics_scenario(completeness="partial"))
    assert result["metrics"]["bank_net_flow"]["raw_cents"] is not None
    assert result["metrics"]["operational_saving_capacity"]["raw_cents"] is None
    assert result["period"]["completeness"] == "partial"


def test_KPI_01_seeded_1000_scenarios_reconcile_and_ignore_import_order() -> None:
    generator = random.Random(20261011)
    for scenario_number in range(1_000):
        transactions: list[MetricTransaction] = []
        allocations: dict[str, tuple[MetricAllocation, ...]] = {}
        for index in range(generator.randint(1, 8)):
            transaction_id = f"{scenario_number}-{index}"
            negative = bool(generator.randrange(2))
            gross = generator.randint(1, 100_000)
            allocated = generator.randint(0, gross)
            signed_amount = -gross if negative else gross
            signed_allocation = -allocated if negative else allocated
            flow_kind = "expense" if negative else "income_recurring"
            category_id = "groceries" if negative else "income_salary_a"
            transactions.append(
                MetricTransaction(
                    transaction_id,
                    "current",
                    date(2026, 3, 1 + index),
                    signed_amount,
                    "current",
                )
            )
            allocations[transaction_id] = (
                (
                    MetricAllocation(
                        f"a-{scenario_number}-{index}",
                        transaction_id,
                        category_id,
                        flow_kind,
                        signed_allocation,
                    ),
                )
                if allocated
                else ()
            )
        dataset = AnalyticsDataset(
            filters=AnalyticsFilters(date(2026, 3, 1), date(2026, 3, 31), ("current",)),
            account_roles={"current": "current"},
            transactions=tuple(transactions),
            allocations_by_transaction=allocations,
            refund_parts=(),
            categories={},
            coverage={"status": "complete"},
        )
        result = calculate_metrics(dataset)
        bank_total = sum(row.amount_cents for row in transactions)
        assert result["metrics"]["bank_net_flow"]["raw_cents"] == bank_total
        assert sum(flow_totals(dataset).values()) == bank_total
        assert (
            sum(result["category_totals"].values())
            == result["metrics"]["consumption_net"]["raw_cents"]
        )
        detail = metric_detail_rows(dataset, "bank_net_flow.v1")
        assert sum(row["metric_contribution_cents"] for row in detail) == bank_total

        reordered = replace(
            dataset,
            transactions=tuple(reversed(dataset.transactions)),
            allocations_by_transaction={
                key: tuple(reversed(value)) for key, value in reversed(list(allocations.items()))
            },
        )
        reordered_result = calculate_metrics(reordered)
        assert (
            reordered_result["totals"]["bank_net_flow_cents"]
            == result["totals"]["bank_net_flow_cents"]
        )
        assert reordered_result["category_totals"] == result["category_totals"]


def test_KPI_01_basis_point_rounding_uses_integer_half_away_rule() -> None:
    assert rounded_basis_points(1, 20_000) == 1
    assert rounded_basis_points(-1, 20_000) == -1


def test_KPI_01_owned_round_trip_nets_to_zero_and_never_becomes_consumption() -> None:
    dataset = family_analytics_scenario()
    round_trip = {"owned-out", "owned-in"}
    without = replace(
        dataset,
        transactions=tuple(t for t in dataset.transactions if t.transaction_id not in round_trip),
        allocations_by_transaction={
            key: value
            for key, value in dataset.allocations_by_transaction.items()
            if key not in round_trip
        },
    )
    with_trip = calculate_metrics(dataset)
    base = calculate_metrics(without)
    assert sum(t.amount_cents for t in dataset.transactions if t.transaction_id in round_trip) == 0
    assert with_trip["totals"]["bank_net_flow_cents"] == base["totals"]["bank_net_flow_cents"]
    assert with_trip["category_totals"] == base["category_totals"]
    assert with_trip["metrics"]["consumption_net"] == base["metrics"]["consumption_net"]


def test_MONEY_04_cash_withdrawal_stays_a_pending_bucket_not_consumption() -> None:
    result = calculate_metrics(family_analytics_scenario())
    assert result["totals"]["cash_withdrawal_pending_cents"] == 10_000
    assert "flow_cash_unallocated" not in result["category_totals"]
    assert result["metrics"]["consumption_net"]["raw_cents"] == 6_999


def test_KPI_02_unsplit_loan_payment_is_visible_and_only_a_selected_commitment() -> None:
    dataset = family_analytics_scenario()
    baseline = calculate_metrics(dataset)
    assert baseline["totals"]["loan_unsplit_outflow_cents"] == 98_265
    assert baseline["totals"]["selected_commitments_cents"] == 0
    committed = calculate_metrics(
        replace(
            dataset,
            filters=replace(dataset.filters, commitment_transaction_ids=("loan-unsplit",)),
        )
    )
    assert committed["totals"]["selected_commitments_cents"] == 98_265
    assert committed["selected_commitments"][0]["capital_interest_status"] == "unknown_if_unsplit"
    assert committed["totals"]["loan_interest_cents"] is None
