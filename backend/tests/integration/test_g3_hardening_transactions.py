from __future__ import annotations

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

from app.api.v1.transactions import _safe_frontend_summary
from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.db.models import Transaction
from app.db.session import session_scope
from app.main import create_app
from app.services import import_commit

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
        punctuated = client.get(
            "/api/v1/transactions", params={"q": "OPERATION%", "limit": 100}
        )
        separators_only = client.get(
            "/api/v1/transactions", params={"q": "%_-", "limit": 100}
        )
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

        release.set()
        worker.join(timeout=20)
        assert not worker.is_alive()
        assert "error" not in outcome
        assert outcome["response"]["status"] == "committed"  # type: ignore[index]

        next_page = client.get(
            "/api/v1/transactions",
            params={"limit": 100, "cursor": first["next_cursor"]},
        )
        assert next_page.status_code == 200, next_page.text
        payload = next_page.json()
        assert payload["total_count"] == first["total_count"]
        assert all(
            not (
                row["booked_on"] == "2026-01-01"
                and "NOUVELLE OPERATION" in row["raw_label_preview"]
            )
            for row in payload["items"]
        )


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
