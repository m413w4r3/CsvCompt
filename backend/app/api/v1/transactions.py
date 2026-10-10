"""Read-only transaction list and detail endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, tuple_

from app.core.errors import AppError
from app.db.models import Account, Allocation, ImportFile, SourceRow, Transaction
from app.db.session import DbSession
from app.services.app_secrets import read_hmac_key
from app.services.categories import require_database_ready
from app.services.idempotency import sign_cursor, verify_cursor
from app.services.transactions import (
    MAX_SAFE_FRONTEND_INTEGER,
    SnapshotUnavailable,
    TransactionSnapshotStore,
    filtered_transactions,
    matches_query,
    sanitize_api_cents,
    sql_summary,
    summary_for,
    transaction_row,
    transaction_rows,
    transaction_statement,
)


@asynccontextmanager
async def _transaction_lifespan(application: Any) -> AsyncIterator[None]:
    yield
    store = getattr(application.state, "transaction_snapshot_store", None)
    if isinstance(store, TransactionSnapshotStore):
        store.close_all()


router = APIRouter(
    prefix="/transactions", tags=["transactions"], lifespan=_transaction_lifespan
)


class TransactionSummary(BaseModel):
    count: int
    debit_total_cents: int | None
    credit_total_cents: int | None
    net_cents: int | None
    total_out_of_range: bool = False


class TransactionPage(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None
    total_count: int
    summary: TransactionSummary
    snapshot_at: str


def _safe_frontend_summary(summary: dict[str, int]) -> dict[str, int | None | bool]:
    """Keep cent totals in JavaScript's exact integer range or return null."""

    result: dict[str, int | None | bool] = dict(summary)
    out_of_range = False
    for field in ("debit_total_cents", "credit_total_cents", "net_cents"):
        amount = summary[field]
        if abs(amount) > MAX_SAFE_FRONTEND_INTEGER:
            result[field] = None
            out_of_range = True
    result["total_out_of_range"] = out_of_range
    return result


def _cursor_filter_values(filters: dict[str, Any], key: bytes) -> dict[str, Any]:
    canonical = json.dumps(
        filters, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    digest = hmac.new(key, b"transactions-filters\0" + canonical, hashlib.sha256).hexdigest()
    public = {name: value for name, value in filters.items() if name != "q"}
    public["filters_hmac"] = digest
    return public


def _filters(
    account_ids: list[UUID],
    period_start: date | None,
    period_end: date | None,
    direction: str | None,
    amount_min_cents: int | None,
    amount_max_cents: int | None,
    q: str | None,
    import_file_id: UUID | None,
    classification: str,
    flow_kind: str | None,
) -> dict[str, Any]:
    return {
        "account_ids": sorted(str(value) for value in set(account_ids)),
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
        "direction": direction,
        "amount_min_cents": amount_min_cents,
        "amount_max_cents": amount_max_cents,
        "q": q,
        "import_file_id": str(import_file_id) if import_file_id else None,
        "classification": classification,
        "flow_kind": flow_kind,
    }


@router.get("", response_model=TransactionPage)
def list_transactions(
    request: Request,
    session: DbSession,
    account_ids: Annotated[list[UUID] | None, Query()] = None,
    period_start: date | None = None,
    period_end: date | None = None,
    direction: Literal["debit", "credit"] | None = None,
    amount_min_cents: int | None = Query(default=None, ge=0, le=MAX_SAFE_FRONTEND_INTEGER),
    amount_max_cents: int | None = Query(default=None, ge=0, le=MAX_SAFE_FRONTEND_INTEGER),
    q: str | None = Query(default=None, max_length=200),
    import_file_id: UUID | None = None,
    classification: Literal["all", "unclassified"] = "all",
    flow_kind: str | None = Query(default=None, max_length=40),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    account_ids = account_ids or []
    require_database_ready(request.app.state.db_engine)
    if period_start and period_end and period_end < period_start:
        raise AppError("VALIDATION_ERROR", 422, "La période de filtre est invalide.")
    if (
        amount_min_cents is not None
        and amount_max_cents is not None
        and amount_min_cents > amount_max_cents
    ):
        raise AppError("VALIDATION_ERROR", 422, "La plage de montant est invalide.")
    if flow_kind is not None and flow_kind not in {
        "expense",
        "income_recurring",
        "income_other",
        "transfer_savings",
        "transfer_owned",
        "allocation_personal",
        "loan_disbursement",
        "loan_repayment",
        "capital_event",
        "cash_withdrawal_pending",
        "refund",
        "intermediary_pending",
        "reconciliation",
        "unknown",
    }:
        raise AppError("VALIDATION_ERROR", 422, "Le type de flux est invalide.")

    filters = _filters(
        account_ids,
        period_start,
        period_end,
        direction,
        amount_min_cents,
        amount_max_cents,
        q,
        import_file_id,
        classification,
        flow_kind,
    )
    key = read_hmac_key(session)
    if key is None:
        raise AppError("DATABASE_NOT_READY", 503, "La pagination locale n'est pas initialisée.")
    cursor_filters = _cursor_filter_values(filters, key)
    now = (
        request.app.state.clock()
        .astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    store = getattr(request.app.state, "transaction_snapshot_store", None)
    if not isinstance(store, TransactionSnapshotStore):
        store = TransactionSnapshotStore()
        request.app.state.transaction_snapshot_store = store
    snapshot_id: str
    last_date: str | None = None
    last_id: str | None = None
    if cursor:
        decoded = verify_cursor(cursor, key, cursor_filters)
        snapshot_id_value = decoded.get("snapshot_id")
        last_date = decoded.get("last_booked_on")
        last_id = decoded.get("last_id")
        if (
            not isinstance(snapshot_id_value, str)
            or not isinstance(last_date, str)
            or not isinstance(last_id, str)
        ):
            raise AppError("CURSOR_INVALID", 422, "Le curseur est invalide.")
        snapshot_id = snapshot_id_value
        snapshot_at = decoded.get("snapshot_at")
        if not isinstance(snapshot_at, str):
            raise AppError("CURSOR_INVALID", 422, "Le curseur est invalide.")
        created_snapshot = False
    else:
        snapshot_at = now
        snapshot_id, _read_session = store.create(request.app.state.db_engine, snapshot_at)
        created_snapshot = True

    try:
        with store.use(snapshot_id) as (read_session, snapshot_at):
            statement = transaction_statement(
                account_ids=filters["account_ids"],
                period_start=period_start,
                period_end=period_end,
                direction=direction,
                amount_min_cents=amount_min_cents,
                amount_max_cents=amount_max_cents,
                import_file_id=str(import_file_id) if import_file_id else None,
                classification=classification,
                flow_kind=flow_kind,
                snapshot_at=None,
            )
            if q:
                matching = [
                    row
                    for row in filtered_transactions(
                        read_session,
                        account_ids=filters["account_ids"],
                        period_start=period_start,
                        period_end=period_end,
                        direction=direction,
                        amount_min_cents=amount_min_cents,
                        amount_max_cents=amount_max_cents,
                        import_file_id=str(import_file_id) if import_file_id else None,
                        classification=classification,
                        flow_kind=flow_kind,
                        snapshot_at=None,
                    )
                    if matches_query(row[0].raw_label, q)
                ]
                summary = summary_for(matching)
                rows = matching
                if last_date is not None and last_id is not None:
                    rows = [
                        row
                        for row in rows
                        if (row[0].booked_on.isoformat(), row[0].transaction_id)
                        < (last_date, last_id)
                    ]
                has_more = len(rows) > limit
                page = rows[:limit]
            else:
                summary = sql_summary(read_session, statement)
                if last_date is not None and last_id is not None:
                    statement = statement.where(
                        tuple_(Transaction.booked_on, Transaction.transaction_id)
                        < (date.fromisoformat(last_date), last_id)
                    )
                selected = list(
                    read_session.execute(
                        statement.order_by(
                            Transaction.booked_on.desc(), Transaction.transaction_id.desc()
                        ).limit(limit + 1)
                    )
                )
                has_more = len(selected) > limit
                page = [cast(tuple[Transaction, str], tuple(row)) for row in selected[:limit]]
            next_cursor = None
            if has_more and page:
                last = page[-1][0]
                next_cursor = sign_cursor(
                    {
                        "filters": cursor_filters,
                        "snapshot_id": snapshot_id,
                        "snapshot_at": snapshot_at,
                        "last_booked_on": last.booked_on.isoformat(),
                        "last_id": last.transaction_id,
                    },
                    key,
                )
            response = {
                "items": transaction_rows(read_session, page),
                "next_cursor": next_cursor,
                "total_count": summary["count"],
                "summary": _safe_frontend_summary(summary),
                "snapshot_at": snapshot_at,
            }
    except SnapshotUnavailable as error:
        if created_snapshot:
            store.discard(snapshot_id)
        raise AppError(
            "CURSOR_STALE",
            409,
            "Le snapshot de pagination a expiré; relancez la liste.",
        ) from error
    except BaseException:
        store.discard(snapshot_id)
        raise
    if response["next_cursor"] is None:
        store.discard(snapshot_id)
    return sanitize_api_cents(response)


@router.get("/{transaction_id}")
def get_transaction(transaction_id: UUID, request: Request, session: DbSession) -> dict[str, Any]:
    require_database_ready(request.app.state.db_engine)
    transaction = session.get(Transaction, str(transaction_id))
    if transaction is None:
        raise AppError("NOT_FOUND", 404, "L'opération demandée n'existe pas.")
    account = session.get(Account, transaction.account_id)
    assert account is not None
    row = transaction_row(session, transaction, account.alias)
    sources = session.execute(
        select(SourceRow, ImportFile.started_at_utc)
        .join(ImportFile, ImportFile.import_file_id == SourceRow.import_file_id)
        .where(SourceRow.transaction_id == transaction.transaction_id)
        .order_by(ImportFile.started_at_utc, SourceRow.section_index, SourceRow.logical_row_idx)
    ).all()
    allocations = list(
        session.scalars(
            select(Allocation).where(
                Allocation.transaction_id == transaction.transaction_id,
                Allocation.superseded_by_allocation_id.is_(None),
                Allocation.retracted_at_utc.is_(None),
            )
        )
    )
    row.update(
        {
            "raw_label": transaction.raw_label,
            "identity_label": transaction.identity_label,
            "sources": [
                {
                    "import_file_id": source.import_file_id,
                    "imported_at": imported_at.isoformat().replace("+00:00", "Z"),
                    "section": source.section_index,
                    "logical_row_idx": source.logical_row_idx,
                }
                for source, imported_at in sources
            ],
            "allocations": [
                {
                    "allocation_id": allocation.allocation_id,
                    "category_id": allocation.category_id,
                    "flow_kind": allocation.flow_kind,
                    "amount_cents": int(allocation.amount_cents),
                }
                for allocation in allocations
            ],
            "classification_state": "unclassified" if not allocations else "classified",
        }
    )
    return sanitize_api_cents(row)
