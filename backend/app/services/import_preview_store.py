"""Stockage temporaire privé des aperçus d'import."""

from __future__ import annotations

import calendar
import json
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from app.core.errors import AppError


class ImportPreviewStore:
    """Fichiers temporaires à noms opaques, permissions strictes et TTL."""

    def __init__(
        self,
        directory: Path,
        *,
        ttl_seconds: int = 1800,
        max_bytes: int = 100 * 1024 * 1024,
        clock: Any = None,
    ) -> None:
        self.directory = directory
        self.ttl_seconds = ttl_seconds
        self.max_bytes = max_bytes
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._commit_in_progress: set[str] = set()
        self._ensure_directory()
        self.purge_expired()

    def _ensure_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)

    def _now(self) -> datetime:
        return self.clock().astimezone(UTC)

    def _path(self, preview_id: str) -> Path:
        try:
            opaque_id = str(uuid.UUID(preview_id))
        except (ValueError, AttributeError) as error:
            raise AppError("NOT_FOUND", 404, "Aperçu introuvable.") from error
        return self.directory / f"{opaque_id}.json"

    def purge_expired(self) -> None:
        with self._lock:
            if not self.directory.exists():
                return
            now = self._now()
            for path in self.directory.glob("*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    expiry = datetime.fromisoformat(value["expires_at_utc"].replace("Z", "+00:00"))
                    if expiry <= now:
                        path.unlink(missing_ok=True)
                        path.with_suffix(".bin").unlink(missing_ok=True)
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    AttributeError,
                    json.JSONDecodeError,
                ):
                    path.unlink(missing_ok=True)
                    path.with_suffix(".bin").unlink(missing_ok=True)
            stale_before = now - timedelta(seconds=self.ttl_seconds)
            stale_before_ns = (
                calendar.timegm(stale_before.utctimetuple()) * 1_000_000_000
                + stale_before.microsecond * 1_000
            )
            for path in self.directory.glob("*.bin"):
                if path.with_suffix(".json").exists():
                    continue
                self._unlink_if_stale(path, stale_before_ns)
            for path in self.directory.iterdir():
                if path.name.startswith(".") and path.name.endswith(".tmp"):
                    self._unlink_if_stale(path, stale_before_ns)

    @staticmethod
    def _unlink_if_stale(path: Path, stale_before_ns: int) -> None:
        try:
            if path.stat().st_mtime_ns <= stale_before_ns:
                path.unlink(missing_ok=True)
        except OSError:
            return

    def create(self, payload: dict[str, Any], raw_bytes: bytes) -> dict[str, Any]:
        with self._lock:
            self.purge_expired()
            used = sum(
                path.stat().st_size for path in self.directory.glob("*.bin") if path.exists()
            )
            if used + len(raw_bytes) > self.max_bytes:
                raise AppError("PREVIEW_STORAGE_FULL", 507, "Le stockage temporaire est plein.")
            preview_id = str(uuid.uuid4())
            expiry = self._now() + timedelta(seconds=self.ttl_seconds)
            value = dict(payload)
            value.update(
                preview_id=preview_id,
                version=1,
                created_at_utc=self._now()
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                expires_at_utc=expiry.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            )
            serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            binary_path = self.directory / f"{preview_id}.bin"
            json_path = self.directory / f"{preview_id}.json"
            try:
                self._atomic_write(binary_path, raw_bytes)
                self._atomic_write(json_path, serialized)
            except BaseException:
                binary_path.unlink(missing_ok=True)
                json_path.unlink(missing_ok=True)
                raise
            return value

    def _atomic_write(self, final_path: Path, data: bytes) -> None:
        temp_path = self.directory / f".{uuid.uuid4()}.tmp"
        descriptor = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temp_path.chmod(0o600)
            os.replace(temp_path, final_path)
            final_path.chmod(0o600)
        finally:
            temp_path.unlink(missing_ok=True)

    def get(self, preview_id: str) -> dict[str, Any]:
        with self._lock:
            self.purge_expired()
            path = self._path(preview_id)
            if not path.exists():
                raise AppError(
                    "IMPORT_PREVIEW_EXPIRED", 410, "Cet aperçu a expiré ou a été supprimé."
                )
            value = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
            expiry = datetime.fromisoformat(value["expires_at_utc"].replace("Z", "+00:00"))
            if expiry <= self._now():
                self.delete(preview_id)
                raise AppError(
                    "IMPORT_PREVIEW_EXPIRED", 410, "Cet aperçu a expiré ou a été supprimé."
                )
            return value

    def raw_bytes(self, preview_id: str) -> bytes:
        self.get(preview_id)
        return self._path(preview_id).with_suffix(".bin").read_bytes()

    def update(self, preview_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.get(preview_id)
            if preview_id in self._commit_in_progress:
                raise AppError(
                    "IMPORT_COMMIT_IN_PROGRESS", 409, "Le commit de cet aperçu est en cours."
                )
            if payload.get("version") != current["version"]:
                raise AppError(
                    "VERSION_CONFLICT",
                    409,
                    "Cet aperçu a changé.",
                    {"current_version": current["version"]},
                )
            next_value = dict(payload)
            next_value["version"] = current["version"] + 1
            self._atomic_write(
                self._path(preview_id),
                json.dumps(next_value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            )
            return next_value

    def begin_commit(self, preview_id: str, expected_version: int) -> dict[str, Any]:
        """Atomically mark an approved preview as committing before reading its bytes."""

        with self._lock:
            current = self.get(preview_id)
            if preview_id in self._commit_in_progress:
                raise AppError(
                    "IMPORT_COMMIT_IN_PROGRESS", 409, "Le commit de cet aperçu est en cours."
                )
            if current.get("version") != expected_version:
                raise AppError(
                    "VERSION_CONFLICT",
                    409,
                    "Cet aperçu a changé.",
                    {"current_version": current.get("version")},
                )
            self._commit_in_progress.add(preview_id)
            return current

    def finish_commit(self, preview_id: str) -> None:
        with self._lock:
            self._commit_in_progress.discard(preview_id)

    def update_commit_failure(self, preview_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a fresh blocking state after an authoritative commit-time conflict."""

        with self._lock:
            current = self.get(preview_id)
            next_value = dict(payload)
            next_value["version"] = current["version"] + 1
            self._atomic_write(
                self._path(preview_id),
                json.dumps(next_value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            )
            return next_value

    def delete(self, preview_id: str, *, during_commit: bool = False) -> None:
        with self._lock:
            if preview_id in self._commit_in_progress and not during_commit:
                raise AppError(
                    "IMPORT_COMMIT_IN_PROGRESS", 409, "Le commit de cet aperçu est en cours."
                )
            path = self._path(preview_id)
            path.unlink(missing_ok=True)
            path.with_suffix(".bin").unlink(missing_ok=True)
