#!/usr/bin/env python3
"""Oracle indépendant des exemples financiers synthétiques de T01."""
from __future__ import annotations

import argparse
import calendar
import sys
from datetime import date
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # PyYAML est un lecteur optionnel du fichier YAML, pas un moteur métier.
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
RELEVANT_FLOW_KINDS = {
    "expense", "income_recurring", "income_other", "allocation_personal",
    "refund", "cash_withdrawal_pending", "intermediary_pending", "unknown",
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
    pass


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


def check_sign(flow_kind: str, amount: int, allocations: list[dict[str, Any]], where: str) -> None:
    if amount == 0:
        return
    positive_only = {"income_recurring", "income_other", "loan_disbursement", "refund"}
    negative_only = {
        "expense", "allocation_personal", "loan_repayment", "capital_event",
        "cash_withdrawal_pending", "intermediary_pending",
    }
    if flow_kind in positive_only:
        require(amount > 0, f"{where}: {flow_kind} doit être positif")
    if flow_kind in negative_only:
        require(amount < 0, f"{where}: {flow_kind} doit être négatif")
    if flow_kind == "transfer_savings":
        categories = {allocation.get("category_id") for allocation in allocations}
        if "flow_savings_out" in categories:
            require(amount < 0, f"{where}: flow_savings_out doit être négatif")
        if "flow_savings_in" in categories:
            require(amount > 0, f"{where}: flow_savings_in doit être positif")


def normalize_transaction(example_id: str, transaction: dict[str, Any], category_flow_map: dict[str, set[str]]) -> None:
    where = f"{example_id}.{transaction.get('id', '?')}"
    require(isinstance(transaction.get("id"), str), f"{where}: id manquant")
    amount = require_int(transaction.get("amount_cents"), f"{where}.amount_cents")
    flow_kind = transaction.get("flow_kind")
    require(flow_kind in FLOW_KINDS, f"{where}: flow_kind inconnu {flow_kind!r}")
    allocations = transaction.get("allocations", [])
    require(isinstance(allocations, list), f"{where}: allocations doit être une liste")
    check_sign(flow_kind, amount, allocations, where)
    signed_total = 0
    for index, allocation in enumerate(allocations):
        label = f"{where}.allocations[{index}]"
        allocation_amount = require_int(allocation.get("amount_cents"), f"{label}.amount_cents")
        allocation_flow = allocation.get("flow_kind")
        category_id = allocation.get("category_id")
        require(allocation_flow == flow_kind, f"{label}: un seul flow_kind par transaction dans ces exemples")
        require(category_id in category_flow_map, f"{label}: catégorie absente du YAML source: {category_id}")
        require(allocation_flow in category_flow_map[category_id], f"{label}: catégorie {category_id} interdite pour {allocation_flow}")
        decision = allocation.get("decision")
        require(decision in {"confirmed", "review", "unknown"}, f"{label}: decision invalide")
        origin = allocation.get("origin")
        require(origin in {"manual", "rule", "history", "llm", "unclassified"}, f"{label}: origin invalide")
        if decision == "confirmed":
            require(allocation_amount == 0 or amount == 0 or (allocation_amount > 0) == (amount > 0), f"{label}: signe contraire à la transaction")
            signed_total += allocation_amount
    require(abs(signed_total) <= abs(amount), f"{where}: allocations confirmées supérieures au montant bancaire")


def check_refund_links(example: dict[str, Any], all_transactions: dict[str, dict[str, Any]]) -> None:
    refunded_by_source: dict[str, int] = {}
    matched_by_purchase: dict[str, int] = {}
    for index, link in enumerate(example.get("transaction_links", [])):
        where = f"{example['id']}.transaction_links[{index}]"
        require(link.get("kind") == "purchase_refund", f"{where}: type de lien inconnu")
        refund_id = link.get("from_transaction")
        purchase_id = link.get("to_transaction")
        amount = require_int(link.get("amount_cents"), f"{where}.amount_cents")
        require(amount > 0, f"{where}: montant de lien positif requis")
        require(refund_id in all_transactions and purchase_id in all_transactions, f"{where}: transaction liée absente")
        refund = all_transactions[refund_id]
        purchase = all_transactions[purchase_id]
        require(refund.get("flow_kind") == "refund" and refund["amount_cents"] > 0, f"{where}: source non-remboursement")
        require(purchase.get("flow_kind") == "expense" and purchase["amount_cents"] < 0, f"{where}: cible non dépense")
        refunded_by_source[refund_id] = refunded_by_source.get(refund_id, 0) + amount
        matched_by_purchase[purchase_id] = matched_by_purchase.get(purchase_id, 0) + amount
    for refund_id, matched in refunded_by_source.items():
        require(matched <= all_transactions[refund_id]["amount_cents"], f"{example['id']}.{refund_id}: lien supérieur au crédit reçu")
    for purchase_id, matched in matched_by_purchase.items():
        require(matched <= abs(all_transactions[purchase_id]["amount_cents"]), f"{example['id']}.{purchase_id}: remboursement supérieur à l'achat")


def round_ratio_half_away(numerator: int, denominator: int, scale: int = 10000) -> int:
    require(denominator > 0, "dénominateur positif requis")
    product = numerator * scale
    sign = -1 if product < 0 else 1
    magnitude = abs(product)
    rounded = (2 * magnitude + denominator) // (2 * denominator)
    return sign * rounded


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


def derive_month_completeness(period: dict[str, Any], windows: list[dict[str, Any]], accounts: list[str]) -> str:
    start = date.fromisoformat(period["start"])
    end = date.fromisoformat(period["end"])
    month_start = date(start.year, start.month, 1)
    month_end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    if period.get("kind") != "calendar_month":
        return "unknown"
    if start != month_start or end != month_end:
        return "partial"
    as_of = period.get("as_of")
    if as_of is not None and date.fromisoformat(as_of) < month_end:
        return "partial"
    windows_by_account: dict[str, list[dict[str, Any]]] = {}
    for window in windows:
        windows_by_account.setdefault(window["account_id"], []).append(window)
    for account_id in accounts:
        account_windows = windows_by_account.get(account_id)
        if not account_windows:
            return "unknown"
        ranges = sorted(
            (date.fromisoformat(window["covered_from"]), date.fromisoformat(window["covered_through"]))
            for window in account_windows
        )
        cursor = month_start
        for range_start, range_end in ranges:
            if range_start > cursor:
                break
            if range_end >= cursor:
                cursor = range_end.fromordinal(range_end.toordinal() + 1)
            if cursor > month_end:
                break
        covered = cursor > month_end
        if not covered:
            return "partial"
    return "complete"


def confirmed_allocations(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for transaction in transactions:
        for allocation in transaction.get("allocations", []):
            if allocation.get("decision") == "confirmed":
                result.append(allocation)
    return result


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


def compute_metrics(
    transactions: list[dict[str, Any]], accounts: list[str], period: dict[str, Any],
    coverage_status: str, links: list[dict[str, Any]], account_roles: dict[str, dict[str, Any]],
    other_commitment_transaction_ids: list[str] | None = None,
) -> dict[str, Any]:
    selected = transactions_in_scope(transactions, accounts, period)
    selected_ids = {transaction["id"] for transaction in selected}
    allocations = confirmed_allocations(selected)
    bank_net = sum(transaction["amount_cents"] for transaction in selected)
    regular_income = sum(a["amount_cents"] for a in allocations if a["flow_kind"] == "income_recurring")
    expense_signed = sum(a["amount_cents"] for a in allocations if a["flow_kind"] == "expense")
    matched_refunds = sum(
        link["amount_cents"] for link in links
        if link["from_transaction"] in selected_ids
    )
    consumption_net = -(expense_signed + matched_refunds)
    personal_signed = sum(a["amount_cents"] for a in allocations if a["flow_kind"] == "allocation_personal")
    personal_allowances = -personal_signed
    savings_signed = sum(
        transaction["amount_cents"] for transaction in selected
        if transaction["flow_kind"] == "transfer_savings"
        and transaction.get("savings_linked") is True
        and account_roles[transaction["account_id"]].get("role") == "current"
    )
    savings_net = -savings_signed

    residual_by_id: dict[str, int] = {}
    for transaction in selected:
        allocated = sum(
            allocation["amount_cents"] for allocation in transaction.get("allocations", [])
            if allocation.get("decision") == "confirmed"
        )
        residual_by_id[transaction["id"]] = transaction["amount_cents"] - allocated
    relevant = [transaction for transaction in selected if transaction["flow_kind"] in RELEVANT_FLOW_KINDS]
    relevant_residuals = [residual_by_id[transaction["id"]] for transaction in relevant]
    relevant_unclassified_signed = sum(relevant_residuals)
    relevant_unclassified_absolute = sum(abs(residual) for residual in relevant_residuals)
    relevant_gross = sum(abs(transaction["amount_cents"]) for transaction in relevant)
    unclassified_share = None if relevant_gross == 0 or coverage_status == "unknown" else round_ratio_half_away(
        relevant_unclassified_absolute, relevant_gross
    )

    total_count = len(selected)
    fully_classified_count = sum(1 for transaction in selected if residual_by_id[transaction["id"]] == 0)
    operation_coverage = None if total_count == 0 or coverage_status == "unknown" else round_ratio_half_away(
        fully_classified_count, total_count
    )

    selected_commitments = set(other_commitment_transaction_ids or [])
    require(selected_commitments <= selected_ids, "engagement sélectionné absent de la requête")
    require(all(
        next(t for t in selected if t["id"] == transaction_id)["flow_kind"] not in {"expense", "allocation_personal"}
        for transaction_id in selected_commitments
    ), "engagement sélectionné doublonne consommation ou allocation personnelle")
    commitments = sum(
        abs(transaction["amount_cents"]) for transaction in selected
        if transaction["id"] in selected_commitments and transaction["amount_cents"] < 0
    )
    has_relevant_residual = any(residual_by_id[transaction["id"]] != 0 for transaction in relevant)
    capacity = None if coverage_status != "complete" or has_relevant_residual else (
        regular_income - consumption_net - personal_allowances - commitments
    )
    saving_rate = None if capacity is None or regular_income <= 0 else round_ratio_half_away(capacity, regular_income)

    refund_total = sum(t["amount_cents"] for t in selected if t["flow_kind"] == "refund")
    matched_refund_total = sum(link["amount_cents"] for link in links if link["from_transaction"] in selected_ids)
    loan_repayment_outflow = sum(-t["amount_cents"] for t in selected if t["flow_kind"] == "loan_repayment" and t["amount_cents"] < 0)
    cash_pending_outflow = sum(-t["amount_cents"] for t in selected if t["flow_kind"] == "cash_withdrawal_pending" and t["amount_cents"] < 0)
    intermediary_pending_outflow = sum(-t["amount_cents"] for t in selected if t["flow_kind"] == "intermediary_pending" and t["amount_cents"] < 0)
    return {
        "bank_net_flow_cents": bank_net,
        "regular_income_cents": regular_income,
        "consumption_net_cents": consumption_net,
        "personal_allowances_cents": personal_allowances,
        "savings_net_transferred_cents": savings_net,
        "operational_saving_capacity_cents": capacity,
        "operational_saving_rate_bps": saving_rate,
        "unclassified_amount_share_bps": unclassified_share,
        "unclassified_signed_residual_cents": relevant_unclassified_signed,
        "unclassified_absolute_residual_cents": relevant_unclassified_absolute,
        "operation_coverage_bps": operation_coverage,
        "operation_count": total_count,
        "fully_classified_count": fully_classified_count,
        "loan_repayment_outflow_cents": loan_repayment_outflow,
        "cash_withdrawal_pending_outflow_cents": cash_pending_outflow,
        "intermediary_pending_outflow_cents": intermediary_pending_outflow,
        "matched_refunds_cents": matched_refund_total,
        "unmatched_refunds_cents": refund_total - matched_refund_total,
        "month_completeness": coverage_status,
        "residual_by_transaction": residual_by_id,
        "selected_ids": selected_ids,
    }


def compute_monthly_delta(
    example: dict[str, Any], query: dict[str, Any], current_metrics: dict[str, Any],
    category_flow_map: dict[str, set[str]],
) -> int | None:
    comparison = example.get("comparison")
    if not comparison:
        return None
    current_status = derive_month_completeness(example["period"], example.get("coverage_windows", []), query["accounts"])
    previous_status = derive_month_completeness(comparison["period"], comparison.get("coverage_windows", []), query["accounts"])
    if current_status != "complete" or previous_status != "complete":
        return None
    if not month_is_previous(example["period"], comparison["period"]):
        return None
    metric_id = query.get("delta_metric_definition_id")
    field = next((field for field, identifier in METRIC_FIELD_TO_ID.items() if identifier == metric_id), None)
    if field is None:
        return None
    prior_transactions = comparison.get("transactions", [])
    for transaction in prior_transactions:
        normalize_transaction(example["id"] + ".comparison", transaction, category_flow_map)
    prior_links = [
        link for link in example.get("transaction_links", [])
        if link["from_transaction"] in {t["id"] for t in prior_transactions}
    ]
    prior = compute_metrics(
        prior_transactions, query["accounts"], comparison["period"], previous_status,
        prior_links, example["accounts"], [],
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
        valid = all(percentage > 0 for percentage in percentages) and sum(percentages) == 10000
        actual: list[int] = []
        if valid:
            order = sorted(range(len(percentages)), key=lambda i: (-percentages[i], category_ids[i]))
            remaining = amount
            for position, original_index in enumerate(order):
                if position == len(order) - 1:
                    part = remaining
                else:
                    part = round_ratio_half_away(amount * percentages[original_index], 10000, 1)
                    remaining -= part
                actual.append(part)
            require(sum(actual) == amount, f"{where}: split non conservatif")
        expected_valid = split.get("expected_valid")
        require(type(expected_valid) is bool and valid == expected_valid, f"{where}: validité attendue {expected_valid}, calculée {valid}")
        expected = split.get("expected_allocations_cents")
        require(isinstance(expected, list), f"{where}: oracle allocations absent")
        expected_ints = [require_int(value, f"{where}.expected_allocations_cents") for value in expected]
        require(actual == expected_ints, f"{where}: allocations attendues {expected_ints}, calculées {actual}")
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
    examples_checked = 0
    splits_checked = 0
    policy_cases_checked = 0
    example_ids: set[str] = set()
    for example in examples:
        example_id = example.get("id")
        require(isinstance(example_id, str) and example_id not in example_ids, f"id d’exemple absent ou dupliqué: {example_id}")
        example_ids.add(example_id)
        splits_checked += verify_splits(example, category_flow_map)
        policy_cases_checked += verify_priority(example)
        if "transactions" not in example:
            continue
        require(example.get("coverage_status") in {"complete", "partial", "unknown"}, f"{example_id}: coverage_status invalide")
        accounts = example.get("accounts", {})
        require(isinstance(accounts, dict), f"{example_id}: comptes invalides")
        derived_root_status = derive_month_completeness(
            example["period"], example.get("coverage_windows", []), list(accounts)
        )
        require(
            derived_root_status == example["coverage_status"],
            f"{example_id}: statut calendrier déclaré {example['coverage_status']}, calculé {derived_root_status}",
        )
        comparison = example.get("comparison")
        if comparison:
            derived_comparison_status = derive_month_completeness(
                comparison["period"], comparison.get("coverage_windows", []), list(accounts)
            )
            require(
                derived_comparison_status == comparison.get("coverage_status"),
                f"{example_id}: couverture de comparaison déclarée {comparison.get('coverage_status')}, calculée {derived_comparison_status}",
            )
        transactions = example["transactions"]
        require(isinstance(transactions, list), f"{example_id}: transactions invalides")
        all_transactions: dict[str, dict[str, Any]] = {}
        for transaction in transactions:
            normalize_transaction(example_id, transaction, category_flow_map)
            require(transaction["id"] not in all_transactions, f"{example_id}: transaction dupliquée {transaction['id']}")
            require(transaction.get("account_id") in accounts, f"{example_id}.{transaction['id']}: compte absent")
            all_transactions[transaction["id"]] = transaction
        check_refund_links(example, all_transactions)

        for query in example.get("queries", []):
            require(set(query.get("accounts", [])) <= set(accounts), f"{example_id}.{query.get('id')}: compte de filtre absent")
            expected = query.get("expected", {})
            query_coverage_status = derive_month_completeness(
                example["period"], example.get("coverage_windows", []), query["accounts"]
            )
            calculated = compute_metrics(
                transactions, query["accounts"], example["period"], query_coverage_status,
                example.get("transaction_links", []), accounts,
                query.get("other_commitment_transaction_ids", []),
            )
            calculated["monthly_delta_cents"] = compute_monthly_delta(example, query, calculated, category_flow_map)
            for field, expected_value in expected.items():
                if field == "month_completeness":
                    actual = calculated[field]
                elif field in calculated:
                    actual = calculated[field]
                else:
                    raise VerificationError(f"{example_id}.{query.get('id')}: oracle inconnu {field}")
                if field.endswith("_cents") and expected_value is not None:
                    require_int(expected_value, f"{example_id}.{query.get('id')}.{field}")
                if field.endswith("_bps") and expected_value is not None:
                    require_int(expected_value, f"{example_id}.{query.get('id')}.{field}")
                require(actual == expected_value, f"{example_id}.{query.get('id')}.{field}: attendu {expected_value}, calculé {actual}")
                seen_metric_fields.add(field)
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
