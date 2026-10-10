from __future__ import annotations

import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.flow_kinds import (
    CATEGORY_TO_FLOWS,
    FLOW_TO_CATEGORIES,
    allowed_categories,
    allowed_flow_kinds,
    expected_sign,
    is_category_allowed,
)
from app.domain.taxonomy import load_taxonomy
from app.domain.types import Cents, FlowKind

ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.unit
def test_RULE_00_code_matrix_matches_domain_model_exactly() -> None:
    text = (ROOT / "docs" / "DOMAIN_MODEL.md").read_text(encoding="utf-8")
    matrix = text.split("### Compatibilité exhaustive", 1)[1].split("## Remboursement", 1)[0]
    documented: set[tuple[str, str]] = set()
    for line in matrix.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 2:
            continue
        flow = cells[0].strip("`")
        documented.update((flow, category) for category in re.findall(r"`([^`]+)`", cells[1]))
    encoded = {
        (flow.value, category)
        for flow, categories in FLOW_TO_CATEGORIES.items()
        for category in categories
    }
    assert len({category for _flow, category in documented}) == 87
    assert len(documented) == 88  # income_benefits a deux flux admissibles.
    assert documented == encoded


@pytest.mark.unit
def test_RULE_00_every_taxonomy_id_has_admissible_flow_except_unknown() -> None:
    taxonomy = load_taxonomy(ROOT / "config" / "categories.v1.yaml")
    assert len(taxonomy.categories) == len(CATEGORY_TO_FLOWS)
    assert all(category.allowed_flow_kinds for category in taxonomy.categories)
    assert not allowed_categories(FlowKind.UNKNOWN)


@pytest.mark.unit
def test_domain_sign_expectations_follow_ADR_019e_and_domain_model() -> None:
    assert expected_sign(FlowKind.EXPENSE, "groceries") == -1
    assert expected_sign(FlowKind.TRANSFER_SAVINGS, "flow_savings_out") == -1
    assert expected_sign(FlowKind.TRANSFER_SAVINGS, "flow_savings_in") == 1
    assert expected_sign(FlowKind.LOAN_DISBURSEMENT, "flow_loan_disbursement") == 1
    assert expected_sign(FlowKind.LOAN_REPAYMENT, "flow_loan_principal") == -1
    assert expected_sign(FlowKind.RECONCILIATION, "flow_reconciliation") is None


@pytest.mark.unit
def test_Cents_rejects_float_and_bool() -> None:
    assert Cents(-123) == -123
    with pytest.raises(TypeError):
        Cents(1.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Cents(True)  # type: ignore[arg-type]


@pytest.mark.unit
@given(
    flow=st.sampled_from(list(FlowKind)),
    category=st.sampled_from(sorted(CATEGORY_TO_FLOWS)),
)
def test_flow_and_category_helpers_are_reciprocal(flow: FlowKind, category: str) -> None:
    assert is_category_allowed(flow, category) == (flow in allowed_flow_kinds(category))
    assert (category in allowed_categories(flow)) == is_category_allowed(flow, category)
