"""Read-only transaction queries and their stable filter semantics."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, cast

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db.models import Account, Allocation, ImportFile, SourceRow, Transaction
from app.importers.normalize import search_label


def canonical_search(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(search_label(value))
    return normalized or None


def transaction_statement(
    *,
    account_ids: list[str],
    period_start: date | None,
    period_end: date | None,
    direction: str | None,
    amount_min_cents: int | None,
    amount_max_cents: int | None,
    import_file_id: str | None,
    classification: str,
    flow_kind: str | None,
    snapshot_at: datetime | None,
) -> Any:
    """Build the indexed transaction scan with all SQL-capable filters."""
    statement = select(Transaction, Account.alias).join(
        Account, Account.account_id == Transaction.account_id
    )
    if account_ids:
        statement = statement.where(Transaction.account_id.in_(account_ids))
    if period_start:
        statement = statement.where(Transaction.booked_on >= period_start)
    if period_end:
        statement = statement.where(Transaction.booked_on <= period_end)
    if direction == "debit":
        statement = statement.where(Transaction.amount_cents < 0)
    elif direction == "credit":
        statement = statement.where(Transaction.amount_cents > 0)
    if amount_min_cents is not None:
        statement = statement.where(func.abs(Transaction.amount_cents) >= amount_min_cents)
    if amount_max_cents is not None:
        statement = statement.where(func.abs(Transaction.amount_cents) <= amount_max_cents)
    if import_file_id:
        statement = statement.where(
            select(SourceRow.source_row_id)
            .where(
                SourceRow.transaction_id == Transaction.transaction_id,
                SourceRow.import_file_id == import_file_id,
            )
            .exists()
        )
    if snapshot_at:
        statement = statement.where(Transaction.imported_at_utc <= snapshot_at)
    active_allocations = select(Allocation).where(
        Allocation.transaction_id == Transaction.transaction_id,
        Allocation.superseded_by_allocation_id.is_(None),
    )
    if classification == "unclassified":
        statement = statement.where(~active_allocations.exists())
    if flow_kind == "unknown":
        allocated = (
            select(func.coalesce(func.sum(Allocation.amount_cents), 0))
            .where(
                Allocation.transaction_id == Transaction.transaction_id,
                Allocation.superseded_by_allocation_id.is_(None),
            )
            .scalar_subquery()
        )
        statement = statement.where(Transaction.amount_cents - allocated != 0)
    elif flow_kind:
        statement = statement.where(
            select(Allocation.allocation_id)
            .where(
                Allocation.transaction_id == Transaction.transaction_id,
                Allocation.superseded_by_allocation_id.is_(None),
                Allocation.flow_kind == flow_kind,
            )
            .exists()
        )
    return statement


def sql_summary(session: Session, statement: Any) -> dict[str, int]:
    """Aggregate the full filter in SQLite using integer cent columns."""
    subquery = statement.order_by(None).subquery()
    row = session.execute(
        select(
            func.count(),
            func.coalesce(
                func.sum(case((subquery.c.amount_cents < 0, subquery.c.amount_cents), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((subquery.c.amount_cents > 0, subquery.c.amount_cents), else_=0)), 0
            ),
        )
    ).one()
    debit, credit = int(row[1]), int(row[2])
    return {
        "count": int(row[0]),
        "debit_total_cents": debit,
        "credit_total_cents": credit,
        "net_cents": debit + credit,
    }


def filtered_transactions(
    session: Session,
    *,
    account_ids: list[str],
    period_start: date | None,
    period_end: date | None,
    direction: str | None,
    amount_min_cents: int | None,
    amount_max_cents: int | None,
    import_file_id: str | None,
    classification: str,
    flow_kind: str | None,
    snapshot_at: datetime | None,
) -> list[tuple[Transaction, str]]:
    statement = select(Transaction, Account.alias).join(
        Account, Account.account_id == Transaction.account_id
    )
    if account_ids:
        statement = statement.where(Transaction.account_id.in_(account_ids))
    if period_start:
        statement = statement.where(Transaction.booked_on >= period_start)
    if period_end:
        statement = statement.where(Transaction.booked_on <= period_end)
    if direction == "debit":
        statement = statement.where(Transaction.amount_cents < 0)
    elif direction == "credit":
        statement = statement.where(Transaction.amount_cents > 0)
    if amount_min_cents is not None:
        statement = statement.where(func.abs(Transaction.amount_cents) >= amount_min_cents)
    if amount_max_cents is not None:
        statement = statement.where(func.abs(Transaction.amount_cents) <= amount_max_cents)
    if import_file_id:
        statement = statement.where(
            select(SourceRow.source_row_id)
            .where(
                SourceRow.transaction_id == Transaction.transaction_id,
                SourceRow.import_file_id == import_file_id,
            )
            .exists()
        )
    if snapshot_at:
        statement = statement.where(Transaction.imported_at_utc <= snapshot_at)
    statement = statement.order_by(Transaction.booked_on.desc(), Transaction.transaction_id.desc())

    rows = [cast(tuple[Transaction, str], tuple(row)) for row in session.execute(statement)]
    if classification == "unclassified":
        rows = [row for row in rows if not _active_allocations(session, row[0].transaction_id)]
    if flow_kind:
        filtered: list[tuple[Transaction, str]] = []
        for row in rows:
            allocations = _active_allocations(session, row[0].transaction_id)
            residual = row[0].amount_cents - sum(int(item.amount_cents) for item in allocations)
            kinds = {str(item.flow_kind) for item in allocations}
            if (flow_kind == "unknown" and residual != 0) or flow_kind in kinds:
                filtered.append(row)
        rows = filtered
    return rows


def _active_allocations(session: Session, transaction_id: str) -> list[Allocation]:
    return list(
        session.scalars(
            select(Allocation).where(
                Allocation.transaction_id == transaction_id,
                Allocation.superseded_by_allocation_id.is_(None),
            )
        )
    )


def matches_query(label: str, query: str | None) -> bool:
    needle = canonical_search(query)
    return needle is None or needle in str(search_label(label))


def transaction_row(session: Session, transaction: Transaction, alias: str) -> dict[str, Any]:
    sources = list(
        session.execute(
            select(SourceRow, ImportFile.started_at_utc)
            .join(ImportFile, ImportFile.import_file_id == SourceRow.import_file_id)
            .where(SourceRow.transaction_id == transaction.transaction_id)
            .order_by(ImportFile.started_at_utc, SourceRow.section_index, SourceRow.logical_row_idx)
        )
    )
    allocations = _active_allocations(session, transaction.transaction_id)
    residual = transaction.amount_cents - sum(int(row.amount_cents) for row in allocations)
    return {
        "id": transaction.transaction_id,
        "account_id": transaction.account_id,
        "account_alias": alias,
        "booked_on": transaction.booked_on,
        "amount_cents": int(transaction.amount_cents),
        "raw_label_preview": transaction.raw_label[:200],
        "occurrence_rank": int(transaction.occurrence_rank),
        "import_count": len(sources),
        "classification_state": "unclassified" if not allocations else "classified",
        "residual_cents": residual,
    }


def transaction_rows(session: Session, rows: list[tuple[Transaction, str]]) -> list[dict[str, Any]]:
    """Build a page with grouped lookups instead of per-row queries."""
    if not rows:
        return []
    ids = [transaction.transaction_id for transaction, _ in rows]
    source_counts = dict(
        session.execute(
            select(SourceRow.transaction_id, func.count(SourceRow.source_row_id))
            .where(SourceRow.transaction_id.in_(ids))
            .group_by(SourceRow.transaction_id)
        ).all()
    )
    allocation_totals = {
        transaction_id: (int(count), int(total or 0))
        for transaction_id, count, total in session.execute(
            select(
                Allocation.transaction_id,
                func.count(Allocation.allocation_id),
                func.sum(Allocation.amount_cents),
            )
            .where(
                Allocation.transaction_id.in_(ids),
                Allocation.superseded_by_allocation_id.is_(None),
            )
            .group_by(Allocation.transaction_id)
        )
    }
    items: list[dict[str, Any]] = []
    for transaction, alias in rows:
        allocation_count, allocated = allocation_totals.get(transaction.transaction_id, (0, 0))
        items.append(
            {
                "id": transaction.transaction_id,
                "account_id": transaction.account_id,
                "account_alias": alias,
                "booked_on": transaction.booked_on,
                "amount_cents": int(transaction.amount_cents),
                "raw_label_preview": transaction.raw_label[:200],
                "occurrence_rank": int(transaction.occurrence_rank),
                "import_count": int(source_counts.get(transaction.transaction_id, 0)),
                "classification_state": "unclassified" if allocation_count == 0 else "classified",
                "residual_cents": int(transaction.amount_cents) - allocated,
            }
        )
    return items


def summary_for(rows: list[tuple[Transaction, str]]) -> dict[str, int]:
    debits = sum(
        int(transaction.amount_cents) for transaction, _ in rows if transaction.amount_cents < 0
    )
    credits = sum(
        int(transaction.amount_cents) for transaction, _ in rows if transaction.amount_cents > 0
    )
    return {
        "count": len(rows),
        "debit_total_cents": debits,
        "credit_total_cents": credits,
        "net_cents": debits + credits,
    }
