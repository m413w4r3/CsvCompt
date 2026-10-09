"""Fabrique SQLAlchemy/SQLite avec garde-fous d'intégrité par connexion."""

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.orm import Session, sessionmaker


def create_sqlite_engine(url: str, *, busy_timeout_ms: int = 5000) -> Engine:
    """Crée un moteur SQLite configuré avec les PRAGMA requis à chaque connexion."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        raise ValueError("Seules les URL SQLite sont acceptées par create_sqlite_engine")
    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms doit être positif ou nul")
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": busy_timeout_ms / 1000},
    )

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
        transaction.rollback()
        raise
    else:
        transaction.commit()


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Fournit une session transactionnelle et la ferme proprement."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        with session.begin():
            yield session
    finally:
        session.close()


def get_db(request: Request) -> Generator[Session, None, None]:
    """Dépendance FastAPI; l'engine se remplace via app.state ou dependency_overrides."""
    with session_scope(request.app.state.db_engine) as session:
        yield session


DbSession = Annotated[Session, Depends(get_db)]
