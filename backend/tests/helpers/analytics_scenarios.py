"""Synthetic scenario builders for canonical financial metric tests."""

from __future__ import annotations

from datetime import date

from app.analytics.definitions import (
    AnalyticsDataset,
    AnalyticsFilters,
    MetricAllocation,
    MetricTransaction,
    RefundPart,
)


def family_analytics_scenario(
    *,
    personal_mode: str = "envelope",
    completeness: str = "complete",
) -> AnalyticsDataset:
    """Synthetic salary/aid/loan, transfer pair, split refund, cash and personal cases."""
    accounts = {"current": "current", "personal": "personal", "savings": "savings"}
    transactions = (
        MetricTransaction("salary", "current", date(2026, 3, 2), 300_000, "current"),
        MetricTransaction("benefit", "current", date(2026, 3, 2), 18_000, "current"),
        MetricTransaction("loan", "current", date(2026, 3, 3), 5_000_000, "current"),
        MetricTransaction("split-purchase", "current", date(2026, 3, 4), -10_000, "current"),
        MetricTransaction("split-refund", "current", date(2026, 3, 5), 3_001, "current"),
        MetricTransaction("savings-out", "current", date(2026, 3, 6), -20_000, "current"),
        MetricTransaction("savings-return", "current", date(2026, 3, 7), 5_000, "current"),
        MetricTransaction("allowance", "current", date(2026, 3, 8), -25_000, "current"),
        MetricTransaction("personal-in", "personal", date(2026, 3, 8), 25_000, "personal"),
        MetricTransaction("owned-out", "current", date(2026, 3, 8), -7_000, "current"),
        MetricTransaction("owned-in", "personal", date(2026, 3, 8), 7_000, "personal"),
        MetricTransaction("personal-purchase", "personal", date(2026, 3, 9), -18_000, "personal"),
        MetricTransaction("cash", "current", date(2026, 3, 10), -10_000, "current"),
        MetricTransaction("loan-principal", "current", date(2026, 3, 11), -9_826_561, "current"),
        MetricTransaction("loan-unsplit", "current", date(2026, 3, 12), -98_265, "current"),
    )
    rows = {
        "salary": ("income_salary_a", "income_recurring", 300_000, "parent"),
        "benefit": ("income_benefits", "income_other", 18_000, "household"),
        "loan": ("flow_loan_disbursement", "loan_disbursement", 5_000_000, "unknown"),
        "split-purchase": (
            ("groceries", "expense", -6_000, "parent"),
            ("restaurants", "expense", -4_000, "parent"),
        ),
        "split-refund": ("income_refund", "refund", 3_001, "unknown"),
        "savings-out": ("flow_savings_out", "transfer_savings", -20_000, "unknown"),
        "savings-return": ("flow_savings_in", "transfer_savings", 5_000, "unknown"),
        "allowance": ("flow_personal_a", "allocation_personal", -25_000, "parent"),
        "personal-in": ("flow_owned_transfer", "transfer_owned", 25_000, "unknown"),
        "owned-out": ("flow_owned_transfer", "transfer_owned", -7_000, "unknown"),
        "owned-in": ("flow_owned_transfer", "transfer_owned", 7_000, "unknown"),
        "personal-purchase": ("groceries", "expense", -18_000, "child_a"),
        "cash": ("flow_cash_unallocated", "cash_withdrawal_pending", -10_000, "unknown"),
        "loan-principal": ("flow_loan_principal", "loan_repayment", -9_826_561, "unknown"),
        "loan-unsplit": (
            "flow_loan_payment_unknown_split",
            "loan_repayment",
            -98_265,
            "unknown",
        ),
    }
    allocation_rows: dict[str, tuple[MetricAllocation, ...]] = {}
    for transaction_id, transaction_allocations in rows.items():
        if transaction_allocations and isinstance(transaction_allocations[0], tuple):
            values = transaction_allocations
        else:
            values = (transaction_allocations,)
        allocation_rows[transaction_id] = tuple(
            MetricAllocation(
                allocation_id=f"allocation-{transaction_id}-{index}",
                transaction_id=transaction_id,
                category_id=value[0],
                flow_kind=value[1],
                amount_cents=value[2],
                beneficiary=value[3],
            )
            for index, value in enumerate(values)
        )
    purchase_allocations = allocation_rows["split-purchase"]
    parts = (
        RefundPart(
            "refund-link",
            "split-refund",
            "split-purchase",
            "current",
            purchase_allocations[0].allocation_id,
            "groceries",
            1_801,
            date(2026, 3, 4),
            "parent",
        ),
        RefundPart(
            "refund-link",
            "split-refund",
            "split-purchase",
            "current",
            purchase_allocations[1].allocation_id,
            "restaurants",
            1_200,
            date(2026, 3, 4),
            "parent",
        ),
    )
    categories = {
        "groceries": {
            "category_id": "groceries",
            "parent_category_id": None,
            "label": "Alimentation",
            "sort_order": 1,
        },
        "restaurants": {
            "category_id": "restaurants",
            "parent_category_id": None,
            "label": "Restaurants",
            "sort_order": 2,
        },
        "income_salary_a": {
            "category_id": "income_salary_a",
            "parent_category_id": None,
            "label": "Salaire",
            "sort_order": 3,
        },
        "income_benefits": {
            "category_id": "income_benefits",
            "parent_category_id": None,
            "label": "Aide synthétique",
            "sort_order": 13,
        },
        "income_refund": {
            "category_id": "income_refund",
            "parent_category_id": None,
            "label": "Remboursement",
            "sort_order": 4,
        },
        "flow_loan_disbursement": {
            "category_id": "flow_loan_disbursement",
            "parent_category_id": None,
            "label": "Prêt reçu",
            "sort_order": 5,
        },
        "flow_savings_out": {
            "category_id": "flow_savings_out",
            "parent_category_id": None,
            "label": "Épargne sortante",
            "sort_order": 6,
        },
        "flow_savings_in": {
            "category_id": "flow_savings_in",
            "parent_category_id": None,
            "label": "Retour épargne",
            "sort_order": 7,
        },
        "flow_personal_a": {
            "category_id": "flow_personal_a",
            "parent_category_id": None,
            "label": "Enveloppe",
            "sort_order": 8,
        },
        "flow_owned_transfer": {
            "category_id": "flow_owned_transfer",
            "parent_category_id": None,
            "label": "Transfert propre",
            "sort_order": 9,
        },
        "flow_cash_unallocated": {
            "category_id": "flow_cash_unallocated",
            "parent_category_id": None,
            "label": "Espèces en attente",
            "sort_order": 10,
        },
        "flow_loan_principal": {
            "category_id": "flow_loan_principal",
            "parent_category_id": None,
            "label": "Capital remboursé",
            "sort_order": 11,
        },
        "flow_loan_payment_unknown_split": {
            "category_id": "flow_loan_payment_unknown_split",
            "parent_category_id": None,
            "label": "Échéance non scindée",
            "sort_order": 12,
        },
    }
    return AnalyticsDataset(
        filters=AnalyticsFilters(
            date(2026, 3, 1),
            date(2026, 3, 31),
            tuple(accounts),
            mode="family_life",
            personal_mode=personal_mode,
        ),
        account_roles=accounts,
        transactions=transactions,
        allocations_by_transaction=allocation_rows,
        refund_parts=parts,
        categories=categories,
        coverage={
            "status": completeness,
            "complete_day_count": 31 if completeness == "complete" else 12,
            "sections": [],
            "missing_days": []
            if completeness == "complete"
            else [{"from": date(2026, 3, 13), "through": date(2026, 3, 31)}],
            "provisional_operation_count": 0,
        },
        transfer_pairs=(
            {
                "transaction_link_id": "owned-transfer-roundtrip",
                "source_transaction_id": "owned-out",
                "target_transaction_id": "owned-in",
                "amount_cents": 7_000,
            },
        ),
    )
