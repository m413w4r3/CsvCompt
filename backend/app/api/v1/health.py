"""Contrôle de disponibilité hors ligne conforme au contrat REST v1."""

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Request

from app.core.config import Settings
from app.db.migrate import MigrationError, database_file_path, database_state

router = APIRouter()


def database_status(database_url: str) -> tuple[str, str, str | None]:
    """Renvoie l'état contractuel, l'état SQLite détaillé et sa révision, en lecture seule."""
    settings = Settings(database_url=database_url, _env_file=None)
    state, revision = database_state(settings)
    if state == "ok":
        return "ok", state, revision
    if state == "not_initialized":
        return "degraded", state, revision

    # Un schéma lisible mais ancien est dégradé. Une base illisible ou dont
    # SQLite échoue au contrôle d'intégrité est indisponible.
    try:
        path: Path | None = database_file_path(database_url)
        if path is None or not path.is_file():
            return "degraded", "not_initialized", None
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            return "unavailable", "unavailable", revision
    except (MigrationError, OSError, sqlite3.Error):
        return "unavailable", "unavailable", revision
    return "degraded", state, revision


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    storage_status, detail_status, revision = database_status(settings.database_url)
    return {
        "status": "ok",
        "api_version": "1",
        "storage_status": storage_status,
        "llm_enabled": settings.llm_enabled,
        "version": settings.version,
        "time_utc": request.app.state.clock().isoformat().replace("+00:00", "Z"),
        "database": {"status": detail_status, "revision": revision},
        "offline_ready": True,
    }
