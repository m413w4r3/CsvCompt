"""Coherent SQLite backups, validated restores, and privacy-safe exports."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Never, TextIO

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.config import Settings, get_settings
from app.db.migrate import database_file_path, upgrade_to_head

ROOT = Path(__file__).resolve().parents[3]
BACKUP_FORMAT_VERSION = 1
BACKUP_EXTENSION = ".cfbackup"
_MANIFEST_NAME = "manifest.json"
_DATABASE_MEMBER = "database.sqlite"
_HMAC_SETTING = "import.account_hmac_key.v1"
_BACKUP_TIMEOUT_ENV = "COMPTES_BACKUP_TIMEOUT_SECONDS"
_DEFAULT_BACKUP_TIMEOUT_SECONDS = 30
_OUTPUT_TEMP_GLOB = "._comptes-backup-publish.cfbackup.*.tmp"
_BACKUP_STAGING_LOCK = threading.RLock()
_BACKUP_STAGING_LOCK_STATE = threading.local()
_MAX_BACKUP_MEMBER_BYTES = 256 * 1024 * 1024
_MAX_BACKUP_TOTAL_BYTES = 1024 * 1024 * 1024
_KEY_TABLES = (
    "accounts",
    "transactions",
    "source_rows",
    "classification_decisions",
    "allocations",
    "import_files",
    "import_account_sections",
    "backup_manifests",
)
_MANIFEST_KEYS = {
    "format_version",
    "created_at_utc",
    "schema_version",
    "alembic_revision",
    "integrity_check",
    "foreign_key_check",
    "table_counts",
    "includes_originals",
    "includes_hmac_key",
    "files",
}
_FILE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_AUTOMATIC_BACKUP_RE = re.compile(
    r"^family-backup-(?P<stamp>[0-9]{8}T[0-9]{12}Z)-[0-9a-f]{8}\.cfbackup$"
)


class BackupError(RuntimeError):
    """A backup or restore failed without exposing local data in its message."""


class BackupTimeoutError(BackupError):
    """A SQLite snapshot exceeded its configured deadline."""

    code = "BACKUP_TIMEOUT"


@dataclass(frozen=True, slots=True)
class BackupVerification:
    """Non-sensitive summary of a validated backup."""

    created_at_utc: str
    schema_version: str
    file_count: int
    table_counts: Mapping[str, int]
    includes_originals: bool
    includes_hmac_key: bool


@dataclass(frozen=True, slots=True)
class BackupResult:
    """Result of publishing one backup archive."""

    path: Path
    verification: BackupVerification


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Result of restoring a backup into a distinct database."""

    target_database: Path
    safety_backup: Path | None
    verification: BackupVerification


@dataclass(frozen=True, slots=True)
class _DatabaseSummary:
    revision: str | None
    integrity_ok: bool
    foreign_keys_ok: bool
    table_counts: dict[str, int]
    includes_hmac_key: bool
    referenced_archives: frozenset[str]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA recursive_triggers=ON")
    return connection


def _copy_database_snapshot(source_name: str, destination_name: str) -> bool:
    """Run SQLite's one-pass backup without returning database or path details."""
    try:
        source_path = Path(source_name)
        destination_path = Path(destination_name)
        with (
            closing(_readonly_connection(source_path)) as source,
            closing(sqlite3.connect(destination_path, timeout=30)) as target,
        ):
            target.execute("PRAGMA foreign_keys=ON")
            target.execute("PRAGMA recursive_triggers=ON")
            source.backup(target, pages=-1, sleep=0.01)
            target.commit()
        return True
    except BaseException:
        return False


def _snapshot_worker_command() -> int:
    """Private subprocess entry point for the killable SQLite snapshot."""
    try:
        payload = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError):
        return 1
    if (
        not isinstance(payload, list)
        or len(payload) != 2
        or any(not isinstance(value, str) for value in payload)
    ):
        return 1
    return 0 if _copy_database_snapshot(payload[0], payload[1]) else 1


def _backup_timeout_seconds() -> int:
    raw = os.environ.get(_BACKUP_TIMEOUT_ENV)
    if raw is None:
        return _DEFAULT_BACKUP_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError as error:
        raise BackupError(
            "COMPTES_BACKUP_TIMEOUT_SECONDS doit être un nombre entier positif."
        ) from error
    if value <= 0:
        raise BackupError("COMPTES_BACKUP_TIMEOUT_SECONDS doit être un nombre entier positif.")
    return value


def _private_staging_directory(parent: Path, *, purge: bool = False) -> Path:
    staging = parent / ".staging"
    try:
        if staging.is_symlink():
            raise OSError
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging.chmod(0o700)
        if purge:
            for entry in staging.iterdir():
                if entry.is_symlink() or not entry.is_dir():
                    entry.unlink(missing_ok=True)
                else:
                    shutil.rmtree(entry)
    except OSError as error:
        raise BackupError("Impossible de préparer le répertoire privé de staging.") from error
    return staging


@contextmanager
def _backup_staging_lock(backup_directory: Path) -> Iterator[None]:
    """Serialize staging purges and backup verification across threads and processes."""
    directory = _absolute_path(backup_directory)
    key = str(directory.resolve(strict=False))
    active: set[str] = getattr(_BACKUP_STAGING_LOCK_STATE, "active", set())
    if key in active:
        yield
        return

    with _BACKUP_STAGING_LOCK:
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(directory, flags)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise BackupError("Impossible de verrouiller le staging des sauvegardes.") from error

        active.add(key)
        _BACKUP_STAGING_LOCK_STATE.active = active
        try:
            yield
        finally:
            active.remove(key)
            with suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _canonical_lock_directories(paths: set[Path]) -> list[Path]:
    """Resolve aliases before ordering locks so every process uses one order."""
    return sorted(
        {_absolute_path(path).resolve(strict=False) for path in paths},
        key=str,
    )


def _purge_output_staging(directory: Path) -> None:
    """Remove only publication temporaries while holding the output directory lock."""
    try:
        for entry in directory.glob(_OUTPUT_TEMP_GLOB):
            if entry.is_symlink() or entry.is_file():
                entry.unlink(missing_ok=True)
    except OSError as error:
        raise BackupError("Impossible de nettoyer le staging de publication.") from error


def _same_filesystem(source: Path, destination_directory: Path) -> bool:
    return source.stat().st_dev == destination_directory.stat().st_dev


def _copy_for_atomic_publication(source: Path, destination_directory: Path) -> Path:
    """Copy a staged archive beside a cross-filesystem destination for atomic linking."""
    destination = destination_directory / (
        f"._comptes-backup-publish.cfbackup.{uuid.uuid4().hex}.tmp"
    )
    try:
        with ExitStack() as stack:
            input_stream = stack.enter_context(source.open("rb"))
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            output_stream = stack.enter_context(os.fdopen(descriptor, "wb"))
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        destination.chmod(0o600)
        return destination
    except Exception:
        with suppress(OSError):
            destination.unlink(missing_ok=True)
        raise


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _ensure_application_output_path(path: Path) -> None:
    """Refuse private output in ordinary repository folders."""
    resolved = path.resolve(strict=False)
    try:
        relative = resolved.relative_to(ROOT.resolve())
    except ValueError:
        return
    if any(part in {"data", "exports", ".staging"} for part in relative.parts[:-1]):
        return
    raise BackupError(
        "Destination refusée sous la racine du dépôt; choisissez un dossier ignoré "
        "(data/, exports/) ou un chemin privé extérieur."
    )


def _installed_revision() -> str:
    try:
        config = Config(str(ROOT / "alembic.ini"))
        heads = ScriptDirectory.from_config(config).get_heads()
    except Exception as error:
        raise BackupError("Impossible de déterminer la version de schéma installée.") from error
    if len(heads) != 1:
        raise BackupError("L'historique de schéma n'a pas une tête unique.")
    return heads[0]


def _migration_files_signature() -> tuple[tuple[str, str], ...]:
    migration_roots = (
        ROOT / "backend" / "app" / "db" / "migrations" / "versions",
        ROOT / "backend" / "app" / "db" / "migrations" / "sql",
    )
    files = sorted(
        path for directory in migration_roots for path in directory.glob("*") if path.is_file()
    )
    return tuple(
        (str(path.relative_to(ROOT)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in files
    )


def _known_revisions() -> tuple[str, set[str]]:
    try:
        config = Config(str(ROOT / "alembic.ini"))
        script = ScriptDirectory.from_config(config)
        heads = script.get_heads()
        if len(heads) != 1:
            raise BackupError("L'historique de schéma n'a pas une tête unique.")
        revisions = {
            revision.revision for revision in script.walk_revisions(base="base", head=heads[0])
        }
        return heads[0], revisions
    except BackupError:
        raise
    except Exception as error:
        raise BackupError("Impossible de lire l'historique des migrations.") from error


def _normalize_schema_sql(value: str | None) -> str | None:
    if value is None:
        return None
    output: list[str] = []
    quote: str | None = None
    pending_space = False
    index = 0
    while index < len(value):
        char = value[index]
        if quote is not None:
            output.append(char)
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    output.append(value[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"', "`"}:
            if pending_space and output:
                output.append(" ")
            pending_space = False
            quote = char
            output.append(char)
        elif char == "[":
            if pending_space and output:
                output.append(" ")
            pending_space = False
            quote = "]"
            output.append(char)
        elif char.isspace():
            pending_space = True
        else:
            if pending_space and output:
                output.append(" ")
            pending_space = False
            output.append(char)
        index += 1
    return "".join(output).strip().rstrip(";").strip()


def _sqlite_schema_objects(path: Path) -> tuple[tuple[str, str, str, str | None], ...]:
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE type IN ('table','index','trigger','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ).fetchall()
    except (OSError, sqlite3.Error) as error:
        raise BackupError("Impossible de comparer le schéma SQLite.") from error
    return tuple(
        (str(kind), str(name), str(table), _normalize_schema_sql(sql))
        for kind, name, table, sql in rows
    )


@lru_cache(maxsize=16)
def _reference_schema_objects(
    revision: str, migration_signature: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, str, str, str | None], ...]:
    del migration_signature  # The content hash invalidates the cache when a migration changes.
    head, known = _known_revisions()
    if revision not in known:
        raise BackupError("La sauvegarde utilise une révision Alembic inconnue.")
    try:
        with tempfile.TemporaryDirectory(prefix="comptes-schema-reference-") as temp_dir:
            directory = Path(temp_dir)
            database_path = directory / "reference.sqlite"
            settings = Settings(
                database_url=f"sqlite:///{database_path}",
                data_dir=directory / "data",
                _env_file=None,
            )
            config = Config(str(ROOT / "alembic.ini"))
            config.attributes["settings"] = settings
            config.attributes["operation"] = "upgrade"
            config.attributes["pre_upgrade_backup_checked"] = True
            command.upgrade(config, revision)
            objects = _sqlite_schema_objects(database_path)
            if revision == head and not objects:
                raise BackupError("La migration de tête n'a pas produit de schéma SQLite.")
            return objects
    except BackupError:
        raise
    except Exception as error:
        raise BackupError("Impossible de générer le schéma de référence des migrations.") from error


def _verify_schema(path: Path, revision: str) -> None:
    _head, known = _known_revisions()
    if revision not in known:
        raise BackupError("La sauvegarde utilise une révision Alembic inconnue.")
    expected = _reference_schema_objects(revision, _migration_files_signature())
    actual = _sqlite_schema_objects(path)
    expected_triggers = tuple(item for item in expected if item[0] == "trigger")
    actual_triggers = tuple(item for item in actual if item[0] == "trigger")
    if actual_triggers != expected_triggers:
        raise BackupError("Les triggers de la base sauvegardée sont absents ou modifiés.")
    if actual != expected:
        raise BackupError(
            "Les tables, index ou vues de la base ne correspondent pas au schéma attendu."
        )


def _archive_filename(relative_path: str) -> str:
    parts = PurePosixPath(relative_path).parts
    if len(parts) != 3 or parts[:2] != ("data", "originals"):
        raise BackupError("Une référence d'archive de source est invalide.")
    filename = parts[2]
    if not filename.endswith(".bin"):
        raise BackupError("Une référence d'archive de source est invalide.")
    try:
        parsed_id = uuid.UUID(filename[:-4])
    except (ValueError, AttributeError) as error:
        raise BackupError("Une référence d'archive de source est invalide.") from error
    if str(parsed_id) != filename[:-4].lower():
        raise BackupError("Une référence d'archive de source est invalide.")
    return filename


def _database_summary(path: Path) -> _DatabaseSummary:
    try:
        with closing(_readonly_connection(path)) as connection:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if "alembic_version" not in table_names:
                revision = None
            else:
                revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
                revision = str(revisions[0][0]) if len(revisions) == 1 else None

            if not set(_KEY_TABLES).issubset(table_names):
                raise BackupError("La base ne contient pas le schéma applicatif attendu.")
            table_counts = {
                table: int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
                for table in _KEY_TABLES
            }
            secret_keys = {
                str(row[0])
                for row in connection.execute(
                    "SELECT setting_key FROM app_settings WHERE is_secret=1"
                )
            }
            if secret_keys - {_HMAC_SETTING}:
                raise BackupError(
                    "La base contient un secret applicatif non autorisé en sauvegarde."
                )
            archive_rows = connection.execute(
                "SELECT archive_relpath FROM import_files WHERE archive_relpath IS NOT NULL"
            ).fetchall()
            referenced_archives = frozenset(_archive_filename(str(row[0])) for row in archive_rows)
    except BackupError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise BackupError("Impossible de vérifier l'intégrité de la base SQLite.") from error
    return _DatabaseSummary(
        revision=revision,
        integrity_ok=integrity_rows == [("ok",)],
        foreign_keys_ok=not foreign_key_rows,
        table_counts=table_counts,
        includes_hmac_key=_HMAC_SETTING in secret_keys,
        referenced_archives=referenced_archives,
    )


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _sha256_stream(
    stream: Any,
    output_path: Path | None = None,
    *,
    max_bytes: int | None = None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    output: TextIO | Any | None = None
    if output_path is not None:
        descriptor = os.open(output_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        output = os.fdopen(descriptor, "wb")
    try:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
            if max_bytes is not None and size > max_bytes:
                raise BackupError(
                    "Le contenu décompressé dépasse le plafond indépendant du manifeste."
                )
            if output is not None:
                output.write(block)
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
    finally:
        if output is not None:
            output.close()
    return digest.hexdigest(), size


def _ensure_parent(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as error:
        raise BackupError("Impossible de préparer le répertoire de destination.") from error


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Some filesystems do not support syncing directories; the file itself is synced.
        pass


def _publish_no_replace(temporary_path: Path, final_path: Path) -> None:
    """Atomically publish a completed file and refuse an existing destination.

    A same-filesystem hard link gives us atomic no-replace publication: readers
    see either no destination or the complete temporary inode. The temporary
    name is removed after publication.
    """
    published = False
    try:
        temporary_path.chmod(0o600)
        os.link(temporary_path, final_path, follow_symlinks=False)
        published = True
        if stat.S_IMODE(final_path.stat().st_mode) != 0o600:
            raise BackupError("Les permissions privées du fichier ne sont pas disponibles.")
        _fsync_directory(final_path.parent)
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)
    except FileExistsError as error:
        raise BackupError(
            "Le fichier de destination existe déjà; il n'a pas été écrasé."
        ) from error
    except BackupError:
        if published:
            with suppress(OSError):
                final_path.unlink(missing_ok=True)
        raise
    except OSError as error:
        if published:
            with suppress(OSError):
                final_path.unlink(missing_ok=True)
        raise BackupError("Impossible de publier le fichier de façon atomique.") from error


def _snapshot_database(
    source_path: Path, destination: Path, *, timeout_seconds: int | None = None
) -> None:
    if not source_path.is_file() or source_path.stat().st_size == 0:
        raise BackupError("La base source est absente ou vide.")
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    succeeded = False
    try:
        timeout = _backup_timeout_seconds() if timeout_seconds is None else timeout_seconds
        if type(timeout) is not int or timeout <= 0:
            raise BackupError("Le délai de sauvegarde doit être un nombre entier positif.")
        environment = os.environ.copy()
        backend_path = str(ROOT / "backend")
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            backend_path
            if not existing_pythonpath
            else backend_path + os.pathsep + existing_pythonpath
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "app.services.backups", "_snapshot-worker"],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            process.communicate(
                input=json.dumps([str(source_path.resolve()), str(destination.resolve())]).encode(
                    "utf-8"
                ),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.communicate()
            raise BackupTimeoutError(
                "BACKUP_TIMEOUT : la capture SQLite a dépassé son délai configurable."
            ) from error
        if process.returncode != 0:
            raise BackupError("La capture cohérente de la base SQLite a échoué.")
        destination.chmod(0o600)
        succeeded = True
    except BackupTimeoutError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise BackupError("La capture cohérente de la base SQLite a échoué.") from error
    except Exception as error:
        if isinstance(error, BackupError):
            raise
        raise BackupError("La capture cohérente de la base SQLite a échoué.") from error
    finally:
        if not succeeded:
            for suffix in ("", "-journal", "-wal", "-shm"):
                with suppress(OSError):
                    Path(f"{destination}{suffix}").unlink(missing_ok=True)


def _collect_originals(data_dir: Path, referenced: frozenset[str]) -> list[Path]:
    directory = data_dir / "originals"
    if directory.is_symlink():
        raise BackupError("Le répertoire des archives d'import n'est pas sûr.")
    files: list[Path] = []
    if directory.exists():
        if not directory.is_dir():
            raise BackupError("Le stockage des archives d'import est invalide.")
        for path in sorted(directory.glob("*.bin")):
            if path.is_symlink() or not path.is_file():
                raise BackupError("Le stockage des archives d'import est invalide.")
            _archive_filename(f"data/originals/{path.name}")
            files.append(path)
    names = {path.name for path in files}
    if not referenced.issubset(names):
        raise BackupError("Une archive de source référencée par la base est absente.")
    return files


def _new_backup_path(directory: Path, prefix: str = "family-backup") -> Path:
    stamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    return directory / f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}{BACKUP_EXTENSION}"


def _prune_backups(directory: Path, keep: int, published_path: Path) -> None:
    """Prune only valid, application-named automatic backups by their name timestamp."""
    candidates: list[tuple[str, Path]] = []
    for path in directory.iterdir():
        match = _AUTOMATIC_BACKUP_RE.fullmatch(path.name)
        if match is not None and path.is_file() and not path.is_symlink():
            candidates.append((match.group("stamp"), path))
    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    valid: list[Path] = []
    for _stamp, path in candidates:
        try:
            verify_backup(path)
        except BackupError:
            continue
        valid.append(path)
    if len(valid) <= 1:
        return
    to_keep = set(valid[:keep])
    to_keep.add(valid[0])
    to_keep.add(published_path)
    for old_path in valid:
        if old_path in to_keep:
            continue
        try:
            old_path.unlink()
        except OSError as error:
            raise BackupError(
                "La sauvegarde est vérifiée, mais la rétention n'a pas pu être appliquée."
            ) from error


def create_backup(
    database_path: Path,
    data_dir: Path,
    destination: Path | None = None,
    *,
    retention_count: int | None = 10,
) -> BackupResult:
    """Create a backup while serializing staging cleanup and publication."""
    source_database = _absolute_path(database_path)
    source_data_dir = _absolute_path(data_dir)
    backup_directory = source_data_dir / "backups"
    try:
        if source_data_dir.is_symlink() or backup_directory.is_symlink():
            raise OSError
        backup_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_directory.chmod(0o700)
    except OSError as error:
        raise BackupError("Impossible de préparer le répertoire privé de sauvegarde.") from error

    lock_directories = {backup_directory}
    if destination is not None:
        final_path = _absolute_path(destination)
        _ensure_application_output_path(final_path)
        _ensure_parent(final_path)
        lock_directories.add(final_path.parent)

    with ExitStack() as lock_stack:
        for directory in _canonical_lock_directories(lock_directories):
            lock_stack.enter_context(_backup_staging_lock(directory))
        return _create_backup_locked(
            source_database,
            source_data_dir,
            destination,
            retention_count=retention_count,
        )


def _create_backup_locked(
    database_path: Path,
    data_dir: Path,
    destination: Path | None = None,
    *,
    retention_count: int | None = 10,
) -> BackupResult:
    """Create a WAL-coherent database + import-archive backup as one private file."""
    database_path = _absolute_path(database_path)
    data_dir = _absolute_path(data_dir)
    if retention_count is not None and (type(retention_count) is not int or retention_count < 1):
        raise BackupError("La rétention doit être un entier positif ou être désactivée.")

    automatic = destination is None
    backup_directory = data_dir / "backups"
    try:
        if data_dir.is_symlink() or backup_directory.is_symlink():
            raise OSError
        backup_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_directory.chmod(0o700)
    except OSError as error:
        raise BackupError("Impossible de préparer le répertoire privé de sauvegarde.") from error
    staging_directory = _private_staging_directory(backup_directory, purge=True)

    if automatic:
        final_path = _new_backup_path(backup_directory)
    else:
        assert destination is not None
        final_path = _absolute_path(destination)
        _ensure_application_output_path(final_path)
        _ensure_parent(final_path)
        _purge_output_staging(final_path.parent)

    work_context = tempfile.TemporaryDirectory(prefix="backup-", dir=staging_directory)
    published = False
    verified = False
    temporary_path: Path | None = None
    publication_temp: Path | None = None
    try:
        with work_context as temporary_directory:
            work_directory = Path(temporary_directory)
            work_directory.chmod(0o700)
            temporary_path = work_directory / f"{final_path.name}.tmp"
            snapshot_path = Path(temporary_directory) / "database.sqlite"
            _snapshot_database(database_path, snapshot_path)
            summary = _database_summary(snapshot_path)
            head = _installed_revision()
            if summary.revision != head:
                raise BackupError("La base source n'utilise pas la version de schéma installée.")
            if not summary.integrity_ok or not summary.foreign_keys_ok:
                raise BackupError("La base source échoue aux contrôles d'intégrité.")

            originals = _collect_originals(data_dir, summary.referenced_archives)
            payloads: list[tuple[str, Path]] = [(_DATABASE_MEMBER, snapshot_path)]
            payloads.extend((f"originals/{path.name}", path) for path in originals)
            file_entries: list[dict[str, str | int]] = []
            for archive_name, path in payloads:
                digest, size = _sha256_file(path)
                file_entries.append({"path": archive_name, "size_bytes": size, "sha256": digest})

            manifest: dict[str, Any] = {
                "format_version": BACKUP_FORMAT_VERSION,
                "created_at_utc": _utc_now()
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z"),
                "schema_version": summary.revision,
                "alembic_revision": summary.revision,
                "integrity_check": "ok",
                "foreign_key_check": "ok",
                "table_counts": summary.table_counts,
                "includes_originals": bool(originals),
                "includes_hmac_key": summary.includes_hmac_key,
                "files": file_entries,
            }

            descriptor = os.open(temporary_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            with zipfile.ZipFile(
                temporary_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                for archive_name, path in payloads:
                    archive.write(path, arcname=archive_name)
                archive.writestr(
                    _MANIFEST_NAME,
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    + "\n",
                )
            descriptor = os.open(temporary_path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            temporary_path.chmod(0o600)
            publication_path = temporary_path
            if not _same_filesystem(temporary_path, final_path.parent):
                publication_temp = _copy_for_atomic_publication(temporary_path, final_path.parent)
                publication_path = publication_temp
            _publish_no_replace(publication_path, final_path)
            published = True

        verification = verify_backup(final_path)
        verified = True
        if automatic and retention_count is not None:
            _prune_backups(final_path.parent, retention_count, final_path)
        return BackupResult(path=final_path, verification=verification)
    except BackupError:
        if published and not verified:
            with suppress(OSError):
                final_path.unlink(missing_ok=True)
        raise
    except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError, TypeError) as error:
        if published and not verified:
            with suppress(OSError):
                final_path.unlink(missing_ok=True)
        if verified:
            raise BackupError(
                "La sauvegarde est créée et vérifiée, mais la rétention n'a pas pu être appliquée."
            ) from error
        raise BackupError("La sauvegarde a échoué sans modifier la base source.") from error
    except Exception as error:
        if published and not verified:
            with suppress(OSError):
                final_path.unlink(missing_ok=True)
        raise BackupError("La sauvegarde a échoué sans modifier la base source.") from error
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
        if publication_temp is not None:
            with suppress(OSError):
                publication_temp.unlink(missing_ok=True)


def _load_manifest(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> dict[str, Any]:
    if info.file_size > 1024 * 1024:
        raise BackupError("Le manifeste de sauvegarde est invalide.")
    try:
        raw = archive.read(info)
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        raise BackupError("Le manifeste de sauvegarde est absent ou illisible.") from error
    if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS:
        raise BackupError("Le manifeste de sauvegarde est incohérent.")
    if (
        type(value.get("format_version")) is not int
        or value["format_version"] != BACKUP_FORMAT_VERSION
    ):
        raise BackupError("Le format de sauvegarde n'est pas pris en charge.")
    if value.get("integrity_check") != "ok" or value.get("foreign_key_check") != "ok":
        raise BackupError("Le manifeste ne confirme pas les contrôles d'intégrité.")
    if type(value.get("includes_originals")) is not bool:
        raise BackupError("Le manifeste de sauvegarde est incohérent.")
    if type(value.get("includes_hmac_key")) is not bool:
        raise BackupError("Le manifeste de sauvegarde est incohérent.")
    revision = value.get("alembic_revision")
    if not isinstance(revision, str) or value.get("schema_version") != revision:
        raise BackupError("Le manifeste de sauvegarde est incohérent.")
    created = value.get("created_at_utc")
    if not isinstance(created, str) or not created.endswith("Z"):
        raise BackupError("Le manifeste de sauvegarde est incohérent.")
    try:
        datetime.fromisoformat(created[:-1] + "+00:00")
    except ValueError as error:
        raise BackupError("Le manifeste de sauvegarde est incohérent.") from error
    counts = value.get("table_counts")
    if not isinstance(counts, dict) or set(counts) != set(_KEY_TABLES):
        raise BackupError("Le manifeste de sauvegarde est incohérent.")
    if any(type(counts[name]) is not int or counts[name] < 0 for name in _KEY_TABLES):
        raise BackupError("Le manifeste de sauvegarde est incohérent.")
    entries = value.get("files")
    if not isinstance(entries, list):
        raise BackupError("Le manifeste de sauvegarde est incohérent.")
    return value


def _validate_member_path(name: str) -> None:
    if name == _DATABASE_MEMBER:
        return
    if name.startswith("originals/") and name.count("/") == 1:
        _archive_filename(f"data/originals/{name.removeprefix('originals/')}")
        return
    raise BackupError("La sauvegarde contient un chemin de fichier inattendu.")


def _verify_backup_contents_unlocked(
    backup_path: Path,
) -> tuple[BackupVerification, dict[str, Any]]:
    try:
        with zipfile.ZipFile(backup_path, mode="r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise BackupError("La sauvegarde contient des fichiers dupliqués.")
            manifest_infos = [info for info in infos if info.filename == _MANIFEST_NAME]
            if len(manifest_infos) != 1:
                raise BackupError("Le manifeste de sauvegarde est absent ou dupliqué.")
            manifest = _load_manifest(archive, manifest_infos[0])
            entries = manifest["files"]
            entry_map: dict[str, dict[str, Any]] = {}
            for item in entries:
                if not isinstance(item, dict) or set(item) != {"path", "size_bytes", "sha256"}:
                    raise BackupError("Le manifeste de sauvegarde est incohérent.")
                member = item.get("path")
                size = item.get("size_bytes")
                digest = item.get("sha256")
                if not isinstance(member, str):
                    raise BackupError("Le manifeste de sauvegarde est incohérent.")
                _validate_member_path(member)
                if member in entry_map:
                    raise BackupError("Le manifeste de sauvegarde est incohérent.")
                if type(size) is not int or size < 0:
                    raise BackupError("Le manifeste de sauvegarde est incohérent.")
                if not isinstance(digest, str) or not _FILE_HASH_RE.fullmatch(digest):
                    raise BackupError("Le manifeste de sauvegarde est incohérent.")
                entry_map[member] = item

            expected_names = {_MANIFEST_NAME, *entry_map.keys()}
            if set(names) != expected_names or _DATABASE_MEMBER not in entry_map:
                raise BackupError("Le manifeste ne décrit pas les fichiers de la sauvegarde.")
            original_names = {
                name.removeprefix("originals/")
                for name in entry_map
                if name.startswith("originals/")
            }
            if manifest["includes_originals"] != bool(original_names):
                raise BackupError("Le manifeste ne décrit pas les archives d'import.")

            if backup_path.parent.name == "backups":
                staging_directory = _private_staging_directory(backup_path.parent)
                verification_context = tempfile.TemporaryDirectory(
                    prefix="verify-", dir=staging_directory
                )
            else:
                verification_context = tempfile.TemporaryDirectory(prefix="comptes-verify-")
            with verification_context as temp_dir:
                database_path = Path(temp_dir) / "database.sqlite"
                total_uncompressed = 0
                for member, item in entry_map.items():
                    info = archive.getinfo(member)
                    if (
                        info.file_size > _MAX_BACKUP_MEMBER_BYTES
                        or total_uncompressed + info.file_size > _MAX_BACKUP_TOTAL_BYTES
                    ):
                        raise BackupError(
                            "Le contenu décompressé dépasse le plafond indépendant du manifeste."
                        )
                    if info.file_size != item["size_bytes"]:
                        raise BackupError("La taille d'un fichier de sauvegarde ne correspond pas.")
                    destination = database_path if member == _DATABASE_MEMBER else None
                    with archive.open(info, mode="r") as stream:
                        digest, size = _sha256_stream(
                            stream,
                            destination,
                            max_bytes=min(
                                _MAX_BACKUP_MEMBER_BYTES,
                                _MAX_BACKUP_TOTAL_BYTES - total_uncompressed,
                            ),
                        )
                    total_uncompressed += size
                    if size != item["size_bytes"] or digest != item["sha256"]:
                        raise BackupError(
                            "L'empreinte d'un fichier de sauvegarde ne correspond pas."
                        )

                summary = _database_summary(database_path)
                if not summary.integrity_ok:
                    raise BackupError("La base de la sauvegarde échoue à integrity_check.")
                if not summary.foreign_keys_ok:
                    raise BackupError(
                        "La base de la sauvegarde contient des références orphelines."
                    )
                _head, known_revisions = _known_revisions()
                if manifest["alembic_revision"] not in known_revisions:
                    raise BackupError("La sauvegarde utilise une révision Alembic inconnue.")
                if summary.revision != manifest["alembic_revision"]:
                    raise BackupError(
                        "La version déclarée ne correspond pas à la base sauvegardée."
                    )
                if summary.table_counts != manifest["table_counts"]:
                    raise BackupError("Les comptes de tables du manifeste ne correspondent pas.")
                if summary.includes_hmac_key != manifest["includes_hmac_key"]:
                    raise BackupError("Le contenu de sauvegarde ne correspond pas au manifeste.")
                _verify_schema(database_path, manifest["alembic_revision"])
                if not summary.referenced_archives.issubset(original_names):
                    raise BackupError(
                        "Une archive référencée par la base manque dans la sauvegarde."
                    )

            verification = BackupVerification(
                created_at_utc=manifest["created_at_utc"],
                schema_version=manifest["schema_version"],
                file_count=len(entry_map),
                table_counts=dict(manifest["table_counts"]),
                includes_originals=manifest["includes_originals"],
                includes_hmac_key=manifest["includes_hmac_key"],
            )
            return verification, manifest
    except BackupError:
        raise
    except Exception as error:
        raise BackupError("La sauvegarde est absente, tronquée ou corrompue.") from error


def _verify_backup_contents(backup_path: Path) -> tuple[BackupVerification, dict[str, Any]]:
    """Verify an archive without racing a purge of its shared staging directory."""
    path = _absolute_path(backup_path)
    if path.parent.name == "backups":
        with _backup_staging_lock(path.parent):
            return _verify_backup_contents_unlocked(path)
    return _verify_backup_contents_unlocked(path)


def verify_backup(backup_path: Path) -> BackupVerification:
    """Validate archive hashes, SQLite integrity, foreign keys and schema compatibility."""
    path = _absolute_path(backup_path)
    if not path.is_file():
        raise BackupError("La sauvegarde est absente ou illisible.")
    verification, _manifest = _verify_backup_contents(path)
    return verification


def _decimal_amount(amount_cents: int) -> str:
    if type(amount_cents) is not int:
        raise BackupError("Un montant exporté n'est pas un entier en centimes.")
    sign = "-" if amount_cents < 0 else ""
    absolute = abs(amount_cents)
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"


def _with_decimal_fields(value: Any) -> Any:
    if isinstance(value, dict):
        result = {key: _with_decimal_fields(item) for key, item in value.items()}
        for key, item in tuple(result.items()):
            if key.endswith("_cents"):
                if type(item) is not int:
                    raise BackupError("Une décision contient un montant non entier.")
                result[f"{key[:-6]}_decimal"] = _decimal_amount(item)
        return result
    if isinstance(value, list):
        return [_with_decimal_fields(item) for item in value]
    if isinstance(value, float):
        raise BackupError("Une décision contient une valeur numérique non exacte.")
    return value


def _decision_json(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise BackupError("L'historique de décision n'est pas exportable.") from error
    transformed = _with_decimal_fields(value)
    return json.dumps(transformed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _export_rows(connection: sqlite3.Connection) -> Iterator[dict[str, Any]]:
    transactions = connection.execute(
        "SELECT t.transaction_id,t.account_id,a.alias,t.booked_on,t.amount_cents,t.raw_label,"
        "p.display_name FROM transactions AS t "
        "JOIN accounts AS a ON a.account_id=t.account_id "
        "LEFT JOIN payees AS p ON p.payee_id=t.payee_id "
        "ORDER BY t.booked_on,t.transaction_id"
    )
    for row in transactions:
        amount = int(row[4])
        yield {
            "record_type": "transaction",
            "transaction_id": row[0],
            "account_id": row[1],
            "account_alias": row[2],
            "booked_on": row[3],
            "amount_cents": amount,
            "amount_decimal": _decimal_amount(amount),
            "raw_label": row[5],
            "payee_name": row[6],
        }

    allocations = connection.execute(
        "SELECT allocation_id,transaction_id,category_id,flow_kind,amount_cents,origin,decision,"
        "beneficiary,cadence,flexibility,acquisition,superseded_by_allocation_id,"
        "retracted_at_utc,retracted_by_decision_id FROM allocations "
        "ORDER BY transaction_id,created_at_utc,allocation_id"
    )
    for row in allocations:
        amount = int(row[4])
        yield {
            "record_type": "allocation",
            "allocation_id": row[0],
            "transaction_id": row[1],
            "category_id": row[2],
            "flow_kind": row[3],
            "amount_cents": amount,
            "amount_decimal": _decimal_amount(amount),
            "origin": row[5],
            "decision": row[6],
            "beneficiary": row[7],
            "cadence": row[8],
            "flexibility": row[9],
            "acquisition": row[10],
            "superseded_by_allocation_id": row[11],
            "retracted_at_utc": row[12],
            "retracted_by_decision_id": row[13],
        }

    decisions = connection.execute(
        "SELECT decision_id,transaction_id,suggestion_id,action,actor,before_json,after_json,"
        "undone_by_decision_id,created_at_utc FROM classification_decisions "
        "ORDER BY created_at_utc,decision_id"
    )
    for row in decisions:
        yield {
            "record_type": "decision",
            "decision_id": row[0],
            "transaction_id": row[1],
            "suggestion_id": row[2],
            "action": row[3],
            "actor": row[4],
            "before_json": _decision_json(row[5]),
            "after_json": _decision_json(row[6]),
            "undone_by_decision_id": row[7],
            "created_at_utc": row[8],
        }


_CSV_COLUMNS = (
    "record_type",
    "transaction_id",
    "account_id",
    "account_alias",
    "booked_on",
    "amount_cents",
    "amount_decimal",
    "raw_label",
    "payee_name",
    "allocation_id",
    "category_id",
    "flow_kind",
    "origin",
    "decision",
    "beneficiary",
    "cadence",
    "flexibility",
    "acquisition",
    "superseded_by_allocation_id",
    "retracted_at_utc",
    "retracted_by_decision_id",
    "decision_id",
    "suggestion_id",
    "action",
    "actor",
    "before_json",
    "after_json",
    "undone_by_decision_id",
    "created_at_utc",
)


def _neutralize_csv_row(row: Mapping[str, Any]) -> dict[str, Any]:
    protected = {"amount_cents", "amount_decimal"}
    dangerous_prefixes = ("=", "+", "-", "@", "\t", "\r")
    result: dict[str, Any] = {}
    for key, value in row.items():
        if key not in protected and isinstance(value, str) and value.startswith(dangerous_prefixes):
            result[key] = "'" + value
        else:
            result[key] = value
    return result


def export_data(
    database_path: Path,
    destination: Path,
    *,
    format: str,
    delimiter: str = ",",
) -> int:
    """Export operations, allocation history and decisions without any key material."""
    if format not in {"json", "csv"}:
        raise BackupError("Le format d'export doit être json ou csv.")
    if delimiter not in {",", ";"}:
        raise BackupError("Le séparateur CSV doit être une virgule ou un point-virgule.")
    database_path = _absolute_path(database_path)
    destination = _absolute_path(destination)
    _ensure_application_output_path(destination)
    _ensure_parent(destination)
    temporary_path = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        if not database_path.is_file():
            raise BackupError("La base d'export est absente.")
        with closing(_readonly_connection(database_path)) as connection:
            connection.execute("BEGIN")
            rows = list(_export_rows(connection))
        descriptor = os.open(temporary_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        if format == "json":
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                payload = {
                    "format_version": 1,
                    "transactions": [row for row in rows if row["record_type"] == "transaction"],
                    "allocations": [row for row in rows if row["record_type"] == "allocation"],
                    "decisions": [row for row in rows if row["record_type"] == "decision"],
                }
                json.dump(
                    payload, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=_CSV_COLUMNS,
                    extrasaction="ignore",
                    delimiter=delimiter,
                )
                writer.writeheader()
                writer.writerows(_neutralize_csv_row(row) for row in rows)
                stream.flush()
                os.fsync(stream.fileno())
        temporary_path.chmod(0o600)
        _publish_no_replace(temporary_path, destination)
        return len(rows)
    except BackupError:
        raise
    except Exception as error:
        raise BackupError("L'export a échoué sans modifier la base.") from error
    finally:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)


def _rename_path(source: Path, target: Path) -> None:
    os.replace(source, target)


def _same_path(left: Path, right: Path) -> bool:
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _create_safety_backup(target: Path, target_data_dir: Path) -> Path | None:
    if not target.exists():
        original_dir = target_data_dir / "originals"
        if original_dir.exists() and any(original_dir.iterdir()):
            raise BackupError("La cible contient des archives sans base de sécurité exploitable.")
        return None
    if target.is_symlink() or not target.is_file():
        raise BackupError("La base cible existante ne peut pas être sauvegardée de façon sûre.")
    physical_target = target.resolve(strict=True)
    physical_data_dir = target_data_dir.resolve(strict=True)
    safety_path = _new_backup_path(physical_target.parent, prefix="restore-safety")
    result = create_backup(physical_target, physical_data_dir, safety_path, retention_count=None)
    return result.path


def _extract_backup_to_stage(
    backup_path: Path,
    generation_stage: Path,
    manifest: dict[str, Any],
) -> tuple[Path, Path]:
    if not generation_stage.is_dir():
        raise BackupError("Le staging de restauration est absent.")
    generation_stage.chmod(0o700)
    data_stage = generation_stage / "data"
    originals_stage = data_stage / "originals"
    data_stage.mkdir(mode=0o700)
    originals_stage.mkdir(mode=0o700)
    total_uncompressed = 0
    with zipfile.ZipFile(backup_path, mode="r") as archive:
        for entry in manifest["files"]:
            member = str(entry["path"])
            info = archive.getinfo(member)
            if (
                info.file_size > _MAX_BACKUP_MEMBER_BYTES
                or total_uncompressed + info.file_size > _MAX_BACKUP_TOTAL_BYTES
            ):
                raise BackupError(
                    "Le contenu décompressé dépasse le plafond indépendant du manifeste."
                )
            if member == _DATABASE_MEMBER:
                destination = generation_stage / "database.sqlite"
            else:
                destination = originals_stage / member.removeprefix("originals/")
            with archive.open(info, mode="r") as source:
                digest, size = _sha256_stream(
                    source,
                    destination,
                    max_bytes=min(
                        _MAX_BACKUP_MEMBER_BYTES,
                        _MAX_BACKUP_TOTAL_BYTES - total_uncompressed,
                    ),
                )
            total_uncompressed += size
            if size != entry["size_bytes"] or digest != entry["sha256"]:
                raise BackupError("Une archive a changé pendant la restauration.")
            destination.chmod(0o600)
    return generation_stage / "database.sqlite", originals_stage


def _verify_restored_database(
    path: Path,
    originals_dir: Path,
    *,
    revision: str,
    manifest: dict[str, Any] | None = None,
) -> None:
    summary = _database_summary(path)
    if not summary.integrity_ok or not summary.foreign_keys_ok:
        raise BackupError("La base restaurée échoue aux contrôles d'intégrité.")
    if summary.revision != revision:
        raise BackupError("La version de schéma restaurée est incompatible.")
    _verify_schema(path, revision)
    if manifest is not None:
        if summary.table_counts != manifest["table_counts"]:
            raise BackupError("La base restaurée ne correspond pas au manifeste.")
        if summary.includes_hmac_key != manifest["includes_hmac_key"]:
            raise BackupError("Le contenu secret attendu n'est pas présent dans la base restaurée.")
    originals = {archive_path.name for archive_path in originals_dir.glob("*.bin")}
    if not summary.referenced_archives.issubset(originals):
        raise BackupError("Une archive de source manque après restauration.")


def _restore_root_paths(target_database: Path, target_data: Path) -> tuple[Path, Path, Path]:
    current_link = target_database.parent
    if (
        target_database.name != "database.sqlite"
        or current_link.name != "current"
        or target_data != current_link / "data"
    ):
        raise BackupError(
            "La restauration atomique exige --target-db <racine>/current/database.sqlite "
            "et --target-data-dir <racine>/current/data."
        )
    root = current_link.parent
    if root.is_symlink():
        raise BackupError("La racine de génération cible ne peut pas être un lien symbolique.")
    return root, current_link, root / "generations"


def _current_generation(root: Path, current_link: Path, generations: Path) -> Path | None:
    if current_link.is_symlink():
        try:
            resolved = current_link.resolve(strict=True)
            if (
                not resolved.is_relative_to(generations.resolve(strict=True))
                or not resolved.is_dir()
            ):
                raise BackupError(
                    "Le pointeur current ne désigne pas une génération locale valide."
                )
            return resolved
        except OSError as error:
            raise BackupError("Le pointeur current de la cible est invalide.") from error
    if current_link.exists():
        raise BackupError("Le chemin current existe mais n'est pas un lien de génération sûr.")
    return None


def _purge_restore_staging(
    root: Path,
    current_link: Path,
    generations: Path,
    *,
    preserve_archive: Path | None = None,
) -> None:
    current_generation = _current_generation(root, current_link, generations)
    if generations.exists():
        for entry in generations.iterdir():
            if entry.is_symlink():
                continue
            if entry.name.startswith(".restore-") and entry.is_dir():
                shutil.rmtree(entry)
                continue
            marker = entry / ".restore-pending"
            if marker.is_file():
                if current_generation is not None and entry.resolve() == current_generation:
                    marker.unlink(missing_ok=True)
                else:
                    shutil.rmtree(entry)
    for entry in root.glob(".current-*.tmp"):
        if entry.is_symlink() or entry.is_file():
            entry.unlink(missing_ok=True)
    for entry in root.glob(".restore-archive-*.tmp"):
        if entry == preserve_archive:
            continue
        if entry.is_symlink() or entry.is_file():
            entry.unlink(missing_ok=True)
    old_artifact = re.compile(
        r"^\.(?:database\.sqlite(?:-(?:wal|shm))?|originals)\.[0-9a-f-]{32,36}\.old$"
    )
    cleanup_directories = {root}
    if current_generation is not None:
        cleanup_directories.add(current_generation)
    for directory in cleanup_directories:
        for entry in directory.iterdir():
            if not old_artifact.fullmatch(entry.name):
                continue
            if entry.is_symlink() or entry.is_file():
                entry.unlink(missing_ok=True)
            elif entry.is_dir():
                shutil.rmtree(entry)


def _validate_target_originals(originals_dir: Path) -> None:
    if originals_dir.is_symlink():
        raise BackupError("Le stockage d'archives cible ne peut pas être un lien symbolique.")
    if not originals_dir.exists():
        return
    if not originals_dir.is_dir():
        raise BackupError("Le stockage d'archives cible est invalide.")
    uuid_filename = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.bin$"
    )
    for entry in originals_dir.iterdir():
        if entry.is_symlink() or not entry.is_file() or not uuid_filename.fullmatch(entry.name):
            raise BackupError(
                "La restauration refuse originals/ contenant un fichier autre que <uuid>.bin."
            )


@contextmanager
def _exclusive_target_lock(target_database: Path) -> Iterator[None]:
    if not target_database.exists():
        yield
        return
    connection: sqlite3.Connection | None = None
    try:
        physical_path = target_database.resolve(strict=True)
        connection = sqlite3.connect(physical_path, timeout=0.25, isolation_level=None)
        connection.execute("BEGIN EXCLUSIVE")
    except (OSError, sqlite3.Error) as error:
        if connection is not None:
            connection.close()
        raise BackupError(
            "La base cible est occupée; fermez ses connexions avant restauration."
        ) from error
    try:
        yield
    finally:
        with suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        connection.close()


def _restore_checkpoint(step: int) -> None:
    """Fault-injection seam used to exercise process-kill points around publication."""
    del step


def _validate_restore_identity(
    backup_path: Path,
    target_database_path: Path,
    target_data_dir: Path,
    active_database_path: Path | None,
    active_data_dir: Path | None,
) -> tuple[Path, Path, Path, Path]:
    target_database = _absolute_path(target_database_path)
    target_data = _absolute_path(target_data_dir)
    settings = get_settings()
    if active_database_path is None:
        active_database_path = database_file_path(settings.database_url)
    if active_data_dir is None:
        active_data_dir = settings.data_dir
    if active_database_path is None:
        raise BackupError("La base active ne peut pas être déterminée.")
    active_database = _absolute_path(active_database_path)
    active_data = _absolute_path(active_data_dir)
    if _same_path(target_database, active_database):
        raise BackupError("La restauration exige une base distincte de la base active.")
    if _same_path(target_data, active_data):
        raise BackupError("La restauration exige un stockage distinct du stockage actif.")
    if _same_path(target_data / "originals", active_data / "originals"):
        raise BackupError(
            "La restauration exige un stockage d'archives distinct du stockage actif."
        )
    if any(
        _same_path(target_database, Path(f"{active_database}{suffix}"))
        for suffix in ("-wal", "-shm")
    ):
        raise BackupError("La cible ne peut pas remplacer un fichier SQLite actif.")
    if _same_path(backup_path, target_database):
        raise BackupError("Le fichier de sauvegarde et la base cible doivent être distincts.")
    return target_database, target_data, active_database, active_data


def restore_backup(
    backup_path: Path,
    target_database_path: Path,
    target_data_dir: Path,
    *,
    active_database_path: Path | None = None,
    active_data_dir: Path | None = None,
) -> RestoreResult:
    """Restore without letting automatic retention remove the source archive mid-run."""
    source = _absolute_path(backup_path)
    if source.is_symlink():
        raise BackupError("La sauvegarde ne peut pas être un lien symbolique.")
    try:
        source = source.parent.resolve(strict=True) / source.name
    except OSError as error:
        raise BackupError("La sauvegarde est absente ou illisible.") from error
    target_database, target_data, active_database, active_data = _validate_restore_identity(
        source,
        target_database_path,
        target_data_dir,
        active_database_path,
        active_data_dir,
    )
    if source.parent.name != "backups":
        return _restore_backup_locked(
            source,
            target_database,
            target_data,
            active_database_path=active_database,
            active_data_dir=active_data,
        )

    stable_source: Path | None = None
    try:
        root, current_link, generations = _restore_root_paths(target_database, target_data)
        if root.is_symlink() or generations.is_symlink():
            raise BackupError("La racine de génération cible n'est pas sûre.")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        generations.mkdir(mode=0o700, exist_ok=True)
        generations.chmod(0o700)

        lock_directories = {source.parent, root}
        if target_database.exists():
            if target_database.is_symlink() or not target_database.is_file():
                raise BackupError(
                    "La base cible existante ne peut pas être sauvegardée de façon sûre."
                )
            physical_target = target_database.resolve(strict=True)
            physical_data = target_data.resolve(strict=True)
            safety_backup_directory = physical_data / "backups"
            if safety_backup_directory.is_symlink():
                raise BackupError("Le répertoire privé de sauvegarde cible n'est pas sûr.")
            safety_backup_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            safety_backup_directory.chmod(0o700)
            lock_directories.update({physical_target.parent, safety_backup_directory})

        with ExitStack() as lock_stack:
            for directory in _canonical_lock_directories(lock_directories):
                lock_stack.enter_context(_backup_staging_lock(directory))
            if source.is_symlink() or not source.is_file():
                raise BackupError("La sauvegarde est absente ou illisible.")
            verify_backup(source)
            _purge_restore_staging(root, current_link, generations)
            stable_source = root / f".restore-archive-{uuid.uuid4().hex}.tmp"
            descriptor: int | None = None
            try:
                descriptor = os.open(stable_source, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with (
                    source.open("rb") as input_stream,
                    os.fdopen(descriptor, "wb") as output_stream,
                ):
                    descriptor = None
                    shutil.copyfileobj(input_stream, output_stream)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            stable_source.chmod(0o600)
            result = _restore_backup_locked(
                stable_source,
                target_database,
                target_data,
                active_database_path=active_database,
                active_data_dir=active_data,
            )
            stable_source.unlink(missing_ok=True)
            stable_source = None
            return result
    except BackupError:
        raise
    except OSError as error:
        raise BackupError("Impossible de figer la sauvegarde avant restauration.") from error
    finally:
        if stable_source is not None:
            with suppress(OSError):
                stable_source.unlink(missing_ok=True)


def _restore_backup_locked(
    backup_path: Path,
    target_database_path: Path,
    target_data_dir: Path,
    *,
    active_database_path: Path | None = None,
    active_data_dir: Path | None = None,
) -> RestoreResult:
    """Restore to a distinct generation and atomically publish it through ``current``."""
    backup_path = _absolute_path(backup_path)
    target_database, target_data, active_database, active_data = _validate_restore_identity(
        backup_path,
        target_database_path,
        target_data_dir,
        active_database_path,
        active_data_dir,
    )
    root, current_link, generations = _restore_root_paths(target_database, target_data)
    verification, manifest = _verify_backup_contents(backup_path)
    try:
        if root.is_symlink():
            raise BackupError("La racine de génération cible ne peut pas être un lien symbolique.")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        if generations.is_symlink():
            raise BackupError("Le répertoire de générations ne peut pas être un lien symbolique.")
        generations.mkdir(mode=0o700, exist_ok=True)
        generations.chmod(0o700)
        preserve_archive = (
            backup_path
            if backup_path.parent == root and backup_path.name.startswith(".restore-archive-")
            else None
        )
        _purge_restore_staging(
            root,
            current_link,
            generations,
            preserve_archive=preserve_archive,
        )
        target_originals = target_data / "originals"
        _validate_target_originals(target_originals)
        safety_backup = _create_safety_backup(target_database, target_data)
    except BackupError:
        raise
    except OSError as error:
        raise BackupError("Impossible de préparer la cible de restauration.") from error

    stage_dir: Path | None = None
    generation_path: Path | None = None
    temporary_link: Path | None = None
    published = False
    try:
        token = uuid.uuid4().hex
        stage_dir = Path(tempfile.mkdtemp(prefix=f".restore-{token}-stage-", dir=generations))
        stage_dir.chmod(0o700)
        _restore_checkpoint(1)
        staged_database, staged_originals = _extract_backup_to_stage(
            backup_path, stage_dir, manifest
        )
        (stage_dir / ".restore-pending").touch(mode=0o600, exist_ok=False)
        _restore_checkpoint(2)
        source_revision = str(manifest["alembic_revision"])
        _verify_restored_database(
            staged_database,
            staged_originals,
            revision=source_revision,
            manifest=manifest,
        )
        _restore_checkpoint(3)

        migrated = source_revision != _installed_revision()
        if migrated:
            stage_settings = Settings(
                database_url=f"sqlite:///{staged_database}",
                data_dir=stage_dir / "data",
                _env_file=None,
            )
            upgrade_to_head(stage_settings)
            _verify_restored_database(
                staged_database,
                staged_originals,
                revision=_installed_revision(),
            )
        _restore_checkpoint(4)

        database_entry = next(
            item for item in manifest["files"] if item["path"] == _DATABASE_MEMBER
        )
        if not migrated:
            digest, size = _sha256_file(staged_database)
            if digest != database_entry["sha256"] or size != database_entry["size_bytes"]:
                raise BackupError("L'empreinte de la base restaurée ne correspond pas.")
        for entry in manifest["files"]:
            if not str(entry["path"]).startswith("originals/"):
                continue
            restored_file = staged_originals / str(entry["path"]).removeprefix("originals/")
            file_digest, file_size = _sha256_file(restored_file)
            if file_digest != entry["sha256"] or file_size != entry["size_bytes"]:
                raise BackupError("L'empreinte d'une archive restaurée ne correspond pas.")

        generation_path = generations / token
        _rename_path(stage_dir, generation_path)
        stage_dir = None
        _restore_checkpoint(5)
        temporary_link = root / f".current-{token}.tmp"
        os.symlink(Path("generations") / token, temporary_link)
        with _exclusive_target_lock(target_database):
            os.replace(temporary_link, current_link)
            published = True
            _fsync_directory(root)
            _restore_checkpoint(6)
        (generation_path / ".restore-pending").unlink(missing_ok=True)
    except BackupError:
        raise
    except Exception as error:
        raise BackupError(
            "La restauration a échoué avant publication; la cible précédente reste intacte."
        ) from error
    finally:
        if stage_dir is not None and stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
        if temporary_link is not None:
            with suppress(OSError):
                temporary_link.unlink(missing_ok=True)
        if generation_path is not None and generation_path.exists() and not published:
            shutil.rmtree(generation_path, ignore_errors=True)

    return RestoreResult(
        target_database=target_database,
        safety_backup=safety_backup,
        verification=verification,
    )


class _QuietArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise BackupError("Arguments invalides; consultez l'aide de la commande.")


def _parser() -> _QuietArgumentParser:
    parser = _QuietArgumentParser(description="Sauvegarde locale des données Comptes familiaux")
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_QuietArgumentParser
    )

    backup_parser = commands.add_parser("backup", help="créer et vérifier une sauvegarde")
    backup_parser.add_argument("--output", type=Path)
    backup_parser.add_argument("--retention", type=int, default=10)

    verify_parser = commands.add_parser("verify", help="vérifier une sauvegarde existante")
    verify_parser.add_argument("backup", type=Path)

    restore_parser = commands.add_parser("restore", help="restaurer vers une base distincte")
    restore_parser.add_argument("backup", type=Path)
    restore_parser.add_argument("--target-db", required=True, type=Path)
    restore_parser.add_argument("--target-data-dir", required=True, type=Path)

    export_parser = commands.add_parser("export", help="exporter opérations et décisions")
    export_parser.add_argument("--format", choices=("csv", "json"), required=True)
    export_parser.add_argument("--output", required=True, type=Path)
    export_parser.add_argument("--delimiter", choices=(",", ";"), default=",")
    return parser


def _main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["_snapshot-worker"]:
        return _snapshot_worker_command()
    try:
        args = _parser().parse_args(argv)
        settings: Settings = get_settings()
        database_path = database_file_path(settings.database_url)
        if database_path is None:
            raise BackupError("Une base SQLite locale sur fichier est requise.")
        if args.command == "backup":
            backup_result = create_backup(
                database_path,
                settings.data_dir,
                args.output,
                retention_count=args.retention,
            )
            del backup_result
            print("backup=created-and-verified")
            return 0
        if args.command == "verify":
            verify_backup(args.backup)
            print("backup=verified")
            return 0
        if args.command == "restore":
            restore_result = restore_backup(
                args.backup,
                args.target_db,
                args.target_data_dir,
                active_database_path=database_path,
                active_data_dir=settings.data_dir,
            )
            del restore_result
            print("restore=verified")
            return 0
        if args.command == "export":
            export_data(
                database_path,
                args.output,
                format=args.format,
                delimiter=args.delimiter,
            )
            print("export=created")
            return 0
        raise BackupError("Commande de sauvegarde inconnue.")
    except BackupError as error:
        print(f"backup_error={error}", file=sys.stderr)
        return 1
    except Exception as error:
        del error
        print("backup_error=opération impossible; aucune donnée n'a été affichée", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
