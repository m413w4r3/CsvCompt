"""Contrôle de disponibilité hors ligne."""

from pathlib import Path

from fastapi import APIRouter, Request
from sqlalchemy.engine import make_url

router = APIRouter()


def database_status(database_url: str) -> str:
    """Vérifie la présence d'une base SQLite sans l'ouvrir ni la créer."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return "configured"
    database = url.database
    if not database or database == ":memory:":
        return "not_initialized"
    return "ok" if Path(database).expanduser().is_file() else "not_initialized"


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    return {
        "status": "ok",
        "version": settings.version,
        "time_utc": request.app.state.clock().isoformat().replace("+00:00", "Z"),
        "database": {"status": database_status(settings.database_url)},
        "llm": {"enabled": settings.llm_enabled},
        "offline_ready": True,
    }
