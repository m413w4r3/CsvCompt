from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.main import create_app

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _ready(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'transactions.sqlite'}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    return TestClient(create_app(settings))


@pytest.mark.integration
def test_TXN_01_list_summary_detail_filters_and_safe_exposure(tmp_path: Path) -> None:
    client = _ready(tmp_path)
    try:
        source = (FIXTURES / "ca_2026_simple.csv").read_bytes()
        preview = client.post(
            "/api/v1/imports/preview", files={"file": ("synthetic.csv", source, "text/csv")}
        ).json()
        mapped = client.patch(
            f"/api/v1/imports/{preview['preview_id']}/sections/1/account-mapping",
            json={
                "expected_version": preview["version"],
                "new_account": {"alias": "Compte test synthétique", "account_role": "current"},
            },
        )
        assert mapped.status_code == 200
        response = client.post(
            f"/api/v1/imports/{preview['preview_id']}/commit",
            headers={"Idempotency-Key": str(uuid4())},
            json={"expected_version": mapped.json()["version"]},
        )
        assert response.status_code == 200

        result = client.get("/api/v1/transactions?limit=1")
        assert result.status_code == 200
        body = result.json()
        assert body["total_count"] == preview["operation_count"]
        assert body["summary"]["count"] == body["total_count"]
        assert body["summary"]["net_cents"] == (
            body["summary"]["debit_total_cents"] + body["summary"]["credit_total_cents"]
        )
        assert "identity_label" not in body["items"][0]
        assert len(body["items"][0]["raw_label_preview"]) <= 200
        detail = client.get(f"/api/v1/transactions/{body['items'][0]['id']}")
        assert detail.status_code == 200
        assert detail.json()["sources"]
        assert "identity_label" in detail.json()
        assert "account_fingerprint" not in detail.text
        assert "archive_relpath" not in detail.text
        assert client.get("/api/v1/transactions?limit=101").status_code == 422
        assert (
            client.get(
                "/api/v1/transactions?period_start=2026-12-31&period_end=2026-01-01"
            ).status_code
            == 422
        )
        assert client.get("/api/v1/transactions?cursor=invalid").status_code == 422
        for special in ("%", "_", "'", "é漢字"):
            assert client.get("/api/v1/transactions", params={"q": special}).status_code == 200
        account_id = response.json()["sections"][0]["account_id"]
        import_id = response.json()["import_file_id"]
        for params in (
            {"account_ids": account_id},
            {"period_start": "2026-01-01", "period_end": "2026-12-31"},
            {"direction": "debit"},
            {"direction": "credit"},
            {"amount_min_cents": 0},
            {"amount_max_cents": 10_000_000},
            {"import_file_id": import_id},
            {"classification": "unclassified"},
            {"flow_kind": "unknown"},
        ):
            assert client.get("/api/v1/transactions", params=params).status_code == 200
        first_page = client.get("/api/v1/transactions?limit=1").json()
        if first_page["next_cursor"]:
            second_page = client.get(
                "/api/v1/transactions",
                params={"limit": 1, "cursor": first_page["next_cursor"]},
            ).json()
            assert first_page["items"][0]["id"] != second_page["items"][0]["id"]
        with client.app.state.db_engine.connect() as connection:
            plan = connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT * FROM transactions WHERE account_id = ? "
                "ORDER BY booked_on DESC, transaction_id DESC",
                (detail.json()["account_id"],),
            ).all()
        assert any("ix_transactions_account_booked" in str(row) for row in plan)
    finally:
        client.close()
