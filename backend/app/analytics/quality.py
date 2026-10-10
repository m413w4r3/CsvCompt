"""Couverture temporelle recalculée à partir des sections importées."""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Account, ImportAccountSection, ImportFile, SourceRow, Transaction

QualityRecord = Mapping[str, Any] | object
DateInterval = tuple[date, date]


def _value(record: QualityRecord, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError("date value required")


def _is_committed(section: QualityRecord) -> bool:
    return bool(_value(section, "status", "committed") == "committed")


def _bounds(section: QualityRecord) -> tuple[date | None, date | None]:
    return (
        _as_date(_value(section, "coverage_from", _value(section, "covered_from"))),
        _as_date(_value(section, "coverage_through", _value(section, "covered_through"))),
    )


def _complete_through(section: QualityRecord) -> date | None:
    """Resolve the immutable complete-day bound without trusting a status flag."""
    start, through = _bounds(section)
    if start is None or through is None or start > through:
        return None

    generated_on = _as_date(_value(section, "generated_on"))
    if generated_on is not None:
        calculated = min(through, generated_on - timedelta(days=1))
        stored = _as_date(_value(section, "complete_through"))
        return min(calculated, stored) if stored is not None else calculated

    max_booked_on = _as_date(_value(section, "max_booked_on"))
    if max_booked_on is not None:
        calculated = min(through, max_booked_on - timedelta(days=1))
        stored = _as_date(_value(section, "complete_through"))
        return min(calculated, stored) if stored is not None else calculated

    return _as_date(_value(section, "complete_through"))


def effective_complete_through(section: QualityRecord) -> date | None:
    """Public accessor for a section's recomputed complete-day boundary."""
    return _complete_through(section)


def _committed_for_account(
    sections: Iterable[QualityRecord], account_id: str
) -> list[QualityRecord]:
    return [
        section
        for section in sections
        if _is_committed(section) and _value(section, "account_id") == account_id
    ]


def covered_days(sections: Iterable[QualityRecord]) -> set[date]:
    """Return the union of inclusive complete-day intervals from committed sections."""
    result: set[date] = set()
    for section in sections:
        if not _is_committed(section):
            continue
        start, _through = _bounds(section)
        complete_through = _complete_through(section)
        if start is None or complete_through is None or start > complete_through:
            continue
        result.update(_days(start, complete_through))
    return result


def provisional_days(sections: Iterable[QualityRecord]) -> set[date]:
    """Return declared section days after its last complete day."""
    result: set[date] = set()
    for section in sections:
        if not _is_committed(section):
            continue
        start, through = _bounds(section)
        complete_through = _complete_through(section)
        if start is None or through is None or start > through or complete_through is None:
            continue
        first_provisional = max(start, complete_through + timedelta(days=1))
        if first_provisional <= through:
            result.update(_days(first_provisional, through))
    return result


def _days(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _merged_complete_intervals(sections: Iterable[QualityRecord]) -> list[DateInterval]:
    intervals: list[DateInterval] = []
    for section in sections:
        if not _is_committed(section):
            continue
        start, _through = _bounds(section)
        end = _complete_through(section)
        if start is not None and end is not None and start <= end:
            intervals.append((start, end))
    intervals.sort()
    merged: list[DateInterval] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def gaps(
    sections: Iterable[QualityRecord],
    period_start: date | None = None,
    period_end: date | None = None,
) -> list[DateInterval]:
    """Return uncovered ranges strictly between attested complete intervals.

    Days before the first complete interval and after the last interval are not
    gaps. ``period_start`` and ``period_end`` only clip interior gaps.
    """
    intervals = _merged_complete_intervals(sections)
    found: list[DateInterval] = []
    for (_left_start, left_end), (right_start, _right_end) in zip(
        intervals, intervals[1:], strict=False
    ):
        start, end = left_end + timedelta(days=1), right_start - timedelta(days=1)
        if period_start is not None:
            start = max(start, period_start)
        if period_end is not None:
            end = min(end, period_end)
        if start <= end:
            found.append((start, end))
    return found


def _account_ids(accounts: str | Iterable[str] | Iterable[QualityRecord]) -> list[str]:
    if isinstance(accounts, str):
        return [accounts]
    result: list[str] = []
    for account in accounts:
        account_id = account if isinstance(account, str) else _value(account, "account_id")
        if isinstance(account_id, str) and account_id not in result:
            result.append(account_id)
    return result


def _period_bounds(period: Mapping[str, Any] | tuple[date, date]) -> tuple[date, date]:
    if isinstance(period, Mapping):
        start = _as_date(period.get("start", period.get("period_start")))
        end = _as_date(period.get("end", period.get("period_end")))
    else:
        start, end = period
    if start is None or end is None or start > end:
        raise ValueError("period must contain an ordered start and end")
    return start, end


def _range_rows(days: Iterable[date]) -> list[dict[str, date]]:
    ordered = sorted(set(days))
    if not ordered:
        return []
    result: list[dict[str, date]] = []
    start = previous = ordered[0]
    for current in ordered[1:]:
        if current != previous + timedelta(days=1):
            result.append({"from": start, "through": previous})
            start = current
        previous = current
    result.append({"from": start, "through": previous})
    return result


def _check_touches_period(
    check: QualityRecord,
    start: date,
    end: date,
    account_ids: set[str] | None = None,
) -> bool:
    if _value(check, "status") != "mismatch":
        return False
    check_account = _value(check, "account_id")
    if account_ids is not None and check_account is not None and check_account not in account_ids:
        return False
    check_start = _as_date(_value(check, "start_balance_as_of"))
    check_end = _as_date(_value(check, "end_balance_as_of"))
    if check_start is not None and check_end is not None:
        return check_start < end and check_end >= start
    observed = check_end or check_start or _as_date(_value(check, "balance_as_of"))
    return observed is not None and start <= observed <= end


def coverage_summary(
    accounts: str | Iterable[str] | Iterable[QualityRecord],
    period: Mapping[str, Any] | tuple[date, date],
    sections: Iterable[QualityRecord],
    transactions: Iterable[QualityRecord] = (),
    balance_checks: Iterable[QualityRecord] = (),
) -> dict[str, Any]:
    """Summarize exact temporal coverage for the selected accounts and period."""
    start, end = _period_bounds(period)
    account_ids = _account_ids(accounts)
    account_scope = set(account_ids)
    section_rows = list(sections)
    transaction_rows = list(transactions)
    check_rows = list(balance_checks)
    if not account_ids:
        return {
            "status": "unknown",
            "covered_from": None,
            "covered_through": None,
            "complete_day_count": 0,
            "missing_days": [],
            "provisional_days": [],
            "operation_count": 0,
            "provisional_operation_count": 0,
            "complete_intervals_by_account": {},
            "sections": [],
        }

    days_in_period = list(_days(start, end))
    intervals_by_account: dict[str, list[DateInterval]] = {}
    covered_by_account: dict[str, set[date]] = {}
    account_sections: dict[str, list[QualityRecord]] = {}
    first_known_by_account: dict[str, date | None] = {}
    unknown_range_by_account: dict[str, bool] = {}
    provisional_by_account: dict[str, set[date]] = {}
    provisional: set[date] = set()
    for account_id in account_ids:
        rows = _committed_for_account(section_rows, account_id)
        account_sections[account_id] = rows
        known_starts = [
            section_start for section in rows if (section_start := _bounds(section)[0]) is not None
        ]
        first_known = min(known_starts) if known_starts else None
        first_known_by_account[account_id] = first_known
        unknown_range = False
        for section in rows:
            section_start, section_end = _bounds(section)
            if section_start is None or section_end is None:
                unknown_range = True
                break
            if section_start <= end and section_end >= start and _complete_through(section) is None:
                unknown_range = True
                break
        unknown_range_by_account[account_id] = unknown_range
        intervals = _merged_complete_intervals(rows)
        intervals_by_account[account_id] = intervals
        day_set = covered_days(rows)
        covered_by_account[account_id] = day_set
        account_provisional = provisional_days(rows)
        provisional_by_account[account_id] = account_provisional
        provisional.update(day for day in account_provisional if start <= day <= end)

    common_days = {
        day
        for day in days_in_period
        if all(day in covered_by_account[account_id] for account_id in account_ids)
    }
    missing: set[date] = set()
    for account_id in account_ids:
        first_known = first_known_by_account[account_id]
        if first_known is None:
            continue
        for day in days_in_period:
            if day >= first_known and day not in covered_by_account[account_id]:
                missing.add(day)

    mismatch = any(_check_touches_period(check, start, end, account_scope) for check in check_rows)
    no_sections = any(not account_sections[account_id] for account_id in account_ids)
    no_known_range = any(
        account_sections[account_id] and first_known_by_account[account_id] is None
        for account_id in account_ids
    )
    before_all_known = all(
        (known_from := first_known_by_account[account_id]) is not None and end < known_from
        for account_id in account_ids
    )
    unknown_evidence = any(
        unknown_range_by_account[account_id] and not before_all_known for account_id in account_ids
    )
    if mismatch or no_sections or no_known_range or unknown_evidence:
        status = "unknown"
    elif before_all_known:
        status = "not_started"
    elif len(common_days) == len(days_in_period):
        status = "complete"
    else:
        status = "partial"

    actual_covered = sorted(day for day in common_days if start <= day <= end)
    complete_intervals = {
        account_id: [
            {"from": max(interval_start, start), "through": min(interval_end, end)}
            for interval_start, interval_end in intervals_by_account[account_id]
            if interval_start <= end and interval_end >= start
        ]
        for account_id in account_ids
    }
    selected_transactions = [
        transaction
        for transaction in transaction_rows
        if _value(transaction, "account_id") in account_scope
        and (booked_on := _as_date(_value(transaction, "booked_on"))) is not None
        and start <= booked_on <= end
    ]
    operation_count = len(selected_transactions)
    provisional_operation_count = sum(
        1
        for transaction in selected_transactions
        if (booked_on := _as_date(_value(transaction, "booked_on"))) is not None
        and booked_on in provisional_by_account[str(_value(transaction, "account_id"))]
        and booked_on not in covered_by_account[str(_value(transaction, "account_id"))]
    )
    section_summaries: list[dict[str, Any]] = []
    for account_id in account_ids:
        for section in account_sections[account_id]:
            covered_from, covered_through = _bounds(section)
            section_summaries.append(
                {
                    "section_id": _value(section, "section_id"),
                    "account_id": account_id,
                    "covered_from": covered_from,
                    "covered_through": covered_through,
                    "complete_through": _complete_through(section),
                    "generated_on": _as_date(_value(section, "generated_on")),
                    "generation_date_source": _value(section, "generation_date_source"),
                    "status": "committed",
                    "provisional_days": sorted(
                        day for day in provisional_days([section]) if start <= day <= end
                    ),
                }
            )
    section_summaries.sort(key=lambda item: (item["account_id"], str(item["section_id"])))

    if actual_covered:
        covered_from, covered_through = actual_covered[0], actual_covered[-1]
    else:
        covered_from = covered_through = None
    return {
        "status": status,
        "covered_from": covered_from,
        "covered_through": covered_through,
        "complete_day_count": len(common_days),
        "missing_days": _range_rows(missing),
        "provisional_days": sorted(provisional),
        "operation_count": operation_count,
        "provisional_operation_count": provisional_operation_count,
        "complete_intervals_by_account": complete_intervals,
        "sections": section_summaries,
    }


def month_completeness(
    account_id: str | Iterable[str] | Iterable[QualityRecord],
    year: int,
    month: int,
    sections: Iterable[QualityRecord],
    balance_checks: Iterable[QualityRecord] = (),
    *,
    as_of: date | None = None,
) -> str:
    """Return ``complete``, ``partial`` or ``unknown`` for a calendar month."""
    if not 1 <= month <= 12:
        raise ValueError("month must be between 1 and 12")
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    ids = _account_ids(account_id)
    if not ids:
        return "unknown"
    rows = list(sections)
    checks = list(balance_checks)
    if any(_check_touches_period(check, start, end, set(ids)) for check in checks):
        return "unknown"

    unknown_evidence = False
    all_complete = True
    month_days = set(_days(start, end))
    for selected_account in ids:
        selected_sections = _committed_for_account(rows, selected_account)
        if not selected_sections:
            return "unknown"
        if any(
            _bounds(section)[0] is None or _bounds(section)[1] is None
            for section in selected_sections
        ):
            unknown_evidence = True
        for section in selected_sections:
            section_start, section_end = _bounds(section)
            if (
                section_start is not None
                and section_end is not None
                and section_start <= end
                and section_end >= start
                and effective_complete_through(section) is None
            ):
                unknown_evidence = True
        if not month_days.issubset(covered_days(selected_sections)):
            all_complete = False
    if unknown_evidence:
        return "unknown"
    if as_of is not None and (year, month) == (as_of.year, as_of.month):
        return "partial"
    return "complete" if all_complete else "partial"


def load_quality_snapshot(
    session: Session, account_ids: Sequence[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load only committed section metadata and transaction dates/cents."""
    unique_ids = list(dict.fromkeys(account_ids))
    if not unique_ids:
        return [], []

    section_rows = session.execute(
        select(
            ImportAccountSection,
            ImportFile.generation_date,
            ImportFile.generation_date_source,
            ImportFile.status,
        )
        .join(ImportFile, ImportFile.import_file_id == ImportAccountSection.import_file_id)
        .where(
            ImportAccountSection.account_id.in_(unique_ids),
            ImportFile.status == "committed",
        )
    ).all()
    transactions = session.execute(
        select(
            Transaction.transaction_id,
            Transaction.account_id,
            Transaction.booked_on,
            Transaction.amount_cents,
        ).where(Transaction.account_id.in_(unique_ids))
    ).all()
    max_by_section: dict[tuple[str, int], date] = {}
    max_rows = session.execute(
        select(SourceRow.import_file_id, SourceRow.section_index, func.max(Transaction.booked_on))
        .join(Transaction, Transaction.transaction_id == SourceRow.transaction_id)
        .where(Transaction.account_id.in_(unique_ids))
        .group_by(SourceRow.import_file_id, SourceRow.section_index)
    ).all()
    for import_file_id, section_index, maximum in max_rows:
        parsed = _as_date(maximum)
        if parsed is not None:
            max_by_section[(import_file_id, section_index)] = parsed

    sections: list[dict[str, Any]] = []
    for section, generation_date, generation_source, status in section_rows:
        generated_on = generation_date if generation_source == "header_download" else None
        max_booked_on = max_by_section.get((section.import_file_id, section.section_index))
        through = section.coverage_through
        calculated: date | None = None
        if through is not None and generated_on is not None:
            calculated = min(through, generated_on - timedelta(days=1))
        elif through is not None and max_booked_on is not None:
            calculated = min(through, max_booked_on - timedelta(days=1))
        elif through is not None:
            calculated = section.complete_through
        stored = section.complete_through
        if calculated is not None and stored is not None:
            calculated = min(calculated, stored)
        sections.append(
            {
                "section_id": f"{section.import_file_id}:{section.section_index}",
                "import_file_id": section.import_file_id,
                "section_index": section.section_index,
                "account_id": section.account_id,
                "coverage_from": section.coverage_from,
                "coverage_through": section.coverage_through,
                "complete_through": calculated,
                "balance_as_of": section.balance_as_of,
                "header_balance_cents": section.header_balance_cents,
                "generated_on": generated_on,
                "received_on": generation_date
                if generation_source == "received_on_fallback"
                else None,
                "generation_date_source": generation_source,
                "max_booked_on": max_booked_on,
                "status": status,
            }
        )
    sections.sort(key=lambda item: (item["account_id"] or "", item["section_id"]))
    transactions_data = [
        {
            "transaction_id": transaction_id,
            "account_id": account_id,
            "booked_on": _as_date(booked_on),
            "amount_cents": amount_cents,
        }
        for transaction_id, account_id, booked_on, amount_cents in transactions
    ]
    transactions_data.sort(
        key=lambda item: (item["account_id"], item["booked_on"], item["transaction_id"])
    )
    return sections, transactions_data


def load_accounts(session: Session) -> list[Account]:
    """Return accounts in a stable display order, without fingerprint fields."""
    return list(session.scalars(select(Account).order_by(Account.alias, Account.account_id)).all())
