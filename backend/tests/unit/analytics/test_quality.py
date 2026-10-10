from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml

from app.analytics.quality import coverage_summary, covered_days, gaps, month_completeness
from app.services.balance_checks import calculate_balance_checks

ROOT = Path(__file__).resolve().parents[4]
EXAMPLES_PATH = ROOT / "docs" / "examples" / "metrics_examples.yaml"


def _section(
    section_id: str,
    start: str,
    through: str,
    complete_through: str,
    *,
    account_id: str = "current",
    **more: Any,
) -> dict[str, Any]:
    return {
        "section_id": section_id,
        "account_id": account_id,
        "status": "committed",
        "coverage_from": start,
        "coverage_through": through,
        "complete_through": complete_through,
        **more,
    }


def test_KPI_04_current_month_is_partial_and_previous_full_month_is_complete() -> None:
    sections = [
        _section("january", "2026-01-01", "2026-01-31", "2026-01-31"),
        _section(
            "october",
            "2026-10-01",
            "2026-10-09",
            "2026-10-08",
            generated_on="2026-10-09",
        ),
    ]
    assert month_completeness("current", 2026, 1, sections) == "complete"
    assert month_completeness("current", 2026, 10, sections) == "partial"
    full_october = _section("full-october", "2026-10-01", "2026-10-31", "2026-10-31")
    assert (
        month_completeness("current", 2026, 10, [full_october], as_of=date(2026, 10, 10))
        == "partial"
    )


def test_KPI_05_2026_only_leaves_2025_not_started_without_a_false_gap() -> None:
    sections = [_section("first-2026", "2026-01-01", "2026-02-28", "2026-02-28")]
    summary = coverage_summary(
        ["current"],
        (date(2025, 4, 1), date(2025, 12, 31)),
        sections,
    )
    assert summary["status"] == "not_started"
    assert summary["missing_days"] == []
    assert summary["complete_day_count"] == 0
    assert gaps(sections, date(2025, 4, 1), date(2025, 12, 31)) == []
    assert month_completeness("current", 2025, 4, sections) == "partial"


def test_KPI_05_a_real_gap_is_reported_only_between_attested_intervals() -> None:
    sections = [
        _section("first", "2026-01-01", "2026-01-03", "2026-01-03"),
        _section("second", "2026-01-07", "2026-01-10", "2026-01-10"),
    ]
    assert gaps(sections) == [(date(2026, 1, 4), date(2026, 1, 6))]
    summary = coverage_summary(["current"], (date(2026, 1, 1), date(2026, 1, 10)), sections)
    assert summary["missing_days"] == [{"from": date(2026, 1, 4), "through": date(2026, 1, 6)}]
    assert summary["complete_day_count"] == 7


def test_coverage_union_is_order_independent_idempotent_and_monotone() -> None:
    first = _section("first", "2026-01-01", "2026-01-10", "2026-01-10")
    second = _section("second", "2026-01-05", "2026-01-15", "2026-01-15")
    one_copy = covered_days([first, second])
    reordered = covered_days([second, first])
    duplicated = covered_days([first, first, second])
    assert one_copy == reordered == duplicated
    assert one_copy <= covered_days(
        [first, second, _section("third", "2026-01-16", "2026-01-20", "2026-01-20")]
    )


def test_multi_account_month_is_complete_only_when_each_account_is_complete() -> None:
    sections = [
        _section("current-full", "2026-01-01", "2026-01-31", "2026-01-31"),
        _section(
            "pea-partial",
            "2026-01-01",
            "2026-01-31",
            "2026-01-30",
            account_id="pea",
        ),
    ]
    assert month_completeness("current", 2026, 1, sections) == "complete"
    assert month_completeness(["current", "pea"], 2026, 1, sections) == "partial"


def test_KPI_09_mismatch_on_unselected_account_does_not_change_selected_month() -> None:
    sections = [
        _section("current", "2026-01-01", "2026-01-31", "2026-01-31"),
        _section("pea", "2026-01-01", "2026-01-31", "2026-01-31", account_id="pea"),
    ]
    pea_mismatch = {
        "account_id": "pea",
        "status": "mismatch",
        "start_balance_as_of": date(2025, 12, 31),
        "end_balance_as_of": date(2026, 1, 31),
    }
    assert month_completeness("current", 2026, 1, sections, [pea_mismatch]) == "complete"
    assert month_completeness(["current", "pea"], 2026, 1, sections, [pea_mismatch]) == "unknown"


def _oracle_inputs(example: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    transactions = example.get("transactions", [])
    operations_by_section: dict[str, list[date]] = {}
    for transaction in transactions:
        section_id = transaction.get("section_id")
        if section_id is not None:
            operations_by_section.setdefault(section_id, []).append(
                date.fromisoformat(transaction["booked_on"])
            )

    sections: list[dict[str, Any]] = []
    observations_by_section: dict[str, list[dict[str, Any]]] = {}
    for observation in example.get("balance_observations", []):
        observations_by_section.setdefault(observation["section_id"], []).append(observation)
    for section in example.get("coverage_sections", []):
        section_id = section["section_id"]
        operation_days = operations_by_section.get(section_id, [])
        base = {
            **section,
            "generated_on": section.get("generated_on"),
            "max_booked_on": max(operation_days) if operation_days else None,
            "balance_as_of": None,
            "header_balance_cents": None,
        }
        observations = observations_by_section.get(section_id, [])
        if not observations:
            sections.append(base)
            continue
        for index, observation in enumerate(observations):
            sections.append(
                {
                    **base,
                    "section_id": f"{section_id}#balance-{index}",
                    "balance_as_of": observation["balance_date"],
                    "header_balance_cents": observation["balance_cents"],
                }
            )
    return sections, transactions


def test_quality_reproduces_coverage_and_balance_oracles_without_editing_yaml() -> None:
    examples = yaml.safe_load(EXAMPLES_PATH.read_text(encoding="utf-8"))["examples"]
    selected_ids = {
        "KPI_04_partial_month",
        "ADR_018_balance_mismatch_unknown",
        "ADR_024_intraday_balance_not_comparable",
        "ADR_024_nonoverlapping_next_export_partial",
    }
    selected = {item["id"]: item for item in examples if item["id"] in selected_ids}
    assert selected.keys() == selected_ids

    for example_id, example in selected.items():
        sections, transactions = _oracle_inputs(example)
        period = example["period"]
        actual = month_completeness(
            list(example["accounts"]),
            date.fromisoformat(period["start"]).year,
            date.fromisoformat(period["start"]).month,
            sections,
            calculate_balance_checks("family_current", sections, transactions),
        )
        assert actual == example["coverage_status"], example_id
        for query in example.get("queries", []):
            expected_by_account = query.get("expected", {}).get(
                "expected_balance_check_status_by_account", {}
            )
            for account_id, expected_status in expected_by_account.items():
                statuses = {
                    check.status
                    for check in calculate_balance_checks(account_id, sections, transactions)
                }
                assert expected_status in statuses, (example_id, account_id, statuses)
