"""Fabrique de l'application FastAPI locale."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from app.api.v1.accounts import router as accounts_router
from app.api.v1.categories import router as categories_router
from app.api.v1.health import router as health_router
from app.api.v1.imports import router as imports_router
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


async def _expire_transaction_snapshots(store: TransactionSnapshotStore) -> None:
    while True:
        await asyncio.sleep(store.sweep_interval_milliseconds / 1000)
        try:
            await asyncio.to_thread(store.expire_expired)
        except Exception as error:
            logger.warning(
                "transaction_snapshot_sweep_failed exception_type=%s correlation_id=%s",
                type(error).__name__,
                uuid4().hex,
            )
            await asyncio.sleep(0.1)


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
                    try:
                        await sweeper
                    except asyncio.CancelledError:
                        current_task = asyncio.current_task()
                        if current_task is not None and current_task.cancelling():
                            raise
                    except Exception:
                        pass
            finally:
                try:
                    application.state.transaction_snapshot_store.close_all()
                finally:
                    try:
                        application.state.transaction_snapshot_engine.dispose()
                    finally:
                        application.state.db_engine.dispose()

    application = FastAPI(
        title="Comptes familiaux API", version=active_settings.version, lifespan=lifespan
    )
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

    # Point d'extension documenté pour servir ultérieurement le build frontend statique.
    return application
