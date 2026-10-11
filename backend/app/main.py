"""Fabrique de l'application FastAPI locale."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import FileResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.accounts import router as accounts_router
from app.api.v1.analytics import router as analytics_router
from app.api.v1.categories import router as categories_router
from app.api.v1.health import router as health_router
from app.api.v1.imports import router as imports_router
from app.api.v1.review import router as review_router
from app.api.v1.transactions import router as transactions_router
from app.core.config import Settings, get_settings
from app.core.errors import InternalErrorBoundary, install_error_handlers
from app.core.logging import configure_logging
from app.core.security import LocalSecurityMiddleware
from app.db.errors import DatabaseBusyError
from app.db.migrate import database_state
from app.db.session import create_sqlite_engine, create_sqlite_snapshot_engine
from app.domain.taxonomy import load_taxonomy
from app.services.categories import sync_taxonomy
from app.services.idempotency import purge_expired_idempotency
from app.services.import_archive import ArchiveLockUnavailable, purge_orphaned_archives
from app.services.import_preview_store import ImportPreviewStore
from app.services.transactions import TransactionSnapshotStore

logger = logging.getLogger(__name__)
_SWEEP_SHUTDOWN_TIMEOUT_SECONDS = 5.0


def _consume_sweep_result(future: asyncio.Future[int]) -> None:
    """Retrieve a late worker exception after a shutdown timeout."""
    with suppress(asyncio.CancelledError):
        future.exception()


async def _expire_transaction_snapshots(store: TransactionSnapshotStore) -> None:
    while True:
        await asyncio.sleep(store.sweep_interval_milliseconds / 1000)
        loop = asyncio.get_running_loop()
        sweep = loop.run_in_executor(None, store.expire_expired)
        sweep.add_done_callback(_consume_sweep_result)
        try:
            await asyncio.shield(sweep)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "transaction_snapshot_sweep_failed exception_type=%s correlation_id=%s",
                type(error).__name__,
                uuid4().hex,
            )


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
        sweeper: asyncio.Task[None] | None = None
        try:
            application.state.import_preview_store.purge_expired()
            state, _revision = database_state(active_settings)
            if state == "ok":
                sync_taxonomy(application.state.db_engine, load_taxonomy())
                try:
                    purge_expired_idempotency(application.state.db_engine)
                    purge_orphaned_archives(application.state.db_engine, active_settings.data_dir)
                except ArchiveLockUnavailable:
                    logger.warning("archive_purge_skipped code=ARCHIVE_LOCK_UNAVAILABLE")
                except (DatabaseBusyError, OperationalError):
                    pass
            sweeper = asyncio.create_task(
                _expire_transaction_snapshots(application.state.transaction_snapshot_store),
                name="transaction-snapshot-expiration",
            )
            yield
        finally:
            try:
                if sweeper is not None:
                    sweeper.cancel()
                    _done, pending = await asyncio.wait(
                        {sweeper}, timeout=_SWEEP_SHUTDOWN_TIMEOUT_SECONDS
                    )
                    if pending:
                        logger.warning(
                            "transaction_snapshot_sweep_shutdown_timeout timeout_seconds=%s",
                            _SWEEP_SHUTDOWN_TIMEOUT_SECONDS,
                        )
            finally:
                try:
                    application.state.transaction_snapshot_store.close_all()
                finally:
                    try:
                        application.state.transaction_snapshot_engine.dispose()
                    finally:
                        application.state.db_engine.dispose()

    application = FastAPI(
        title="Comptes familiaux API",
        version=active_settings.version,
        lifespan=lifespan,
        docs_url="/docs" if active_settings.enable_api_docs else None,
        redoc_url="/redoc" if active_settings.enable_api_docs else None,
        openapi_url="/openapi.json" if active_settings.enable_api_docs else None,
    )

    @application.middleware("http")
    async def disable_analytics_caching(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/analytics/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    application.state.settings = active_settings
    application.state.clock = clock or (lambda: datetime.now(UTC))
    application.state.db_engine = create_sqlite_engine(active_settings.database_url)
    application.state.transaction_snapshot_engine = create_sqlite_snapshot_engine(
        active_settings.database_url
    )
    application.state.transaction_snapshot_store = TransactionSnapshotStore(
        ttl_milliseconds=60_000, max_snapshots=3
    )
    application.state.import_preview_store = ImportPreviewStore(
        active_settings.effective_preview_dir,
        ttl_seconds=active_settings.preview_ttl_seconds,
    )
    application.add_middleware(InternalErrorBoundary)
    application.add_middleware(
        LocalSecurityMiddleware,
        allowed_origins=active_settings.cors_allowed_origins,
        bind_host=active_settings.app_host,
        bind_port=active_settings.app_port,
    )
    install_error_handlers(application)
    application.include_router(health_router, prefix="/api/v1")
    application.include_router(imports_router, prefix="/api/v1")
    application.include_router(categories_router, prefix="/api/v1")
    application.include_router(accounts_router, prefix="/api/v1")
    application.include_router(transactions_router, prefix="/api/v1")
    application.include_router(analytics_router, prefix="/api/v1")
    application.include_router(review_router, prefix="/api/v1")

    if active_settings.frontend_dist_dir is not None:
        frontend_root = active_settings.frontend_dist_dir.resolve()
        index_path = (frontend_root / "index.html").resolve()
        if not index_path.is_relative_to(frontend_root) or not index_path.is_file():
            raise ValueError("FRONTEND_DIST_DIR doit contenir index.html")

        def frontend_file(asset_path: str = "") -> FileResponse:
            # Keep API misses as API 404s and never expose a directory listing.
            if asset_path == "api" or asset_path.startswith("api/"):
                raise StarletteHTTPException(status_code=404)
            if asset_path in {"docs", "redoc", "openapi.json"}:
                raise StarletteHTTPException(status_code=404)
            candidate = (frontend_root / asset_path).resolve()
            if not candidate.is_relative_to(frontend_root):
                raise StarletteHTTPException(status_code=404)
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_path)

        application.add_api_route(
            "/", frontend_file, methods=["GET", "HEAD"], include_in_schema=False
        )
        application.add_api_route(
            "/{asset_path:path}", frontend_file, methods=["GET", "HEAD"], include_in_schema=False
        )

    return application
