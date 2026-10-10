"""Fabrique de l'application FastAPI locale."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI

from app.api.v1.accounts import router as accounts_router
from app.api.v1.categories import router as categories_router
from app.api.v1.health import router as health_router
from app.api.v1.imports import router as imports_router
from app.api.v1.transactions import router as transactions_router
from app.core.config import Settings, get_settings
from app.core.errors import InternalErrorBoundary, install_error_handlers
from app.core.logging import configure_logging
from app.core.security import LocalTrustedHostMiddleware
from app.db.migrate import database_state
from app.db.session import create_sqlite_engine
from app.domain.taxonomy import load_taxonomy
from app.services.categories import sync_taxonomy
from app.services.import_preview_store import ImportPreviewStore


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
        application.state.import_preview_store.purge_expired()
        state, _revision = database_state(active_settings)
        if state == "ok":
            sync_taxonomy(application.state.db_engine, load_taxonomy())
        yield
        application.state.db_engine.dispose()

    application = FastAPI(
        title="Comptes familiaux API", version=active_settings.version, lifespan=lifespan
    )
    application.state.settings = active_settings
    application.state.clock = clock or (lambda: datetime.now(UTC))
    application.state.db_engine = create_sqlite_engine(active_settings.database_url)
    application.state.import_preview_store = ImportPreviewStore(
        active_settings.effective_preview_dir,
        ttl_seconds=active_settings.preview_ttl_seconds,
    )
    application.add_middleware(InternalErrorBoundary)
    application.add_middleware(
        LocalTrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost"],
    )
    install_error_handlers(application)
    application.include_router(health_router, prefix="/api/v1")
    application.include_router(imports_router, prefix="/api/v1")
    application.include_router(categories_router, prefix="/api/v1")
    application.include_router(accounts_router, prefix="/api/v1")
    application.include_router(transactions_router, prefix="/api/v1")

    # Point d'extension documenté pour servir ultérieurement le build frontend statique.
    return application
