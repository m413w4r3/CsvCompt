from __future__ import annotations

import json
import sqlite3
import stat
import time
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db import migrate
from app.db.migrate import (
    BackupError,
    MigrationError,
    create_verified_backup,
    database_state,
    upgrade_to_head,
)
from app.db.session import create_sqlite_engine
from app.main import create_app


def _settings(database: Path, data_dir: Path) -> Settings:
    return Settings(database_url=f"sqlite:///{database}", data_dir=data_dir, _env_file=None)


def _alembic_config(settings: Settings, *, operation: str | None = None) -> Config:
    config = Config(str(migrate.ALEMBIC_INI))
    config.attributes["settings"] = settings
    if operation is not None:
        config.attributes["operation"] = operation
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

    assert first.revision == second.revision == "0002_t17_refund_safety"
    assert first.backup is None and second.backup is None
    assert snapshot_after_first == snapshot_after_second
    assert migrate.current_revision(settings.database_url) == "0002_t17_refund_safety"


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
def test_MAJ_07_direct_alembic_upgrade_refuses_nonempty_database(tmp_path: Path) -> None:
    database = tmp_path / "legacy-direct.sqlite"
    settings = _settings(database, tmp_path / "data")
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_probe(value) VALUES ('keep-me')")
    before = _schema_snapshot(database)

    with pytest.raises(RuntimeError, match="upgrade direct refusé.*sauvegarde vérifiée"):
        command.upgrade(_alembic_config(settings, operation="upgrade"), "head")

    assert _schema_snapshot(database) == before
    assert not (settings.data_dir / "backups").exists()


@pytest.mark.integration
def test_M06_data_loss_flag_never_disables_foreign_keys_during_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPTES_ALLOW_DATA_LOSS_DOWNGRADE", "1")
    database = tmp_path / "upgrade-with-downgrade-flag.sqlite"
    result = upgrade_to_head(_settings(database, tmp_path / "data"))
    assert result.revision == "0002_t17_refund_safety"


@pytest.mark.integration
def test_MAJ_15_upgrade_failure_message_matches_backup_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_upgrade(*_args, **_kwargs) -> None:
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(migrate.command, "upgrade", fail_upgrade)
    fresh = tmp_path / "fresh.sqlite"
    with pytest.raises(MigrationError) as fresh_error:
        upgrade_to_head(_settings(fresh, tmp_path / "fresh-data"))
    assert str(fresh_error.value) == (
        "La migration Alembic a échoué; aucune sauvegarde pré-migration n'a été créée."
    )

    existing = tmp_path / "existing.sqlite"
    with sqlite3.connect(existing) as connection:
        connection.execute("CREATE TABLE legacy_probe (value INTEGER)")
    with pytest.raises(MigrationError) as existing_error:
        upgrade_to_head(_settings(existing, tmp_path / "existing-data"))
    message = str(existing_error.value)
    assert message.startswith(
        "La migration Alembic a échoué; la sauvegarde vérifiée est conservée: pre-upgrade-"
    )
    backup_name = message.rsplit(": ", maxsplit=1)[-1]
    assert (tmp_path / "existing-data" / "backups" / backup_name).is_file()


@pytest.mark.integration
def test_MAJ_07_upgrade_permissions_harden_existing_sqlite_files_and_data_dir(
    tmp_path: Path,
) -> None:
    database = tmp_path / "permissions.sqlite"
    data_directory = tmp_path / "data"
    settings = _settings(database, data_directory)
    upgrade_to_head(settings)
    database.chmod(0o644)
    data_directory.chmod(0o755)

    engine = create_sqlite_engine(settings.database_url)
    try:
        connection = engine.connect()
        try:
            connection.exec_driver_sql("SELECT count(*) FROM accounts").scalar_one()
            for journal_file in (Path(f"{database}-wal"), Path(f"{database}-shm")):
                if journal_file.exists():
                    journal_file.chmod(0o644)
            upgrade_to_head(settings)
            assert stat.S_IMODE(database.stat().st_mode) == 0o600
            assert stat.S_IMODE(data_directory.stat().st_mode) == 0o700
            for journal_file in (Path(f"{database}-wal"), Path(f"{database}-shm")):
                if journal_file.exists():
                    assert stat.S_IMODE(journal_file.stat().st_mode) == 0o600
        finally:
            connection.close()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MAJ_08_database_state_is_bounded_and_degraded_when_exclusively_locked(
    tmp_path: Path,
) -> None:
    database = tmp_path / "exclusive-lock.sqlite"
    settings = _settings(database, tmp_path / "data")
    upgrade_to_head(settings)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)

    blocker = sqlite3.connect(database, isolation_level=None)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        state, revision = database_state(settings)
        elapsed = time.monotonic() - started
        assert (state, revision) == ("degraded", None)
        assert elapsed < 0.5
    finally:
        blocker.rollback()
        blocker.close()


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
    with pytest.raises(
        RuntimeError,
        match="downgrade 0002 refusé.*COMPTES_ALLOW_DATA_LOSS_DOWNGRADE=1.*sauvegarde vérifiée",
    ):
        command.downgrade(_alembic_config(settings, operation="downgrade"), "base")

    assert migrate.current_revision(settings.database_url) == "0002_t17_refund_safety"
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
        database, settings.data_dir / "backups", source_revision="0002_t17_refund_safety"
    )
    monkeypatch.setenv("COMPTES_ALLOW_DATA_LOSS_DOWNGRADE", "1")
    monkeypatch.setenv("COMPTES_DOWNGRADE_BACKUP", str(backup.database_path))
    command.downgrade(_alembic_config(settings, operation="downgrade"), "0001_initial")

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0001_initial",
        )
        assert connection.execute("SELECT count(*) FROM accounts").fetchone() == (1,)
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='refund_link_allocation_parts'"
            ).fetchone()
            is None
        )
        assert connection.execute("SELECT value FROM local_extension").fetchone() == (
            "preserve-on-downgrade",
        )
        downgraded_objects = tuple(
            sorted(
                connection.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' "
                    "AND name NOT IN ('alembic_version', 'local_extension')"
                ).fetchall()
            )
        )
    frozen_schema = Path(__file__).resolve().parents[2] / "app/db/migrations/sql/0001_initial.sql"
    with sqlite3.connect(":memory:") as expected:
        expected.executescript(frozen_schema.read_text(encoding="utf-8"))
        frozen_objects = tuple(
            sorted(
                expected.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%'"
                ).fetchall()
            )
        )
    assert downgraded_objects == frozen_objects
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
    assert current["database"] == {"status": "ok", "revision": "0002_t17_refund_safety"}


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
