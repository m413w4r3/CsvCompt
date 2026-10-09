"""Fabrique de l'application FastAPI locale."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI

from app.api.v1.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.errors import InternalErrorBoundary, install_error_handlers
from app.core.logging import configure_logging
from app.db.session import create_sqlite_engine


def create_app(
    settings: Settings | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Construit l'application; `clock` est surchargeable pour des tests déterministes."""
    active_settings = settings or get_settings()
    configure_logging()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        yield
        application.state.db_engine.dispose()

    application = FastAPI(
        title="Comptes familiaux API", version=active_settings.version, lifespan=lifespan
    )
    application.state.settings = active_settings
    application.state.clock = clock or (lambda: datetime.now(UTC))
    application.state.db_engine = create_sqlite_engine(active_settings.database_url)
    application.add_middleware(InternalErrorBoundary)
    install_error_handlers(application)
    application.include_router(health_router, prefix="/api/v1")

    # Point d'extension documenté pour servir ultérieurement le build frontend statique.
    return application


app = create_app()
