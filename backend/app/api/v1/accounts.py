"""Lectures de comptes, de couverture et de cohérence des soldes."""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.analytics.quality import (
    coverage_summary,
    load_accounts,
    load_quality_snapshot,
    month_completeness,
)
from app.core.errors import AppError
from app.db.models import Account
from app.db.session import DbSession
from app.services.app_secrets import read_hmac_key
from app.services.balance_checks import (
    BalanceCheck,
    balance_snapshot_sha256,
    calculate_balance_checks,
)
from app.services.categories import require_database_ready
from app.services.idempotency import sign_cursor, verify_cursor
from app.services.transactions import sanitize_api_cents

router = APIRouter()


class DateIntervalResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    start: date = Field(alias="from")
    through: date


class SectionCoverageResponse(BaseModel):
    section_id: str | None
    account_id: str
    covered_from: date | None
    covered_through: date | None
    complete_through: date | None
    generated_on: date | None
    generation_date_source: str | None
    status: Literal["committed"]
    provisional_days: list[date]


class CoverageSummaryResponse(BaseModel):
    status: Literal["complete", "partial", "unknown", "not_started"]
    covered_from: date | None
    covered_through: date | None
    complete_day_count: int
    missing_days: list[DateIntervalResponse]
    provisional_days: list[date]
    operation_count: int
    provisional_operation_count: int
    complete_intervals_by_account: dict[str, list[DateIntervalResponse]]
    sections: list[SectionCoverageResponse]


class AccountItem(BaseModel):
    account_id: str
    alias: str
    account_role: str
    is_active: bool
    version: int
    coverage: CoverageSummaryResponse


class AccountListResponse(BaseModel):
    items: list[AccountItem]


class PeriodResponse(BaseModel):
    start: date
    end: date
    kind: Literal["calendar_month", "custom"]
    completeness: Literal["complete", "partial", "unknown", "not_started"]
    as_of: date


class AccountCoverageResponse(BaseModel):
    period: PeriodResponse
    accounts: list[str]
    coverage: CoverageSummaryResponse
    filters: dict[str, Any]
    metric_definition_id: Literal["month_completeness.v1"]
    raw_cents: None = None
    unit: Literal["state"] = "state"


class BalanceCheckResponse(BaseModel):
    balance_check_id: str
    account_id: str
    status: Literal["match", "mismatch", "not_comparable"]
    start_balance_as_of: date | None
    end_balance_as_of: date | None
    start_balance_cents: int | None
    end_balance_cents: int | None
    recalculated_balance_cents: int | None
    delta_cents: int | None
    total_out_of_range: bool = False
    start_section_id: str | None
    end_section_id: str | None
    reason: str | None
    input_snapshot_sha256: str
    version: int


class PageResponse(BaseModel):
    limit: int
    next_cursor: str | None


class BalanceChecksPageResponse(BaseModel):
    items: list[BalanceCheckResponse]
    page: PageResponse


def _unknown_coverage(account_id: str) -> dict[str, Any]:
    return {
        "status": "unknown",
        "covered_from": None,
        "covered_through": None,
        "complete_day_count": 0,
        "missing_days": [],
        "provisional_days": [],
        "operation_count": 0,
        "provisional_operation_count": 0,
        "complete_intervals_by_account": {account_id: []},
        "sections": [],
    }


def _sort_key(check: BalanceCheck) -> tuple[date, date, str]:
    return (
        check.end_balance_as_of or check.start_balance_as_of or date.min,
        check.start_balance_as_of or date.min,
        check.balance_check_id,
    )


def _month_bounds(start: date, end: date) -> bool:
    return (
        start.day == 1
        and (start.year, start.month) == (end.year, end.month)
        and end.day == calendar.monthrange(end.year, end.month)[1]
    )


@router.get("/accounts", response_model=AccountListResponse)
def list_accounts(request: Request, session: DbSession) -> dict[str, Any]:
    require_database_ready(request.app.state.db_engine)
    accounts = load_accounts(session)
    ids = [account.account_id for account in accounts]
    sections, transactions = load_quality_snapshot(session, ids)

    items: list[dict[str, Any]] = []
    for account in accounts:
        rows = [section for section in sections if section["account_id"] == account.account_id]
        starts = [section["coverage_from"] for section in rows if section["coverage_from"]]
        ends = [section["coverage_through"] for section in rows if section["coverage_through"]]
        if starts and ends:
            period_start, period_end = min(starts), max(ends)
            checks = calculate_balance_checks(account.account_id, rows, transactions)
            summary = coverage_summary(
                [account.account_id],
                (period_start, period_end),
                rows,
                transactions,
                checks,
            )
        else:
            summary = _unknown_coverage(account.account_id)
        items.append(
            {
                "account_id": account.account_id,
                "alias": account.alias,
                "account_role": account.account_role,
                "is_active": bool(account.is_active),
                "version": account.version,
                "coverage": summary,
            }
        )
    return {"items": items}


@router.get("/accounts/{account_id}/coverage", response_model=AccountCoverageResponse)
def get_account_coverage(
    account_id: UUID,
    request: Request,
    session: DbSession,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    require_database_ready(request.app.state.db_engine)
    if period_end < period_start:
        raise AppError("VALIDATION_ERROR", 422, "La période demandée est invalide.")
    account_key = str(account_id)
    account = session.get(Account, account_key)
    if account is None:
        raise AppError("NOT_FOUND", 404, "Le compte demandé n'existe pas.")

    sections, transactions = load_quality_snapshot(session, [account_key])
    checks = calculate_balance_checks(
        account_key,
        sections,
        transactions,
        period_start=period_start,
        period_end=period_end,
    )
    period = {"start": period_start, "end": period_end}
    summary = coverage_summary([account_key], period, sections, transactions, checks)
    calendar_month = _month_bounds(period_start, period_end)
    as_of = request.app.state.clock().date()
    completeness = (
        "not_started"
        if summary["status"] == "not_started"
        else (
            month_completeness(
                account_key,
                period_start.year,
                period_start.month,
                sections,
                checks,
                as_of=as_of,
            )
            if calendar_month
            else summary["status"]
        )
    )
    if completeness == "partial" and summary["status"] == "complete":
        summary["status"] = "partial"
    return {
        "period": {
            "start": period_start,
            "end": period_end,
            "kind": "calendar_month" if calendar_month else "custom",
            "completeness": completeness,
            "as_of": as_of,
        },
        "accounts": [account_key],
        "coverage": summary,
        "filters": {"flow_kinds": None, "category_ids": None},
        "metric_definition_id": "month_completeness.v1",
        "raw_cents": None,
        "unit": "state",
    }


@router.get("/accounts/{account_id}/balance-checks", response_model=BalanceChecksPageResponse)
def get_balance_checks(
    account_id: UUID,
    request: Request,
    session: DbSession,
    period_start: date,
    period_end: date,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    require_database_ready(request.app.state.db_engine)
    if period_end < period_start:
        raise AppError("VALIDATION_ERROR", 422, "La période demandée est invalide.")
    account_key = str(account_id)
    if session.get(Account, account_key) is None:
        raise AppError("NOT_FOUND", 404, "Le compte demandé n'existe pas.")

    sections, transactions = load_quality_snapshot(session, [account_key])
    snapshot = balance_snapshot_sha256(account_key, sections, transactions)
    checks = calculate_balance_checks(
        account_key,
        sections,
        transactions,
        period_start=period_start,
        period_end=period_end,
    )
    filters = {
        "account_id": account_key,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
    }
    last_key: tuple[date, date, str] | None = None
    if cursor is not None:
        key = read_hmac_key(session)
        if key is None:
            raise AppError(
                "CURSOR_KEY_UNAVAILABLE", 503, "La clé locale de pagination est indisponible."
            )
        decoded = verify_cursor(cursor, key, filters)
        if decoded.get("snapshot_sha256") != snapshot:
            raise AppError("CURSOR_STALE", 409, "Les contrôles ont changé; relancez la liste.")
        last_end = decoded.get("last_end")
        last_start = decoded.get("last_start")
        last_id = decoded.get("last_id")
        if (
            not isinstance(last_end, str)
            or not isinstance(last_start, str)
            or not isinstance(last_id, str)
        ):
            raise AppError("CURSOR_INVALID", 422, "Le curseur est invalide.")
        last_key = (date.fromisoformat(last_end), date.fromisoformat(last_start), last_id)

    if last_key is not None:
        checks = [check for check in checks if _sort_key(check) > last_key]
    more = len(checks) > limit
    page_items = checks[:limit]
    next_cursor = None
    if more and page_items:
        key = read_hmac_key(session)
        if key is None:
            raise AppError(
                "CURSOR_KEY_UNAVAILABLE", 503, "La clé locale de pagination est indisponible."
            )
        last = page_items[-1]
        next_cursor = sign_cursor(
            {
                "filters": filters,
                "snapshot_sha256": snapshot,
                "last_end": (
                    last.end_balance_as_of or last.start_balance_as_of or date.min
                ).isoformat(),
                "last_start": (last.start_balance_as_of or date.min).isoformat(),
                "last_id": last.balance_check_id,
            },
            key,
        )
    return sanitize_api_cents(
        {
            "items": [check.as_dict() for check in page_items],
            "page": {"limit": limit, "next_cursor": next_cursor},
        }
    )


__all__ = ["router"]
