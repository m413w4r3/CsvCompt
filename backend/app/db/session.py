"""Fabrique SQLAlchemy/SQLite avec garde-fous d'intégrité par connexion."""

import sqlite3
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import QueuePool

from app.db.errors import DatabaseBusyError, VersionConflictError


def create_sqlite_engine(
    url: str,
    *,
    busy_timeout_ms: int = 5000,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    pool_timeout: int | None = None,
) -> Engine:
    """Crée un moteur SQLite configuré avec les PRAGMA requis à chaque connexion."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        raise ValueError("Seules les URL SQLite sont acceptées par create_sqlite_engine")
    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms doit être positif ou nul")
    engine_options: dict[str, object] = {
        "connect_args": {"check_same_thread": False, "timeout": busy_timeout_ms / 1000}
    }
    if pool_size is not None:
        engine_options["poolclass"] = QueuePool
        engine_options["pool_size"] = pool_size
    if max_overflow is not None:
        engine_options["max_overflow"] = max_overflow
    if pool_timeout is not None:
        engine_options["pool_timeout"] = pool_timeout
    engine = create_engine(url, **engine_options)

    @event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection: object, _connection_record: object) -> None:
        # isolation_level=None laisse SQLAlchemy contrôler explicitement BEGIN.
        dbapi_connection.isolation_level = None  # type: ignore[attr-defined]
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA recursive_triggers=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def begin_transaction(connection: Connection) -> None:
        if connection.info.pop("_begin_immediate", False):
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.exec_driver_sql("BEGIN")

    return engine


def create_sqlite_snapshot_engine(url: str) -> Engine:
    """Use a small, isolated pool for pinned transaction read snapshots."""

    return create_sqlite_engine(
        url,
        busy_timeout_ms=1000,
        pool_size=4,
        max_overflow=0,
        pool_timeout=0,
    )


@contextmanager
def immediate_transaction(connection: Connection) -> Iterator[Connection]:
    """Ouvre une transaction qui réserve immédiatement le verrou d'écriture."""
    if connection.in_transaction():
        raise RuntimeError("immediate_transaction exige une connexion sans transaction active")
    connection.info["_begin_immediate"] = True
    transaction = connection.begin()
    try:
        yield connection
    except BaseException:
        if transaction.is_active:
            transaction.rollback()
        raise
    else:
        transaction.commit()


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Fournit une session de lecture SQLite et annule toujours sa transaction."""
    with engine.connect() as connection:
        dbapi_connection = connection.connection.driver_connection
        assert dbapi_connection is not None
        dbapi_connection.execute("PRAGMA query_only=ON")
        factory = sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="rollback_only",
        )
        session = factory()
        try:
            yield session
        finally:
            session.rollback()
            session.close()
            if connection.in_transaction():
                connection.rollback()
            try:
                dbapi_connection.execute("PRAGMA query_only=OFF")
            except sqlite3.Error:
                connection.invalidate()
                raise


def is_sqlite_busy_error(error: OperationalError) -> bool:
    """Recognizes SQLITE_BUSY/LOCKED, including their extended SQLite codes."""
    original = error.orig
    code = getattr(original, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return True
    message = str(original).lower()
    return "database is locked" in message or "database table is locked" in message


@contextmanager
def write_transaction(engine: Engine) -> Iterator[Session]:
    """Runs one write unit under ``BEGIN IMMEDIATE`` and commits before returning.

    The yielded ORM session is closed before the outer SQLite transaction commits.
    Commit errors are raised from this context manager, before a handler can build
    or send its HTTP response.
    """
    try:
        with engine.connect() as connection, immediate_transaction(connection):
            factory = sessionmaker(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="rollback_only",
            )
            with factory() as session:
                yield session
                session.flush()
                # End the ORM unit while leaving the externally-owned SQLite
                # transaction active; immediate_transaction commits it next.
                session.commit()
    except StaleDataError as error:
        raise VersionConflictError() from error
    except OperationalError as error:
        if is_sqlite_busy_error(error):
            raise DatabaseBusyError() from error
        raise


def get_db(request: Request) -> Generator[Session, None, None]:
    """Dépendance FastAPI en lecture seule, sans commit implicite."""
    with session_scope(request.app.state.db_engine) as session:
        yield session


DbSession = Annotated[Session, Depends(get_db)]
