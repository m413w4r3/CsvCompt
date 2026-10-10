from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db import migrate as migration
from app.db.migrate import upgrade_to_head
from app.db.models import Transaction
from app.db.session import session_scope
from app.main import create_app
from app.services import backups
from app.services.allocations import AllocationPart, allocate

FIXTURES = Path(__file__).parents[1] / "fixtures"
ROOT = Path(__file__).resolve().parents[3]
TEST_IMPORT_HEADERS = {
    "host": "127.0.0.1",
    "referer": "http://localhost:5173/transactions",
}


def _ready_app(tmp_path: Path, name: str) -> tuple[object, Path, Path]:
    database = tmp_path / f"{name}.sqlite"
    data_dir = tmp_path / f"{name}-data"
    settings = Settings(database_url=f"sqlite:///{database}", data_dir=data_dir, _env_file=None)
    upgrade_to_head(settings)
    return create_app(settings), database, data_dir


def _ready_generation_app(root: Path, name: str) -> tuple[object, Path, Path]:
    generation_root = root / name
    initial_generation = generation_root / "generations" / "initial"
    initial_generation.mkdir(parents=True, mode=0o700)
    (generation_root / "current").symlink_to(
        Path("generations") / "initial", target_is_directory=True
    )
    database = generation_root / "current" / "database.sqlite"
    data_dir = generation_root / "current" / "data"
    settings = Settings(database_url=f"sqlite:///{database}", data_dir=data_dir, _env_file=None)
    upgrade_to_head(settings)
    return create_app(settings), database, data_dir


def _import_fixture(
    app: object,
    *,
    classify: bool = True,
    fixture_name: str = "overlap_A.csv",
    fixture_bytes: bytes | None = None,
) -> tuple[Path, Path, str]:
    application = app
    database = Path(application.state.settings.database_url.removeprefix("sqlite:///"))
    data_dir = application.state.settings.data_dir
    raw = fixture_bytes if fixture_bytes is not None else (FIXTURES / fixture_name).read_bytes()
    with TestClient(application) as client:
        preview_response = client.post(
            "/api/v1/imports/preview",
            files={"file": ("synthetic.csv", raw, "text/csv")},
            headers=TEST_IMPORT_HEADERS,
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        mapped_response = client.patch(
            f"/api/v1/imports/{preview['preview_id']}/sections/1/account-mapping",
            json={
                "expected_version": preview["version"],
                "new_account": {
                    "alias": "Compte synthétique de sauvegarde",
                    "account_role": "current",
                },
            },
            headers=TEST_IMPORT_HEADERS,
        )
        assert mapped_response.status_code == 200, mapped_response.text
        mapped = mapped_response.json()
        committed = client.post(
            f"/api/v1/imports/{mapped['preview_id']}/commit",
            headers={"Idempotency-Key": str(uuid4()), **TEST_IMPORT_HEADERS},
            json={"expected_version": mapped["version"]},
        )
        assert committed.status_code == 200, committed.text
        import_id = str(committed.json()["import_file_id"])
        assert committed.json()["status"] == "committed"

        if classify:
            with session_scope(application.state.db_engine) as session:
                transaction = session.scalar(select_transaction_query())
                assert transaction is not None
                transaction_id = transaction.transaction_id
                amount_cents = transaction.amount_cents
            state = allocate(
                application.state.db_engine,
                transaction_id,
                [
                    AllocationPart(
                        flow_kind="expense",
                        category_id="groceries",
                        amount_cents=amount_cents,
                    )
                ],
                mode="complete",
                origin="manual",
                actor="testeur_synthetique",
                expected_version=1,
            )
            assert state.state == "complete"
    return database, data_dir, import_id


def select_transaction_query():
    from sqlalchemy import select

    return select(Transaction).where(Transaction.amount_cents < 0).order_by(Transaction.booked_on)


def _table_snapshot(database: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    with sqlite3.connect(database) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "AND name <> 'alembic_version' ORDER BY name"
            )
        ]
        result: dict[str, tuple[tuple[object, ...], ...]] = {}
        for table in tables:
            columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]
            order = ", ".join(f'"{column}"' for column in columns)
            result[table] = tuple(connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}'))
        return result


def _financial_snapshot(database: Path) -> tuple[object, ...]:
    with sqlite3.connect(database) as connection:
        return (
            connection.execute(
                "SELECT account_id,substr(booked_on,1,7),sum(amount_cents) "
                "FROM transactions GROUP BY account_id,substr(booked_on,1,7) "
                "ORDER BY account_id,substr(booked_on,1,7)"
            ).fetchall(),
            connection.execute("SELECT count(*) FROM source_rows").fetchone(),
            connection.execute("SELECT count(*) FROM classification_decisions").fetchone(),
            connection.execute("SELECT count(*) FROM allocations").fetchone(),
            connection.execute(
                "SELECT account_id,bank_fingerprint FROM accounts ORDER BY account_id"
            ).fetchall(),
        )


def _rewrite_manifest(source: Path, destination: Path, update) -> None:
    with zipfile.ZipFile(source, "r") as input_archive:
        contents = {item.filename: input_archive.read(item) for item in input_archive.infolist()}
    manifest = json.loads(contents["manifest.json"])
    update(manifest)
    contents["manifest.json"] = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as output_archive:
        for name, content in contents.items():
            output_archive.writestr(name, content)


def _rewrite_database(source: Path, destination: Path, mutate) -> None:
    with zipfile.ZipFile(source, "r") as input_archive:
        contents = {item.filename: input_archive.read(item) for item in input_archive.infolist()}
    manifest = json.loads(contents["manifest.json"])
    with tempfile.TemporaryDirectory() as temporary_directory:
        database_path = Path(temporary_directory) / "database.sqlite"
        database_path.write_bytes(contents["database.sqlite"])
        connection = sqlite3.connect(database_path)
        try:
            mutate(connection)
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        contents["database.sqlite"] = database_path.read_bytes()
    for entry in manifest["files"]:
        payload = contents[entry["path"]]
        entry["size_bytes"] = len(payload)
        entry["sha256"] = hashlib.sha256(payload).hexdigest()
    contents["manifest.json"] = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as output_archive:
        for name, content in contents.items():
            output_archive.writestr(name, content)


_WRITER_SCRIPT = r"""
import sqlite3, sys, time
database, started_path, stop_path = sys.argv[1:]
connection = sqlite3.connect(database, timeout=30, isolation_level=None)
counter = 0
while True:
    try:
        open(stop_path, "rb").close()
        break
    except FileNotFoundError:
        pass
    connection.execute("BEGIN IMMEDIATE")
    counter += 1
    value = str(counter)
    update = "UPDATE app_settings SET setting_value=?, version=version+1 WHERE setting_key=?"
    connection.execute(update, (value, "backup_writer_a"))
    connection.execute(update, (value, "backup_writer_b"))
    connection.execute("COMMIT")
    if counter == 1:
        open(started_path, "wb").close()
    time.sleep(0.001)
connection.close()
"""


@pytest.mark.integration
def test_OPS_01_wal_backup_and_restore_finish_during_large_concurrent_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, database, data_dir = _ready_app(tmp_path, "concurrent")
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO app_settings(setting_key,setting_value) VALUES (?,?)",
            ((f"backup-load-{index:04d}", "x" * 5000) for index in range(2100)),
        )
        connection.executemany(
            "INSERT INTO app_settings(setting_key,setting_value) VALUES (?,?)",
            (("backup_writer_a", "0"), ("backup_writer_b", "0")),
        )
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    assert page_count >= 2000

    monkeypatch.setenv("COMPTES_BACKUP_TIMEOUT_SECONDS", "15")
    started_path = tmp_path / "writer.started"
    stop_path = tmp_path / "writer.stop"
    writer = subprocess.Popen(
        [sys.executable, "-c", _WRITER_SCRIPT, str(database), str(started_path), str(stop_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while not started_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started_path.exists()
        with sqlite3.connect(database) as connection:
            before = int(
                connection.execute(
                    "SELECT setting_value FROM app_settings WHERE setting_key='backup_writer_a'"
                ).fetchone()[0]
            )
        backup_path = tmp_path / "concurrent.cfbackup"
        started = time.monotonic()
        result = backups.create_backup(database, data_dir, backup_path, retention_count=None)
        elapsed = time.monotonic() - started
        with sqlite3.connect(database) as connection:
            after = int(
                connection.execute(
                    "SELECT setting_value FROM app_settings WHERE setting_key='backup_writer_a'"
                ).fetchone()[0]
            )
        assert elapsed < 15
        assert writer.poll() is None
        assert after > before  # The second process committed during the snapshot.
        assert result.verification.schema_version == backups._installed_revision()
        assert backup_path.stat().st_mode & 0o777 == 0o600

        target_root = tmp_path / "concurrent-restore"
        target_database = target_root / "current" / "database.sqlite"
        target_data = target_root / "current" / "data"
        backups.restore_backup(
            backup_path,
            target_database,
            target_data,
            active_database_path=database,
            active_data_dir=data_dir,
        )
        assert writer.poll() is None  # The writer stayed active through restoration.
        with sqlite3.connect(target_database) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            paired_values = connection.execute(
                "SELECT setting_value FROM app_settings "
                "WHERE setting_key IN ('backup_writer_a','backup_writer_b') ORDER BY setting_key"
            ).fetchall()
        assert len(paired_values) == 2
        assert paired_values[0] == paired_values[1]

        snapshot_path = tmp_path / "concurrent-snapshot.sqlite"
        with zipfile.ZipFile(backup_path, "r") as archive:
            snapshot_path.write_bytes(archive.read("database.sqlite"))
        assert _table_snapshot(snapshot_path) == _table_snapshot(target_database)
    finally:
        stop_path.touch()
        writer.wait(timeout=5)


@pytest.mark.integration
def test_OPS_01_backup_timeout_is_clear_and_leaves_no_staging_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, database, data_dir = _ready_app(tmp_path, "backup-timeout")
    before = _table_snapshot(database)

    real_popen = subprocess.Popen
    workers: list[subprocess.Popen] = []

    def start_slow_worker(*_args, **_kwargs):
        worker = real_popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        workers.append(worker)
        return worker

    monkeypatch.setattr(backups.subprocess, "Popen", start_slow_worker)
    monkeypatch.setenv("COMPTES_BACKUP_TIMEOUT_SECONDS", "1")
    with pytest.raises(backups.BackupTimeoutError) as error:
        backups.create_backup(database, data_dir)
    assert error.value.code == "BACKUP_TIMEOUT"
    assert "BACKUP_TIMEOUT" in str(error.value)
    assert len(workers) == 1
    assert workers[0].returncode == -9
    assert _table_snapshot(database) == before
    staging = data_dir / "backups" / ".staging"
    assert staging.is_dir()
    assert list(staging.iterdir()) == []
    assert list((data_dir / "backups").glob("family-backup-*.cfbackup")) == []


@pytest.mark.integration
def test_OPS_01_custom_output_kill_staging_is_private_and_purged_on_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, database, data_dir = _ready_app(tmp_path, "custom-output-kill")
    output_directory = tmp_path / "custom-output"
    output_directory.mkdir()
    output_path = output_directory / "first.cfbackup"
    child_code = """
import os, signal, sys
from pathlib import Path
from app.services import backups
backups._same_filesystem = lambda *_args: False
backups._publish_no_replace = lambda *_args: os.kill(os.getpid(), signal.SIGKILL)
backups.create_backup(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), retention_count=None)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT / "backend"), environment.get("PYTHONPATH", "")) if part
    )
    killed = subprocess.run(
        [sys.executable, "-c", child_code, str(database), str(data_dir), str(output_path)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert killed.returncode == -9
    staging = data_dir / "backups" / ".staging"
    assert staging.is_dir()
    assert staging.stat().st_mode & 0o777 == 0o700
    assert list(staging.iterdir())
    assert len(list(output_directory.glob("._comptes-backup-publish.cfbackup.*.tmp"))) == 1

    next_output = output_directory / "second.cfbackup"
    monkeypatch.setattr(backups, "_same_filesystem", lambda _source, _directory: False)
    backups.create_backup(database, data_dir, next_output, retention_count=None)
    assert backups.verify_backup(next_output).schema_version == backups._installed_revision()
    assert list(staging.iterdir()) == []
    assert list(output_directory.glob("._comptes-backup-publish.cfbackup.*.tmp")) == []


@pytest.mark.integration
@pytest.mark.parametrize("held_lock", ["source", "output"])
def test_OPS_01_cross_process_backup_waits_before_purging_active_staging(
    tmp_path: Path, held_lock: str
) -> None:
    _app, database, data_dir = _ready_app(tmp_path, "staging-lock")
    backup_directory = data_dir / "backups"
    staging = backup_directory / ".staging"
    if held_lock == "source":
        lock_directory = backup_directory
        output_directory = backup_directory
        stale_entry = staging / "interrupted-stage"
        stale_entry.mkdir(parents=True)
        (stale_entry / "marker").write_text("synthetic", encoding="utf-8")
    else:
        lock_directory = tmp_path / "custom-output"
        lock_directory.mkdir()
        output_directory = lock_directory
        stale_entry = output_directory / "._comptes-backup-publish.cfbackup.stale.tmp"
        stale_entry.write_text("synthetic", encoding="utf-8")
    lock_busy = tmp_path / "lock-busy"
    lock_unexpected = tmp_path / "lock-unexpectedly-free"
    snapshot_started = tmp_path / "snapshot-started"
    output = output_directory / "cross-process.cfbackup"
    child_code = """
import os, sys
from pathlib import Path
from app.services import backups

real_flock = backups.fcntl.flock
expected = os.stat(sys.argv[5])
def notified_flock(descriptor, operation):
    actual = os.fstat(descriptor)
    if (
        (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)
        and operation & backups.fcntl.LOCK_EX
    ):
        try:
            real_flock(descriptor, operation | backups.fcntl.LOCK_NB)
        except BlockingIOError:
            Path(sys.argv[3]).touch()
        else:
            Path(sys.argv[4]).touch()
            return real_flock(descriptor, backups.fcntl.LOCK_UN)
    return real_flock(descriptor, operation)
backups.fcntl.flock = notified_flock
real_snapshot = backups._snapshot_database
def notified_snapshot(source, destination):
    Path(sys.argv[6]).touch()
    return real_snapshot(source, destination)
backups._snapshot_database = notified_snapshot
backups.create_backup(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[7]), retention_count=None)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT / "backend"), environment.get("PYTHONPATH", "")) if part
    )

    process: subprocess.Popen[bytes] | None = None
    try:
        with backups._backup_staging_lock(lock_directory):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child_code,
                    str(database),
                    str(data_dir),
                    str(lock_busy),
                    str(lock_unexpected),
                    str(lock_directory),
                    str(snapshot_started),
                    str(output),
                ],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 10
            while (
                not lock_busy.exists()
                and not lock_unexpected.exists()
                and time.monotonic() < deadline
            ):
                if process.poll() is not None:
                    pytest.fail("Le processus enfant s'est arrêté avant le verrouillage.")
                time.sleep(0.01)
            assert lock_busy.exists(), "flock n'a pas confirmé que le verrou était occupé."
            assert not lock_unexpected.exists()
            assert stale_entry.exists()
            assert not snapshot_started.exists()
        assert process.wait(timeout=30) == 0
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    assert snapshot_started.exists()
    assert not stale_entry.exists()
    assert backups.verify_backup(output).schema_version == backups._installed_revision()
    assert list(staging.iterdir()) == []


@pytest.mark.integration
def test_M02_restore_source_survives_concurrent_retention_by_private_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, database, data_dir = _ready_app(tmp_path, "restore-retention")
    source_backup = backups.create_backup(database, data_dir, retention_count=None).path
    real_restore = backups._restore_backup_locked

    def prune_source_then_restore(
        stable_source: Path,
        target_database: Path,
        target_data: Path,
        *,
        active_database_path: Path | None = None,
        active_data_dir: Path | None = None,
    ) -> backups.RestoreResult:
        assert stable_source != source_backup
        backups.create_backup(database, data_dir, retention_count=1)
        assert not source_backup.exists()
        return real_restore(
            stable_source,
            target_database,
            target_data,
            active_database_path=active_database_path,
            active_data_dir=active_data_dir,
        )

    monkeypatch.setattr(backups, "_restore_backup_locked", prune_source_then_restore)
    target_root = tmp_path / "restore-retention-target"
    backup_alias = tmp_path / "backup-directory-alias"
    backup_alias.symlink_to(source_backup.parent, target_is_directory=True)
    source_alias = tmp_path / "backup-file-alias.cfbackup"
    source_alias.symlink_to(source_backup)
    with pytest.raises(backups.BackupError, match="lien symbolique"):
        backups.restore_backup(
            source_alias,
            target_root / "current" / "database.sqlite",
            target_root / "current" / "data",
            active_database_path=tmp_path / "other-active.sqlite",
            active_data_dir=tmp_path / "other-active-data",
        )
    result = backups.restore_backup(
        backup_alias / source_backup.name,
        target_root / "current" / "database.sqlite",
        target_root / "current" / "data",
        active_database_path=tmp_path / "other-active.sqlite",
        active_data_dir=tmp_path / "other-active-data",
    )

    assert result.target_database.exists()
    assert result.verification.schema_version == backups._installed_revision()
    assert not source_backup.exists()


@pytest.mark.integration
def test_M05_killed_restore_archive_copy_is_private_and_purged_on_retry(
    tmp_path: Path,
) -> None:
    _app, database, data_dir = _ready_app(tmp_path, "restore-copy-kill")
    source_backup = backups.create_backup(database, data_dir, retention_count=None).path
    target_root = tmp_path / "restore-copy-target"
    target_database = target_root / "current" / "database.sqlite"
    target_data = target_root / "current" / "data"
    child_code = """
import os, signal, sys
from pathlib import Path
from app.services import backups
def stop_at_first_checkpoint(step):
    if step == 1:
        os.kill(os.getpid(), signal.SIGKILL)
backups._restore_checkpoint = stop_at_first_checkpoint
backups.restore_backup(
    Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]),
    active_database_path=Path(sys.argv[4]), active_data_dir=Path(sys.argv[5]),
)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT / "backend"), environment.get("PYTHONPATH", "")) if part
    )
    killed = subprocess.run(
        [
            sys.executable,
            "-c",
            child_code,
            str(source_backup),
            str(target_database),
            str(target_data),
            str(tmp_path / "other-active.sqlite"),
            str(tmp_path / "other-active-data"),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert killed.returncode == -9
    abandoned = list(target_root.glob(".restore-archive-*.tmp"))
    assert len(abandoned) == 1
    assert abandoned[0].stat().st_mode & 0o777 == 0o600

    backups.restore_backup(
        source_backup,
        target_database,
        target_data,
        active_database_path=tmp_path / "other-active.sqlite",
        active_data_dir=tmp_path / "other-active-data",
    )
    assert target_database.exists()
    assert list(target_root.glob(".restore-archive-*.tmp")) == []
    assert not any(
        path.name.startswith(".restore-") for path in (target_root / "generations").iterdir()
    )


@pytest.mark.integration
def test_T06_pre_migration_backup_uses_shared_timeout_and_cleans_partial_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, database, _data_dir = _ready_app(tmp_path, "migration-backup-timeout")

    def timeout(_source: Path, _destination: Path) -> None:
        raise backups.BackupTimeoutError(
            "BACKUP_TIMEOUT : la capture SQLite a dépassé son délai configurable."
        )

    monkeypatch.setattr(backups, "_snapshot_database", timeout)
    backup_directory = tmp_path / "pre-upgrade-backups"
    with pytest.raises(migration.BackupError, match="BACKUP_TIMEOUT"):
        migration.create_verified_backup(
            database, backup_directory, source_revision=backups._installed_revision()
        )
    assert list(backup_directory.iterdir()) == []


@pytest.mark.integration
def test_OPS_02_restore_fixture_import_to_distinct_database_exactly(tmp_path: Path) -> None:
    app, source_database, source_data = _ready_app(tmp_path, "source")
    formula_labels = ("=1+1", "@SUM(1+1)", "+1", "-1", "\t=1+1", "\r=1+1")
    fixture_text = (FIXTURES / "overlap_A.csv").read_bytes().decode("cp1252")
    for label in formula_labels:
        fixture_text = fixture_text.replace('"Paiement par carte', '"' + label, 1)
    database, data_dir, import_id = _import_fixture(
        app, fixture_bytes=fixture_text.encode("cp1252")
    )
    assert database == source_database
    assert data_dir == source_data

    archive_path = source_data / "originals" / f"{import_id}.bin"
    assert archive_path.is_file()
    backup_path = tmp_path / "full-backup.cfbackup"
    result = backups.create_backup(source_database, source_data, backup_path, retention_count=None)
    assert result.verification.includes_originals is True
    assert result.verification.includes_hmac_key is True
    with zipfile.ZipFile(backup_path, "r") as archive:
        manifest_text = archive.read("manifest.json").decode("utf-8")
        with sqlite3.connect(source_database) as connection:
            hmac_value = connection.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key=?",
                ("import.account_hmac_key.v1",),
            ).fetchone()[0]
            raw_label = connection.execute("SELECT raw_label FROM transactions LIMIT 1").fetchone()[
                0
            ]
        assert raw_label not in manifest_text
        assert "bank_fingerprint" not in manifest_text
        assert hmac_value not in manifest_text

    target_root = tmp_path / "restored"
    target_database = target_root / "current" / "database.sqlite"
    target_data = target_root / "current" / "data"
    restored = backups.restore_backup(
        backup_path,
        target_database,
        target_data,
        active_database_path=source_database,
        active_data_dir=source_data,
    )
    assert restored.safety_backup is None
    assert _table_snapshot(source_database) == _table_snapshot(target_database)
    assert _financial_snapshot(source_database) == _financial_snapshot(target_database)
    restored_archive = target_data / "originals" / f"{import_id}.bin"
    assert restored_archive.read_bytes() == archive_path.read_bytes()

    # Repeating the same restore leaves the restored application data identical.
    before_repeat = _table_snapshot(target_database)
    repeat = backups.restore_backup(
        backup_path,
        target_database,
        target_data,
        active_database_path=source_database,
        active_data_dir=source_data,
    )
    assert repeat.safety_backup is not None
    assert (
        backups.verify_backup(repeat.safety_backup).schema_version
        == backups._installed_revision()
    )
    assert _table_snapshot(target_database) == before_repeat

    json_export = tmp_path / "operations.json"
    csv_export = tmp_path / "operations.csv"
    french_csv_export = tmp_path / "operations-fr.csv"
    assert backups.export_data(source_database, json_export, format="json") > 0
    assert backups.export_data(source_database, csv_export, format="csv") > 0
    assert backups.export_data(source_database, french_csv_export, format="csv", delimiter=";") > 0
    export_object = json.loads(json_export.read_text(encoding="utf-8"))
    assert export_object["transactions"][0]["amount_cents"] < 0
    assert export_object["transactions"][0]["amount_decimal"].endswith(
        f".{abs(export_object['transactions'][0]['amount_cents']) % 100:02d}"
    )
    assert export_object["decisions"]
    assert "import.account_hmac_key.v1" not in json_export.read_text(encoding="utf-8")
    assert hmac_value not in json_export.read_text(encoding="utf-8")
    assert "bank_fingerprint" not in json_export.read_text(encoding="utf-8")
    assert json_export.stat().st_mode & 0o777 == 0o600
    assert csv_export.stat().st_mode & 0o777 == 0o600
    assert french_csv_export.stat().st_mode & 0o777 == 0o600
    with csv_export.open("r", encoding="utf-8-sig", newline="") as stream:
        exported_rows = list(csv.DictReader(stream))
    with french_csv_export.open("r", encoding="utf-8-sig", newline="") as stream:
        french_rows = list(csv.DictReader(stream, delimiter=";"))
    exported_labels = [
        row["raw_label"] for row in exported_rows if row["record_type"] == "transaction"
    ]
    assert all(
        any(value.startswith("'" + label) for value in exported_labels) for label in formula_labels
    )
    negative_row = next(
        row
        for row in exported_rows
        if row["record_type"] == "transaction" and row["amount_cents"].startswith("-")
    )
    assert negative_row["amount_decimal"].startswith("-")
    assert negative_row["amount_cents"].startswith("-")
    assert any(row["record_type"] == "decision" for row in exported_rows)
    assert len(french_rows) == len(exported_rows)
    assert french_csv_export.read_text(encoding="utf-8-sig").splitlines()[0].count(";") > 5
    assert hmac_value not in csv_export.read_text(encoding="utf-8-sig")


@pytest.mark.integration
def test_OPS_02_reimport_after_restore_is_no_op_then_known_import_commits(
    tmp_path: Path,
) -> None:
    source_app, source_database, source_data = _ready_app(tmp_path, "reimport-source")
    raw_a = (FIXTURES / "overlap_A.csv").read_bytes()
    _import_fixture(source_app, classify=False)
    backup_path = tmp_path / "reimport.cfbackup"
    backups.create_backup(source_database, source_data, backup_path, retention_count=None)

    target_root = tmp_path / "reimport-target"
    target_database = target_root / "current" / "database.sqlite"
    target_data = target_root / "current" / "data"
    backups.restore_backup(
        backup_path,
        target_database,
        target_data,
        active_database_path=source_database,
        active_data_dir=source_data,
    )
    restored_app = create_app(
        Settings(
            database_url=f"sqlite:///{target_database}",
            data_dir=target_data,
            _env_file=None,
        )
    )

    with TestClient(restored_app) as client:
        preview_response = client.post(
            "/api/v1/imports/preview",
            files={"file": ("synthetic.csv", raw_a, "text/csv")},
            headers=TEST_IMPORT_HEADERS,
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert all(
            section["account_mapping"]["status"] == "known" for section in preview["sections"]
        )
        no_op = client.post(
            f"/api/v1/imports/{preview['preview_id']}/commit",
            headers={"Idempotency-Key": str(uuid4()), **TEST_IMPORT_HEADERS},
            json={"expected_version": preview["version"]},
        )
        assert no_op.status_code == 200, no_op.text
        assert no_op.json()["status"] == "no_op"

        raw_b = (FIXTURES / "overlap_B.csv").read_bytes()
        next_preview_response = client.post(
            "/api/v1/imports/preview",
            files={"file": ("synthetic-b.csv", raw_b, "text/csv")},
            headers=TEST_IMPORT_HEADERS,
        )
        assert next_preview_response.status_code == 200, next_preview_response.text
        next_preview = next_preview_response.json()
        assert all(
            section["account_mapping"]["status"] == "known" for section in next_preview["sections"]
        )
        committed = client.post(
            f"/api/v1/imports/{next_preview['preview_id']}/commit",
            headers={"Idempotency-Key": str(uuid4()), **TEST_IMPORT_HEADERS},
            json={"expected_version": next_preview["version"]},
        )
        assert committed.status_code == 200, committed.text
        assert committed.json()["status"] == "committed"

    source_preview = TestClient(source_app)
    with source_preview as client:
        source_response = client.post(
            "/api/v1/imports/preview",
            files={"file": ("synthetic-b.csv", raw_b, "text/csv")},
            headers=TEST_IMPORT_HEADERS,
        )
        assert source_response.status_code == 200, source_response.text
        source_preview_body = source_response.json()
        assert all(
            section["account_mapping"]["status"] == "known"
            for section in source_preview_body["sections"]
        )
        source_commit = client.post(
            f"/api/v1/imports/{source_preview_body['preview_id']}/commit",
            headers={"Idempotency-Key": str(uuid4()), **TEST_IMPORT_HEADERS},
            json={"expected_version": source_preview_body["version"]},
        )
        assert source_commit.status_code == 200, source_commit.text
        assert source_commit.json()["status"] == "committed"
    assert _financial_snapshot(source_database) == _financial_snapshot(target_database)


@pytest.mark.integration
@pytest.mark.parametrize(
    "kind", ["manifest", "future", "truncated", "changed_byte", "missing_triggers"]
)
def test_OPS_02_rejects_corrupt_incoherent_future_or_truncated_backup(
    tmp_path: Path, kind: str
) -> None:
    app, database, data_dir = _ready_app(tmp_path, f"invalid-{kind}")
    _import_fixture(app, classify=False)
    original = tmp_path / "valid.cfbackup"
    backups.create_backup(database, data_dir, original, retention_count=None)
    candidate = tmp_path / f"{kind}.cfbackup"

    if kind == "manifest":

        def inconsistent(manifest):
            manifest["table_counts"]["transactions"] += 1

        _rewrite_manifest(original, candidate, inconsistent)
    elif kind == "future":

        def future_schema(manifest):
            manifest["schema_version"] = "9999_future"
            manifest["alembic_revision"] = "9999_future"

        _rewrite_manifest(original, candidate, future_schema)
    elif kind == "missing_triggers":

        def drop_triggers(connection: sqlite3.Connection) -> None:
            triggers = [
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
            ]
            assert triggers
            for name in triggers:
                connection.execute(f'DROP TRIGGER "{name}"')

        _rewrite_database(original, candidate, drop_triggers)
    elif kind == "truncated":
        candidate.write_bytes(original.read_bytes()[:-32])
    else:
        altered = bytearray(original.read_bytes())
        altered[len(altered) // 2] ^= 0x01
        candidate.write_bytes(altered)

    with pytest.raises(backups.BackupError) as error:
        backups.verify_backup(candidate)
    if kind == "future":
        assert "inconnue" in str(error.value)
    if kind == "missing_triggers":
        assert "triggers" in str(error.value)


@pytest.mark.integration
def test_OPS_02_backup_rejects_secret_settings_other_than_hmac(tmp_path: Path) -> None:
    app, database, data_dir = _ready_app(tmp_path, "unexpected-secret")
    _import_fixture(app, classify=False)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO app_settings(setting_key,setting_value,is_secret) VALUES (?,?,1)",
            ("synthetic.unapproved-secret", "synthetic-only"),
        )
    with pytest.raises(backups.BackupError, match="secret applicatif non autorisé"):
        backups.create_backup(
            database, data_dir, tmp_path / "secret.cfbackup", retention_count=None
        )


@pytest.mark.integration
def test_OPS_02_known_ancestor_revision_restores_then_migrates_with_prebackup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, database, data_dir = _ready_app(tmp_path, "ancestor-source")
    _import_fixture(app, classify=False)
    head = backups._installed_revision()
    current_backup = tmp_path / "ancestor-current.cfbackup"
    backups.create_backup(database, data_dir, current_backup, retention_count=None)

    fake_revision = "0000_fake_ancestor"
    fake_backup = tmp_path / "ancestor-fake.cfbackup"
    _rewrite_database(
        current_backup,
        fake_backup,
        lambda connection: connection.execute(
            "UPDATE alembic_version SET version_num=?", (fake_revision,)
        ),
    )
    revised_backup = tmp_path / "ancestor-revision.cfbackup"

    def declare_fake_ancestor(manifest: dict[str, object]) -> None:
        manifest["schema_version"] = fake_revision
        manifest["alembic_revision"] = fake_revision

    _rewrite_manifest(fake_backup, revised_backup, declare_fake_ancestor)

    original_reference = backups._reference_schema_objects
    current_schema = original_reference(head, backups._migration_files_signature())
    monkeypatch.setattr(
        backups,
        "_known_revisions",
        lambda: (head, {head, fake_revision}),
    )
    monkeypatch.setattr(
        backups,
        "_reference_schema_objects",
        lambda revision, signature: (
            current_schema if revision == fake_revision else original_reference(revision, signature)
        ),
    )
    assert fake_revision in backups._known_revisions()[1]
    assert backups.verify_backup(revised_backup).schema_version == fake_revision

    def simulated_migration(settings: Settings) -> None:
        staged_database = migration.database_file_path(settings.database_url)
        assert staged_database is not None
        migration.create_verified_backup(
            staged_database,
            settings.data_dir / "backups",
            source_revision=fake_revision,
        )
        with sqlite3.connect(staged_database) as connection:
            connection.execute("UPDATE alembic_version SET version_num=?", (head,))

    monkeypatch.setattr(backups, "upgrade_to_head", simulated_migration)
    target_root = tmp_path / "ancestor-target"
    target_database = target_root / "current" / "database.sqlite"
    target_data = target_root / "current" / "data"
    restored = backups.restore_backup(
        revised_backup,
        target_database,
        target_data,
        active_database_path=database,
        active_data_dir=data_dir,
    )
    assert restored.verification.schema_version == fake_revision
    with sqlite3.connect(target_database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (head,)
    assert len(list((target_data / "backups").glob("pre-upgrade-*.sqlite"))) == 1
    assert len(list((target_data / "backups").glob("pre-upgrade-*.manifest.json"))) == 1


@pytest.mark.integration
def test_OPS_02_decompression_limit_is_independent_of_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, database, data_dir = _ready_app(tmp_path, "decompression-limit")
    _import_fixture(app, classify=False)
    backup_path = tmp_path / "decompression-limit.cfbackup"
    backups.create_backup(database, data_dir, backup_path, retention_count=None)
    monkeypatch.setattr(backups, "_MAX_BACKUP_MEMBER_BYTES", 1)
    with pytest.raises(backups.BackupError, match="plafond indépendant"):
        backups.verify_backup(backup_path)


@pytest.mark.integration
def test_OPS_03_exports_under_unignored_repository_paths_are_refused(tmp_path: Path) -> None:
    _app, database, data_dir = _ready_app(tmp_path, "repository-output-refusal")
    backup_output = ROOT / f"t41-refused-{uuid4()}.cfbackup"
    export_output = ROOT / f"family-export-refused-{uuid4()}.json"
    with pytest.raises(backups.BackupError, match="Destination refusée sous la racine du dépôt"):
        backups.create_backup(database, data_dir, backup_output, retention_count=None)
    with pytest.raises(backups.BackupError, match="Destination refusée sous la racine du dépôt"):
        backups.export_data(database, export_output, format="json")
    assert not backup_output.exists()
    assert not export_output.exists()


@pytest.mark.integration
def test_OPS_02_mid_restore_failure_rolls_back_existing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source_app, source_database, source_data = _ready_app(tmp_path, "rollback-source")
    _import_fixture(source_app, classify=False)
    backup_path = tmp_path / "rollback-source.cfbackup"
    backups.create_backup(source_database, source_data, backup_path, retention_count=None)

    target_app, target_database, target_data = _ready_generation_app(tmp_path, "rollback-target")
    del target_app
    target_originals = target_data / "originals"
    target_originals.mkdir(mode=0o700)
    sentinel_name = f"{uuid4()}.bin"
    sentinel_bytes = b"synthetic-target-archive"
    (target_originals / sentinel_name).write_bytes(sentinel_bytes)
    before = _table_snapshot(target_database)
    original_rename = backups._rename_path

    def fail_install(source: Path, target: Path) -> None:
        if source.name.startswith(".restore-") and source.parent.name == "generations":
            raise OSError("synthetic-private-failure")
        original_rename(source, target)

    monkeypatch.setattr(backups, "_rename_path", fail_install)
    with pytest.raises(backups.BackupError, match="cible précédente reste intacte") as error:
        backups.restore_backup(
            backup_path,
            target_database,
            target_data,
            active_database_path=source_database,
            active_data_dir=source_data,
        )
    assert "synthetic-private-failure" not in str(error.value)
    assert _table_snapshot(target_database) == before
    assert (target_originals / sentinel_name).read_bytes() == sentinel_bytes
    safety_backups = list(
        (tmp_path / "rollback-target" / "generations").rglob("restore-safety-*.cfbackup")
    )
    assert len(safety_backups) == 1
    assert backups.verify_backup(safety_backups[0]).schema_version == backups._installed_revision()
    assert "synthetic-private-failure" not in caplog.text


@pytest.mark.integration
def test_OPS_02_restore_refuses_non_uuid_bin_original_files(tmp_path: Path) -> None:
    source_app, source_database, source_data = _ready_app(tmp_path, "non-bin-source")
    _import_fixture(source_app, classify=False)
    backup_path = tmp_path / "non-bin-source.cfbackup"
    backups.create_backup(source_database, source_data, backup_path, retention_count=None)

    target_app, target_database, target_data = _ready_generation_app(tmp_path, "non-bin-target")
    del target_app
    originals = target_data / "originals"
    originals.mkdir(mode=0o700)
    note = originals / "notes-synthetiques.txt"
    inflight = originals / ".partial.tmp"
    note.write_text("synthetic", encoding="utf-8")
    inflight.write_bytes(b"synthetic")
    before = _table_snapshot(target_database)

    with pytest.raises(backups.BackupError, match="autre que <uuid>.bin"):
        backups.restore_backup(
            backup_path,
            target_database,
            target_data,
            active_database_path=source_database,
            active_data_dir=source_data,
        )
    assert note.read_text(encoding="utf-8") == "synthetic"
    assert inflight.read_bytes() == b"synthetic"
    assert _table_snapshot(target_database) == before


@pytest.mark.integration
def test_OPS_02_restore_refuses_target_held_by_another_writer(tmp_path: Path) -> None:
    source_app, source_database, source_data = _ready_app(tmp_path, "busy-source")
    _import_fixture(source_app, classify=False)
    backup_path = tmp_path / "busy-source.cfbackup"
    backups.create_backup(source_database, source_data, backup_path, retention_count=None)

    target_app, target_database, target_data = _ready_generation_app(tmp_path, "busy-target")
    _import_fixture(target_app, classify=False, fixture_name="overlap_B.csv")
    before = _table_snapshot(target_database)
    active_generation = (tmp_path / "busy-target" / "current").resolve()
    writer = sqlite3.connect(target_database, timeout=2, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "UPDATE app_settings SET version=version+1 WHERE setting_key='import.account_hmac_key.v1'"
    )
    try:
        with pytest.raises(backups.BackupError, match="base cible est occupée"):
            backups.restore_backup(
                backup_path,
                target_database,
                target_data,
                active_database_path=source_database,
                active_data_dir=source_data,
            )
    finally:
        writer.execute("COMMIT")
        writer.close()
    after = _table_snapshot(target_database)
    assert (tmp_path / "busy-target" / "current").resolve() == active_generation
    assert after["import_files"] == before["import_files"]
    assert after["app_settings"] != before["app_settings"]


@pytest.mark.integration
def test_OPS_02_restore_kill_points_keep_whole_generation_and_allow_retry(
    tmp_path: Path,
) -> None:
    source_app, source_database, source_data = _ready_app(tmp_path, "kill-source")
    _import_fixture(source_app, classify=False)
    backup_path = tmp_path / "kill-source.cfbackup"
    backups.create_backup(source_database, source_data, backup_path, retention_count=None)
    source_tables = _table_snapshot(source_database)
    source_archives = {
        path.name: path.read_bytes() for path in (source_data / "originals").glob("*.bin")
    }

    target_app, template_database, template_data = _ready_generation_app(tmp_path, "kill-template")
    _import_fixture(target_app, classify=False, fixture_name="overlap_B.csv")
    target_app.state.db_engine.dispose()
    template_root = tmp_path / "kill-template"
    template_tables = _table_snapshot(template_database)
    template_archives = {
        path.name: path.read_bytes() for path in (template_data / "originals").glob("*.bin")
    }
    python_path = str(ROOT / "backend")
    child_code = """
import os, signal, sys
from pathlib import Path
from app.services import backups
kill_step = int(sys.argv[1])
def checkpoint(step):
    if step == kill_step:
        os.kill(os.getpid(), signal.SIGKILL)
backups._restore_checkpoint = checkpoint
backups.restore_backup(
    Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]),
    active_database_path=Path(sys.argv[5]), active_data_dir=Path(sys.argv[6]),
)
"""

    for kill_step in range(1, 7):
        target_root = tmp_path / f"kill-{kill_step}"
        shutil.copytree(template_root, target_root, symlinks=True)
        target_database = target_root / "current" / "database.sqlite"
        target_data = target_root / "current" / "data"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (python_path, environment.get("PYTHONPATH", "")) if part
        )
        environment["DATABASE_URL"] = f"sqlite:///{source_database}"
        environment["DATA_DIR"] = str(source_data)
        killed = subprocess.run(
            [
                sys.executable,
                "-c",
                child_code,
                str(kill_step),
                str(backup_path),
                str(target_database),
                str(target_data),
                str(source_database),
                str(source_data),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert killed.returncode == -9
        expected_tables = source_tables if kill_step == 6 else template_tables
        expected_archives = source_archives if kill_step == 6 else template_archives
        assert _table_snapshot(target_database) == expected_tables
        actual_archives = {
            path.name: path.read_bytes() for path in (target_data / "originals").glob("*.bin")
        }
        assert actual_archives == expected_archives

        backups.restore_backup(
            backup_path,
            target_database,
            target_data,
            active_database_path=source_database,
            active_data_dir=source_data,
        )
        assert _table_snapshot(target_database) == source_tables
        assert {
            path.name: path.read_bytes() for path in (target_data / "originals").glob("*.bin")
        } == source_archives
        generations = target_root / "generations"
        assert not any(path.name.startswith(".restore-") for path in generations.iterdir())
        assert not list(target_root.glob(".current-*.tmp"))
        assert not list(target_root.rglob("*.old"))


@pytest.mark.integration
def test_OPS_02_restore_refuses_active_database_and_no_private_error_output(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app, database, data_dir = _ready_app(tmp_path, "active-refusal")
    _import_fixture(app, classify=False)
    backup_path = tmp_path / "active-refusal.cfbackup"
    backups.create_backup(database, data_dir, backup_path, retention_count=None)
    with pytest.raises(backups.BackupError, match="base distincte") as error:
        backups.restore_backup(
            backup_path,
            database,
            tmp_path / "not-used-data",
            active_database_path=database,
            active_data_dir=data_dir,
        )
    assert str(database) not in str(error.value)
    assert "raw_label" not in caplog.text


@pytest.mark.integration
def test_restore_refusal_does_not_change_active_generation_permissions(tmp_path: Path) -> None:
    _source_app, source_database, source_data = _ready_app(tmp_path, "active-mode-source")
    backup_path = backups.create_backup(source_database, source_data).path
    _target_app, target_database, target_data = _ready_generation_app(
        tmp_path, "active-mode-target"
    )
    target_root = tmp_path / "active-mode-target"
    target_root.chmod(0o750)
    before_mode = target_root.stat().st_mode & 0o777

    with pytest.raises(backups.BackupError, match="base distincte"):
        backups.restore_backup(
            backup_path,
            target_database,
            target_data,
            active_database_path=target_database,
            active_data_dir=target_data,
        )

    assert target_root.stat().st_mode & 0o777 == before_mode


@pytest.mark.unit
def test_OPS_02_decimal_export_format_uses_integer_cents_only() -> None:
    assert backups._decimal_amount(0) == "0.00"
    assert backups._decimal_amount(1) == "0.01"
    assert backups._decimal_amount(-12345) == "-123.45"
    with pytest.raises(backups.BackupError):
        backups._decimal_amount(12.5)  # type: ignore[arg-type]


@pytest.mark.integration
def test_OPS_01_backup_publishes_complete_file_refuses_overwrite_and_applies_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, database, data_dir = _ready_app(tmp_path, "retention")
    del app
    output = tmp_path / "chosen.cfbackup"

    original_link = os.link
    publication_observations: list[tuple[bool, bool]] = []

    def observe_atomic_link(source, destination, **kwargs):
        if Path(destination) == output:
            destination_existed = Path(destination).exists()
            with zipfile.ZipFile(source, "r") as archive:
                complete_archive = set(archive.namelist()) == {"database.sqlite", "manifest.json"}
            publication_observations.append((destination_existed, complete_archive))
        original_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", observe_atomic_link)
    backups.create_backup(database, data_dir, output, retention_count=None)
    assert publication_observations == [(False, True)]
    monkeypatch.setattr(os, "link", original_link)
    original_bytes = output.read_bytes()
    with pytest.raises(backups.BackupError, match="n'a pas été écrasé"):
        backups.create_backup(database, data_dir, output, retention_count=None)
    assert output.read_bytes() == original_bytes

    for _ in range(3):
        backups.create_backup(database, data_dir, retention_count=2)
    retained = list((data_dir / "backups").glob("family-backup-*.cfbackup"))
    assert len(retained) == 2
    assert all(
        backups.verify_backup(path).schema_version == backups._installed_revision()
        for path in retained
    )


@pytest.mark.integration
def test_OPS_01_retention_uses_valid_named_auto_backups_and_never_prunes_output(
    tmp_path: Path,
) -> None:
    _app, database, data_dir = _ready_app(tmp_path, "retention-cases")
    first = backups.create_backup(database, data_dir, retention_count=None).path
    second = backups.create_backup(database, data_dir, retention_count=None).path
    future = time.time() + 3600
    os.utime(first, (future, future))
    os.utime(second, (future, future))
    published = backups.create_backup(database, data_dir, retention_count=2).path
    assert published.is_file()

    backup_directory = data_dir / "backups"
    good_old = backups.create_backup(database, data_dir, retention_count=None).path
    broken = []
    for _ in range(2):
        broken_path = backups.create_backup(database, data_dir, retention_count=None).path
        broken_path.write_bytes(broken_path.read_bytes()[:100])
        broken.append(broken_path)
    latest = backups.create_backup(database, data_dir, retention_count=2).path
    assert good_old.is_file()
    assert latest.is_file()
    for path in (good_old, latest):
        assert backups.verify_backup(path).schema_version == backups._installed_revision()
    assert all(path.exists() for path in broken)  # Invalid files do not count toward pruning.

    shared = tmp_path / "shared-output"
    shared.mkdir()
    unrelated = shared / "family-backup-photos.cfbackup"
    unrelated.write_bytes(b"user-owned synthetic file")
    manual = shared / "family-backup-manual.cfbackup"
    backups.create_backup(database, data_dir, manual, retention_count=1)
    assert manual.is_file()
    assert unrelated.read_bytes() == b"user-owned synthetic file"
    assert len(list(backup_directory.glob("family-backup-*.cfbackup"))) >= 1
