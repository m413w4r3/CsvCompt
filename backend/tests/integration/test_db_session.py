import re

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.db.session import create_sqlite_engine, immediate_transaction


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
