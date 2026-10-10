"""Private, opaque, raw-byte archives for committed imports."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app.core.errors import AppError
from app.db.models import AuditEvent, ImportFile
from app.db.session import session_scope, write_transaction


class ImportArchive:
    """Store original upload bytes with atomic rename and strict local permissions."""

    def __init__(self, data_dir: Path) -> None:
        self.directory = data_dir / "originals"

    def _open_lifecycle_lock(self) -> int:
        self.directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        # Keep lifecycle metadata outside originals/, whose contents are also
        # validated as UUID-named archives by backup restore.
        lock_path = self.directory.parent / ".archive-lifecycle.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        return descriptor

    def acquire_commit_reservation(self) -> int:
        """Reserve archive lifecycle against purges through the SQLite COMMIT."""

        descriptor = self._open_lifecycle_lock()
        try:
            _archive_flock(descriptor, fcntl.LOCK_SH)
        except ArchiveLockUnavailable as error:
            os.close(descriptor)
            raise AppError(
                "ARCHIVE_LOCK_UNAVAILABLE",
                503,
                "Le système de fichiers ne permet pas de verrouiller l’archive; "
                "réessayez ailleurs.",
            ) from error
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def release_commit_reservation(descriptor: int) -> None:
        try:
            _archive_flock(descriptor, fcntl.LOCK_UN)
        except ArchiveLockUnavailable as error:
            raise AppError(
                "ARCHIVE_LOCK_UNAVAILABLE",
                503,
                "Le système de fichiers ne permet pas de verrouiller l’archive; "
                "réessayez ailleurs.",
            ) from error
        finally:
            os.close(descriptor)

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
        temporary_path = self.directory / f".{uuid.uuid4()}.tmp"
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
    if not archive.directory.exists():
        return 0
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    cutoff = current - older_than
    lock_descriptor = archive._open_lifecycle_lock()
    try:
        try:
            _archive_flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        for path in (*archive.directory.glob("*.bin"), *archive.directory.glob(".*.tmp")):
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix == ".bin" and path.name in references:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            newest_timestamp = max(stat.st_mtime, stat.st_ctime)
            if datetime.fromtimestamp(newest_timestamp, UTC) > cutoff:
                continue
            if path.suffix == ".bin":
                binary_candidates.append(path)
            else:
                temporary_candidates.append(path)

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
            _archive_flock(lock_descriptor, fcntl.LOCK_UN)
        except ArchiveLockUnavailable:
            pass
        finally:
            os.close(lock_descriptor)


class ArchiveLockUnavailable(RuntimeError):
    """The filesystem cannot provide the lock required for archive safety."""


def _archive_flock(descriptor: int, operation: int) -> None:
    try:
        fcntl.flock(descriptor, operation)
    except OSError as error:
        if error.errno == errno.ENOLCK:
            raise ArchiveLockUnavailable("Archive lifecycle locking is unavailable.") from error
        raise
