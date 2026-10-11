"""Seeded random synthetic scenarios for the metric reconciliation properties."""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import date

from app.analytics.definitions import (
    AnalyticsDataset,
    AnalyticsFilters,
    MetricAllocation,
    MetricTransaction,
    RefundPart,
    _category_scope,
)

ACCOUNT_ROLES = {"cur": "current", "per": "personal", "sav": "savings"}
BENEFICIARIES = ("parent_a", "child_a", "household", "unknown")
EXPENSE_CATEGORIES = ("groceries_organic", "groceries_bakery", "housing_energy", "health_dentist")
CATEGORIES: dict[str, dict[str, object]] = {
    "groceries": {
        "category_id": "groceries",
        "parent_category_id": None,
        "label": "A",
        "sort_order": 1,
    },
    "groceries_organic": {
        "category_id": "groceries_organic",
        "parent_category_id": "groceries",
        "label": "A1",
        "sort_order": 2,
    },
    "groceries_bakery": {
        "category_id": "groceries_bakery",
        "parent_category_id": "groceries",
        "label": "A2",
        "sort_order": 3,
    },
    "housing": {
        "category_id": "housing",
        "parent_category_id": None,
        "label": "B",
        "sort_order": 4,
    },
    "housing_energy": {
        "category_id": "housing_energy",
        "parent_category_id": "housing",
        "label": "B1",
        "sort_order": 5,
    },
    "health": {"category_id": "health", "parent_category_id": None, "label": "C", "sort_order": 6},
    "health_dentist": {
        "category_id": "health_dentist",
        "parent_category_id": "health",
        "label": "C1",
        "sort_order": 7,
    },
}
DEBIT_FLOWS = (
    ("expense", EXPENSE_CATEGORIES),
    ("expense", EXPENSE_CATEGORIES),
    ("expense", EXPENSE_CATEGORIES),
    ("allocation_personal", ("flow_personal_a",)),
    ("transfer_savings", ("flow_savings_out",)),
    ("transfer_owned", ("flow_owned_transfer",)),
    ("loan_repayment", ("flow_loan_principal", "flow_loan_payment_unknown_split")),
    ("cash_withdrawal_pending", ("flow_cash_unallocated",)),
    ("intermediary_pending", ("flow_intermediary",)),
    ("capital_event", ("flow_capital",)),
)
CREDIT_FLOWS = (
    ("income_recurring", ("income_salary_a",)),
    ("income_recurring", ("income_salary_a",)),
    ("income_other", ("income_benefits",)),
    ("refund", ("income_refund",)),
    ("refund", ("income_refund",)),
    ("loan_disbursement", ("flow_loan_disbursement",)),
    ("transfer_savings", ("flow_savings_in",)),
    ("transfer_owned", ("flow_owned_transfer",)),
)
FLOW_KIND_CHOICES = (
    "expense",
    "income_recurring",
    "income_other",
    "refund",
    "allocation_personal",
    "transfer_savings",
    "loan_repayment",
    "unknown",
)


def random_dataset(
    generator: random.Random,
    scenario_number: int,
    *,
    filtered: bool = False,
    personal_mode: str | None = None,
    clean: bool = False,
) -> AnalyticsDataset:
    """One synthetic month: random roles, splits, residuals, refund parts and coverage."""
    mode = personal_mode or generator.choice(("envelope", "observed"))
    account_ids = ("cur", "per", "sav") if generator.random() < 0.7 else ("cur", "per")
    transactions: list[MetricTransaction] = []
    allocations: dict[str, tuple[MetricAllocation, ...]] = {}
    for index in range(generator.randint(1, 12)):
        transaction_id = f"t{scenario_number}-{index}"
        account_id = generator.choice(("cur", "cur", "per", "sav"))
        negative = generator.random() < 0.6
        gross = generator.randint(1, 200_000)
        sign = -1 if negative else 1
        transactions.append(
            MetricTransaction(
                transaction_id,
                account_id,
                date(2026, 3, generator.randint(1, 28)),
                sign * gross,
                ACCOUNT_ROLES[account_id],
                payee_id=generator.choice((None, "payee-a", "payee-b")),
            )
        )
        if not clean and generator.random() < 0.2:
            allocations[transaction_id] = ()
            continue
        remaining = gross if clean or generator.random() < 0.6 else generator.randint(1, gross)
        rows: list[MetricAllocation] = []
        row_count = generator.randint(1, 3)
        for position in range(row_count):
            if remaining <= 0:
                break
            last = position == row_count - 1
            part = remaining if last and clean else generator.randint(1, remaining)
            remaining -= part
            flow_kind, categories = generator.choice(DEBIT_FLOWS if negative else CREDIT_FLOWS)
            rows.append(
                MetricAllocation(
                    f"a{scenario_number}-{index}-{position}",
                    transaction_id,
                    generator.choice(categories),
                    flow_kind,
                    sign * part,
                    generator.choice(BENEFICIARIES),
                )
            )
        allocations[transaction_id] = tuple(rows)

    parts: list[RefundPart] = []
    expenses = [
        (row, transaction)
        for transaction in transactions
        for row in allocations[transaction.transaction_id]
        if row.flow_kind == "expense"
    ]
    capacity_left: dict[str, int] = {}
    for transaction in transactions:
        for row in allocations[transaction.transaction_id]:
            if row.flow_kind == "refund":
                capacity_left[row.allocation_id] = row.amount_cents
    expense_left = {row.allocation_id: -row.amount_cents for row, _ in expenses}
    for transaction in transactions:
        for refund in allocations[transaction.transaction_id]:
            if refund.flow_kind != "refund" or not expenses or generator.random() < 0.3:
                continue
            target, target_transaction = generator.choice(expenses)
            amount = min(capacity_left[refund.allocation_id], expense_left[target.allocation_id])
            if amount <= 0:
                continue
            amount = generator.randint(1, amount)
            capacity_left[refund.allocation_id] -= amount
            expense_left[target.allocation_id] -= amount
            parts.append(
                RefundPart(
                    f"link-{refund.allocation_id}-{target.allocation_id}",
                    transaction.transaction_id,
                    target_transaction.transaction_id,
                    target_transaction.account_id,
                    target.allocation_id,
                    target.category_id,
                    amount,
                    target_transaction.booked_on,
                    target.beneficiary,
                )
            )
    filters = AnalyticsFilters(
        date(2026, 3, 1),
        date(2026, 3, 31),
        account_ids,
        mode="family_life",
        personal_mode=mode,
    )
    if filtered:
        filters = replace(
            filters,
            category_ids=tuple(
                sorted(
                    generator.sample(
                        ("groceries", "groceries_organic", "housing", "health_dentist"),
                        generator.randint(0, 2),
                    )
                )
            ),
            flow_kinds=tuple(sorted(generator.sample(FLOW_KIND_CHOICES, generator.randint(0, 3)))),
        )
    status = (
        "complete"
        if clean
        else generator.choices(("complete", "partial", "unknown"), weights=(8, 1, 1))[0]
    )
    return AnalyticsDataset(
        filters=filters,
        account_roles={key: ACCOUNT_ROLES[key] for key in account_ids},
        transactions=tuple(item for item in transactions if item.account_id in account_ids),
        allocations_by_transaction=allocations,
        refund_parts=tuple(parts),
        categories=CATEGORIES,  # type: ignore[arg-type]
        coverage={"status": status, "provisional_operation_count": 0, "missing_days": []},
        _expanded_categories=_category_scope(filters, CATEGORIES),  # type: ignore[arg-type]
    )
