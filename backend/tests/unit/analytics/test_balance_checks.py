from __future__ import annotations

from datetime import date, timedelta
from time import perf_counter
from typing import Any

from app.analytics.quality import coverage_summary, month_completeness
from app.services.balance_checks import calculate_balance_checks


def _section(
    section_id: str,
    account_id: str,
    start: str,
    complete_through: str,
    balance_date: str,
    balance: int,
) -> dict[str, Any]:
    return {
        "section_id": section_id,
        "account_id": account_id,
        "status": "committed",
        "coverage_from": start,
        "coverage_through": complete_through,
        "complete_through": complete_through,
        "balance_as_of": balance_date,
        "header_balance_cents": balance,
    }


def test_KPI_09_comparable_balances_match_and_one_cent_mismatch_is_visible() -> None:
    sections = [
        _section("opening", "current", "2026-01-01", "2026-01-31", "2025-12-31", 100_000),
        _section("closing", "current", "2026-01-01", "2026-01-31", "2026-01-31", 101_000),
    ]
    transactions = [
        {
            "transaction_id": "t1",
            "account_id": "current",
            "booked_on": "2026-01-05",
            "amount_cents": 600,
        },
        {
            "transaction_id": "t2",
            "account_id": "current",
            "booked_on": "2026-01-20",
            "amount_cents": 400,
        },
    ]

    matching = calculate_balance_checks("current", sections, transactions)
    assert len(matching) == 1
    assert matching[0].status == "match"
    assert matching[0].recalculated_balance_cents == 101_000
    assert matching[0].delta_cents == 0

    sections[1]["header_balance_cents"] = 101_001
    mismatching = calculate_balance_checks("current", sections, transactions)
    assert len(mismatching) == 1
    assert mismatching[0].status == "mismatch"
    assert mismatching[0].start_balance_cents == 100_000
    assert mismatching[0].end_balance_cents == 101_001
    assert mismatching[0].recalculated_balance_cents == 101_000
    assert mismatching[0].delta_cents == 1
    assert month_completeness("current", 2026, 1, sections, mismatching) == "unknown"


def test_balance_checks_are_order_independent_and_snapshot_is_canonical() -> None:
    sections = [
        _section("first", "current", "2026-01-01", "2026-01-31", "2025-12-31", 50_000),
        _section("last", "current", "2026-01-01", "2026-01-31", "2026-01-31", 50_025),
    ]
    transactions = [
        {
            "transaction_id": "t2",
            "account_id": "current",
            "booked_on": "2026-01-08",
            "amount_cents": 20,
        },
        {
            "transaction_id": "t1",
            "account_id": "current",
            "booked_on": "2026-01-06",
            "amount_cents": 5,
        },
    ]
    forward = calculate_balance_checks("current", sections, transactions)
    reversed_order = calculate_balance_checks(
        "current", list(reversed(sections)), list(reversed(transactions))
    )
    assert forward == reversed_order
    assert forward[0].status == "match"


def test_provisional_day_balance_is_not_comparable_and_never_a_false_mismatch() -> None:
    section = _section(
        "generated-today", "current", "2026-10-01", "2026-10-08", "2026-10-09", 900_000
    )
    result = calculate_balance_checks("current", [section], [])
    assert len(result) == 1
    assert result[0].status == "not_comparable"
    assert result[0].reason == "balance_after_complete_through"
    assert result[0].delta_cents is None


def test_missing_coverage_between_balances_is_not_comparable_not_mismatch() -> None:
    sections = [
        _section("first-range", "current", "2026-01-01", "2026-01-05", "2026-01-01", 10_000),
        _section("second-range", "current", "2026-01-07", "2026-01-10", "2026-01-10", 10_500),
    ]
    result = calculate_balance_checks("current", sections, [])
    assert len(result) == 1
    assert result[0].status == "not_comparable"
    assert result[0].reason == "coverage_discontinuous"
    assert result[0].recalculated_balance_cents is None
    assert month_completeness("current", 2026, 1, sections, result) == "partial"


def test_incomplete_or_conflicting_headers_are_explicitly_not_comparable() -> None:
    incomplete = {
        **_section(
            "incomplete",
            "current",
            "2026-01-01",
            "2026-01-31",
            "2026-01-20",
            10_000,
        ),
        "header_balance_cents": None,
    }
    incomplete_result = calculate_balance_checks("current", [incomplete], [])
    assert len(incomplete_result) == 1
    assert incomplete_result[0].status == "not_comparable"
    assert incomplete_result[0].reason == "incomplete_header_balance"
    assert incomplete_result[0].start_section_id == "incomplete"

    first = _section("first", "current", "2026-01-01", "2026-01-31", "2026-01-20", 10_000)
    second = _section("second", "current", "2026-01-01", "2026-01-31", "2026-01-20", 10_001)
    conflict = calculate_balance_checks("current", [first, second], [])
    assert len(conflict) == 1
    assert conflict[0].status == "not_comparable"
    assert conflict[0].reason == "conflicting_balances_same_day"
    assert {conflict[0].start_balance_cents, conflict[0].end_balance_cents} == {
        10_000,
        10_001,
    }


def test_one_account_never_uses_another_accounts_transactions_or_balance() -> None:
    sections = [
        _section("current-opening", "current", "2026-01-01", "2026-01-31", "2025-12-31", 10_000),
        _section("current-closing", "current", "2026-01-01", "2026-01-31", "2026-01-31", 10_100),
        _section("pea-opening", "pea", "2026-01-01", "2026-01-31", "2025-12-31", 90_000),
        _section("pea-closing", "pea", "2026-01-01", "2026-01-31", "2026-01-31", 90_200),
    ]
    transactions = [
        {"account_id": "current", "booked_on": date(2026, 1, 2), "amount_cents": 100},
        {"account_id": "pea", "booked_on": date(2026, 1, 2), "amount_cents": 200},
    ]
    current = calculate_balance_checks("current", sections, transactions)
    pea = calculate_balance_checks("pea", sections, transactions)
    assert [item.status for item in current] == ["match"]
    assert [item.status for item in pea] == ["match"]
    assert current[0].recalculated_balance_cents == 10_100
    assert pea[0].recalculated_balance_cents == 90_200


def test_quality_performance_100000_synthetic_transactions_is_measured() -> None:
    opening = _section("opening", "current", "2026-01-01", "2026-12-31", "2025-12-31", 100_000)
    closing = _section("closing", "current", "2026-01-01", "2026-12-31", "2026-12-31", 200_000)
    transactions = [
        {
            "transaction_id": f"synthetic-{index:06d}",
            "account_id": "current",
            "booked_on": date(2026, 1, 1) + timedelta(days=index % 365),
            "amount_cents": 1,
        }
        for index in range(100_000)
    ]
    started = perf_counter()
    summary = coverage_summary(
        ["current"],
        (date(2026, 1, 1), date(2026, 12, 31)),
        [opening, closing],
        transactions,
    )
    checks = calculate_balance_checks("current", [opening, closing], transactions)
    elapsed = perf_counter() - started
    assert summary["status"] == "complete"
    assert summary["operation_count"] == 100_000
    assert len(checks) == 1
    assert checks[0].status == "match"
    print(f"QUALITY_PERF transactions=100000 elapsed_seconds={elapsed:.6f}")
