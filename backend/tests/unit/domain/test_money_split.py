from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.money_split import (
    MoneySplitError,
    prorate_link,
    prorate_link_bounded,
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


def test_MONEY_15_bounded_largest_remainder_handles_tiny_and_four_category_links() -> None:
    allocations = [
        ("groceries", -2_500),
        ("restaurants", -2_500),
        ("health", -2_500),
        ("housing", -2_500),
    ]
    assert prorate_link(2, allocations) == {
        "groceries": 1,
        "health": 1,
        "housing": 0,
        "restaurants": 0,
    }
    assert prorate_link(4, [(f"category-{index}", -1) for index in range(4)]) == {
        "category-0": 1,
        "category-1": 1,
        "category-2": 1,
        "category-3": 1,
    }


@st.composite
def _bounded_prorata_cases(draw: Any) -> tuple[list[tuple[str, int]], dict[str, int], int]:
    count = draw(st.integers(min_value=1, max_value=8))
    weights = [draw(st.integers(min_value=1, max_value=10**15)) for _ in range(count)]
    capacities = [draw(st.integers(min_value=1, max_value=weight)) for weight in weights]
    total_capacity = sum(capacities)
    amount = draw(
        st.one_of(
            st.integers(min_value=1, max_value=min(20, total_capacity)),
            st.integers(min_value=1, max_value=total_capacity),
            st.just(total_capacity),
            st.integers(min_value=total_capacity + 1, max_value=total_capacity + 10**15),
        )
    )
    allocations = [(f"category-{index:02d}", -weight) for index, weight in enumerate(weights)]
    caps = {f"category-{index:02d}": capacity for index, capacity in enumerate(capacities)}
    return allocations, caps, amount


@st.composite
def _bounded_link_sequences(
    draw: Any,
) -> tuple[list[tuple[str, int]], dict[str, int], list[int]]:
    count = draw(st.integers(min_value=1, max_value=8))
    weights = [draw(st.integers(min_value=1, max_value=10**15)) for _ in range(count)]
    capacities = [draw(st.integers(min_value=1, max_value=weight)) for weight in weights]
    total_capacity = sum(capacities)
    linked_total = draw(st.integers(min_value=1, max_value=total_capacity))
    link_count = draw(st.integers(min_value=1, max_value=min(6, linked_total)))
    if link_count == 1:
        amounts = [linked_total]
    else:
        boundaries = sorted(
            draw(
                st.lists(
                    st.integers(min_value=1, max_value=linked_total - 1),
                    min_size=link_count - 1,
                    max_size=link_count - 1,
                    unique=True,
                )
            )
        )
        points = [0, *boundaries, linked_total]
        amounts = [right - left for left, right in zip(points, points[1:], strict=False)]
    allocations = [(f"category-{index:02d}", -weight) for index, weight in enumerate(weights)]
    caps = {f"category-{index:02d}": capacity for index, capacity in enumerate(capacities)}
    return allocations, caps, amounts


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


@given(case=_bounded_prorata_cases())
def test_MONEY_15_prorata_is_bounded_exact_deterministic_and_conservative(
    case: tuple[list[tuple[str, int]], dict[str, int], int],
) -> None:
    allocations, capacities, amount = case
    total_capacity = sum(capacities.values())
    if amount > total_capacity:
        with pytest.raises(MoneySplitError, match="REFUND_PRORATA_INFEASIBLE"):
            prorate_link_bounded(amount, allocations, capacities)
        return

    forward = prorate_link_bounded(amount, allocations, capacities)
    backward = prorate_link_bounded(amount, list(reversed(allocations)), capacities)
    assert forward == backward
    assert sum(forward.values()) == amount
    assert set(forward) == set(capacities)
    assert all(0 <= value <= capacities[category] for category, value in forward.items())


def test_MONEY_15_prorata_refuses_when_remaining_capacity_is_insufficient() -> None:
    with pytest.raises(MoneySplitError, match="REFUND_PRORATA_INFEASIBLE"):
        prorate_link_bounded(
            3,
            [("groceries", -5), ("restaurants", -5)],
            {"groceries": 1, "restaurants": 1},
        )


@given(case=_bounded_link_sequences())
def test_MONEY_15_varied_link_orders_never_exceed_category_capacity(
    case: tuple[list[tuple[str, int]], dict[str, int], list[int]],
) -> None:
    allocations, initial_capacities, amounts = case
    orders: Iterable[list[int]] = (
        amounts,
        list(reversed(amounts)),
        sorted(amounts),
        sorted(amounts, reverse=True),
        amounts[1:] + amounts[:1],
    )
    for order in {tuple(candidate) for candidate in orders}:
        remaining = dict(initial_capacities)
        for amount in order:
            parts = prorate_link_bounded(amount, allocations, remaining)
            assert sum(parts.values()) == amount
            assert all(0 <= part <= remaining[category] for category, part in parts.items())
            remaining = {
                category: remaining[category] - parts[category] for category in remaining
            }
        assert all(capacity >= 0 for capacity in remaining.values())
        assert sum(initial_capacities.values()) - sum(remaining.values()) == sum(order)
