from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.api.v1.transactions import _safe_frontend_summary
from app.core.config import Settings
from app.core.errors import AppError
from app.db.migrate import upgrade_to_head
from app.db.models import Transaction
from app.db.session import session_scope
from app.main import create_app
from app.services import import_commit
from app.services.transactions import SnapshotUnavailable, TransactionSnapshotStore

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _ready(tmp_path: Path, name: str):
    database = tmp_path / f"{name}.sqlite"
    data_dir = tmp_path / f"{name}-data"
    settings = Settings(database_url=f"sqlite:///{database}", data_dir=data_dir, _env_file=None)
    upgrade_to_head(settings)
    return create_app(settings), data_dir


def _upload(client: TestClient, raw: bytes) -> dict[str, object]:
    response = client.post(
        "/api/v1/imports/preview",
        files={"file": ("synthetic.csv", raw, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ready_preview(client: TestClient, raw: bytes) -> dict[str, object]:
    preview = _upload(client, raw)
    if preview["sections"][0]["account_resolution"] != "known":
        response = client.patch(
            f"/api/v1/imports/{preview['preview_id']}/sections/1/account-mapping",
            json={
                "expected_version": preview["version"],
                "new_account": {"alias": "Compte curseur synthétique", "account_role": "current"},
            },
        )
        assert response.status_code == 200, response.text
        preview = response.json()
    return preview


def _commit(client: TestClient, preview: dict[str, object]) -> dict[str, object]:
    response = client.post(
        f"/api/v1/imports/{preview['preview_id']}/commit",
        headers={"Idempotency-Key": str(uuid4())},
        json={"expected_version": preview["version"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.integration
def test_IMP_14_transaction_cursor_orders_ties_by_id_and_hides_search_text(
    tmp_path: Path,
) -> None:
    app, _data_dir = _ready(tmp_path, "cursor-sort")
    raw = (FIXTURES / "overlap_A.csv").read_bytes()
    with TestClient(app) as client:
        _commit(client, _ready_preview(client, raw))
        rows: list[dict[str, object]] = []
        cursor = None
        while True:
            params: dict[str, object] = {"limit": 4}
            if cursor:
                params["cursor"] = cursor
            response = client.get("/api/v1/transactions", params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            rows.extend(page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break

        actual = [(row["booked_on"], row["id"]) for row in rows]
        assert actual == sorted(actual, reverse=True)
        assert len({row["id"] for row in rows}) == len(rows) == 21

        searched = client.get("/api/v1/transactions", params={"limit": 1, "q": "OPERATION"})
        assert searched.status_code == 200
        cursor = searched.json()["next_cursor"]
        assert cursor
        encoded, _signature = cursor.split(".", 1)
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(decoded)
        assert "q" not in payload["filters"]
        assert payload["filters"]["filters_hmac"]
        assert b"OPERATION" not in decoded


@pytest.mark.integration
def test_IMP_14_transaction_search_uses_normalized_punctuation(tmp_path: Path) -> None:
    app, _data_dir = _ready(tmp_path, "normalized-search")
    raw = (FIXTURES / "overlap_A.csv").read_bytes()
    with TestClient(app) as client:
        _commit(client, _ready_preview(client, raw))

        plain = client.get("/api/v1/transactions", params={"q": "OPERATION", "limit": 100})
        punctuated = client.get("/api/v1/transactions", params={"q": "OPERATION%", "limit": 100})
        separators_only = client.get("/api/v1/transactions", params={"q": "%_-", "limit": 100})
        unfiltered = client.get("/api/v1/transactions", params={"limit": 100})

    responses = (plain, punctuated, separators_only, unfiltered)
    assert all(response.status_code == 200 for response in responses)
    assert [row["id"] for row in punctuated.json()["items"]] == [
        row["id"] for row in plain.json()["items"]
    ]
    assert [row["id"] for row in separators_only.json()["items"]] == [
        row["id"] for row in unfiltered.json()["items"]
    ]


@pytest.mark.integration
def test_IMP_14_transaction_snapshot_excludes_rows_imported_after_first_page(
    tmp_path: Path,
) -> None:
    app, _data_dir = _ready(tmp_path, "cursor-snapshot")
    raw = (FIXTURES / "overlap_A.csv").read_bytes()
    new_row = raw.replace(
        b'31/03/2026;"Paiement par carte\nX0331 OPERATION FICTIVE',
        b'01/01/2026;"Paiement par carte\nX9999 NOUVELLE OPERATION',
        1,
    )
    assert new_row != raw

    with TestClient(app) as client:
        _commit(client, _ready_preview(client, raw))
        with session_scope(client.app.state.db_engine) as session:
            latest_imported_at = session.scalar(
                select(Transaction.imported_at_utc)
                .order_by(Transaction.imported_at_utc.desc())
                .limit(1)
            )
        assert latest_imported_at is not None
        if latest_imported_at.tzinfo is None:
            latest_imported_at = latest_imported_at.replace(tzinfo=UTC)
        snapshot_at = latest_imported_at + timedelta(milliseconds=200)
        app.state.clock = lambda: snapshot_at
        first_page = client.get("/api/v1/transactions", params={"limit": 1})
        assert first_page.status_code == 200
        cursor = first_page.json()["next_cursor"]
        assert cursor

        wait_seconds = (
            snapshot_at + timedelta(milliseconds=250) - datetime.now(UTC)
        ).total_seconds()
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        committed_later = _commit(client, _ready_preview(client, new_row))
        assert committed_later["created_transaction_count"] == 1
        with session_scope(client.app.state.db_engine) as session:
            newly_added = session.scalar(
                select(Transaction.transaction_id).where(
                    Transaction.booked_on == date(2026, 1, 1),
                    Transaction.identity_label.contains("NOUVELLE OPERATION"),
                )
            )
        assert newly_added is not None

        next_page = client.get("/api/v1/transactions", params={"limit": 1, "cursor": cursor})
        assert next_page.status_code == 200, next_page.text
        assert all(row["id"] != newly_added for row in next_page.json()["items"])


@pytest.mark.integration
def test_IMP_14_transaction_snapshot_excludes_commit_finalized_after_page_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _data_dir = _ready(tmp_path, "cursor-commit-visibility")
    raw = (FIXTURES / "overlap_A.csv").read_bytes()
    new_row = raw.replace(
        b'31/03/2026;"Paiement par carte\nX0331 OPERATION FICTIVE',
        b'01/01/2026;"Paiement par carte\nX9999 NOUVELLE OPERATION',
        1,
    )
    assert new_row != raw
    entered = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}
    original_save = import_commit.save_response

    def pause_before_sqlite_commit(*args, **kwargs):
        response = original_save(*args, **kwargs)
        entered.set()
        if not release.wait(timeout=15):
            raise RuntimeError("synthetic snapshot release timeout")
        return response

    with TestClient(app) as client:
        baseline = _ready_preview(client, raw)
        _commit(client, baseline)
        pending = _ready_preview(client, new_row)
        monkeypatch.setattr(import_commit, "save_response", pause_before_sqlite_commit)

        def commit_worker() -> None:
            try:
                outcome["response"] = import_commit.commit_preview(
                    str(pending["preview_id"]),
                    int(pending["version"]),
                    str(uuid4()),
                    "synthetic-snapshot-race",
                    engine=client.app.state.db_engine,
                    store=client.app.state.import_preview_store,
                    data_dir=_data_dir,
                )
            except BaseException as error:
                outcome["error"] = error

        worker = threading.Thread(target=commit_worker)
        worker.start()
        assert entered.wait(timeout=10)
        first_page = client.get("/api/v1/transactions", params={"limit": 1})
        assert first_page.status_code == 200, first_page.text
        first = first_page.json()
        assert first["next_cursor"]
        held_cursors = [(first["next_cursor"], {}, first["total_count"])]
        held_filters = [
            {"q": "OPERATION%"},
            {"q": "opération"},
        ]
        for index in range(2):
            filters = held_filters[index]
            pinned_page = client.get(
                "/api/v1/transactions",
                params={"limit": 1, **filters},
            )
            assert pinned_page.status_code == 200, pinned_page.text
            assert pinned_page.json()["next_cursor"]
            held_cursors.append(
                (
                    pinned_page.json()["next_cursor"],
                    filters,
                    pinned_page.json()["total_count"],
                )
            )

        release.set()
        worker.join(timeout=20)
        assert not worker.is_alive()
        assert "error" not in outcome
        assert outcome["response"]["status"] == "committed"  # type: ignore[index]

        for held_cursor, filters, expected_total in held_cursors:
            next_page = client.get(
                "/api/v1/transactions",
                params={"limit": 100, "cursor": held_cursor, **filters},
            )
            assert next_page.status_code == 200, next_page.text
            payload = next_page.json()
            assert payload["total_count"] == expected_total
            assert all(
                not (
                    row["booked_on"] == "2026-01-01"
                    and "NOUVELLE OPERATION" in row["raw_label_preview"]
                )
                for row in payload["items"]
            )


@pytest.mark.integration
def test_IMP_14_transaction_snapshot_pool_lru_handles_33_pages_and_rapid_filters(
    tmp_path: Path,
) -> None:
    app, _data_dir = _ready(tmp_path, "snapshot-pool-lru")
    raw = (FIXTURES / "overlap_A.csv").read_bytes()

    with TestClient(app) as client:
        _commit(client, _ready_preview(client, raw))
        pending_commit = _ready_preview(client, raw)
        store = client.app.state.transaction_snapshot_store
        snapshot_engine = client.app.state.transaction_snapshot_engine
        main_engine = client.app.state.db_engine
        first_cursor: str | None = None

        for _index in range(33):
            started = time.perf_counter()
            response = client.get("/api/v1/transactions", params={"limit": 1})
            elapsed = time.perf_counter() - started
            assert response.status_code == 200, response.text
            assert elapsed < 1
            assert response.json()["next_cursor"]
            if first_cursor is None:
                first_cursor = response.json()["next_cursor"]
            assert store.snapshot_count <= 3

        assert store.snapshot_count == 3
        assert snapshot_engine.pool.checkedout() == 3
        assert main_engine.pool.checkedout() == 0
        stale = client.get("/api/v1/transactions", params={"limit": 1, "cursor": first_cursor})
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "CURSOR_STALE"

        for index in range(50):
            response = client.get(
                "/api/v1/transactions",
                params={
                    "limit": 1,
                    "q": "OPERATION%" if index % 2 == 0 else "opération",
                    "classification": "all" if index % 2 == 0 else "unclassified",
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["next_cursor"]
            assert store.snapshot_count <= 3

        started = time.perf_counter()
        listed = client.get("/api/v1/transactions", params={"limit": 1})
        assert listed.status_code == 200, listed.text
        assert time.perf_counter() - started < 1

        started = time.perf_counter()
        committed = client.post(
            f"/api/v1/imports/{pending_commit['preview_id']}/commit",
            headers={"Idempotency-Key": str(uuid4())},
            json={"expected_version": pending_commit["version"]},
        )
        assert committed.status_code == 200, committed.text
        assert time.perf_counter() - started < 1

        started = time.perf_counter()
        accounts = client.get("/api/v1/accounts")
        assert accounts.status_code == 200, accounts.text
        assert time.perf_counter() - started < 1
        assert snapshot_engine.pool.checkedout() == 3
        assert main_engine.pool.checkedout() == 0


@pytest.mark.integration
def test_IMP_14_expired_snapshot_closes_without_navigation_and_allows_wal_truncate(
    tmp_path: Path,
) -> None:
    app, _data_dir = _ready(tmp_path, "snapshot-expiry")
    app.state.transaction_snapshot_store = TransactionSnapshotStore(
        ttl_milliseconds=150, max_snapshots=3
    )
    raw = (FIXTURES / "overlap_A.csv").read_bytes()

    with TestClient(app) as client:
        _commit(client, _ready_preview(client, raw))
        store = client.app.state.transaction_snapshot_store
        snapshot_engine = client.app.state.transaction_snapshot_engine
        database_engine = client.app.state.db_engine
        first = client.get("/api/v1/transactions", params={"limit": 1})
        assert first.status_code == 200, first.text
        cursor = first.json()["next_cursor"]
        assert cursor
        assert store.snapshot_count == 1
        assert snapshot_engine.pool.checkedout() == 1

        deadline = time.monotonic() + 1.5
        while store.snapshot_count and time.monotonic() < deadline:
            time.sleep(0.02)
        assert store.snapshot_count == 0
        assert snapshot_engine.pool.checkedout() == 0

        stale = client.get("/api/v1/transactions", params={"limit": 1, "cursor": cursor})
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "CURSOR_STALE"
        invalid = client.get("/api/v1/transactions", params={"cursor": "invalid"})
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "CURSOR_INVALID"

        with database_engine.connect() as connection:
            checkpoint = connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").one()
        assert checkpoint[0] == 0
        assert snapshot_engine.pool.checkedout() == 0


@pytest.mark.integration
def test_IMP_14_snapshot_sweeper_recovers_after_repeated_expiration_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _data_dir = _ready(tmp_path, "snapshot-sweeper-recovery")
    raw = (FIXTURES / "overlap_A.csv").read_bytes()
    store = app.state.transaction_snapshot_store
    store._ttl_milliseconds = 300
    original_expire = store.expire_expired
    calls = 0
    recovered = threading.Event()

    def fail_twice_then_expire() -> int:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("synthetic sweep failure")
        recovered.set()
        return original_expire()

    monkeypatch.setattr(store, "expire_expired", fail_twice_then_expire)
    with TestClient(app) as client:
        _commit(client, _ready_preview(client, raw))
        first = client.get("/api/v1/transactions", params={"limit": 1})
        assert first.status_code == 200, first.text
        assert first.json()["next_cursor"]
        assert store.snapshot_count == 1

        deadline = time.monotonic() + 3
        while store.snapshot_count and time.monotonic() < deadline:
            time.sleep(0.02)

        assert calls >= 3
        assert recovered.is_set()
        assert store.snapshot_count == 0


@pytest.mark.integration
def test_IMP_14_snapshot_sweeper_error_does_not_block_shutdown_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _data_dir = _ready(tmp_path, "snapshot-sweeper-shutdown")
    raw = (FIXTURES / "overlap_A.csv").read_bytes()
    store = app.state.transaction_snapshot_store
    store._ttl_milliseconds = 100
    failed = threading.Event()

    def fail_expiration() -> int:
        failed.set()
        raise RuntimeError("synthetic shutdown sweep failure")

    monkeypatch.setattr(store, "expire_expired", fail_expiration)
    snapshot_engine = app.state.transaction_snapshot_engine
    db_engine = app.state.db_engine
    with TestClient(app) as client:
        _commit(client, _ready_preview(client, raw))
        first = client.get("/api/v1/transactions", params={"limit": 1})
        assert first.status_code == 200, first.text
        assert store.snapshot_count == 1
        assert snapshot_engine.pool.checkedout() == 1

        assert failed.wait(timeout=2)

    assert store.snapshot_count == 0
    assert snapshot_engine.pool.checkedout() == 0
    assert db_engine.pool.checkedout() == 0


@pytest.mark.integration
@pytest.mark.parametrize("cancel_count", [1, 2, 3])
def test_IMP_14_snapshot_sweeper_late_worker_is_safe_after_lifespan_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_count: int
) -> None:
    app, _data_dir = _ready(tmp_path, f"snapshot-sweeper-cancel-{cancel_count}")
    store = app.state.transaction_snapshot_store
    store._ttl_milliseconds = 100
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    wait_entered = threading.Event()
    release_wait = asyncio.Event()
    loop_errors: list[dict[str, object]] = []
    original_expire = store.expire_expired

    def fail_after_close() -> int:
        worker_started.set()
        try:
            if not release_worker.wait(timeout=10):
                raise RuntimeError("synthetic worker release timeout")
            assert original_expire() == 0
            raise RuntimeError("synthetic late sweep failure")
        finally:
            worker_finished.set()

    monkeypatch.setattr(store, "expire_expired", fail_after_close)
    original_wait = asyncio.wait

    async def gated_wait(tasks, *, timeout):
        wait_entered.set()
        await release_wait.wait()
        return await original_wait(tasks, timeout=timeout)

    monkeypatch.setattr(main_module.asyncio, "wait", gated_wait)
    snapshot_engine = app.state.transaction_snapshot_engine
    db_engine = app.state.db_engine
    store.create(snapshot_engine, datetime.now(UTC).isoformat())
    assert snapshot_engine.pool.checkedout() == 1
    dispose_checkout_counts: list[tuple[int, int]] = []
    for engine in (snapshot_engine, db_engine):
        original_dispose = engine.dispose

        def observe_dispose(*args, _dispose=original_dispose, _engine=engine, **kwargs) -> None:
            checked_out_before = _engine.pool.checkedout()
            _dispose(*args, **kwargs)
            dispose_checkout_counts.append((checked_out_before, _engine.pool.checkedout()))

        monkeypatch.setattr(engine, "dispose", observe_dispose)

    async def cancel_lifespan() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        async def serve() -> None:
            async with app.router.lifespan_context(app):
                await asyncio.Event().wait()

        lifespan_task = asyncio.create_task(serve())
        assert await asyncio.to_thread(worker_started.wait, 2), "sweep worker did not start"
        lifespan_task.cancel()
        assert await asyncio.to_thread(wait_entered.wait, 2), "shutdown wait did not start"
        for _ in range(cancel_count - 1):
            lifespan_task.cancel()
        release_wait.set()
        with pytest.raises(asyncio.CancelledError):
            await lifespan_task

        assert not worker_finished.is_set()
        assert store.snapshot_count == 0
        assert snapshot_engine.pool.checkedout() == 0
        assert db_engine.pool.checkedout() == 0

        # Release the executor thread only after close_all() and both dispose().
        release_worker.set()
        assert await asyncio.to_thread(worker_finished.wait, 2)
        await asyncio.sleep(0)

    try:
        asyncio.run(cancel_lifespan())
    finally:
        release_worker.set()

    assert store.snapshot_count == 0
    assert snapshot_engine.pool.checkedout() == 0
    assert db_engine.pool.checkedout() == 0
    assert dispose_checkout_counts == [(0, 0), (0, 0)]
    assert not any("never retrieved" in str(context.get("message", "")) for context in loop_errors)


@pytest.mark.integration
def test_IMP_14_closed_snapshot_store_preserves_cursor_and_create_contracts(
    tmp_path: Path,
) -> None:
    app, _data_dir = _ready(tmp_path, "snapshot-store-closed")
    raw = (FIXTURES / "overlap_A.csv").read_bytes()

    with TestClient(app) as client:
        _commit(client, _ready_preview(client, raw))
        store = client.app.state.transaction_snapshot_store
        snapshot_engine = client.app.state.transaction_snapshot_engine
        first_page = client.get("/api/v1/transactions", params={"limit": 1})
        assert first_page.status_code == 200, first_page.text
        cursor = first_page.json()["next_cursor"]
        assert cursor
        snapshot_id, _session = store.create(snapshot_engine, datetime.now(UTC).isoformat())

        store.close_all()

        assert store.expire_expired() == 0
        with pytest.raises(SnapshotUnavailable), store.use(snapshot_id):
            pytest.fail("a closed snapshot must not be usable")
        with pytest.raises(AppError) as create_error:
            store.create(snapshot_engine, datetime.now(UTC).isoformat())
        assert (create_error.value.code, create_error.value.status_code) == ("DATABASE_BUSY", 503)
        store.discard(snapshot_id)
        assert store.snapshot_count == 0
        assert snapshot_engine.pool.checkedout() == 0

        stale_cursor = client.get("/api/v1/transactions", params={"limit": 1, "cursor": cursor})
        assert stale_cursor.status_code == 409
        assert stale_cursor.json()["error"]["code"] == "CURSOR_STALE"
        new_page = client.get("/api/v1/transactions", params={"limit": 1})
        assert new_page.status_code == 503
        assert new_page.json()["error"]["code"] == "DATABASE_BUSY"


@pytest.mark.integration
def test_IMP_14_snapshot_sweeper_timeout_logs_without_request_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _data_dir = _ready(tmp_path, "snapshot-sweeper-timeout")
    store = app.state.transaction_snapshot_store
    store._ttl_milliseconds = 100
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    loop_errors: list[dict[str, object]] = []
    warnings: list[str] = []
    timeout = 0.2
    release_delayed_sweeper = asyncio.Event()
    original_sweeper = main_module._expire_transaction_snapshots

    def fail_after_release() -> int:
        worker_started.set()
        try:
            if not release_worker.wait(timeout=10):
                raise RuntimeError("synthetic request marker worker release timeout")
            raise RuntimeError("synthetic request marker late failure")
        finally:
            worker_finished.set()

    monkeypatch.setattr(store, "expire_expired", fail_after_release)
    monkeypatch.setattr(main_module, "_SWEEP_SHUTDOWN_TIMEOUT_SECONDS", timeout)
    monkeypatch.setattr(
        main_module.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )

    async def delayed_cancel_sweeper(store):
        try:
            await original_sweeper(store)
        except asyncio.CancelledError:
            # Keep only this test task alive to exercise a real wait timeout.
            await release_delayed_sweeper.wait()
            raise

    monkeypatch.setattr(main_module, "_expire_transaction_snapshots", delayed_cancel_sweeper)
    original_close_all = store.close_all

    def close_store_and_release_sweeper() -> None:
        original_close_all()
        release_delayed_sweeper.set()

    monkeypatch.setattr(store, "close_all", close_store_and_release_sweeper)

    async def timeout_shutdown() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        async def serve() -> None:
            async with app.router.lifespan_context(app):
                assert await asyncio.to_thread(worker_started.wait, 2)

        lifespan_task = asyncio.create_task(serve())
        assert await asyncio.to_thread(worker_started.wait, 2), "sweep worker did not start"
        await lifespan_task
        assert not worker_finished.is_set()
        assert store.snapshot_count == 0
        assert app.state.transaction_snapshot_engine.pool.checkedout() == 0
        assert app.state.db_engine.pool.checkedout() == 0

        release_worker.set()
        assert await asyncio.to_thread(worker_finished.wait, 2)
        await asyncio.sleep(0)

    try:
        asyncio.run(timeout_shutdown())
    finally:
        release_worker.set()

    timeout_warnings = [
        message
        for message in warnings
        if message.startswith("transaction_snapshot_sweep_shutdown_timeout")
    ]
    assert timeout_warnings == [
        f"transaction_snapshot_sweep_shutdown_timeout timeout_seconds={timeout}"
    ]
    assert not any("request marker" in warning for warning in warnings)
    assert not any("never retrieved" in str(context.get("message", "")) for context in loop_errors)


@pytest.mark.integration
def test_IMP_14_snapshot_sweeper_external_cancel_wins_over_concurrent_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _data_dir = _ready(tmp_path, "snapshot-sweeper-cancel-timeout")
    store = app.state.transaction_snapshot_store
    store._ttl_milliseconds = 100
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    wait_entered = threading.Event()
    loop_errors: list[dict[str, object]] = []

    def finish_after_release() -> int:
        worker_started.set()
        try:
            if not release_worker.wait(timeout=10):
                raise RuntimeError("synthetic late worker timeout")
            return 0
        finally:
            worker_finished.set()

    monkeypatch.setattr(store, "expire_expired", finish_after_release)

    async def blocked_wait(_tasks, *, timeout):
        wait_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module.asyncio, "wait", blocked_wait)

    async def concurrent_cancel_and_timeout() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        async def serve() -> None:
            async with asyncio.timeout(2.0):
                async with app.router.lifespan_context(app):
                    assert await asyncio.to_thread(worker_started.wait, 2)

        lifespan_task = asyncio.create_task(serve())
        assert await asyncio.to_thread(wait_entered.wait, 2), "shutdown wait did not start"
        loop.call_later(0.5, lifespan_task.cancel)
        with pytest.raises(asyncio.CancelledError):
            await lifespan_task
        assert not worker_finished.is_set()

        release_worker.set()
        assert await asyncio.to_thread(worker_finished.wait, 2)
        await asyncio.sleep(0)

    try:
        asyncio.run(concurrent_cancel_and_timeout())
    finally:
        release_worker.set()

    assert not any("never retrieved" in str(context.get("message", "")) for context in loop_errors)


@pytest.mark.integration
def test_IMP_14_transaction_snapshot_saturation_is_retryable_503(tmp_path: Path) -> None:
    app, _data_dir = _ready(tmp_path, "snapshot-saturation")
    raw = (FIXTURES / "overlap_A.csv").read_bytes()

    with TestClient(app) as client:
        _commit(client, _ready_preview(client, raw))
        store = client.app.state.transaction_snapshot_store
        snapshot_engine = client.app.state.transaction_snapshot_engine
        locks: list[threading.Lock] = []
        for _ in range(3):
            store.create(snapshot_engine, "2026-10-10T12:00:00.000Z")
        for entry in store._entries.values():
            entry.lock.acquire()
            locks.append(entry.lock)

        try:
            response = client.get("/api/v1/transactions", params={"limit": 1})
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "DATABASE_BUSY"
        finally:
            for lock in locks:
                lock.release()
            store.close_all()

        assert snapshot_engine.pool.checkedout() == 0

        held_connections = [snapshot_engine.connect() for _ in range(4)]
        try:
            pool_busy = client.get("/api/v1/transactions", params={"limit": 1})
            assert pool_busy.status_code == 503
            assert pool_busy.json()["error"]["code"] == "DATABASE_BUSY"
        finally:
            for connection in held_connections:
                connection.close()

        assert snapshot_engine.pool.checkedout() == 0


def test_M_03_transaction_summary_never_returns_unsafe_cent_total() -> None:
    unsafe = 2**53
    safe = _safe_frontend_summary(
        {
            "count": 3,
            "debit_total_cents": -unsafe,
            "credit_total_cents": unsafe,
            "net_cents": 0,
        }
    )
    assert safe == {
        "count": 3,
        "debit_total_cents": None,
        "credit_total_cents": None,
        "net_cents": 0,
        "total_out_of_range": True,
    }


@pytest.mark.integration
def test_M_03_transaction_cursor_rejects_unsafe_cent_filter_values(tmp_path: Path) -> None:
    app, _data_dir = _ready(tmp_path, "unsafe-cent-filter")
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/transactions",
            params={"amount_min_cents": str(2**53)},
        )
    assert response.status_code == 422
