from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db import migrate
from app.db.migrate import BackupError, create_verified_backup, upgrade_to_head
from app.db.session import create_sqlite_engine
from app.main import create_app


def _settings(database: Path, data_dir: Path) -> Settings:
    return Settings(database_url=f"sqlite:///{database}", data_dir=data_dir, _env_file=None)


def _alembic_config(settings: Settings) -> Config:
    config = Config(str(migrate.ALEMBIC_INI))
    config.attributes["settings"] = settings
    return config


def _schema_snapshot(database: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(database) as connection:
        return tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name, tbl_name, sql"
            ).fetchall()
        )


@pytest.mark.integration
def test_T06_IMP_06_upgrade_is_idempotent_and_migration_revision_is_stable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "repeat.sqlite"
    settings = _settings(database, tmp_path / "data")

    first = upgrade_to_head(settings)
    snapshot_after_first = _schema_snapshot(database)
    second = upgrade_to_head(settings)
    snapshot_after_second = _schema_snapshot(database)

    assert first.revision == second.revision == "0001_initial"
    assert first.backup is None and second.backup is None
    assert snapshot_after_first == snapshot_after_second
    assert migrate.current_revision(settings.database_url) == "0001_initial"


@pytest.mark.integration
def test_OPS_02_existing_database_is_backed_up_before_upgrade_with_0600_and_integrity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "existing.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE legacy_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_probe(value) VALUES ('synthetic-before-upgrade')")

    result = upgrade_to_head(_settings(database, tmp_path / "data"))
    assert result.backup is not None
    backup = result.backup.database_path
    manifest_path = result.backup.manifest_path
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["integrity_check"] == "ok"
    assert manifest["sha256"] == result.backup.sha256
    with sqlite3.connect(backup) as copied:
        assert copied.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert copied.execute("SELECT value FROM legacy_probe").fetchone() == (
            "synthetic-before-upgrade",
        )
        assert (
            copied.execute("SELECT 1 FROM sqlite_master WHERE name='accounts'").fetchone() is None
        )


@pytest.mark.integration
def test_OPS_02_failed_pre_upgrade_backup_refuses_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "must-not-migrate.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy_probe (value INTEGER NOT NULL)")
        connection.execute("INSERT INTO legacy_probe(value) VALUES (7)")
    before = _schema_snapshot(database)

    def fail_backup(*_args, **_kwargs):
        raise BackupError("synthetic backup failure")

    monkeypatch.setattr(migrate, "create_verified_backup", fail_backup)
    with pytest.raises(BackupError, match="synthetic backup failure"):
        upgrade_to_head(_settings(database, tmp_path / "data"))

    assert _schema_snapshot(database) == before
    assert migrate.current_revision(f"sqlite:///{database}") is None


@pytest.mark.integration
def test_T06_downgrade_refuses_business_data_without_explicit_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "downgrade-data.sqlite"
    settings = _settings(database, tmp_path / "data")
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO accounts(account_id, alias, account_role) "
                "VALUES ('00000000-0000-4000-8000-000000000001', 'Synthétique', 'current')"
            )
    finally:
        engine.dispose()

    monkeypatch.delenv("COMPTES_ALLOW_DATA_LOSS_DOWNGRADE", raising=False)
    monkeypatch.delenv("COMPTES_DOWNGRADE_BACKUP", raising=False)
    with pytest.raises(RuntimeError, match="downgrade refusé.*données métier"):
        command.downgrade(_alembic_config(settings), "base")

    assert migrate.current_revision(settings.database_url) == "0001_initial"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM accounts").fetchone() == (1,)


@pytest.mark.integration
def test_T06_downgrade_succeeds_only_with_explicit_verified_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "downgrade-approved.sqlite"
    settings = _settings(database, tmp_path / "data")
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO accounts(account_id, alias, account_role) "
                "VALUES ('00000000-0000-4000-8000-000000000002', 'Synthétique', 'current')"
            )
            connection.exec_driver_sql("CREATE TABLE local_extension (value TEXT NOT NULL)")
            connection.exec_driver_sql(
                "INSERT INTO local_extension(value) VALUES ('preserve-on-downgrade')"
            )
    finally:
        engine.dispose()

    backup = create_verified_backup(
        database, settings.data_dir / "backups", source_revision="0001_initial"
    )
    monkeypatch.setenv("COMPTES_ALLOW_DATA_LOSS_DOWNGRADE", "1")
    monkeypatch.setenv("COMPTES_DOWNGRADE_BACKUP", str(backup.database_path))
    command.downgrade(_alembic_config(settings), "base")

    with sqlite3.connect(database) as connection:
        app_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name <> 'alembic_version'"
        ).fetchall()
        assert app_tables == [("local_extension",)]
        assert connection.execute("SELECT value FROM local_extension").fetchone() == (
            "preserve-on-downgrade",
        )
    with sqlite3.connect(backup.database_path) as copied:
        assert copied.execute("SELECT count(*) FROM accounts").fetchone() == (1,)
        assert copied.execute("SELECT count(*) FROM local_extension").fetchone() == (1,)


@pytest.mark.integration
def test_T06_health_reports_initialization_schema_age_and_revision(tmp_path: Path) -> None:
    database = tmp_path / "health.sqlite"
    settings = _settings(database, tmp_path / "data")

    with TestClient(create_app(settings)) as client:
        missing = client.get("/api/v1/health").json()
    assert missing["api_version"] == "1"
    assert missing["storage_status"] == "degraded"
    assert missing["llm_enabled"] is False
    assert missing["database"] == {"status": "not_initialized", "revision": None}

    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy_probe (value TEXT)")
    with TestClient(create_app(settings)) as client:
        outdated = client.get("/api/v1/health").json()
    assert outdated["api_version"] == "1"
    assert outdated["storage_status"] == "degraded"
    assert outdated["llm_enabled"] is False
    assert outdated["database"] == {"status": "outdated", "revision": None}

    upgrade_to_head(settings)
    with TestClient(create_app(settings)) as client:
        current = client.get("/api/v1/health").json()
    assert current["api_version"] == "1"
    assert current["storage_status"] == "ok"
    assert current["llm_enabled"] is False
    assert current["database"] == {"status": "ok", "revision": "0001_initial"}


@pytest.mark.integration
def test_OPS_02_backup_api_captures_wal_database_consistently(tmp_path: Path) -> None:
    database = tmp_path / "wal.sqlite"
    raw = sqlite3.connect(database)
    try:
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("CREATE TABLE snapshot_probe (value TEXT NOT NULL)")
        raw.execute("INSERT INTO snapshot_probe(value) VALUES ('wal-visible-row')")
        raw.commit()
        backup = create_verified_backup(database, tmp_path / "backups", source_revision=None)
    finally:
        raw.close()

    with sqlite3.connect(backup.database_path) as copied:
        assert copied.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert copied.execute("SELECT value FROM snapshot_probe").fetchone() == ("wal-visible-row",)
