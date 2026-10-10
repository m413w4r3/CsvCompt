import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config import Settings
from app.db.errors import DatabaseBusyError, VersionConflictError
from app.db.migrate import upgrade_to_head
from app.db.models import Account
from app.db.session import (
    DbSession,
    create_sqlite_engine,
    immediate_transaction,
    session_scope,
    write_transaction,
)


@pytest.mark.integration
def test_SEC_06_pragmas_are_set_on_every_pool_connection(temp_sqlite_url: str) -> None:
    engine = create_sqlite_engine(temp_sqlite_url)
    try:
        connections = [engine.connect(), engine.connect()]
        try:
            for connection in connections:
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
                assert connection.exec_driver_sql("PRAGMA recursive_triggers").scalar_one() == 1
                assert (
                    connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
                )
                assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
                assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1
        finally:
            for connection in connections:
                connection.close()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_begin_immediate_serializes_writer_connections(temp_sqlite_url: str) -> None:
    engine = create_sqlite_engine(temp_sqlite_url, busy_timeout_ms=100)
    try:
        first = engine.connect()
        second = engine.connect()
        try:
            with (
                immediate_transaction(first),
                pytest.raises(OperationalError, match="locked"),
                immediate_transaction(second),
            ):
                pass
        finally:
            first.close()
            second.close()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_transaction_rolls_back_on_exception_and_foreign_key_is_enforced(
    temp_sqlite_url: str,
) -> None:
    engine = create_sqlite_engine(temp_sqlite_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))"
            )
            connection.exec_driver_sql("CREATE TABLE entries (value INTEGER NOT NULL)")
        with engine.connect() as connection, pytest.raises(IntegrityError):
            connection.exec_driver_sql("INSERT INTO child(parent_id) VALUES (99)")
        with (
            engine.connect() as connection,
            pytest.raises(RuntimeError, match="rollback probe"),
            immediate_transaction(connection),
        ):
            connection.exec_driver_sql("INSERT INTO entries(value) VALUES (1)")
            raise RuntimeError("rollback probe")
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM entries").scalar_one() == 0
    finally:
        engine.dispose()


@pytest.mark.integration
def test_write_transaction_uses_immediate_and_maps_busy_and_stale_version(
    temp_sqlite_url: str, tmp_path: Path
) -> None:
    upgrade_to_head(
        Settings(database_url=temp_sqlite_url, data_dir=tmp_path / "data", _env_file=None)
    )
    engine = create_sqlite_engine(temp_sqlite_url, busy_timeout_ms=30)
    try:
        with write_transaction(engine) as session:
            account = Account(alias="Concurrentiel", account_role="current")
            session.add(account)
        account_id = account.account_id

        with session_scope(engine) as read_session:
            stale_account = read_session.get(Account, account_id)
            assert stale_account is not None and stale_account.version == 1
            read_session.expunge(stale_account)

        with write_transaction(engine) as session:
            current = session.get(Account, account_id)
            assert current is not None
            current.alias = "Version suivante"

        with (
            pytest.raises(VersionConflictError) as stale_error,
            write_transaction(engine) as session,
        ):
            session.merge(stale_account)
        assert stale_error.value.code == "VERSION_CONFLICT"
        assert stale_error.value.status_code == 409

        blocker = engine.connect()
        try:
            with (
                immediate_transaction(blocker),
                pytest.raises(DatabaseBusyError) as busy_error,
                write_transaction(engine),
            ):
                pass
            assert busy_error.value.code == "DATABASE_BUSY"
            assert busy_error.value.status_code == 503
            assert busy_error.value.retryable is True
        finally:
            blocker.close()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_get_db_is_read_only_and_write_transaction_resets_connection_state(
    temp_sqlite_url: str, tmp_path: Path
) -> None:
    upgrade_to_head(
        Settings(database_url=temp_sqlite_url, data_dir=tmp_path / "data", _env_file=None)
    )
    engine = create_sqlite_engine(temp_sqlite_url)
    try:
        with pytest.raises(OperationalError, match="readonly"), session_scope(engine) as session:
            session.add(Account(alias="Refusée", account_role="current"))
            session.flush()

        with write_transaction(engine) as session:
            session.add(Account(alias="Écriture autorisée", account_role="current"))
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 0
            assert connection.exec_driver_sql("SELECT count(*) FROM accounts").scalar_one() == 1
    finally:
        engine.dispose()


@pytest.mark.integration
def test_API_write_endpoint_never_returns_success_before_commit(tmp_path: Path) -> None:
    database = tmp_path / "api-write.sqlite"
    settings = Settings(
        database_url=f"sqlite:///{database}", data_dir=tmp_path / "data", _env_file=None
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    application = FastAPI()
    application.state.db_engine = engine

    @application.post("/test/constraint")
    def constraint_failure() -> dict[str, bool]:
        with write_transaction(engine) as session:
            session.add(Account(alias="Contrainte invalide", account_role="invalid"))
        return {"written": True}

    @application.post("/test/success")
    def successful_write() -> dict[str, bool]:
        with write_transaction(engine) as session:
            session.add(Account(alias="Visible avant réponse", account_role="current"))
        return {"written": True}

    @application.post("/test/read-only")
    def forbidden_dependency_write(session: DbSession) -> dict[str, bool]:
        session.add(Account(alias="Dépendance en lecture seule", account_role="current"))
        session.flush()
        return {"written": True}

    try:
        with TestClient(application, raise_server_exceptions=False) as client:
            failed = client.post("/test/constraint")
            assert failed.status_code >= 400
            assert failed.status_code != 200
            assert client.post("/test/read-only").status_code >= 400
            succeeded = client.post("/test/success")
            assert succeeded.status_code == 200
            with engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT count(*) FROM accounts").scalar_one() == 1
    finally:
        engine.dispose()


@pytest.mark.unit
def test_SEC_06_application_sources_do_not_use_replace_statements() -> None:
    from pathlib import Path

    source_root = Path(__file__).parents[2] / "app"
    forbidden = re.compile(r"\bINSERT\s+OR\s+REPLACE\b|\bREPLACE\s+INTO\b", re.IGNORECASE)
    offenders = [
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if forbidden.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
