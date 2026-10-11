"""Private, opaque, raw-byte archives for committed imports."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app.core.errors import AppError
from app.db.models import AuditEvent, ImportFile
from app.db.session import session_scope, write_transaction

_UUID_ARCHIVE_FILENAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.bin$"
)
_UUID_TEMP_FILENAME = re.compile(
    r"^\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.tmp$"
)
_LIFECYCLE_LOCK_ATTEMPTS = 3


class ArchiveLifecycleLock(int):
    """File descriptor handle that also retains the stable directory lock."""

    directory_descriptor: int

    def __new__(cls, file_descriptor: int, directory_descriptor: int) -> ArchiveLifecycleLock:
        lock = int.__new__(cls, file_descriptor)
        lock.directory_descriptor = directory_descriptor
        return lock

    @property
    def file_descriptor(self) -> int:
        return int(self)


class ImportArchive:
    """Store original upload bytes with atomic rename and strict local permissions."""

    def __init__(self, data_dir: Path) -> None:
        self.directory = data_dir / "originals"
        self.staging_directory = data_dir / "originals.staging"

    def _open_lifecycle_lock(self) -> int:
        self.directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.staging_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging_directory.chmod(0o700)
        # Keep lifecycle metadata outside originals/, whose contents are also
        # validated as UUID-named archives by backup restore.
        lock_path = self.directory.parent / ".archive-lifecycle.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        return descriptor

    def _open_data_directory(self) -> int:
        return os.open(
            self.directory.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )

    def acquire_lifecycle_lock(self, operation: int) -> ArchiveLifecycleLock:
        """Lock the stable data directory and verify the compatible lock-file inode."""
        lock_path = self.directory.parent / ".archive-lifecycle.lock"
        for _attempt in range(_LIFECYCLE_LOCK_ATTEMPTS):
            descriptor = self._open_lifecycle_lock()
            try:
                directory_descriptor = self._open_data_directory()
            except BaseException:
                os.close(descriptor)
                raise
            try:
                _archive_flock(directory_descriptor, operation)
                _archive_flock(descriptor, operation)
                descriptor_stat = os.fstat(descriptor)
                try:
                    path_stat = os.stat(lock_path, follow_symlinks=False)
                except FileNotFoundError:
                    path_stat = None
            except BaseException:
                with suppress(OSError):
                    _archive_flock(descriptor, fcntl.LOCK_UN)
                    _archive_flock(directory_descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                os.close(directory_descriptor)
                raise
            if (
                path_stat is not None
                and stat.S_ISREG(path_stat.st_mode)
                and descriptor_stat.st_dev == path_stat.st_dev
                and descriptor_stat.st_ino == path_stat.st_ino
            ):
                return ArchiveLifecycleLock(descriptor, directory_descriptor)
            with suppress(OSError):
                _archive_flock(descriptor, fcntl.LOCK_UN)
                _archive_flock(directory_descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            os.close(directory_descriptor)
        raise ArchiveLockUnavailable("Archive lifecycle lock was replaced repeatedly.")

    def release_lifecycle_lock(self, lock: ArchiveLifecycleLock) -> None:
        try:
            _archive_flock(lock.file_descriptor, fcntl.LOCK_UN)
            _archive_flock(lock.directory_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock.file_descriptor)
            os.close(lock.directory_descriptor)

    def acquire_commit_reservation(self) -> ArchiveLifecycleLock:
        """Reserve archive lifecycle against purges through the SQLite COMMIT."""

        try:
            return self.acquire_lifecycle_lock(fcntl.LOCK_SH)
        except ArchiveLockUnavailable as error:
            raise AppError(
                "ARCHIVE_LOCK_UNAVAILABLE",
                503,
                "Le système de fichiers ne permet pas de verrouiller l’archive; "
                "réessayez ailleurs.",
            ) from error
        except BaseException:
            raise

    @staticmethod
    def release_commit_reservation(lock: ArchiveLifecycleLock) -> None:
        try:
            _archive_flock(lock.file_descriptor, fcntl.LOCK_UN)
            _archive_flock(lock.directory_descriptor, fcntl.LOCK_UN)
        except ArchiveLockUnavailable as error:
            raise AppError(
                "ARCHIVE_LOCK_UNAVAILABLE",
                503,
                "Le système de fichiers ne permet pas de verrouiller l’archive; "
                "réessayez ailleurs.",
            ) from error
        finally:
            os.close(lock.file_descriptor)
            os.close(lock.directory_descriptor)

    @contextmanager
    def commit_reservation(self) -> Iterator[None]:
        descriptor = self.acquire_commit_reservation()
        try:
            yield
        finally:
            self.release_commit_reservation(descriptor)

    @staticmethod
    def relative_path(import_file_id: str) -> str:
        return f"data/originals/{import_file_id}.bin"

    def write(self, import_file_id: str, raw_bytes: bytes) -> Path:
        with self.commit_reservation():
            return self._write_reserved(import_file_id, raw_bytes)

    def _write_reserved(self, import_file_id: str, raw_bytes: bytes) -> Path:
        final_path = self.directory / f"{import_file_id}.bin"
        self.staging_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging_directory.chmod(0o700)
        temporary_path = self.staging_directory / f".{uuid.uuid4()}.tmp"
        descriptor = os.open(temporary_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.chmod(0o600)
            os.replace(temporary_path, final_path)
            final_path.chmod(0o600)
            if final_path.stat().st_mode & 0o777 != 0o600:
                raise AppError(
                    "ARCHIVE_PERMISSIONS_INVALID", 500, "L'archive ne peut pas être protégée."
                )
            return final_path
        except OSError as error:
            raise AppError(
                "ARCHIVE_WRITE_FAILED", 500, "L'archive de l'import a échoué."
            ) from error
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def remove(path: Path) -> None:
        path.unlink(missing_ok=True)


def purge_orphaned_archives(
    engine: Engine,
    data_dir: Path,
    *,
    older_than: timedelta = timedelta(hours=24),
    now: datetime | None = None,
) -> int:
    """Audit then purge aged, unreferenced archives and abandoned write temporaries.

    A non-blocking exclusive flock shares a lifecycle gate with import commits.
    If any process is writing or committing an archive, this sweep skips the
    entire inventory. Candidate age is the newer of mtime and ctime.
    """

    archive = ImportArchive(data_dir)
    if not archive.directory.exists() and not archive.staging_directory.exists():
        return 0
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    cutoff = current - older_than
    lock_descriptor: ArchiveLifecycleLock | None = None
    try:
        try:
            lock_descriptor = archive.acquire_lifecycle_lock(fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        with session_scope(engine) as session:
            references = {
                Path(value).name
                for value in session.scalars(
                    select(ImportFile.archive_relpath).where(
                        ImportFile.status == "committed", ImportFile.archive_relpath.is_not(None)
                    )
                )
                if value
            }

        binary_candidates: list[Path] = []
        temporary_candidates: list[Path] = []
        for path in archive.directory.iterdir():
            if not _UUID_ARCHIVE_FILENAME.fullmatch(path.name) or path.name in references:
                continue
            _append_aged_regular_file(path, cutoff, binary_candidates)
        # Clean exact-name temporaries left by older versions as well as current
        # staging files. New writes never put a temporary in originals/.
        for directory in (archive.directory, archive.staging_directory):
            if not directory.is_dir() or directory.is_symlink():
                continue
            for path in directory.iterdir():
                if _UUID_TEMP_FILENAME.fullmatch(path.name):
                    _append_aged_regular_file(path, cutoff, temporary_candidates)

        if not binary_candidates and not temporary_candidates:
            return 0

        # The durable intent must exist before any raw bytes are removed.
        with write_transaction(engine) as session:
            session.add(
                AuditEvent(
                    import_file_id=None,
                    target_type="import_archive_inventory",
                    target_id=str(uuid.uuid4()),
                    action="import.archive_orphan_purge_intent",
                    actor="system",
                    after_json=json.dumps(
                        {
                            "archive_count": len(binary_candidates),
                            "temporary_count": len(temporary_candidates),
                        },
                        separators=(",", ":"),
                    ),
                )
            )

        removed = 0
        for path in (*binary_candidates, *temporary_candidates):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed
    finally:
        try:
            if lock_descriptor is not None:
                archive.release_lifecycle_lock(lock_descriptor)
        except ArchiveLockUnavailable:
            pass


def _append_aged_regular_file(path: Path, cutoff: datetime, candidates: list[Path]) -> None:
    try:
        file_stat = path.lstat()
    except OSError:
        return
    if not stat.S_ISREG(file_stat.st_mode):
        return
    newest_timestamp = max(file_stat.st_mtime, file_stat.st_ctime)
    if datetime.fromtimestamp(newest_timestamp, UTC) <= cutoff:
        candidates.append(path)


class ArchiveLockUnavailable(RuntimeError):
    """The filesystem cannot provide the lock required for archive safety."""


def _archive_flock(descriptor: int, operation: int) -> None:
    try:
        fcntl.flock(descriptor, operation)
    except OSError as error:
        if error.errno == errno.ENOLCK:
            raise ArchiveLockUnavailable("Archive lifecycle locking is unavailable.") from error
        raise
