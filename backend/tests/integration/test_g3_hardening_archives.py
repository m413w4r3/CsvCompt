from __future__ import annotations

import errno
import multiprocessing
import os
import signal
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.db.errors import DatabaseBusyError
from app.db.migrate import upgrade_to_head
from app.db.models import AuditEvent
from app.main import create_app
from app.main import logger as main_logger
from app.services import import_archive, import_commit
from app.services.backups import _validate_target_originals
from app.services.import_archive import ImportArchive, purge_orphaned_archives

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _ready(tmp_path: Path, name: str):
    database = tmp_path / f"{name}.sqlite"
    data_dir = tmp_path / f"{name}-data"
    settings = Settings(database_url=f"sqlite:///{database}", data_dir=data_dir, _env_file=None)
    upgrade_to_head(settings)
    return create_app(settings), settings, database, data_dir


def _upload(client: TestClient, raw: bytes) -> dict[str, object]:
    response = client.post(
        "/api/v1/imports/preview",
        files={"file": ("synthetic.csv", raw, "text/csv")},
    )
    assert response.status_code == 200, response.text
    preview = response.json()
    mapped = client.patch(
        f"/api/v1/imports/{preview['preview_id']}/sections/1/account-mapping",
        json={
            "expected_version": preview["version"],
            "new_account": {"alias": "Compte archive synthétique", "account_role": "current"},
        },
    )
    assert mapped.status_code == 200, mapped.text
    return mapped.json()


def _commit(client: TestClient, preview: dict[str, object]):
    return client.post(
        f"/api/v1/imports/{preview['preview_id']}/commit",
        headers={"Idempotency-Key": str(uuid4())},
        json={"expected_version": preview["version"]},
    )


def _hold_archive_reservation(data_dir: str, ready, release) -> None:
    archive = ImportArchive(Path(data_dir))
    with archive.commit_reservation():
        ready.set()
        if not release.wait(timeout=15):
            raise RuntimeError("synthetic release timeout")


def _kill_commit_before_archive_replace(
    database_url: str,
    data_dir: str,
    preview_id: str,
    version: int,
) -> None:
    settings = Settings(database_url=database_url, data_dir=Path(data_dir), _env_file=None)
    app = create_app(settings)

    def kill_before_replace(*_args, **_kwargs) -> None:
        os.kill(os.getpid(), signal.SIGKILL)

    import_archive.os.replace = kill_before_replace
    import_commit.commit_preview(
        preview_id,
        version,
        str(uuid4()),
        "synthetic-kill",
        engine=app.state.db_engine,
        store=app.state.import_preview_store,
        data_dir=Path(data_dir),
    )


@pytest.mark.integration
def test_IMP_06_purge_cannot_remove_archive_between_write_and_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _settings, database, data_dir = _ready(tmp_path, "active-archive-reservation")
    raw = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    entered = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}
    original_write = ImportArchive.write

    def pause_after_write(archive: ImportArchive, import_file_id: str, content: bytes) -> Path:
        path = original_write(archive, import_file_id, content)
        old_mtime = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
        os.utime(path, (old_mtime, old_mtime))
        entered.set()
        if not release.wait(timeout=15):
            raise RuntimeError("synthetic commit release timeout")
        return path

    monkeypatch.setattr(ImportArchive, "write", pause_after_write)
    with TestClient(app) as client:
        preview = _upload(client, raw)

        def commit_worker() -> None:
            try:
                outcome["response"] = import_commit.commit_preview(
                    str(preview["preview_id"]),
                    int(preview["version"]),
                    str(uuid4()),
                    "synthetic-race",
                    engine=client.app.state.db_engine,
                    store=client.app.state.import_preview_store,
                    data_dir=data_dir,
                )
            except BaseException as error:
                outcome["error"] = error

        worker = threading.Thread(target=commit_worker)
        worker.start()
        assert entered.wait(timeout=10)
        archive_path = next((data_dir / "originals").glob("*.bin"))
        future = datetime.now(UTC) + timedelta(hours=26)
        purged = purge_orphaned_archives(client.app.state.db_engine, data_dir, now=future)
        assert purged == 0
        assert archive_path.exists()

        release.set()
        worker.join(timeout=20)
        assert not worker.is_alive()
        assert "error" not in outcome
        assert outcome["response"]["status"] == "committed"  # type: ignore[index]
        assert archive_path.exists()

    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT archive_relpath FROM import_files WHERE status='committed'"
        ).fetchone()
    assert stored is not None
    assert Path(stored[0]).name == archive_path.name


@pytest.mark.integration
def test_IMP_06_archive_reservation_blocks_purge_across_processes(tmp_path: Path) -> None:
    app, _settings, _database, data_dir = _ready(tmp_path, "archive-process-lock")
    originals = data_dir / "originals"
    originals.mkdir(parents=True, mode=0o700)
    orphan = originals / f"{uuid4()}.bin"
    orphan.write_bytes(b"synthetic orphan bytes")
    old_mtime = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
    os.utime(orphan, (old_mtime, old_mtime))

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_archive_reservation,
        args=(str(data_dir), ready, release),
    )
    process.start()
    try:
        assert ready.wait(timeout=10)
        future = datetime.now(UTC) + timedelta(hours=26)
        assert purge_orphaned_archives(app.state.db_engine, data_dir, now=future) == 0
        assert orphan.exists()
    finally:
        release.set()
        process.join(timeout=20)
    assert process.exitcode == 0

    future = datetime.now(UTC) + timedelta(hours=26)
    assert purge_orphaned_archives(app.state.db_engine, data_dir, now=future) == 1
    assert not orphan.exists()


@pytest.mark.integration
def test_IMP_06_tmp_purge_never_removes_an_in_progress_writer_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _settings, _database, data_dir = _ready(tmp_path, "tmp-in-progress")
    originals = data_dir / "originals"
    entered = threading.Event()
    release = threading.Event()
    output: dict[str, object] = {}
    original_replace = import_archive.os.replace

    def pause_after_fsync(source, target, *args, **kwargs):
        if Path(source).suffix == ".tmp":
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError("synthetic writer release timeout")
        return original_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(import_archive.os, "replace", pause_after_fsync)

    def write_archive() -> None:
        try:
            output["path"] = ImportArchive(data_dir).write(str(uuid4()), b"synthetic archive")
        except BaseException as error:
            output["error"] = error

    worker = threading.Thread(target=write_archive)
    worker.start()
    try:
        assert entered.wait(timeout=5)
        staging = data_dir / "originals.staging"
        temporary_files = list(staging.glob(".*.tmp"))
        assert len(temporary_files) == 1
        assert temporary_files[0].stat().st_dev == originals.stat().st_dev
        future = datetime.now(UTC) + timedelta(hours=26)
        assert purge_orphaned_archives(app.state.db_engine, data_dir, now=future) == 0
        assert temporary_files[0].exists()
    finally:
        release.set()
        worker.join(timeout=15)
    assert not worker.is_alive()
    assert "error" not in output
    archive_path = output["path"]
    assert isinstance(archive_path, Path) and archive_path.exists()
    future = datetime.now(UTC) + timedelta(hours=26)
    assert purge_orphaned_archives(app.state.db_engine, data_dir, now=future) == 1
    assert not archive_path.exists()


@pytest.mark.integration
def test_IMP_06_archive_purge_uses_maximum_mtime_ctime_and_cleans_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _settings, _database, data_dir = _ready(tmp_path, "archive-age-and-tmp")
    originals = data_dir / "originals"
    originals.mkdir(parents=True, mode=0o700)
    old_bin = originals / f"{uuid4()}.bin"
    staging = data_dir / "originals.staging"
    staging.mkdir(mode=0o700)
    old_tmp = staging / f".{uuid4()}.tmp"
    old_bin.write_bytes(b"synthetic aged archive")
    old_tmp.write_bytes(b"synthetic aged temporary archive")
    old_mtime = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
    os.utime(old_bin, (old_mtime, old_mtime))
    os.utime(old_tmp, (old_mtime, old_mtime))

    # mtime alone is insufficient: the fresh ctime retains both files.
    assert purge_orphaned_archives(app.state.db_engine, data_dir) == 0
    assert old_bin.exists() and old_tmp.exists()

    future = datetime.now(UTC) + timedelta(hours=26)
    original_unlink = Path.unlink
    audit_was_committed_before_delete: list[bool] = []

    def inspect_audit_before_delete(path: Path, *args, **kwargs):
        if path in {old_bin, old_tmp}:
            with app.state.db_engine.connect() as connection:
                audit_was_committed_before_delete.append(
                    connection.execute(
                        select(AuditEvent.action).where(
                            AuditEvent.action == "import.archive_orphan_purge_intent"
                        )
                    ).first()
                    is not None
                )
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", inspect_audit_before_delete)
    assert purge_orphaned_archives(app.state.db_engine, data_dir, now=future) == 2
    assert audit_was_committed_before_delete == [True, True]
    assert not old_bin.exists() and not old_tmp.exists()
    with app.state.db_engine.connect() as connection:
        event = connection.execute(
            select(AuditEvent.action, AuditEvent.after_json).where(
                AuditEvent.action == "import.archive_orphan_purge_intent"
            )
        ).one()
    assert event.after_json == '{"archive_count":1,"temporary_count":1}'
    assert ".tmp" not in event.after_json and "originals" not in event.after_json


@pytest.mark.integration
def test_IMP_06_archive_purge_audit_failure_keeps_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _settings, _database, data_dir = _ready(tmp_path, "archive-audit-failure")
    originals = data_dir / "originals"
    originals.mkdir(parents=True, mode=0o700)
    orphan = originals / f"{uuid4()}.bin"
    orphan.write_bytes(b"synthetic audit failure archive")
    future = datetime.now(UTC) + timedelta(hours=26)

    def audit_failure(_engine):
        raise DatabaseBusyError()

    monkeypatch.setattr(import_archive, "write_transaction", audit_failure)
    with pytest.raises(DatabaseBusyError):
        purge_orphaned_archives(app.state.db_engine, data_dir, now=future)
    assert orphan.exists()


@pytest.mark.integration
def test_IMP_06_sigkill_temp_is_purged_at_startup_after_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, settings, database, data_dir = _ready(tmp_path, "archive-tmp-sigkill")
    raw = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    with TestClient(app) as client:
        preview = _upload(client, raw)
    process = multiprocessing.get_context("spawn").Process(
        target=_kill_commit_before_archive_replace,
        args=(
            settings.database_url,
            str(data_dir),
            str(preview["preview_id"]),
            int(preview["version"]),
        ),
    )
    process.start()
    process.join(timeout=20)
    assert process.exitcode == -signal.SIGKILL

    originals = data_dir / "originals"
    staging = data_dir / "originals.staging"
    temporary_files = list(staging.glob(".*.tmp"))
    assert len(temporary_files) == 1
    assert list(originals.iterdir()) == []
    with sqlite3.connect(database) as connection:
        for table in ("transactions", "source_rows", "import_files", "accounts"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0

    # Startup before the grace expires keeps the raw temporary intact.
    with TestClient(create_app(settings)):
        assert temporary_files[0].exists()

    future = datetime.now(UTC) + timedelta(hours=26)

    class FutureDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return future if tz else future.replace(tzinfo=None)

    monkeypatch.setattr(import_archive, "datetime", FutureDateTime)
    with TestClient(create_app(settings)):
        assert not temporary_files[0].exists()


@pytest.mark.integration
def test_IMP_06_archive_temp_is_outside_restore_validated_originals(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "archive-restore-contract"
    archive = ImportArchive(data_dir)
    result = archive.write(str(uuid4()), b"synthetic archive")

    assert result.parent == data_dir / "originals"
    assert result.exists()
    assert list(archive.staging_directory.iterdir()) == []
    assert archive.staging_directory.stat().st_mode & 0o777 == 0o700
    assert archive.directory.stat().st_dev == archive.staging_directory.stat().st_dev
    _validate_target_originals(archive.directory)


@pytest.mark.integration
def test_IMP_06_lifecycle_lock_retries_if_replaced_while_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = ImportArchive(tmp_path / "archive-lock-replacement")
    holder = archive.acquire_commit_reservation()
    lock_path = archive.directory.parent / ".archive-lifecycle.lock"
    old_inode = os.fstat(holder.file_descriptor).st_ino
    descriptor_opened = threading.Event()
    real_open = archive._open_lifecycle_lock
    opened_count = 0

    def signal_open() -> int:
        nonlocal opened_count
        descriptor = real_open()
        opened_count += 1
        if opened_count == 1:
            descriptor_opened.set()
        return descriptor

    monkeypatch.setattr(archive, "_open_lifecycle_lock", signal_open)
    outcome: dict[str, object] = {}

    def wait_for_exclusive_lock() -> None:
        try:
            outcome["descriptor"] = archive.acquire_lifecycle_lock(import_archive.fcntl.LOCK_EX)
        except BaseException as error:
            outcome["error"] = error

    worker = threading.Thread(target=wait_for_exclusive_lock)
    worker.start()
    try:
        assert descriptor_opened.wait(timeout=5)
        lock_path.unlink()
        lock_path.touch(mode=0o600)
        ImportArchive.release_commit_reservation(holder)
        holder = -1
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert "error" not in outcome
        lock = outcome["descriptor"]
        assert isinstance(lock, import_archive.ArchiveLifecycleLock)
        descriptor_stat = os.fstat(lock.file_descriptor)
        path_stat = lock_path.stat()
        assert descriptor_stat.st_ino == path_stat.st_ino
        assert descriptor_stat.st_dev == path_stat.st_dev
        assert descriptor_stat.st_ino != old_inode
        assert opened_count >= 2
        archive.release_lifecycle_lock(lock)
    finally:
        if holder >= 0:
            ImportArchive.release_commit_reservation(holder)
        if worker.is_alive():
            worker.join(timeout=10)


@pytest.mark.integration
def test_IMP_06_purge_skips_if_lock_file_is_replaced_during_active_commit(
    tmp_path: Path,
) -> None:
    app, _settings, _database, data_dir = _ready(tmp_path, "archive-active-lock-replacement")
    archive = ImportArchive(data_dir)
    originals = data_dir / "originals"
    originals.mkdir(parents=True, mode=0o700)
    candidate = originals / f"{uuid4()}.bin"
    candidate.write_bytes(b"synthetic active archive")
    future = datetime.now(UTC) + timedelta(hours=26)
    lock = archive.acquire_commit_reservation()
    lock_path = data_dir / ".archive-lifecycle.lock"
    lock_path.unlink()
    lock_path.touch(mode=0o600)

    try:
        assert purge_orphaned_archives(app.state.db_engine, data_dir, now=future) == 0
        assert candidate.exists()
    finally:
        archive.release_commit_reservation(lock)

    assert purge_orphaned_archives(app.state.db_engine, data_dir, now=future) == 1
    assert not candidate.exists()


@pytest.mark.integration
def test_IMP_06_purge_only_removes_strict_uuid_archives(
    tmp_path: Path,
) -> None:
    app, _settings, _database, data_dir = _ready(tmp_path, "archive-strict-purge")
    originals = data_dir / "originals"
    originals.mkdir(parents=True, mode=0o700)
    valid = originals / f"{uuid4()}.bin"
    user_note = originals / "notes-utilisateur.bin"
    unrelated = originals / "foreign.txt"
    symlink = originals / f"{uuid4()}.bin"
    directory = originals / f"{uuid4()}.bin"
    external_target = tmp_path / "external-synthetic-target"
    for path in (valid, user_note, unrelated, external_target):
        path.write_bytes(b"synthetic bytes")
    symlink.symlink_to(external_target)
    directory.mkdir()
    old_mtime = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
    for path in (valid, user_note, unrelated, external_target):
        os.utime(path, (old_mtime, old_mtime))

    future = datetime.now(UTC) + timedelta(hours=26)
    assert purge_orphaned_archives(app.state.db_engine, data_dir, now=future) == 1
    assert not valid.exists()
    assert user_note.exists() and unrelated.exists()
    assert symlink.is_symlink() and directory.is_dir()
    assert external_target.exists()


@pytest.mark.integration
def test_IMP_06_archive_enolck_is_503_and_does_not_block_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _settings, _database, data_dir = _ready(tmp_path, "archive-enolck")
    (data_dir / "originals").mkdir(parents=True)
    real_flock = import_archive.fcntl.flock
    locked_operations: list[int] = []
    warning_messages: list[str] = []
    real_warning = main_logger.warning

    def enolck(descriptor: int, operation: int) -> None:
        locked_operations.append(operation)
        if operation & (import_archive.fcntl.LOCK_SH | import_archive.fcntl.LOCK_EX):
            raise OSError(errno.ENOLCK, "No locks available")
        real_flock(descriptor, operation)

    def record_warning(message: str, *args: object, **kwargs: object) -> None:
        warning_messages.append(message % args if args else message)
        real_warning(message, *args, **kwargs)

    monkeypatch.setattr(main_logger, "warning", record_warning)
    monkeypatch.setattr(import_archive.fcntl, "flock", enolck)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        preview = _upload(client, (FIXTURES / "ca_2026_simple.csv").read_bytes())
        response = _commit(client, preview)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ARCHIVE_LOCK_UNAVAILABLE"
    assert any(operation & import_archive.fcntl.LOCK_EX for operation in locked_operations)
    assert "archive_purge_skipped code=ARCHIVE_LOCK_UNAVAILABLE" in warning_messages
    assert list((data_dir / "originals").glob("*.bin")) == []
