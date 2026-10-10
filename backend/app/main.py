"""Fabrique de l'application FastAPI locale."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
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
            # Cancelling the asyncio wrapper cannot stop its worker thread. Wait
            # until the bounded shutdown deadline before disposing its engines.
            deadline = loop.time() + _SWEEP_SHUTDOWN_TIMEOUT_SECONDS
            current_task = asyncio.current_task()
            while current_task is not None and current_task.cancelling() > 1:
                current_task.uncancel()
            while not sweep.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait({sweep}, timeout=remaining)
                except asyncio.CancelledError:
                    # Keep the original cancellation pending while absorbing any
                    # repeated requests until the worker finishes or the deadline.
                    while current_task is not None and current_task.cancelling() > 1:
                        current_task.uncancel()
            if not sweep.done():
                logger.warning(
                    "transaction_snapshot_sweep_shutdown_timeout timeout_seconds=%s",
                    _SWEEP_SHUTDOWN_TIMEOUT_SECONDS,
                )
            raise
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
                    current_task = asyncio.current_task()
                    shutdown_cancelled = False
                    try:
                        while True:
                            try:
                                await asyncio.shield(sweeper)
                                break
                            except asyncio.CancelledError:
                                if sweeper.done():
                                    if shutdown_cancelled or (
                                        current_task is not None and current_task.cancelling()
                                    ):
                                        raise
                                    break
                                # A repeated cancellation of the lifespan must not
                                # interrupt the sweeper's own bounded worker wait.
                                shutdown_cancelled = True
                                while current_task is not None and current_task.cancelling():
                                    current_task.uncancel()
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
