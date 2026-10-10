"""Matrice exhaustive des catégories admissibles par flux (ADR-005/020)."""

from __future__ import annotations

from types import MappingProxyType

from app.domain.types import FlowKind

_EXPENSE_IDS = frozenset(
    {
        "groceries",
        "groceries_supermarket",
        "groceries_organic",
        "groceries_frozen",
        "groceries_bakery",
        "restaurants",
        "restaurants_dining",
        "restaurants_delivery",
        "restaurants_fastfood",
        "housing",
        "housing_energy",
        "housing_water",
        "housing_copro",
        "housing_tax",
        "housing_insurance",
        "housing_maintenance",
        "home",
        "home_furniture",
        "home_appliances",
        "home_diy",
        "home_supplies",
        "children",
        "children_nanny",
        "children_daycare",
        "children_baby",
        "children_school",
        "children_activities",
        "clothing",
        "clothing_children",
        "clothing_adults",
        "clothing_footwear",
        "clothing_unspecified",
        "health",
        "health_visits",
        "health_pharmacy",
        "health_optical",
        "health_other",
        "transport",
        "transport_fuel",
        "transport_tolls",
        "transport_public",
        "transport_vehicle",
        "transport_taxis",
        "household_services",
        "household_cleaning",
        "household_help",
        "household_oneoff",
        "subscriptions",
        "subscriptions_telecom",
        "subscriptions_streaming",
        "subscriptions_apps",
        "subscriptions_other",
        "leisure",
        "leisure_sports",
        "leisure_culture",
        "leisure_travel",
        "leisure_family",
        "pets",
        "pets_vet",
        "pets_food",
        "pets_care",
        "admin",
        "admin_bank",
        "admin_insurance",
        "admin_tax",
        "admin_other",
        "gifts",
        "gifts_present",
        "gifts_donation",
    }
)

_FLOW_TO_CATEGORIES: dict[FlowKind, frozenset[str]] = {
    FlowKind.EXPENSE: _EXPENSE_IDS,
    FlowKind.INCOME_RECURRING: frozenset(
        {"income_salary_a", "income_salary_b", "income_benefits", "income_regular_other"}
    ),
    FlowKind.INCOME_OTHER: frozenset({"income_benefits", "income_extraordinary"}),
    FlowKind.TRANSFER_SAVINGS: frozenset({"flow_savings_out", "flow_savings_in"}),
    FlowKind.TRANSFER_OWNED: frozenset({"flow_owned_transfer"}),
    FlowKind.ALLOCATION_PERSONAL: frozenset({"flow_personal_a", "flow_personal_b"}),
    FlowKind.LOAN_DISBURSEMENT: frozenset({"flow_loan_disbursement"}),
    FlowKind.LOAN_REPAYMENT: frozenset({"flow_loan_principal", "flow_loan_payment_unknown_split"}),
    FlowKind.CAPITAL_EVENT: frozenset({"flow_capital_improvement"}),
    FlowKind.CASH_WITHDRAWAL_PENDING: frozenset({"flow_cash_unallocated"}),
    FlowKind.REFUND: frozenset({"income_refund"}),
    FlowKind.INTERMEDIARY_PENDING: frozenset({"flow_intermediary_unresolved"}),
    FlowKind.RECONCILIATION: frozenset({"flow_reconciliation"}),
    FlowKind.UNKNOWN: frozenset(),
}
FLOW_TO_CATEGORIES = MappingProxyType(_FLOW_TO_CATEGORIES)

_CATEGORY_TO_FLOWS = {
    category_id: frozenset(flow for flow, ids in _FLOW_TO_CATEGORIES.items() if category_id in ids)
    for category_id in set().union(*_FLOW_TO_CATEGORIES.values())
}
CATEGORY_TO_FLOWS = MappingProxyType(_CATEGORY_TO_FLOWS)

_NEGATIVE_ONLY = frozenset(
    {
        FlowKind.EXPENSE,
        FlowKind.ALLOCATION_PERSONAL,
        FlowKind.LOAN_REPAYMENT,
        FlowKind.CAPITAL_EVENT,
        FlowKind.CASH_WITHDRAWAL_PENDING,
        FlowKind.INTERMEDIARY_PENDING,
    }
)
_POSITIVE_ONLY = frozenset(
    {
        FlowKind.INCOME_RECURRING,
        FlowKind.INCOME_OTHER,
        FlowKind.LOAN_DISBURSEMENT,
        FlowKind.REFUND,
    }
)


def _flow(flow_kind: FlowKind | str) -> FlowKind:
    return flow_kind if isinstance(flow_kind, FlowKind) else FlowKind(flow_kind)


def is_category_allowed(flow_kind: FlowKind | str, category_id: str) -> bool:
    """Indique si la paire apparaît exactement dans la matrice contractuelle."""
    try:
        flow = _flow(flow_kind)
    except ValueError:
        return False
    return category_id in FLOW_TO_CATEGORIES[flow]


def allowed_categories(flow_kind: FlowKind | str) -> frozenset[str]:
    """Renvoie les IDs admissibles; unknown renvoie toujours l'ensemble vide."""
    return FLOW_TO_CATEGORIES[_flow(flow_kind)]


def allowed_flow_kinds(category_id: str) -> frozenset[FlowKind]:
    """Renvoie les flux admissibles pour un ID, ou l'ensemble vide si inconnu."""
    return CATEGORY_TO_FLOWS.get(category_id, frozenset())


def expected_sign(flow_kind: FlowKind | str, category_id: str) -> int | None:
    """Signe d'allocation attendu: -1, +1 ou None si les deux signes sont possibles."""
    flow = _flow(flow_kind)
    if not is_category_allowed(flow, category_id):
        raise ValueError(f"Catégorie {category_id!r} interdite pour le flux {flow.value!r}")
    if flow in _NEGATIVE_ONLY:
        return -1
    if flow in _POSITIVE_ONLY:
        return 1
    if category_id == "flow_savings_out":
        return -1
    if category_id == "flow_savings_in" or category_id == "flow_loan_disbursement":
        return 1
    if category_id == "flow_loan_principal":
        return -1
    return None
