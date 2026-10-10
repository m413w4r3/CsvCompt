from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.db.models import Base

ROOT = Path(__file__).resolve().parents[3]
REFERENCE_SQL = ROOT / "docs" / "examples" / "schema.sql"


def _normalized_sql(sql: str | None) -> str | None:
    return None if sql is None else re.sub(r"\s+", " ", sql).strip()


def _objects(database: Path) -> tuple[tuple[str, str, str, str | None], ...]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger') "
            "AND name NOT LIKE 'sqlite_%' AND name <> 'alembic_version'"
        ).fetchall()
    return tuple(
        sorted((kind, name, table, _normalized_sql(sql)) for kind, name, table, sql in rows)
    )


def _sqlite_affinity(declared_type: str) -> str:
    value = declared_type.upper()
    if "INT" in value:
        return "INTEGER"
    if any(token in value for token in ("CHAR", "CLOB", "TEXT")) or not value:
        return "TEXT"
    if any(token in value for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    if "BLOB" in value:
        return "BLOB"
    return "NUMERIC"


@pytest.mark.integration
def test_T06_schema_reference_sqlite_master_objects_match_migration(
    tmp_path: Path,
) -> None:
    reference_db = tmp_path / "reference.sqlite"
    migrated_db = tmp_path / "migrated.sqlite"
    with sqlite3.connect(reference_db) as connection:
        connection.executescript(REFERENCE_SQL.read_text(encoding="utf-8"))
    settings = Settings(
        database_url=f"sqlite:///{migrated_db}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)

    reference_objects = _objects(reference_db)
    migrated_objects = _objects(migrated_db)
    assert migrated_objects == reference_objects
    assert sum(item[0] == "table" for item in migrated_objects) == 29
    assert sum(item[0] == "index" for item in migrated_objects) == 34
    assert sum(item[0] == "trigger" for item in migrated_objects) == 75


@pytest.mark.integration
def test_T06_ORM_columns_match_pragma_table_info_for_each_migrated_table(
    tmp_path: Path,
) -> None:
    database = tmp_path / "orm-parity.sqlite"
    settings = Settings(
        database_url=f"sqlite:///{database}", data_dir=tmp_path / "data", _env_file=None
    )
    upgrade_to_head(settings)

    with sqlite3.connect(database) as connection:
        for table_name, table in Base.metadata.tables.items():
            rows = {
                row[1]: row
                for row in connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            }
            assert set(rows) == set(table.columns.keys()), table_name
            for column in table.columns:
                row = rows[column.name]
                assert _sqlite_affinity(str(column.type)) == _sqlite_affinity(row[2]), (
                    table_name,
                    column.name,
                    str(column.type),
                    row[2],
                )
                assert bool(column.nullable) == (not bool(row[3])), (table_name, column.name)
                assert bool(column.primary_key) == bool(row[5]), (table_name, column.name)

        visible = {row[1] for row in connection.execute("PRAGMA table_info('idempotency_records')")}
        extended = {
            row[1]: row for row in connection.execute("PRAGMA table_xinfo('idempotency_records')")
        }
    # SQLite omet les generated columns de table_info; table_xinfo confirms the SQL-owned expiry.
    assert "expires_at_utc" not in visible
    assert extended["expires_at_utc"][6] == 3
