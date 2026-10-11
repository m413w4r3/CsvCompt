"""Canonical metrics read from a database written through the real allocation/refund services."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.analytics import queries
from app.analytics.definitions import (
    AnalyticsFilters,
    calculate_metrics,
    category_tree,
    family_breakdown,
    flow_totals,
    income_breakdown,
    load_analytics_dataset,
    metric_contribution_sum,
    metric_detail_rows,
    savings_breakdown,
)
from app.db.models import Allocation, Transaction
from app.db.session import session_scope
from app.services.refund_links import net_effect_by_category
from tests.helpers.analytics_db import build_family_database, make_engine, make_settings

MARCH = (date(2026, 3, 1), date(2026, 3, 31))


def _filters(
    ids: dict[str, str], *, personal_mode: str = "envelope", **extra: object
) -> AnalyticsFilters:
    return AnalyticsFilters(
        MARCH[0],
        MARCH[1],
        tuple(sorted((ids["cur"], ids["per"], ids["sav"]))),
        mode="family_life",
        personal_mode=personal_mode,
        as_of=date(2026, 4, 15),
        **extra,  # type: ignore[arg-type]
    )


@pytest.mark.integration
def test_KPI_20_service_written_ledger_reconciles_kpi_01_02_03(tmp_path: Path) -> None:
    engine = make_engine(make_settings(tmp_path))
    try:
        ids = build_family_database(engine)
        with session_scope(engine) as session:
            dataset = load_analytics_dataset(session, _filters(ids))
            march_total = session.scalar(
                select(func.sum(Transaction.amount_cents)).where(
                    Transaction.booked_on >= MARCH[0], Transaction.booked_on <= MARCH[1]
                )
            )
            active_allocation_sum = session.scalar(
                select(func.sum(Allocation.amount_cents))
                .join(Transaction, Transaction.transaction_id == Allocation.transaction_id)
                .where(
                    Transaction.booked_on >= MARCH[0],
                    Transaction.booked_on <= MARCH[1],
                    Allocation.superseded_by_allocation_id.is_(None),
                    Allocation.retracted_at_utc.is_(None),
                )
            )
        result = calculate_metrics(dataset)
        assert dataset.coverage["status"] == "complete"
        # KPI-01 : flux bancaire = Σ transactions; allocations actives + résidu = flux bancaire.
        assert march_total is not None
        assert result["metrics"]["bank_net_flow"]["raw_cents"] == march_total
        assert sum(flow_totals(dataset).values()) == march_total
        assert (
            march_total - active_allocation_sum
            == -1_234
            == result["totals"]["unclassified_signed_residual_cents"]
        )
        # KPI-02 : le prêt reçu et le capital remboursé ne sont ni revenu ni alimentation.
        assert result["metrics"]["regular_income"]["raw_cents"] == 300_000
        assert result["totals"]["loan_principal_outflow_cents"] == 9_826_561
        assert result["totals"]["loan_unsplit_outflow_cents"] == 98_265
        assert result["totals"]["loan_interest_cents"] is None
        # KPI-03 : Σ catégories == total, remboursements imputés par allocation.
        assert result["category_totals"] == {"groceries": 3_199, "restaurants": 2_800}
        assert result["metrics"]["consumption_net"]["raw_cents"] == 5_999
        assert result["totals"]["linked_refunds_cents"] == 4_001
        assert sorted((part.category_id, part.amount_cents) for part in dataset.refund_parts) == [
            ("groceries", 1_000),
            ("groceries", 1_801),
            ("restaurants", 1_200),
        ]
        # KPI-08 : enveloppe personnelle ≠ dépenses observées du compte personnel.
        assert result["metrics"]["personal_allowances"]["raw_cents"] == 25_000
        assert result["metrics"]["savings_net_transferred"]["raw_cents"] == 15_000
        assert result["totals"]["cash_withdrawal_pending_cents"] == 10_000
        # Un résidu non nul dans le périmètre rend capacité et taux indisponibles.
        capacity = result["metrics"]["operational_saving_capacity"]
        assert capacity["raw_cents"] is None
        assert capacity["unavailable_reason"] == "relevant_residual"
        assert result["metrics"]["unclassified_amount_share"]["raw_basis_points"] > 0
    finally:
        engine.dispose()


@pytest.mark.integration
def test_KPI_20_engine_matches_the_refund_service_net_effect_per_purchase(tmp_path: Path) -> None:
    engine = make_engine(make_settings(tmp_path))
    try:
        ids = build_family_database(engine)
        filters = _filters(ids)
        with session_scope(engine) as session:
            result = calculate_metrics(load_analytics_dataset(session, filters))
        by_category: dict[str, int] = {}
        for key in ("purchase", "feb-food", "personal-purchase"):
            if key == "feb-food":
                continue
            effect = net_effect_by_category(
                engine, ids[key], account_ids=[ids["cur"], ids["per"], ids["sav"]]
            )
            if key == "personal-purchase":
                continue  # enveloppe : le compte personnel n'est pas de la consommation
            for category_id, amount in effect.by_category_cents.items():
                by_category[category_id] = by_category.get(category_id, 0) + amount
        assert by_category == result["category_totals"]
    finally:
        engine.dispose()


@pytest.mark.integration
def test_KPI_20_observed_mode_counts_personal_spending_and_no_allowance(tmp_path: Path) -> None:
    engine = make_engine(make_settings(tmp_path))
    try:
        ids = build_family_database(engine)
        with session_scope(engine) as session:
            envelope = calculate_metrics(load_analytics_dataset(session, _filters(ids)))
            observed = calculate_metrics(
                load_analytics_dataset(session, _filters(ids, personal_mode="observed"))
            )
        assert envelope["metrics"]["consumption_net"]["raw_cents"] == 5_999
        assert observed["metrics"]["consumption_net"]["raw_cents"] == 5_999 + 18_000
        assert observed["metrics"]["personal_allowances"]["raw_cents"] == 0
        assert observed["personal_unspent_cents"] == 25_000 - 18_000
        assert (
            observed["metrics"]["bank_net_flow"]["raw_cents"]
            == envelope["metrics"]["bank_net_flow"]["raw_cents"]
        )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_KPI_12_capacity_rate_and_delta_when_every_operation_is_classified(
    tmp_path: Path,
) -> None:
    engine = make_engine(make_settings(tmp_path))
    try:
        ids = build_family_database(engine, classify_unallocated=True)
        with session_scope(engine) as session:
            dataset = load_analytics_dataset(session, _filters(ids))
            result = calculate_metrics(dataset)
            deltas = {
                item["base_metric_definition_id"]: item
                for item in queries.monthly_deltas(session, dataset, result, queries.DELTA_METRICS)
            }
            committed_filters = _filters(ids, commitment_transaction_ids=(ids["loan-unsplit"],))
            committed_dataset = load_analytics_dataset(session, committed_filters)
            committed = calculate_metrics(committed_dataset)
            committed_deltas = {
                item["base_metric_definition_id"]: item
                for item in queries.monthly_deltas(
                    session, committed_dataset, committed, queries.DELTA_METRICS
                )
            }
        assert result["metrics"]["consumption_net"]["raw_cents"] == 5_999 + 1_234
        capacity = result["metrics"]["operational_saving_capacity"]["raw_cents"]
        assert capacity == 300_000 - (5_999 + 1_234) - 25_000
        # 10000 × 267767 / 300000 = 8925,5667 → 8926 bps (demi loin de zéro, entiers).
        assert result["metrics"]["operational_saving_rate"]["raw_basis_points"] == 8_926
        for metric_id in ("operational_saving_capacity.v1", "operational_saving_rate.v1"):
            rows = metric_detail_rows(dataset, metric_id)
            expected = result["metrics"][metric_id.removesuffix(".v1")]
            raw = expected["raw_cents"]
            if raw is None:
                raw = expected.get("raw_basis_points")
            assert metric_contribution_sum(metric_id, rows) == raw
        # Février : salaire 280 000, courses 15 000 → comparateur disponible, jamais inventé.
        assert deltas["regular_income.v1"]["status"] == "available"
        assert deltas["regular_income.v1"]["raw_cents"] == 20_000
        assert deltas["consumption_net.v1"]["raw_cents"] == (5_999 + 1_234) - 15_000
        assert deltas["operational_saving_capacity.v1"]["raw_cents"] == capacity - (
            280_000 - 15_000
        )
        # Un engagement sélectionné ne rejoue pas sur le mois précédent : delta indisponible.
        assert committed["totals"]["selected_commitments_cents"] == 98_265
        assert committed_deltas["operational_saving_capacity.v1"]["status"] == "unavailable"
        assert (
            committed_deltas["operational_saving_capacity.v1"]["unavailable_reason"]
            == "commitments_not_comparable"
        )
        assert committed_deltas["operational_saving_capacity.v1"]["raw_cents"] is None
        # Les métriques indépendantes de l'engagement restent comparables.
        assert committed_deltas["regular_income.v1"]["raw_cents"] == 20_000
    finally:
        engine.dispose()


@pytest.mark.integration
def test_KPI_21_breakdowns_sum_to_the_aggregate_metrics(tmp_path: Path) -> None:
    engine = make_engine(make_settings(tmp_path))
    try:
        ids = build_family_database(engine)
        with session_scope(engine) as session:
            dataset = load_analytics_dataset(session, _filters(ids))
        result = calculate_metrics(dataset)
        selected = result["selected_transactions"]
        family = family_breakdown(dataset, selected, result["refund_parts_used"])
        assert sum(item["consumption_net_cents"] for item in family) == 5_999
        assert {item["beneficiary"]: item["personal_allowances_cents"] for item in family} == {
            "household": 0,
            "adult_a": 25_000,
        }
        assert sum(savings_breakdown(dataset, selected).values()) == 15_000
        assert sum(node["raw_cents"] for node in category_tree(dataset, result)) == 5_999
        income = income_breakdown(dataset, selected)
        assert income["by_flow"]["income_recurring"] == 300_000
        assert income["by_flow"]["loan_disbursement"] == 5_000_000
        assert income["by_flow"]["income_other"] == 18_000
        assert income["by_flow"]["refund"] == 4_001
        # Le retour d'épargne et les virements propres restent des composantes distinctes.
        assert income["by_flow"]["transfer_savings"] == 5_000 + 20_000
        assert income["by_flow"]["transfer_owned"] == 25_000 + 7_000
    finally:
        engine.dispose()


@pytest.mark.integration
def test_KPI_04_incomplete_period_keeps_raw_flows_and_withholds_ratios(tmp_path: Path) -> None:
    engine = make_engine(make_settings(tmp_path))
    try:
        ids = build_family_database(engine, classify_unallocated=True)
        filters = _filters(ids)
        running = replace(filters, as_of=date(2026, 3, 15))
        with session_scope(engine) as session:
            dataset = load_analytics_dataset(session, running)
            result = calculate_metrics(dataset)
            deltas = queries.monthly_deltas(session, dataset, result, queries.DELTA_METRICS)
        assert dataset.coverage["status"] == "partial"
        assert result["metrics"]["bank_net_flow"]["raw_cents"] is not None
        assert result["metrics"]["operational_saving_capacity"]["raw_cents"] is None
        assert result["metrics"]["operational_saving_rate"]["raw_basis_points"] is None
        assert {item["status"] for item in deltas} == {"unavailable"}
        assert {item["unavailable_reason"] for item in deltas} == {"period_not_complete"}
    finally:
        engine.dispose()
