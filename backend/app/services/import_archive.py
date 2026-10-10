"""Private, opaque, raw-byte archives for committed imports."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from app.core.errors import AppError


class ImportArchive:
    """Store original upload bytes with atomic rename and strict local permissions."""

    def __init__(self, data_dir: Path) -> None:
        self.directory = data_dir / "originals"

    @staticmethod
    def relative_path(import_file_id: str) -> str:
        return f"data/originals/{import_file_id}.bin"

    def write(self, import_file_id: str, raw_bytes: bytes) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
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
