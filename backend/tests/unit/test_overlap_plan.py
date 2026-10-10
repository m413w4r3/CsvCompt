from __future__ import annotations

from collections import Counter, defaultdict

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.importers.overlap import (
    ExistingOccurrence,
    IncomingOccurrence,
    plan_reconciliation,
)


@pytest.mark.unit
@pytest.mark.parametrize("n_existing", range(5))
@pytest.mark.parametrize("n_new", range(5))
def test_IMP_03_IMP_04_maximum_multiplicity_assigns_each_duplicate_once(
    n_existing: int, n_new: int
) -> None:
    identity = ("account-a", "2026-03-12", -1123, "Paiement répété")
    existing = [
        ExistingOccurrence(identity, rank, f"tx-{rank}") for rank in range(1, n_existing + 1)
    ]
    incoming = [IncomingOccurrence(identity, row) for row in range(1, n_new + 1)]

    plan = plan_reconciliation(existing, incoming)

    assert plan.can_commit
    if n_existing == 0 and n_new == 0:
        assert plan.identities == ()
        return
    group = plan.identities[0]
    assert group.n_existing == n_existing
    assert group.n_new == n_new
    assert group.create_ranks == tuple(range(n_existing + 1, max(n_existing, n_new) + 1))
    assert [item.occurrence_rank for item in group.assignments] == list(range(1, n_new + 1))
    assert max(n_existing, n_new) == n_existing + len(group.create_ranks)


@pytest.mark.unit
def test_IMP_14_rank_holes_are_blocking_and_never_filled_implicitly() -> None:
    identity = ("account-a", "2026-03-12", -1123, "Paiement répété")
    existing = [
        ExistingOccurrence(identity, 1, "tx-1"),
        ExistingOccurrence(identity, 3, "tx-3"),
    ]
    plan = plan_reconciliation(existing, [IncomingOccurrence(identity, 1)])
    assert not plan.can_commit
    assert plan.anomalies[0].code == "NON_CONTIGUOUS_EXISTING_RANKS"
    assert plan.anomalies[0].actual_count == 2
    assert plan.anomalies[0].expected_max_rank == 3


@pytest.mark.unit
@given(
    existing_ranks=st.integers(min_value=0, max_value=4),
    incoming_count=st.integers(min_value=0, max_value=4),
    order=st.permutations((0, 1, 2, 3)),
)
@settings(max_examples=80, deadline=None)
def test_IMP_05_row_permutation_does_not_change_transaction_set(
    existing_ranks: int, incoming_count: int, order: tuple[int, ...]
) -> None:
    identity = ("account-a", "2026-03-12", 1123, "Crédit strict")
    existing = [
        ExistingOccurrence(identity, rank, f"tx-{rank}") for rank in range(1, existing_ranks + 1)
    ]
    indexes = list(range(1, incoming_count + 1))
    first = plan_reconciliation(existing, [IncomingOccurrence(identity, idx) for idx in indexes])
    permuted_indexes = [indexes[position] for position in order if position < len(indexes)]
    permuted = plan_reconciliation(
        list(reversed(existing)),
        [IncomingOccurrence(identity, idx) for idx in permuted_indexes],
    )

    assert first.can_commit and permuted.can_commit
    if existing_ranks == 0 and incoming_count == 0:
        assert first.identities == permuted.identities == ()
        return
    assert first.identities[0].create_ranks == permuted.identities[0].create_ranks
    assert [
        (assignment.logical_row_idx, assignment.occurrence_rank, assignment.transaction_id)
        for assignment in first.assignments
    ] == [
        (assignment.logical_row_idx, assignment.occurrence_rank, assignment.transaction_id)
        for assignment in permuted.assignments
    ]


@pytest.mark.unit
@given(
    day_counts=st.lists(st.integers(min_value=0, max_value=4), min_size=1, max_size=6),
    exports=st.lists(
        st.sets(st.integers(min_value=0, max_value=5), min_size=0, max_size=6),
        min_size=1,
        max_size=6,
    ),
)
@settings(max_examples=100, deadline=None)
def test_IMP_05_N_complete_day_exports_match_independent_truth_oracle(
    day_counts: list[int], exports: list[set[int]]
) -> None:
    """Whole-day subsets preserve truth because every observed day is complete."""

    truth = {
        ("account-a", f"2026-03-{day + 1:02d}", -100 - day, f"Payment {day}"): count
        for day, count in enumerate(day_counts)
    }
    stored: dict[tuple[str, str, int, str], int] = defaultdict(int)
    for export_index, days in enumerate(exports):
        incoming: list[IncomingOccurrence] = []
        logical_idx = 0
        for day in sorted(days):
            if day >= len(day_counts):
                continue
            identity = list(truth)[day]
            for _ in range(day_counts[day]):
                logical_idx += 1
                incoming.append(IncomingOccurrence(identity, logical_idx))
        existing = [
            ExistingOccurrence(identity, rank, f"{export_index}-{identity[1]}-{rank}")
            for identity, count in stored.items()
            for rank in range(1, count + 1)
        ]
        plan = plan_reconciliation(existing, incoming)
        assert plan.can_commit
        for group in plan.identities:
            stored[group.identity_key] = group.n_existing + len(group.create_ranks)

    covered_days = set().union(*exports)
    expected = {
        list(truth)[day]: day_counts[day]
        for day in covered_days
        if day < len(day_counts) and day_counts[day] > 0
    }
    assert {key: value for key, value in stored.items() if key in expected} == expected
    assert all(value <= 4 for value in stored.values())


@pytest.mark.unit
def test_sign_opposites_and_case_changes_remain_distinct_identity_groups() -> None:
    debit = ("account-a", "2026-03-12", -1123, "CAFÉ")
    credit = ("account-a", "2026-03-12", 1123, "CAFÉ")
    lower = ("account-a", "2026-03-12", -1123, "café")
    plan = plan_reconciliation(
        [],
        [
            IncomingOccurrence(debit, 1),
            IncomingOccurrence(credit, 2),
            IncomingOccurrence(lower, 3),
        ],
    )
    assert len(plan.identities) == 3
    assert Counter(group.n_new for group in plan.identities) == {1: 3}
