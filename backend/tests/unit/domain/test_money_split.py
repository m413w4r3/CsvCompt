from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.money_split import (
    MoneySplitError,
    prorate_link,
    split_by_amounts,
    split_by_basis_points,
)


def test_MONEY_01_split_minus_10000_cents_exactly_70_30() -> None:
    assert split_by_basis_points(-10_000, [("groceries", 7_000), ("restaurants", 3_000)]) == {
        "groceries": -7_000,
        "restaurants": -3_000,
    }


def test_MONEY_02_invalid_basis_points_are_rejected_atomically() -> None:
    with pytest.raises(MoneySplitError, match="total exactly 10000"):
        split_by_basis_points(-10_000, [("groceries", 7_000), ("restaurants", 2_999)])


@pytest.mark.parametrize(
    ("amount_cents", "parts", "expected"),
    [
        (
            -1_101,
            [("groceries", 7_000), ("restaurants", 3_000)],
            {"groceries": -771, "restaurants": -330},
        ),
        (
            -10_001,
            [("groceries", 5_000), ("restaurants", 3_000), ("children", 2_000)],
            {"groceries": -5_001, "restaurants": -3_000, "children": -2_000},
        ),
    ],
)
def test_ADR_007_split_half_away_and_signed_remainder(
    amount_cents: int, parts: list[tuple[str, int]], expected: dict[str, int]
) -> None:
    assert split_by_basis_points(amount_cents, parts) == expected


def test_ADR_007_duplicate_categories_merge_and_category_order_is_canonical() -> None:
    split = split_by_basis_points(
        -10_000,
        [("groceries", 3_000), ("groceries", 4_000), ("restaurants", 3_000)],
    )
    assert list(split) == ["groceries", "restaurants"]
    assert split == {"groceries": -7_000, "restaurants": -3_000}

    tied = split_by_basis_points(-101, [("restaurants", 5_000), ("groceries", 5_000)])
    assert list(tied) == ["groceries", "restaurants"]
    assert tied == {"groceries": -51, "restaurants": -50}


def test_MONEY_02_zero_or_opposite_sign_split_part_rejects_whole_split() -> None:
    with pytest.raises(MoneySplitError, match="zero part"):
        split_by_basis_points(-1, [("groceries", 7_000), ("restaurants", 3_000)])
    with pytest.raises(MoneySplitError, match="wrong sign"):
        split_by_basis_points(
            -2,
            [("groceries", 2_500), ("restaurants", 2_500), ("children", 2_500), ("housing", 2_500)],
        )


def test_split_by_amounts_requires_an_exact_signed_total() -> None:
    assert split_by_amounts(-10_000, [("restaurants", -3_000), ("groceries", -7_000)]) == {
        "groceries": -7_000,
        "restaurants": -3_000,
    }
    with pytest.raises(MoneySplitError, match="sum exactly"):
        split_by_amounts(-10_000, [("groceries", -7_000), ("restaurants", -2_999)])
    with pytest.raises(MoneySplitError, match="source sign"):
        split_by_amounts(-10_000, [("groceries", -10_001), ("restaurants", 1)])


def test_ADR_024_3_prorates_refund_by_category_with_exact_remainder() -> None:
    assert prorate_link(3_001, [("groceries", -6_000), ("restaurants", -4_000)]) == {
        "groceries": 1_801,
        "restaurants": 1_200,
    }


@given(
    amount=st.integers(min_value=4, max_value=10**9),
    first_weight=st.integers(min_value=2_500, max_value=7_500),
    debit=st.booleans(),
)
def test_MONEY_01_properties_conserve_sign_and_ignore_input_order(
    amount: int, first_weight: int, debit: bool
) -> None:
    signed_amount = -amount if debit else amount
    parts = [("groceries", first_weight), ("restaurants", 10_000 - first_weight)]
    forward = split_by_basis_points(signed_amount, parts)
    backward = split_by_basis_points(signed_amount, list(reversed(parts)))
    assert forward == backward
    assert sum(forward.values()) == signed_amount
    assert all(value != 0 and (value > 0) == (signed_amount > 0) for value in forward.values())


@given(
    amount=st.integers(min_value=1, max_value=10**9),
    first_weight=st.integers(min_value=1, max_value=10**9),
    second_weight=st.integers(min_value=1, max_value=10**9),
)
def test_MONEY_10_prorata_properties_total_and_input_order(
    amount: int, first_weight: int, second_weight: int
) -> None:
    allocations = [("groceries", -first_weight), ("restaurants", -second_weight)]
    forward = prorate_link(amount, allocations)
    backward = prorate_link(amount, list(reversed(allocations)))
    assert forward == backward
    assert sum(forward.values()) == amount
    assert all(value >= 0 for value in forward.values())
