"""Read-only transaction queries and their stable filter semantics."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, cast

from sqlalchemy import case, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import Account, Allocation, ImportFile, SourceRow, Transaction
from app.importers.normalize import search_label

MAX_SAFE_FRONTEND_INTEGER = 2**53 - 1


class SnapshotUnavailable(Exception):
    """The process-local read snapshot expired or was already released."""


def sanitize_api_cents[Sanitized](value: Sanitized) -> Sanitized:
    """Null unsafe cent integers recursively and mark the object that contained them."""

    if isinstance(value, list):
        return cast(Sanitized, [sanitize_api_cents(item) for item in value])
    if isinstance(value, tuple):
        return cast(Sanitized, [sanitize_api_cents(item) for item in value])
    if not isinstance(value, Mapping):
        return value

    sanitized: dict[str, Any] = {}
    unsafe_here = False
    for key, item in value.items():
        if (
            isinstance(key, str)
            and key.endswith("_cents")
            and type(item) is int
            and abs(item) > MAX_SAFE_FRONTEND_INTEGER
        ):
            sanitized[key] = None
            unsafe_here = True
            continue
        sanitized[key] = sanitize_api_cents(item)
    if unsafe_here:
        sanitized["total_out_of_range"] = True
    return cast(Sanitized, sanitized)


class TransactionSnapshotStore:
    """Keep short-lived SQLite read snapshots pinned across transaction pages."""

    def __init__(self, *, ttl_seconds: int = 300, max_snapshots: int = 32) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_snapshots = max_snapshots
        self._entries: dict[str, _TransactionSnapshot] = {}
        self._guard = threading.RLock()

    def create(self, engine: Engine, snapshot_at: str) -> tuple[str, Session]:
        self._expire()
        with self._guard:
            if len(self._entries) >= self._max_snapshots:
                available = [
                    key for key, entry in self._entries.items() if not entry.lock.locked()
                ]
                if not available:
                    raise AppError(
                        "DATABASE_BUSY", 503, "La pagination locale est momentanément occupée."
                    )
                oldest = min(available, key=lambda key: self._entries[key].created_at)
                self._discard_locked(oldest)
            connection = engine.connect()
            session: Session | None = None
            try:
                connection.exec_driver_sql("PRAGMA query_only=ON")
                connection.commit()
                session = Session(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="rollback_only",
                )
                session.begin()
                # The first read fixes SQLite's WAL snapshot at commit visibility.
                session.execute(select(Transaction.transaction_id).limit(1)).first()
            except BaseException:
                try:
                    if session is not None:
                        session.rollback()
                        session.close()
                    if connection.in_transaction():
                        connection.rollback()
                    connection.exec_driver_sql("PRAGMA query_only=OFF")
                    connection.commit()
                except Exception:
                    connection.invalidate()
                finally:
                    connection.close()
                raise
            assert session is not None
            snapshot_id = uuid.uuid4().hex
            self._entries[snapshot_id] = _TransactionSnapshot(
                session=session,
                connection=connection,
                created_at=time.monotonic_ns(),
                snapshot_at=snapshot_at,
            )
            return snapshot_id, session

    @contextmanager
    def use(self, snapshot_id: str) -> Iterator[tuple[Session, str]]:
        with self._guard:
            self._expire_locked()
            entry = self._entries.get(snapshot_id)
            if entry is None:
                raise SnapshotUnavailable(snapshot_id)
        entry.lock.acquire()
        try:
            with self._guard:
                if self._entries.get(snapshot_id) is not entry:
                    raise SnapshotUnavailable(snapshot_id)
            yield entry.session, entry.snapshot_at
        finally:
            entry.lock.release()

    def discard(self, snapshot_id: str) -> None:
        with self._guard:
            entry = self._entries.get(snapshot_id)
        if entry is None:
            return
        with entry.lock, self._guard:
            if self._entries.get(snapshot_id) is entry:
                self._discard_locked(snapshot_id)

    def close_all(self) -> None:
        with self._guard:
            snapshot_ids = list(self._entries)
        for snapshot_id in snapshot_ids:
            self.discard(snapshot_id)

    def _expire(self) -> None:
        with self._guard:
            self._expire_locked()

    def _expire_locked(self) -> None:
        now = time.monotonic_ns()
        expired = [
            snapshot_id
            for snapshot_id, entry in self._entries.items()
            if now - entry.created_at >= self._ttl_seconds * 1_000_000_000
            and not entry.lock.locked()
        ]
        for snapshot_id in expired:
            self._discard_locked(snapshot_id)

    def _discard_locked(self, snapshot_id: str) -> None:
        entry = self._entries.pop(snapshot_id, None)
        if entry is None:
            return
        try:
            entry.session.rollback()
            entry.session.close()
            entry.connection.exec_driver_sql("PRAGMA query_only=OFF")
            entry.connection.commit()
        except Exception:
            entry.connection.invalidate()
            raise
        finally:
            entry.connection.close()


class _TransactionSnapshot:
    def __init__(
        self,
        *,
        session: Session,
        connection: Any,
        created_at: int,
        snapshot_at: str,
    ) -> None:
        self.session = session
        self.connection = connection
        self.created_at = created_at
        self.snapshot_at = snapshot_at
        self.lock = threading.Lock()


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
        Allocation.retracted_at_utc.is_(None),
    )
    if classification == "unclassified":
        statement = statement.where(~active_allocations.exists())
    if flow_kind == "unknown":
        allocated = (
            select(func.coalesce(func.sum(Allocation.amount_cents), 0))
            .where(
                Allocation.transaction_id == Transaction.transaction_id,
                Allocation.superseded_by_allocation_id.is_(None),
                Allocation.retracted_at_utc.is_(None),
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
                Allocation.retracted_at_utc.is_(None),
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
                Allocation.retracted_at_utc.is_(None),
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
                Allocation.retracted_at_utc.is_(None),
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
