"""Recalcule à la lecture la cohérence des soldes d'en-tête."""

from __future__ import annotations

import hashlib
import uuid
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from app.analytics.quality import effective_complete_through

BalanceStatus = Literal["match", "mismatch", "not_comparable"]
BalanceRecord = Mapping[str, Any] | object
_BALANCE_NAMESPACE = uuid.UUID("d7f1bf10-20e3-4cdf-b3de-016c0b6af963")


def _value(record: BalanceRecord, name: str, default: Any = None) -> Any:
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


def _frame(digest: Any, value: str | None) -> None:
    encoded = b"\xff" if value is None else value.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def input_snapshot_sha256(
    account_id: str,
    sections: Iterable[BalanceRecord],
    transactions: Iterable[BalanceRecord],
) -> str:
    """Hash a length-framed canonical snapshot, independent of input order."""
    section_rows = [
        section
        for section in sections
        if _value(section, "account_id") == account_id
        and _value(section, "status", "committed") == "committed"
    ]
    transaction_rows = [
        transaction
        for transaction in transactions
        if _value(transaction, "account_id") == account_id
    ]
    section_fields: list[tuple[str | None, ...]] = []
    for section in section_rows:
        coverage_from = _as_date(_value(section, "coverage_from"))
        coverage_through = _as_date(_value(section, "coverage_through"))
        complete_through = _as_date(_value(section, "complete_through"))
        balance_as_of = _as_date(_value(section, "balance_as_of"))
        section_fields.append(
            (
                str(_value(section, "import_file_id", _value(section, "section_id", ""))),
                str(_value(section, "section_index", "")),
                account_id,
                coverage_from.isoformat() if coverage_from else None,
                coverage_through.isoformat() if coverage_through else None,
                complete_through.isoformat() if complete_through else None,
                balance_as_of.isoformat() if balance_as_of else None,
                str(_value(section, "header_balance_cents"))
                if _value(section, "header_balance_cents") is not None
                else None,
            )
        )
    transaction_fields: list[tuple[str, str]] = []
    for transaction in transaction_rows:
        booked_on = _as_date(_value(transaction, "booked_on"))
        amount_cents = _value(transaction, "amount_cents")
        if booked_on is None or type(amount_cents) is not int:
            continue
        transaction_fields.append((booked_on.isoformat(), str(amount_cents)))

    digest = hashlib.sha256()
    _frame(digest, "account")
    _frame(digest, account_id)
    for fields in sorted(section_fields):
        _frame(digest, "section")
        for value in fields:
            _frame(digest, value)
    for fields in sorted(transaction_fields):
        _frame(digest, "transaction")
        for value in fields:
            _frame(digest, value)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class BalanceCheck:
    balance_check_id: str
    account_id: str
    status: BalanceStatus
    start_balance_as_of: date | None
    end_balance_as_of: date | None
    start_balance_cents: int | None
    end_balance_cents: int | None
    recalculated_balance_cents: int | None
    delta_cents: int | None
    start_section_id: str | None
    end_section_id: str | None
    input_snapshot_sha256: str
    version: int
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Observation:
    section_id: str
    as_of: date
    balance_cents: int


def _section_id(section: BalanceRecord) -> str:
    explicit = _value(section, "section_id")
    if explicit is not None:
        return str(explicit)
    return f"{_value(section, 'import_file_id', '')}:{_value(section, 'section_index', '')}"


def _check_id(
    account_id: str,
    snapshot: str,
    status: str,
    start_section_id: str | None,
    end_section_id: str | None,
    start_day: date | None,
    end_day: date | None,
    reason: str | None,
) -> str:
    parts = (
        account_id,
        snapshot,
        status,
        start_section_id or "",
        end_section_id or "",
        start_day.isoformat() if start_day else "",
        end_day.isoformat() if end_day else "",
        reason or "",
    )
    return str(uuid.uuid5(_BALANCE_NAMESPACE, "\0".join(parts)))


def _merge_intervals(sections: Iterable[BalanceRecord]) -> list[tuple[date, date]]:
    intervals: list[tuple[date, date]] = []
    for section in sections:
        if _value(section, "status", "committed") != "committed":
            continue
        start = _as_date(_value(section, "coverage_from", _value(section, "covered_from")))
        end = effective_complete_through(section)
        if start is not None and end is not None and start <= end:
            intervals.append((start, end))
    intervals.sort()
    merged: list[tuple[date, date]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _continuous_coverage(start: date, end: date, intervals: list[tuple[date, date]]) -> bool:
    if start >= end:
        return False
    needed_start = start + timedelta(days=1)
    return any(
        interval_start <= needed_start and interval_end >= end
        for interval_start, interval_end in intervals
    )


def _record(
    *,
    account_id: str,
    snapshot: str,
    status: BalanceStatus,
    start: _Observation | None,
    end: _Observation | None,
    recalculated: int | None = None,
    delta: int | None = None,
    reason: str | None = None,
    start_section_override: str | None = None,
    end_section_override: str | None = None,
) -> BalanceCheck:
    start_day = start.as_of if start else None
    end_day = end.as_of if end else None
    start_section = start.section_id if start else start_section_override
    end_section = end.section_id if end else end_section_override
    return BalanceCheck(
        balance_check_id=_check_id(
            account_id,
            snapshot,
            status,
            start_section,
            end_section,
            start_day,
            end_day,
            reason,
        ),
        account_id=account_id,
        status=status,
        start_balance_as_of=start_day,
        end_balance_as_of=end_day,
        start_balance_cents=start.balance_cents if start else None,
        end_balance_cents=end.balance_cents if end else None,
        recalculated_balance_cents=recalculated,
        delta_cents=delta,
        start_section_id=start_section,
        end_section_id=end_section,
        input_snapshot_sha256=snapshot,
        version=1,
        reason=reason,
    )


def _in_period(check: BalanceCheck, period_start: date | None, period_end: date | None) -> bool:
    if period_start is None and period_end is None:
        return True
    start = check.start_balance_as_of
    end = check.end_balance_as_of
    if start is None:
        start = end
    if end is None:
        end = start
    if start is None or end is None:
        return True
    low = period_start or date.min
    high = period_end or date.max
    if start == end:
        return low <= start <= high
    return start < high and end >= low


def calculate_balance_checks(
    account_id: str,
    sections: Iterable[BalanceRecord],
    transactions: Iterable[BalanceRecord],
    *,
    period_start: date | None = None,
    period_end: date | None = None,
) -> list[BalanceCheck]:
    """Calculate comparable successive balances in date order, without writes."""
    if period_start is not None and period_end is not None and period_start > period_end:
        raise ValueError("period_start must be on or before period_end")
    section_rows = [
        section
        for section in sections
        if _value(section, "account_id") == account_id
        and _value(section, "status", "committed") == "committed"
    ]
    transaction_rows = [
        transaction
        for transaction in transactions
        if _value(transaction, "account_id") == account_id
    ]
    snapshot = input_snapshot_sha256(account_id, section_rows, transaction_rows)
    by_day: dict[date, list[_Observation]] = defaultdict(list)
    checks: list[BalanceCheck] = []

    for section in section_rows:
        as_of = _as_date(_value(section, "balance_as_of"))
        balance = _value(section, "header_balance_cents")
        section_key = _section_id(section)
        if as_of is None and balance is None:
            continue
        if as_of is None or type(balance) is not int:
            checks.append(
                _record(
                    account_id=account_id,
                    snapshot=snapshot,
                    status="not_comparable",
                    start=None,
                    end=None,
                    reason="incomplete_header_balance",
                    start_section_override=section_key,
                )
            )
            continue
        observation = _Observation(section_key, as_of, balance)
        complete_through = effective_complete_through(section)
        if complete_through is None:
            checks.append(
                _record(
                    account_id=account_id,
                    snapshot=snapshot,
                    status="not_comparable",
                    start=observation,
                    end=observation,
                    reason="coverage_bound_unknown",
                )
            )
        elif as_of > complete_through:
            checks.append(
                _record(
                    account_id=account_id,
                    snapshot=snapshot,
                    status="not_comparable",
                    start=observation,
                    end=observation,
                    reason="balance_after_complete_through",
                )
            )
        else:
            by_day[as_of].append(observation)

    comparable: list[_Observation] = []
    for _day, observations in sorted(by_day.items()):
        observations.sort(key=lambda item: (item.balance_cents, item.section_id))
        balances = {item.balance_cents for item in observations}
        if len(balances) > 1:
            first = observations[0]
            checks.append(
                _record(
                    account_id=account_id,
                    snapshot=snapshot,
                    status="not_comparable",
                    start=first,
                    end=observations[-1],
                    reason="conflicting_balances_same_day",
                )
            )
            continue
        comparable.append(observations[0])

    if len(comparable) == 1:
        only = comparable[0]
        checks.append(
            _record(
                account_id=account_id,
                snapshot=snapshot,
                status="not_comparable",
                start=only,
                end=only,
                reason="single_comparable_balance",
            )
        )

    intervals = _merge_intervals(section_rows)
    daily_totals: dict[date, int] = defaultdict(int)
    for transaction in transaction_rows:
        booked_on = _as_date(_value(transaction, "booked_on"))
        amount = _value(transaction, "amount_cents")
        if booked_on is not None and type(amount) is int:
            daily_totals[booked_on] += amount
    days = sorted(daily_totals)
    prefix = [0]
    for day in days:
        prefix.append(prefix[-1] + daily_totals[day])

    for start, end in zip(comparable, comparable[1:], strict=False):
        if not _continuous_coverage(start.as_of, end.as_of, intervals):
            checks.append(
                _record(
                    account_id=account_id,
                    snapshot=snapshot,
                    status="not_comparable",
                    start=start,
                    end=end,
                    reason="coverage_discontinuous",
                )
            )
            continue
        after_start = bisect_right(days, start.as_of)
        through_end = bisect_right(days, end.as_of)
        transaction_sum = prefix[through_end] - prefix[after_start]
        recalculated = start.balance_cents + transaction_sum
        delta = end.balance_cents - recalculated
        checks.append(
            _record(
                account_id=account_id,
                snapshot=snapshot,
                status="match" if delta == 0 else "mismatch",
                start=start,
                end=end,
                recalculated=recalculated,
                delta=delta,
            )
        )

    checks = [check for check in checks if _in_period(check, period_start, period_end)]
    checks.sort(
        key=lambda check: (
            check.end_balance_as_of or check.start_balance_as_of or date.min,
            check.start_balance_as_of or date.min,
            check.balance_check_id,
        )
    )
    return checks


def balance_snapshot_sha256(
    account_id: str,
    sections: Iterable[BalanceRecord],
    transactions: Iterable[BalanceRecord],
) -> str:
    """Public snapshot helper for signed pagination cursors."""
    return input_snapshot_sha256(account_id, sections, transactions)


def touches_period(check: BalanceCheck, period_start: date, period_end: date) -> bool:
    """Whether a balance comparison interval intersects an inclusive date range."""
    return _in_period(check, period_start, period_end)


__all__ = [
    "BalanceCheck",
    "balance_snapshot_sha256",
    "calculate_balance_checks",
    "input_snapshot_sha256",
    "touches_period",
]
