#!/usr/bin/env python3
"""Oracle indépendant des exemples financiers synthétiques de T01."""
from __future__ import annotations

import argparse
import calendar
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # PyYAML est uniquement le lecteur YAML, pas un moteur métier.
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXAMPLES = ROOT / "docs" / "examples" / "metrics_examples.yaml"
CATEGORY_SOURCE = ROOT / ".nimbalyst" / "plan_implementation_comptes_familiaux" / "config" / "categories.v1.yaml"

FLOW_KINDS = {
    "expense", "income_recurring", "income_other", "transfer_savings",
    "transfer_owned", "allocation_personal", "loan_disbursement",
    "loan_repayment", "capital_event", "cash_withdrawal_pending", "refund",
    "intermediary_pending", "reconciliation", "unknown",
}
RELEVANT_ALLOCATION_FLOWS = {
    "expense", "income_recurring", "income_other", "allocation_personal",
    "refund", "cash_withdrawal_pending", "intermediary_pending",
}
PRIORITY_ORDER = [
    "human_confirmed", "approved_rule", "exact_history", "heuristic", "llm_suggestion"
]
METRIC_FIELD_TO_ID = {
    "bank_net_flow_cents": "bank_net_flow.v1",
    "regular_income_cents": "regular_income.v1",
    "consumption_net_cents": "consumption_net.v1",
    "personal_allowances_cents": "personal_allowances.v1",
    "savings_net_transferred_cents": "savings_net_transferred.v1",
    "operational_saving_capacity_cents": "operational_saving_capacity.v1",
}
REQUIRED_METRIC_FIELDS = {
    "bank_net_flow_cents", "regular_income_cents", "consumption_net_cents",
    "personal_allowances_cents", "savings_net_transferred_cents",
    "operational_saving_capacity_cents", "operational_saving_rate_bps",
    "unclassified_amount_share_bps", "operation_coverage_bps",
    "unclassified_signed_residual_cents", "unclassified_absolute_residual_cents",
    "monthly_delta_cents", "month_completeness",
}


class VerificationError(Exception):
    """Erreur d'oracle, avec message exploitable en CI."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def require_int(value: Any, where: str) -> int:
    require(type(value) is int, f"{where}: entier requis, reçu {type(value).__name__}")
    return value


def reject_floats(value: Any, where: str = "document") -> None:
    if type(value) is float:
        raise VerificationError(f"{where}: float interdit, utiliser des entiers")
    if isinstance(value, dict):
        for key, child in value.items():
            reject_floats(child, f"{where}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_floats(child, f"{where}[{index}]")


def load_yaml(path: Path) -> Any:
    require(yaml is not None, "PyYAML absent : impossible de lire le YAML bloc sans dépendance")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as error:
        raise VerificationError(f"lecture YAML impossible ({path}): {error}") from error


def category_ids_by_group(document: dict[str, Any]) -> dict[str, set[str]]:
    groups: dict[str, set[str]] = {"expense": set(), "income": set(), "non_consumption": set()}

    def add_tree(items: list[dict[str, Any]], target: set[str]) -> None:
        for item in items:
            require(isinstance(item, dict) and isinstance(item.get("id"), str), "ID de catégorie invalide")
            target.add(item["id"])
            children = item.get("children", [])
            require(isinstance(children, list), f"enfants invalides pour {item['id']}")
            add_tree(children, target)

    add_tree(document.get("expense_categories", []), groups["expense"])
    add_tree(document.get("income_categories", []), groups["income"])
    add_tree(document.get("non_consumption_flows", []), groups["non_consumption"])
    all_ids = groups["expense"] | groups["income"] | groups["non_consumption"]
    require(len(all_ids) == sum(len(group) for group in groups.values()), "ID catégorie dupliqué dans le YAML source")
    return groups


def build_category_flow_map(groups: dict[str, set[str]]) -> dict[str, set[str]]:
    result = {category_id: {"expense"} for category_id in groups["expense"]}
    income_rules = {
        "income_salary_a": {"income_recurring"},
        "income_salary_b": {"income_recurring"},
        "income_benefits": {"income_recurring", "income_other"},
        "income_regular_other": {"income_recurring"},
        "income_refund": {"refund"},
        "income_extraordinary": {"income_other"},
    }
    non_consumption_rules = {
        "flow_savings_out": {"transfer_savings"},
        "flow_savings_in": {"transfer_savings"},
        "flow_owned_transfer": {"transfer_owned"},
        "flow_personal_a": {"allocation_personal"},
        "flow_personal_b": {"allocation_personal"},
        "flow_loan_disbursement": {"loan_disbursement"},
        "flow_loan_principal": {"loan_repayment"},
        "flow_loan_payment_unknown_split": {"loan_repayment"},
        "flow_capital_improvement": {"capital_event"},
        "flow_cash_unallocated": {"cash_withdrawal_pending"},
        "flow_intermediary_unresolved": {"intermediary_pending"},
        "flow_reconciliation": {"reconciliation"},
    }
    require(set(income_rules) == groups["income"], "les règles de revenu ne couvrent pas exactement le YAML source")
    require(set(non_consumption_rules) == groups["non_consumption"], "les règles de flux ne couvrent pas exactement le YAML source")
    result.update(income_rules)
    result.update(non_consumption_rules)
    require(len(result) == len(groups["expense"] | groups["income"] | groups["non_consumption"]), "mapping de catégorie incomplet")
    return result


def check_allocation_sign(flow_kind: str, amount: int, category_id: str, where: str) -> None:
    positive_only = {"income_recurring", "income_other", "loan_disbursement", "refund"}
    negative_only = {
        "expense", "allocation_personal", "loan_repayment", "capital_event",
        "cash_withdrawal_pending", "intermediary_pending",
    }
    if flow_kind in positive_only:
        require(amount > 0, f"{where}: {flow_kind} doit être positif")
    if flow_kind in negative_only:
        require(amount < 0, f"{where}: {flow_kind} doit être négatif")
    if category_id == "flow_savings_out":
        require(amount < 0, f"{where}: flow_savings_out doit être négatif")
    if category_id == "flow_savings_in":
        require(amount > 0, f"{where}: flow_savings_in doit être positif")
    if category_id == "flow_loan_disbursement":
        require(amount > 0, f"{where}: flow_loan_disbursement doit être positif")
    if category_id == "flow_loan_principal":
        require(amount < 0, f"{where}: flow_loan_principal doit être négatif")


def normalize_transaction(example_id: str, transaction: dict[str, Any], category_flow_map: dict[str, set[str]]) -> None:
    where = f"{example_id}.{transaction.get('id', '?')}"
    require(isinstance(transaction.get("id"), str), f"{where}: id manquant")
    require("flow_kind" not in transaction, f"{where}: flow_kind ne se stocke pas sur la transaction (ADR-020)")
    amount = require_int(transaction.get("amount_cents"), f"{where}.amount_cents")
    require(amount != 0, f"{where}: montant nul interdit (ADR-019.i)")
    allocations = transaction.get("allocations", [])
    require(isinstance(allocations, list), f"{where}: allocations doit être une liste")
    signed_total = 0
    for index, allocation in enumerate(allocations):
        label = f"{where}.allocations[{index}]"
        require(isinstance(allocation, dict), f"{label}: allocation invalide")
        allocation_amount = require_int(allocation.get("amount_cents"), f"{label}.amount_cents")
        allocation_flow = allocation.get("flow_kind")
        category_id = allocation.get("category_id")
        require(allocation_flow in FLOW_KINDS - {"unknown"}, f"{label}: flow_kind invalide {allocation_flow!r}")
        require(category_id in category_flow_map, f"{label}: catégorie absente du YAML source: {category_id}")
        require(allocation_flow in category_flow_map[category_id], f"{label}: catégorie {category_id} interdite pour {allocation_flow}")
        decision = allocation.get("decision")
        require(decision in {"confirmed", "review", "unknown"}, f"{label}: decision invalide")
        origin = allocation.get("origin")
        require(origin in {"manual", "rule", "history", "llm", "unclassified"}, f"{label}: origin invalide")
        if decision == "confirmed":
            require(allocation_amount != 0, f"{label}: allocation confirmée nulle interdite")
            require((allocation_amount > 0) == (amount > 0), f"{label}: signe contraire à la transaction")
            check_allocation_sign(allocation_flow, allocation_amount, category_id, label)
            signed_total += allocation_amount
    require(abs(signed_total) <= abs(amount), f"{where}: allocations confirmées supérieures au montant bancaire")


def confirmed_allocations(transactions: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (transaction, allocation)
        for transaction in transactions
        for allocation in transaction.get("allocations", [])
        if allocation.get("decision") == "confirmed"
    ]


def transaction_residual(transaction: dict[str, Any]) -> int:
    return transaction["amount_cents"] - sum(
        allocation["amount_cents"]
        for allocation in transaction.get("allocations", [])
        if allocation.get("decision") == "confirmed"
    )


def derived_flow_kinds(transaction: dict[str, Any]) -> list[str]:
    flows = {
        allocation["flow_kind"]
        for allocation in transaction.get("allocations", [])
        if allocation.get("decision") == "confirmed"
    }
    if transaction_residual(transaction) != 0 or not flows:
        flows.add("unknown")
    return sorted(flows)


def check_refund_links(example: dict[str, Any], all_transactions: dict[str, dict[str, Any]]) -> None:
    confirmed_by_source: dict[str, int] = {}
    confirmed_by_purchase: dict[str, int] = {}
    for index, link in enumerate(example.get("transaction_links", [])):
        where = f"{example['id']}.transaction_links[{index}]"
        require(link.get("kind") == "refund_of", f"{where}: type de lien doit être refund_of")
        require(link.get("status") in {"proposed", "confirmed"}, f"{where}: statut de lien invalide")
        refund_id = link.get("from_transaction")
        purchase_id = link.get("to_transaction")
        amount = require_int(link.get("amount_cents"), f"{where}.amount_cents")
        require(amount > 0, f"{where}: montant de lien positif requis")
        require(refund_id in all_transactions and purchase_id in all_transactions, f"{where}: transaction liée absente")
        refund = all_transactions[refund_id]
        purchase = all_transactions[purchase_id]
        require("refund" in derived_flow_kinds(refund) and refund["amount_cents"] > 0, f"{where}: source non-remboursement")
        require("expense" in derived_flow_kinds(purchase) and purchase["amount_cents"] < 0, f"{where}: cible non dépense")
        if link["status"] == "confirmed":
            confirmed_by_source[refund_id] = confirmed_by_source.get(refund_id, 0) + amount
            confirmed_by_purchase[purchase_id] = confirmed_by_purchase.get(purchase_id, 0) + amount
    for refund_id, linked_cents in confirmed_by_source.items():
        require(linked_cents <= all_transactions[refund_id]["amount_cents"], f"{example['id']}.{refund_id}: liens supérieurs au crédit reçu")
    for purchase_id, linked_cents in confirmed_by_purchase.items():
        require(linked_cents <= abs(all_transactions[purchase_id]["amount_cents"]), f"{example['id']}.{purchase_id}: liens supérieurs à l'achat")


def round_ratio_half_away(numerator: int, denominator: int, scale: int = 10000) -> int:
    require(denominator > 0, "dénominateur positif requis")
    product = numerator * scale
    sign = -1 if product < 0 else 1
    magnitude = abs(product)
    return sign * ((2 * magnitude + denominator) // (2 * denominator))


def transactions_in_scope(transactions: list[dict[str, Any]], accounts: list[str], period: dict[str, Any]) -> list[dict[str, Any]]:
    require(isinstance(accounts, list) and all(isinstance(item, str) for item in accounts), "liste de comptes invalide")
    start = date.fromisoformat(period["start"])
    end = date.fromisoformat(period["end"])
    require(start <= end, "période inversée")
    return [
        transaction for transaction in transactions
        if transaction["account_id"] in accounts
        and start <= date.fromisoformat(transaction["booked_on"]) <= end
    ]


def date_ranges_cover_month(month_start: date, month_end: date, windows: list[dict[str, Any]]) -> bool:
    ranges = sorted(
        (date.fromisoformat(window["covered_from"]), date.fromisoformat(window["covered_through"]))
        for window in windows
    )
    cursor = month_start
    for range_start, range_end in ranges:
        if range_start > cursor:
            return False
        if range_end >= cursor:
            cursor = range_end + timedelta(days=1)
        if cursor > month_end:
            return True
    return cursor > month_end


def derive_month_completeness(
    period: dict[str, Any], windows: list[dict[str, Any]], account_metadata: dict[str, dict[str, Any]], accounts: list[str]
) -> str:
    start = date.fromisoformat(period["start"])
    end = date.fromisoformat(period["end"])
    if any(account_metadata.get(account, {}).get("balance_status") in {"mismatch", "unknown"} for account in accounts):
        return "unknown"
    if period.get("kind") != "calendar_month":
        return "partial"
    month_start = date(start.year, start.month, 1)
    month_end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    if start != month_start or end != month_end:
        return "partial"
    windows_by_account: dict[str, list[dict[str, Any]]] = {}
    for window in windows:
        windows_by_account.setdefault(window["account_id"], []).append(window)
    completeness: list[str] = []
    for account_id in accounts:
        metadata = account_metadata.get(account_id, {})
        generated_raw = metadata.get("generated_on") or period.get("generated_on") or period.get("as_of")
        if not generated_raw:
            generated_raw = (end + timedelta(days=1)).isoformat()
        account_windows = windows_by_account.get(account_id, [])
        if not generated_raw or not account_windows:
            completeness.append("unknown")
            continue
        generated_on = date.fromisoformat(generated_raw)
        complete_through = min(end, generated_on - timedelta(days=1))
        if month_end > complete_through or not date_ranges_cover_month(month_start, month_end, account_windows):
            completeness.append("partial")
        else:
            completeness.append("complete")
    if "unknown" in completeness:
        return "unknown"
    if "partial" in completeness:
        return "partial"
    return "complete"


def month_is_previous(current: dict[str, Any], previous: dict[str, Any]) -> bool:
    current_start = date.fromisoformat(current["start"])
    current_end = date.fromisoformat(current["end"])
    previous_start = date.fromisoformat(previous["start"])
    previous_end = date.fromisoformat(previous["end"])
    previous_last = calendar.monthrange(previous_start.year, previous_start.month)[1]
    current_last = calendar.monthrange(current_start.year, current_start.month)[1]
    if previous_start.day != 1 or current_start.day != 1:
        return False
    if previous_end.day != previous_last or current_end.day != current_last:
        return False
    return (previous_start.year, previous_start.month + 1) == (current_start.year, current_start.month) or (
        previous_start.month == 12 and previous_start.year + 1 == current_start.year and current_start.month == 1
    )


def selected_confirmed_links(
    links: list[dict[str, Any]], transactions_by_id: dict[str, dict[str, Any]], selected_ids: set[str], accounts: list[str]
) -> tuple[list[dict[str, Any]], int]:
    selected: list[dict[str, Any]] = []
    ignored_out_of_scope = 0
    account_scope = set(accounts)
    for link in links:
        if link.get("kind") != "refund_of" or link.get("status") != "confirmed":
            continue
        refund = transactions_by_id[link["from_transaction"]]
        purchase = transactions_by_id[link["to_transaction"]]
        if refund["account_id"] not in account_scope or purchase["account_id"] not in account_scope:
            if refund["id"] in selected_ids:
                ignored_out_of_scope += link["amount_cents"]
            continue
        if refund["id"] in selected_ids:
            selected.append(link)
    return selected, ignored_out_of_scope


def compute_metrics(
    transactions: list[dict[str, Any]], accounts: list[str], period: dict[str, Any],
    coverage_status: str, links: list[dict[str, Any]], account_metadata: dict[str, dict[str, Any]],
    other_commitment_transaction_ids: list[str] | None = None, personal_mode: str = "envelope",
    category_id: str | None = None, pair_transaction_ids: list[str] | None = None,
) -> dict[str, Any]:
    require(personal_mode in {"envelope", "observed"}, f"personal_mode invalide: {personal_mode}")
    selected = transactions_in_scope(transactions, accounts, period)
    selected_ids = {transaction["id"] for transaction in selected}
    transactions_by_id = {transaction["id"]: transaction for transaction in transactions}
    allocations = confirmed_allocations(selected)
    residual_by_id = {transaction["id"]: transaction_residual(transaction) for transaction in selected}
    bank_net: int | None = sum(transaction["amount_cents"] for transaction in selected)

    all_flows = [(transaction, allocation) for transaction, allocation in allocations]
    regular_income = sum(
        allocation["amount_cents"] for _, allocation in all_flows
        if allocation["flow_kind"] == "income_recurring"
    )
    eligible_expenses: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for transaction, allocation in all_flows:
        if allocation["flow_kind"] != "expense":
            continue
        is_personal_account = account_metadata[transaction["account_id"]].get("role") == "personal"
        if personal_mode == "observed" or not is_personal_account:
            eligible_expenses.append((transaction, allocation))

    account_scope = set(accounts)
    eligible_purchase_ids = {
        transaction["id"]
        for transaction in transactions
        if transaction["account_id"] in account_scope
        and (personal_mode == "observed" or account_metadata[transaction["account_id"]].get("role") != "personal")
        and any(
            allocation.get("decision") == "confirmed" and allocation.get("flow_kind") == "expense"
            for allocation in transaction.get("allocations", [])
        )
    }
    all_confirmed_links, ignored_out_of_scope = selected_confirmed_links(
        links, transactions_by_id, selected_ids, accounts
    )
    attributable_links = [link for link in all_confirmed_links if link["to_transaction"] in eligible_purchase_ids]
    expense_signed = sum(allocation["amount_cents"] for _, allocation in eligible_expenses)
    matched_refunds = sum(link["amount_cents"] for link in attributable_links)
    consumption_net: int | None = -(expense_signed + matched_refunds)

    personal_signed = sum(
        allocation["amount_cents"] for transaction, allocation in all_flows
        if allocation["flow_kind"] == "allocation_personal"
        and account_metadata[transaction["account_id"]].get("role") != "personal"
    )
    personal_allowances: int | None = -personal_signed if personal_mode == "envelope" else 0
    personal_expenses = sum(
        -allocation["amount_cents"] for transaction, allocation in all_flows
        if allocation["flow_kind"] == "expense"
        and account_metadata[transaction["account_id"]].get("role") == "personal"
    )
    envelope_amount = -personal_signed
    personal_unspent: int | None = envelope_amount - personal_expenses if personal_mode == "observed" else None

    savings_signed = sum(
        allocation["amount_cents"] for transaction, allocation in all_flows
        if allocation["flow_kind"] == "transfer_savings"
        and account_metadata[transaction["account_id"]].get("role") == "current"
    )
    savings_net: int | None = -savings_signed

    relevant_confirmed_allocations = sum(
        abs(allocation["amount_cents"]) for _, allocation in all_flows
        if allocation["flow_kind"] in RELEVANT_ALLOCATION_FLOWS
    )
    unclassified_signed = sum(residual_by_id.values())
    unclassified_absolute = sum(abs(value) for value in residual_by_id.values())
    relevant_denominator = relevant_confirmed_allocations + unclassified_absolute
    unclassified_share = None if relevant_denominator == 0 else round_ratio_half_away(
        unclassified_absolute, relevant_denominator
    )

    operation_count = len(selected)
    fully_classified_count = sum(1 for residual in residual_by_id.values() if residual == 0)
    operation_coverage = None if operation_count == 0 else round_ratio_half_away(
        fully_classified_count, operation_count
    )
    selected_commitments = set(other_commitment_transaction_ids or [])
    require(selected_commitments <= selected_ids, "engagement sélectionné absent de la requête")
    for transaction_id in selected_commitments:
        flows = set(derived_flow_kinds(transactions_by_id[transaction_id]))
        require(not flows.intersection({"expense", "allocation_personal", "refund"}), "engagement sélectionné doublonne consommation ou allocation personnelle")
    commitments = sum(
        abs(transaction["amount_cents"]) for transaction in selected
        if transaction["id"] in selected_commitments and transaction["amount_cents"] < 0
    )
    has_residual = any(value != 0 for value in residual_by_id.values())
    capacity: int | None = None if coverage_status != "complete" or has_residual else (
        regular_income - consumption_net - (personal_allowances or 0) - commitments
    )
    saving_rate = None if capacity is None or regular_income <= 0 else round_ratio_half_away(capacity, regular_income)

    refund_credit = sum(
        allocation["amount_cents"] for transaction, allocation in all_flows
        if allocation["flow_kind"] == "refund"
    )
    unmatched_refunds = refund_credit - matched_refunds
    loan_repayment_outflow = sum(
        -allocation["amount_cents"] for _, allocation in all_flows
        if allocation["flow_kind"] == "loan_repayment" and allocation["amount_cents"] < 0
    )
    cash_pending_outflow = sum(
        -allocation["amount_cents"] for _, allocation in all_flows
        if allocation["flow_kind"] == "cash_withdrawal_pending" and allocation["amount_cents"] < 0
    )
    intermediary_pending_outflow = sum(
        -allocation["amount_cents"] for _, allocation in all_flows
        if allocation["flow_kind"] == "intermediary_pending" and allocation["amount_cents"] < 0
    )
    category_net: int | None = None
    if category_id is not None:
        category_expense = sum(
            -allocation["amount_cents"] for transaction, allocation in eligible_expenses
            if allocation["category_id"] == category_id
        )
        category_refunds = sum(
            link["amount_cents"] for link in attributable_links
            if any(
                allocation["category_id"] == category_id
                for allocation in transactions_by_id[link["to_transaction"]].get("allocations", [])
                if allocation.get("decision") == "confirmed" and allocation.get("flow_kind") == "expense"
            )
        )
        category_net = category_expense - category_refunds
    pair_bank_flow: int | None = None
    if pair_transaction_ids is not None:
        require(set(pair_transaction_ids) <= set(transactions_by_id), "transaction de paire absente")
        pair_bank_flow = sum(transactions_by_id[item]["amount_cents"] for item in pair_transaction_ids)

    period_end = date.fromisoformat(period["end"])
    generated_dates = {
        account_id: metadata.get("generated_on") or period.get("generated_on") or period.get("as_of") or (period_end + timedelta(days=1)).isoformat()
        for account_id, metadata in account_metadata.items()
    }
    provisional_count = sum(
        1 for transaction in selected
        if generated_dates.get(transaction["account_id"])
        and date.fromisoformat(transaction["booked_on"]) >= date.fromisoformat(generated_dates[transaction["account_id"]])
    )
    if coverage_status == "unknown":
        bank_net = regular_income = consumption_net = personal_allowances = savings_net = None
        saving_rate = unclassified_share = operation_coverage = None
        unclassified_signed = unclassified_absolute = None
        capacity = None
        matched_refunds = unmatched_refunds = None
        category_net = None if category_id is not None else None

    return {
        "bank_net_flow_cents": bank_net,
        "regular_income_cents": regular_income,
        "consumption_net_cents": consumption_net,
        "personal_allowances_cents": personal_allowances,
        "savings_net_transferred_cents": savings_net,
        "operational_saving_capacity_cents": capacity,
        "operational_saving_rate_bps": saving_rate,
        "unclassified_amount_share_bps": unclassified_share,
        "unclassified_signed_residual_cents": unclassified_signed,
        "unclassified_absolute_residual_cents": unclassified_absolute,
        "operation_coverage_bps": operation_coverage,
        "operation_count": operation_count,
        "fully_classified_count": fully_classified_count,
        "loan_repayment_outflow_cents": loan_repayment_outflow,
        "cash_withdrawal_pending_outflow_cents": cash_pending_outflow,
        "intermediary_pending_outflow_cents": intermediary_pending_outflow,
        "matched_refunds_cents": matched_refunds,
        "unmatched_refunds_cents": unmatched_refunds,
        "unattributed_out_of_scope_refunds_cents": ignored_out_of_scope,
        "personal_unspent_cents": personal_unspent,
        "negative_consumption_signaled": consumption_net is not None and consumption_net < 0,
        "negative_category_net_signaled": category_net is not None and category_net < 0,
        "category_net_cents": category_net,
        "pair_bank_flow_cents": pair_bank_flow,
        "provisional_operation_count": provisional_count,
        "month_completeness": coverage_status,
        "residual_by_transaction": residual_by_id,
        "derived_flow_kinds_by_transaction": {
            transaction["id"]: derived_flow_kinds(transaction) for transaction in selected
        },
        "selected_ids": selected_ids,
    }


def compute_monthly_delta(
    example: dict[str, Any], query: dict[str, Any], current_metrics: dict[str, Any],
    category_flow_map: dict[str, set[str]],
) -> int | None:
    comparison = example.get("comparison")
    if not comparison:
        return None
    accounts = example["accounts"]
    current_status = derive_month_completeness(example["period"], example.get("coverage_windows", []), accounts, query["accounts"])
    previous_status = derive_month_completeness(comparison["period"], comparison.get("coverage_windows", []), accounts, query["accounts"])
    if current_status != "complete" or previous_status != "complete" or not month_is_previous(example["period"], comparison["period"]):
        return None
    metric_id = query.get("delta_metric_definition_id")
    field = next((field for field, identifier in METRIC_FIELD_TO_ID.items() if identifier == metric_id), None)
    if field is None:
        return None
    prior_transactions = comparison.get("transactions", [])
    for transaction in prior_transactions:
        normalize_transaction(example["id"] + ".comparison", transaction, category_flow_map)
    prior = compute_metrics(
        prior_transactions, query["accounts"], comparison["period"], previous_status,
        example.get("transaction_links", []), accounts, [], query.get("personal_mode", "envelope"),
    )
    if current_metrics.get(field) is None or prior.get(field) is None:
        return None
    return current_metrics[field] - prior[field]


def verify_splits(example: dict[str, Any], category_flow_map: dict[str, set[str]]) -> int:
    checked = 0
    for index, split in enumerate(example.get("splits", [])):
        where = f"{example['id']}.splits[{index}]"
        amount = require_int(split.get("amount_cents"), f"{where}.amount_cents")
        percentages = split.get("percentages_bps")
        category_ids = split.get("category_ids")
        require(isinstance(percentages, list) and isinstance(category_ids, list), f"{where}: parts invalides")
        require(len(percentages) == len(category_ids) and len(percentages) > 0, f"{where}: nombre de parts incohérent")
        for index_part, percentage in enumerate(percentages):
            require_int(percentage, f"{where}.percentages_bps[{index_part}]")
        for category_id in category_ids:
            require(category_id in category_flow_map, f"{where}: catégorie absente {category_id}")
            require(split["flow_kind"] in category_flow_map[category_id], f"{where}: catégorie incompatible {category_id}")
        valid = amount != 0 and all(percentage > 0 for percentage in percentages) and sum(percentages) == 10000
        actual: list[tuple[str, int]] = []
        if valid:
            weights: dict[str, int] = {}
            for category_id, percentage in zip(category_ids, percentages):
                weights[category_id] = weights.get(category_id, 0) + percentage
            order = sorted(weights, key=lambda category: (-weights[category], category))
            remaining = amount
            for position, category_id in enumerate(order):
                if position == len(order) - 1:
                    part = remaining
                else:
                    part = round_ratio_half_away(amount * weights[category_id], 10000, 1)
                    remaining -= part
                actual.append((category_id, part))
            valid = all(part != 0 and (part > 0) == (amount > 0) for _, part in actual)
            if not valid:
                actual = []
            else:
                require(sum(part for _, part in actual) == amount, f"{where}: split non conservatif")
        expected_valid = split.get("expected_valid")
        require(type(expected_valid) is bool and valid == expected_valid, f"{where}: validité attendue {expected_valid}, calculée {valid}")
        expected = split.get("expected_allocations_cents")
        require(isinstance(expected, list), f"{where}: oracle allocations absent")
        expected_ints = [require_int(value, f"{where}.expected_allocations_cents") for value in expected]
        require([part for _, part in actual] == expected_ints, f"{where}: allocations attendues {expected_ints}, calculées {[part for _, part in actual]}")
        if "expected_category_ids" in split:
            require([category for category, _ in actual] == split["expected_category_ids"], f"{where}: ordre canonique des catégories différent")
        if "expected_by_category_cents" in split:
            actual_map = {category: cents for category, cents in actual}
            expected_map = {key: require_int(value, f"{where}.expected_by_category_cents.{key}") for key, value in split["expected_by_category_cents"].items()}
            require(actual_map == expected_map, f"{where}: allocations par catégorie attendues {expected_map}, calculées {actual_map}")
        checked += 1
    return checked


def verify_priority(example: dict[str, Any]) -> int:
    if "priority_cases" not in example:
        return 0
    require(example.get("priority_order") == PRIORITY_ORDER, f"{example['id']}: ordre de priorité différent du contrat")
    checked = 0
    for case in example["priority_cases"]:
        present = set(case["present"])
        actual = next((source for source in PRIORITY_ORDER if source in present), None)
        require(actual == case.get("expected_winner"), f"{example['id']}.{case['id']}: priorité attendue {case.get('expected_winner')}, calculée {actual}")
        checked += 1
    return checked


def verify_examples(path: Path) -> tuple[int, int, int]:
    document = load_yaml(path)
    reject_floats(document)
    require(document.get("schema_version") == 1, "schema_version doit valoir 1")
    require(document.get("currency_unit") == "EUR-cent", "unité monétaire inattendue")
    source_categories = load_yaml(CATEGORY_SOURCE)
    category_flow_map = build_category_flow_map(category_ids_by_group(source_categories))
    domain_text = (ROOT / "docs" / "DOMAIN_MODEL.md").read_text(encoding="utf-8")
    matrix_start = domain_text.find("### Compatibilité exhaustive `flow_kind` ↔ identifiants de catégories")
    matrix_end = domain_text.find("`income_benefits` peut être", matrix_start)
    require(matrix_start >= 0 and matrix_end > matrix_start, "matrice de compatibilité absente ou mal délimitée")
    compatibility_matrix = domain_text[matrix_start:matrix_end]
    documented_matrix: dict[str, set[str]] = {}
    documented_flows: set[str] = set()
    for line in compatibility_matrix.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 2:
            continue
        flow_kind = cells[0].strip("`")
        if flow_kind not in FLOW_KINDS:
            continue
        documented_flows.add(flow_kind)
        for raw_category in cells[1].split(","):
            category = raw_category.strip().strip("`")
            if category == "aucune":
                continue
            if category:
                documented_matrix.setdefault(category, set()).add(flow_kind)
    require(documented_flows == FLOW_KINDS, "la matrice DOMAIN_MODEL.md ne contient pas exactement les 14 flow_kind")
    require(documented_matrix == category_flow_map, "matrice DOMAIN_MODEL.md divergente des IDs admissibles calculés depuis la taxonomie")
    examples = document.get("examples")
    require(isinstance(examples, list) and len(examples) >= 12, "au moins 12 exemples sont requis")

    seen_metric_fields: set[str] = set()
    examples_checked = splits_checked = policy_cases_checked = 0
    example_ids: set[str] = set()
    for example in examples:
        example_id = example.get("id")
        require(isinstance(example_id, str) and example_id not in example_ids, f"id d’exemple absent ou dupliqué: {example_id}")
        example_ids.add(example_id)
        splits_checked += verify_splits(example, category_flow_map)
        policy_cases_checked += verify_priority(example)
        if "transactions" not in example:
            continue
        accounts = example.get("accounts", {})
        require(isinstance(accounts, dict), f"{example_id}: comptes invalides")
        for account_id, metadata in accounts.items():
            require(metadata.get("role") in {"current", "savings", "personal", "pea", "other"}, f"{example_id}.{account_id}: role invalide")
            require(metadata.get("balance_status", "match") in {"match", "mismatch", "unknown"}, f"{example_id}.{account_id}: balance_status invalide")
        derived_status = derive_month_completeness(
            example["period"], example.get("coverage_windows", []), accounts, list(accounts)
        )
        require(derived_status == example.get("coverage_status"), f"{example_id}: statut calendrier déclaré {example.get('coverage_status')}, calculé {derived_status}")
        comparison = example.get("comparison")
        if comparison:
            derived_comparison = derive_month_completeness(
                comparison["period"], comparison.get("coverage_windows", []), accounts, list(accounts)
            )
            require(derived_comparison == comparison.get("coverage_status"), f"{example_id}: couverture comparaison déclarée {comparison.get('coverage_status')}, calculée {derived_comparison}")
        transactions = example["transactions"]
        require(isinstance(transactions, list), f"{example_id}: transactions invalides")
        all_transactions: dict[str, dict[str, Any]] = {}
        for transaction in transactions:
            normalize_transaction(example_id, transaction, category_flow_map)
            require(transaction["id"] not in all_transactions, f"{example_id}: transaction dupliquée {transaction['id']}")
            require(transaction.get("account_id") in accounts, f"{example_id}.{transaction['id']}: compte absent")
            all_transactions[transaction["id"]] = transaction
            if "expected_derived_flow_kinds" in transaction:
                require(transaction["expected_derived_flow_kinds"] == derived_flow_kinds(transaction), f"{example_id}.{transaction['id']}: flow_kind dérivés différents")
        check_refund_links(example, all_transactions)

        for query in example.get("queries", []):
            require(set(query.get("accounts", [])) <= set(accounts), f"{example_id}.{query.get('id')}: compte de filtre absent")
            personal_mode = query.get("personal_mode", "envelope")
            query_status = derive_month_completeness(example["period"], example.get("coverage_windows", []), accounts, query["accounts"])
            calculated = compute_metrics(
                transactions, query["accounts"], example["period"], query_status,
                example.get("transaction_links", []), accounts,
                query.get("other_commitment_transaction_ids", []), personal_mode,
                query.get("category_id"), query.get("pair_transaction_ids"),
            )
            calculated["monthly_delta_cents"] = compute_monthly_delta(example, query, calculated, category_flow_map)
            expected = query.get("expected", {})
            for field, expected_value in expected.items():
                require(field in calculated or field == "monthly_delta_cents", f"{example_id}.{query.get('id')}: oracle inconnu {field}")
                actual = calculated.get(field)
                if field.endswith("_cents") and expected_value is not None:
                    require_int(expected_value, f"{example_id}.{query.get('id')}.{field}")
                if field.endswith("_bps") and expected_value is not None:
                    require_int(expected_value, f"{example_id}.{query.get('id')}.{field}")
                require(actual == expected_value, f"{example_id}.{query.get('id')}.{field}: attendu {expected_value}, calculé {actual}")
                seen_metric_fields.add(field)
            if "expected_derived_flow_kinds_by_transaction" in query:
                require(calculated["derived_flow_kinds_by_transaction"] == query["expected_derived_flow_kinds_by_transaction"], f"{example_id}.{query.get('id')}: flux dérivés différents")
            examples_checked += 1

    missing = REQUIRED_METRIC_FIELDS - seen_metric_fields
    require(not missing, f"métriques canoniques sans oracle exécuté: {', '.join(sorted(missing))}")
    return examples_checked, splits_checked, policy_cases_checked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--examples", type=Path, default=DEFAULT_EXAMPLES, help="fichier YAML à vérifier (utile pour un test altéré)")
    args = parser.parse_args()
    try:
        checked, splits, policies = verify_examples(args.examples.resolve())
    except VerificationError as error:
        print(f"ÉCHEC: {error}", file=sys.stderr)
        return 1
    print(f"PASS: {checked} oracles métriques, {splits} splits et {policies} cas de priorité vérifiés en centimes entiers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
