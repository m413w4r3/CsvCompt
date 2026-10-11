"""Seeded property tests: every reconciliation of METRICS.md on random synthetic months."""

from __future__ import annotations

import random
from dataclasses import replace
from typing import Any

import pytest

from app.analytics.definitions import (
    AnalyticsDataset,
    calculate_metrics,
    category_tree,
    family_breakdown,
    flow_totals,
    income_breakdown,
    metric_contribution_sum,
    metric_detail_rows,
    savings_breakdown,
)
from tests.helpers.analytics_random import random_dataset

SEED = 20261011
SCENARIOS = 1_000
ADDITIVE_METRICS = {
    "bank_net_flow.v1": "bank_net_flow",
    "regular_income.v1": "regular_income",
    "consumption_net.v1": "consumption_net",
    "personal_allowances.v1": "personal_allowances",
    "savings_net_transferred.v1": "savings_net_transferred",
    "operational_saving_capacity.v1": "operational_saving_capacity",
}


def _oracle(dataset: AnalyticsDataset) -> dict[str, int]:
    """Plain restatement of METRICS.md on unfiltered data, independent of the engine."""
    accounts = set(dataset.filters.account_ids)
    observed = dataset.filters.personal_mode == "observed"
    transactions = [item for item in dataset.transactions if item.account_id in accounts]
    ids = {item.transaction_id for item in transactions}
    gross = 0
    allowances = 0
    savings = 0
    income = 0
    for transaction in transactions:
        role = dataset.account_roles[transaction.account_id]
        for allocation in dataset.allocations_by_transaction.get(transaction.transaction_id, ()):
            flow_kind = allocation.flow_kind
            if flow_kind == "expense" and (observed or role != "personal"):
                gross += -allocation.amount_cents
            if flow_kind == "allocation_personal" and not observed and role != "personal":
                allowances += -allocation.amount_cents
            if flow_kind == "transfer_savings" and role == "current":
                savings += -allocation.amount_cents
            if flow_kind == "income_recurring":
                income += allocation.amount_cents
    refunds = 0
    for part in dataset.refund_parts:
        if part.source_transaction_id not in ids or part.target_account_id not in accounts:
            continue
        if observed or dataset.account_roles[part.target_account_id] != "personal":
            refunds += part.amount_cents
    return {
        "bank_net_flow": sum(item.amount_cents for item in transactions),
        "regular_income": income,
        "consumption_net": gross - refunds,
        "personal_allowances": allowances,
        "savings_net_transferred": savings,
    }


def _check_structure(dataset: AnalyticsDataset) -> dict[str, Any]:
    result = calculate_metrics(dataset)
    selected = result["selected_transactions"]
    known = dataset.coverage["status"] not in {"unknown", "not_started"}
    bank = sum(item.amount_cents for item in selected)
    assert result["metrics"]["bank_net_flow"]["raw_cents"] == (bank if known else None)
    # Σ allocations actives + résidu == flux bancaire, pour tout filtre.
    assert sum(flow_totals(dataset).values()) == bank
    consumption = sum(result["category_totals"].values())
    assert result["totals"]["consumption_net_cents"] == (consumption if known else None)
    for metric_id, key in ADDITIVE_METRICS.items():
        raw = result["metrics"][key]["raw_cents"]
        rows = metric_detail_rows(dataset, metric_id)
        if raw is not None:
            assert metric_contribution_sum(metric_id, rows) == raw, metric_id
    # Drilldown == agrégat pour les ratios en basis points.
    unclassified = result["metrics"]["unclassified_amount_share"]["raw_basis_points"]
    if unclassified is not None:
        rows = metric_detail_rows(dataset, "unclassified_amount_share.v1")
        assert metric_contribution_sum("unclassified_amount_share.v1", rows) == unclassified
    rate = result["metrics"]["operational_saving_rate"]["raw_basis_points"]
    if rate is not None:
        rows = metric_detail_rows(dataset, "operational_saving_rate.v1")
        assert metric_contribution_sum("operational_saving_rate.v1", rows) == rate
    coverage = result["metrics"]["operation_coverage"]["raw_basis_points"]
    if coverage is not None:
        rows = metric_detail_rows(dataset, "operation_coverage.v1")
        assert metric_contribution_sum("operation_coverage.v1", rows) == coverage
    # Σ par bénéficiaire, par catégorie et par arbre == total.
    family = family_breakdown(dataset, selected, result["refund_parts_used"])
    assert sum(item["consumption_net_cents"] for item in family) == consumption
    if known:
        assert (
            sum(item["personal_allowances_cents"] for item in family)
            == result["totals"]["personal_allowances_cents"]
        )
        assert (
            sum(savings_breakdown(dataset, selected).values())
            == result["totals"]["savings_net_transferred_cents"]
        )
    tree = category_tree(dataset, result)
    assert sum(node["raw_cents"] for node in tree) == consumption
    stack = list(tree)
    while stack:
        node = stack.pop()
        own = node["own_gross_expense_cents"] - node["own_linked_refunds_cents"]
        assert node["raw_cents"] == own + sum(child["raw_cents"] for child in node["children"])
        stack.extend(node["children"])
    income = income_breakdown(dataset, selected)
    if known:
        assert (
            income["by_flow"].get("income_recurring", 0) == result["totals"]["regular_income_cents"]
        )
    # La capacité n'existe jamais sans période complète ni sans ventilation complète.
    capacity = result["metrics"]["operational_saving_capacity"]["raw_cents"]
    if dataset.coverage["status"] != "complete":
        assert capacity is None
    return result


def test_KPI_18_property_1000_seeded_scenarios_reconcile_for_every_filter() -> None:
    generator = random.Random(SEED)
    for scenario_number in range(SCENARIOS):
        _check_structure(
            random_dataset(generator, scenario_number, filtered=scenario_number % 2 == 1)
        )


def test_KPI_18_property_fully_classified_months_reach_capacity_and_rate() -> None:
    generator = random.Random(SEED + 4)
    with_rate = 0
    for scenario_number in range(SCENARIOS):
        dataset = random_dataset(generator, scenario_number, clean=True)
        result = _check_structure(dataset)
        capacity = result["metrics"]["operational_saving_capacity"]["raw_cents"]
        income = result["metrics"]["regular_income"]["raw_cents"]
        if capacity is not None:
            expected = (
                income
                - result["metrics"]["consumption_net"]["raw_cents"]
                - result["metrics"]["personal_allowances"]["raw_cents"]
            )
            assert capacity == expected
        if result["metrics"]["operational_saving_rate"]["raw_basis_points"] is not None:
            with_rate += 1
    assert with_rate > 100


def test_KPI_19_property_unfiltered_metrics_match_independent_oracle() -> None:
    generator = random.Random(SEED + 1)
    for scenario_number in range(SCENARIOS):
        dataset = random_dataset(generator, scenario_number)
        dataset.coverage["status"] = "complete"
        result = calculate_metrics(dataset)
        expected = _oracle(dataset)
        for key, value in expected.items():
            assert result["metrics"][key]["raw_cents"] == value, key
        assert (
            result["totals"]["gross_expense_cents"] - result["totals"]["linked_refunds_cents"]
            == expected["consumption_net"]
        )


def test_KPI_19_property_envelope_and_observed_never_double_count_personal_account() -> None:
    generator = random.Random(SEED + 2)
    for scenario_number in range(SCENARIOS):
        base = random_dataset(generator, scenario_number, personal_mode="envelope")
        base.coverage["status"] = "complete"
        envelope = calculate_metrics(base)
        observed = calculate_metrics(
            replace(base, filters=replace(base.filters, personal_mode="observed"))
        )
        accounts = set(base.filters.account_ids)
        ids = {item.transaction_id for item in base.transactions}
        personal_gross = sum(
            -allocation.amount_cents
            for transaction in base.transactions
            if transaction.account_role == "personal"
            for allocation in base.allocations_by_transaction.get(transaction.transaction_id, ())
            if allocation.flow_kind == "expense"
        )
        personal_refunds = sum(
            part.amount_cents
            for part in base.refund_parts
            if part.source_transaction_id in ids
            and part.target_account_id in accounts
            and base.account_roles[part.target_account_id] == "personal"
        )
        assert observed["metrics"]["personal_allowances"]["raw_cents"] == 0
        # observed = envelope + dépenses nettes du compte personnel : jamais l'enveloppe en plus.
        assert (
            observed["metrics"]["consumption_net"]["raw_cents"]
            == envelope["metrics"]["consumption_net"]["raw_cents"]
            + personal_gross
            - personal_refunds
        )
        # bank_net_flow est indépendant du mode personnel.
        assert (
            observed["metrics"]["bank_net_flow"]["raw_cents"]
            == envelope["metrics"]["bank_net_flow"]["raw_cents"]
        )


def test_KPI_18_property_results_do_not_depend_on_import_order() -> None:
    generator = random.Random(SEED + 3)
    for scenario_number in range(300):
        dataset = random_dataset(generator, scenario_number, filtered=scenario_number % 2 == 1)
        reordered = replace(
            dataset,
            transactions=tuple(reversed(dataset.transactions)),
            refund_parts=tuple(reversed(dataset.refund_parts)),
            allocations_by_transaction={
                key: tuple(reversed(value))
                for key, value in reversed(list(dataset.allocations_by_transaction.items()))
            },
        )
        first = calculate_metrics(dataset)
        second = calculate_metrics(reordered)
        assert first["metrics"] == second["metrics"]
        assert first["totals"] == second["totals"]
        assert first["category_totals"] == second["category_totals"]
        assert first["residuals"] == second["residuals"]
        assert flow_totals(dataset) == flow_totals(reordered)


@pytest.mark.parametrize("personal_mode", ["envelope", "observed"])
def test_KPI_19_personal_refund_follows_the_purchase_account(personal_mode: str) -> None:
    from datetime import date

    from app.analytics.definitions import (
        AnalyticsFilters,
        MetricAllocation,
        MetricTransaction,
        RefundPart,
    )

    purchase = MetricTransaction("purchase", "per", date(2026, 3, 2), -5_000, "personal")
    credit = MetricTransaction("credit", "cur", date(2026, 3, 9), 2_000, "current")
    expense = MetricAllocation("a-purchase", "purchase", "groceries", "expense", -5_000)
    refund = MetricAllocation("a-credit", "credit", "income_refund", "refund", 2_000)
    dataset = AnalyticsDataset(
        filters=AnalyticsFilters(
            date(2026, 3, 1), date(2026, 3, 31), ("cur", "per"), personal_mode=personal_mode
        ),
        account_roles={"cur": "current", "per": "personal"},
        transactions=(purchase, credit),
        allocations_by_transaction={"purchase": (expense,), "credit": (refund,)},
        refund_parts=(
            RefundPart(
                "link",
                "credit",
                "purchase",
                "per",
                "a-purchase",
                "groceries",
                2_000,
                date(2026, 3, 2),
            ),
        ),
        categories={},
        coverage={"status": "complete"},
    )
    result = calculate_metrics(dataset)
    expected = 0 if personal_mode == "envelope" else 3_000
    assert result["metrics"]["consumption_net"]["raw_cents"] == expected
    assert result["category_totals"] == (
        {} if personal_mode == "envelope" else {"groceries": 3_000}
    )
