"""Commande locale Alembic avec sauvegarde cohérente SQLite pré-upgrade."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from app.core.config import Settings, get_settings
from app.db.base import Base

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "alembic.ini"


class MigrationError(RuntimeError):
    """Erreur de migration suffisamment sûre pour être montrée dans le terminal."""


class BackupError(MigrationError):
    """Sauvegarde pré-upgrade absente, incohérente ou impossible à vérifier."""


@dataclass(frozen=True)
class BackupResult:
    database_path: Path
    manifest_path: Path
    sha256: str
    file_size_bytes: int


@dataclass(frozen=True)
class UpgradeResult:
    revision: str
    backup: BackupResult | None


def _alembic_config(settings: Settings) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["settings"] = settings
    return config


def _head_revision(settings: Settings) -> str:
    script = ScriptDirectory.from_config(_alembic_config(settings))
    heads = script.get_heads()
    if len(heads) != 1:
        raise MigrationError("L'historique Alembic doit avoir exactement une tête linéaire")
    return heads[0]


def database_file_path(database_url: str) -> Path | None:
    """Résout le chemin d'une base SQLite fichier; ``None`` désigne la mémoire."""
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "sqlite":
        raise MigrationError("Seules les URL SQLite sont prises en charge par Alembic")
    database = parsed.database
    if database is None or database == "" or database == ":memory:":
        return None
    if database.startswith("file:"):
        raise MigrationError(
            "Les URI SQLite directes ne sont pas prises en charge pour les migrations"
        )
    return Path(database).expanduser().resolve()


def _ensure_private_directory(directory: Path) -> None:
    missing: list[Path] = []
    cursor = directory
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    for path in reversed(missing):
        path.mkdir(mode=0o700)
        path.chmod(0o700)


def _ensure_database_location(database_path: Path | None) -> None:
    if database_path is None:
        return
    _ensure_private_directory(database_path.parent)
    if not database_path.exists():
        try:
            descriptor = os.open(database_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            return
        else:
            os.close(descriptor)
            database_path.chmod(0o600)


def _harden_upgrade_permissions(database_path: Path, data_directory: Path) -> None:
    """Applies private modes to the data directory and SQLite's live files."""
    _ensure_private_directory(data_directory)
    data_directory.chmod(0o700)
    for path in (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
    ):
        if path.exists():
            path.chmod(0o600)


def _readonly_connection(database_path: Path, *, timeout_ms: int = 5000) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro", uri=True, timeout=timeout_ms / 1000
    )
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA recursive_triggers=ON")
    return connection


def _has_schema_objects(database_path: Path | None, *, timeout_ms: int = 5000) -> bool:
    if database_path is None or not database_path.is_file() or database_path.stat().st_size == 0:
        return False
    try:
        with closing(_readonly_connection(database_path, timeout_ms=timeout_ms)) as connection:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
            return row is not None
    except sqlite3.Error as error:
        raise MigrationError("Impossible de lire l'état SQLite avant la migration") from error


def current_revision(database_url: str, *, timeout_ms: int = 5000) -> str | None:
    """Lit la révision sans créer le fichier ni la table Alembic."""
    database_path = database_file_path(database_url)
    if database_path is None or not database_path.is_file() or database_path.stat().st_size == 0:
        return None
    try:
        with closing(_readonly_connection(database_path, timeout_ms=timeout_ms)) as connection:
            version_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
            ).fetchone()
            if version_table is None:
                return None
            rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.Error as error:
        raise MigrationError("Impossible de lire la révision Alembic de la base") from error
    if len(rows) > 1:
        raise MigrationError("La base contient plusieurs révisions Alembic actives")
    return None if not rows else str(rows[0][0])


def _application_tables(database_path: Path | None, *, timeout_ms: int = 5000) -> set[str]:
    if database_path is None or not database_path.is_file() or database_path.stat().st_size == 0:
        return set()
    try:
        with closing(_readonly_connection(database_path, timeout_ms=timeout_ms)) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name <> 'alembic_version'"
            ).fetchall()
    except sqlite3.Error as error:
        raise MigrationError("Impossible de lire les tables de la base SQLite") from error
    return {str(row[0]) for row in rows}


def _is_busy_failure(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        code = getattr(current, "sqlite_errorcode", None)
        if isinstance(code, int) and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return True
        if (
            "database is locked" in str(current).lower()
            or "database table is locked" in str(current).lower()
        ):
            return True
        current = current.__cause__
    return False


def database_state(settings: Settings) -> tuple[str, str | None]:
    """Renvoie l'état du schéma; les verrous SQLite courts sont ``degraded``."""
    try:
        database_path = database_file_path(settings.database_url)
    except MigrationError:
        return "outdated", None
    if database_path is None or not database_path.is_file():
        return "not_initialized", None
    try:
        has_objects = _has_schema_objects(database_path, timeout_ms=200)
        tables = _application_tables(database_path, timeout_ms=200)
    except MigrationError as error:
        if _is_busy_failure(error):
            return "degraded", None
        return "outdated", None
    if not has_objects:
        return "not_initialized", None
    try:
        revision = current_revision(settings.database_url, timeout_ms=200)
        head = _head_revision(settings)
    except MigrationError as error:
        if _is_busy_failure(error):
            return "degraded", None
        return "outdated", None
    if revision == head and set(Base.metadata.tables).issubset(tables):
        return "ok", revision
    return "outdated", revision


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def create_verified_backup(
    database_path: Path, backup_directory: Path, *, source_revision: str | None
) -> BackupResult:
    """Sauvegarde une image SQLite cohérente, y compris son contenu journalisé en WAL."""
    temp_path: Path | None = None
    final_path: Path | None = None
    manifest_temp: Path | None = None
    try:
        _ensure_private_directory(backup_directory)
        now = datetime.now(UTC)
        stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        stem = f"pre-upgrade-{stamp}-{uuid.uuid4()}"
        final_path = backup_directory / f"{stem}.sqlite"
        temp_path = backup_directory / f".{stem}.tmp.sqlite"
        descriptor = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)

        source = _readonly_connection(database_path)
        try:
            destination = sqlite3.connect(temp_path, timeout=5)
            try:
                destination.execute("PRAGMA foreign_keys=ON")
                destination.execute("PRAGMA recursive_triggers=ON")
                source.backup(destination, pages=256, sleep=0.05)
                destination.commit()
                integrity = destination.execute("PRAGMA integrity_check").fetchone()
            finally:
                destination.close()
        finally:
            source.close()
        if integrity != ("ok",):
            raise BackupError("La vérification integrity_check de la sauvegarde a échoué")

        temp_path.chmod(0o600)
        os.replace(temp_path, final_path)
        final_path.chmod(0o600)
        size = final_path.stat().st_size
        digest = _sha256_file(final_path)
        manifest_path = final_path.with_suffix(".manifest.json")
        manifest_temp = backup_directory / f".{manifest_path.name}.tmp"
        manifest = {
            "format_version": 1,
            "created_at_utc": now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "backup_file": final_path.name,
            "source_revision": source_revision,
            "integrity_check": "ok",
            "file_size_bytes": size,
            "sha256": digest,
            "database_scope": "complete-sqlite-database",
        }
        descriptor = os.open(manifest_temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        manifest_temp.chmod(0o600)
        os.replace(manifest_temp, manifest_path)
        manifest_path.chmod(0o600)

        with closing(_readonly_connection(final_path)) as verified:
            final_integrity = verified.execute("PRAGMA integrity_check").fetchone()
        if final_integrity != ("ok",):
            raise BackupError("La sauvegarde publiée ne passe pas integrity_check")
        return BackupResult(final_path, manifest_path, digest, size)
    except BackupError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError) as error:
        raise BackupError(
            "Sauvegarde pré-migration impossible; la migration est refusée"
        ) from error
    finally:
        for path in (temp_path, manifest_temp):
            if path is not None:
                with suppress(OSError):
                    path.unlink(missing_ok=True)


def upgrade_to_head(settings: Settings) -> UpgradeResult:
    """Sauvegarde d'abord toute base existante non vide puis migre jusqu'à la tête."""
    database_path = database_file_path(settings.database_url)
    if database_path is None:
        raise MigrationError("Une base SQLite fichier est requise pour appliquer des migrations")
    _ensure_database_location(database_path)
    data_directory = settings.data_dir.expanduser()
    _harden_upgrade_permissions(database_path, data_directory)
    head = _head_revision(settings)
    current = current_revision(settings.database_url)
    tables = _application_tables(database_path)
    expected_tables = set(Base.metadata.tables)
    if current == head and not expected_tables.issubset(tables):
        raise MigrationError(
            "La base est marquée à la tête Alembic mais son schéma est incomplet; "
            "restaurez une sauvegarde ou créez une migration corrective"
        )

    backup: BackupResult | None = None
    if current != head and _has_schema_objects(database_path):
        backup = create_verified_backup(
            database_path,
            settings.data_dir.expanduser() / "backups",
            source_revision=current,
        )

    config = _alembic_config(settings)
    config.attributes["operation"] = "upgrade"
    config.attributes["pre_upgrade_backup_checked"] = True
    try:
        command.upgrade(config, "head")
    except Exception as error:
        if isinstance(error, MigrationError):
            raise
        if backup is None:
            message = (
                "La migration Alembic a échoué; aucune sauvegarde pré-migration n'a été créée."
            )
        else:
            message = (
                "La migration Alembic a échoué; la sauvegarde vérifiée est conservée: "
                f"{backup.database_path.name}"
            )
        raise MigrationError(message) from error
    finally:
        _harden_upgrade_permissions(database_path, data_directory)
    final_revision = current_revision(settings.database_url)
    if final_revision != head:
        raise MigrationError("Alembic n'a pas atteint la révision attendue")
    return UpgradeResult(final_revision, backup)


def _check_status(settings: Settings) -> tuple[bool, str, str | None, str]:
    state, revision = database_state(settings)
    head = _head_revision(settings)
    return state == "ok", state, revision, head


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gestion locale des migrations SQLite")
    parser.add_argument("command", choices=("upgrade", "current", "check"))
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        if args.command == "upgrade":
            result = upgrade_to_head(settings)
            backup_label = "none" if result.backup is None else result.backup.database_path.name
            print(f"revision={result.revision} backup={backup_label}")
            return 0
        if args.command == "current":
            revision = current_revision(settings.database_url)
            print(f"revision={revision or 'none'}")
            return 0
        ready, state, revision, head = _check_status(settings)
        print(f"status={state} revision={revision or 'none'} head={head}")
        return 0 if ready else 1
    except (MigrationError, BackupError, OSError, ValueError) as error:
        print(f"migration_error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
