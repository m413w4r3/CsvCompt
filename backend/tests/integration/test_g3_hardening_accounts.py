from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import accounts as accounts_api
from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.db.models import Account
from app.db.session import write_transaction
from app.main import create_app

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.integration
def test_KPI_05_month_before_first_coverage_is_neutral_not_started(tmp_path: Path) -> None:
    database = tmp_path / "not-started.sqlite"
    data_dir = tmp_path / "not-started-data"
    settings = Settings(database_url=f"sqlite:///{database}", data_dir=data_dir, _env_file=None)
    upgrade_to_head(settings)
    app = create_app(settings)
    raw = (FIXTURES / "ca_2026_simple.csv").read_bytes()

    with TestClient(app) as client:
        preview_response = client.post(
            "/api/v1/imports/preview",
            files={"file": ("synthetic.csv", raw, "text/csv")},
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        mapping = client.patch(
            f"/api/v1/imports/{preview['preview_id']}/sections/1/account-mapping",
            json={
                "expected_version": preview["version"],
                "new_account": {
                    "alias": "Compte couverture synthétique",
                    "account_role": "current",
                },
            },
        )
        assert mapping.status_code == 200, mapping.text
        committed = client.post(
            f"/api/v1/imports/{preview['preview_id']}/commit",
            headers={"Idempotency-Key": str(uuid4())},
            json={"expected_version": mapping.json()["version"]},
        )
        assert committed.status_code == 200, committed.text

        accounts = client.get("/api/v1/accounts").json()["items"]
        account_id = accounts[0]["account_id"]
        result = client.get(
            f"/api/v1/accounts/{account_id}/coverage",
            params={"period_start": "2025-06-01", "period_end": "2025-06-30"},
        )
        assert result.status_code == 200, result.text
        payload = result.json()
        assert payload["period"]["completeness"] == "not_started"
        assert payload["coverage"]["status"] == "not_started"
        assert payload["coverage"]["missing_days"] == []


@pytest.mark.integration
def test_M_03_balance_checks_neutralize_unsafe_cents_in_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "unsafe-balance.sqlite"
    data_dir = tmp_path / "unsafe-balance-data"
    settings = Settings(database_url=f"sqlite:///{database}", data_dir=data_dir, _env_file=None)
    upgrade_to_head(settings)
    app = create_app(settings)
    with write_transaction(app.state.db_engine) as session:
        account = Account(alias="Compte solde synthétique", account_role="current")
        session.add(account)
        session.flush()
        account_id = account.account_id

    sections = [
        {
            "account_id": account_id,
            "status": "committed",
            "section_id": "synthetic-section-start",
            "import_file_id": "synthetic-import-start",
            "section_index": 1,
            "coverage_from": date(2026, 1, 1),
            "coverage_through": date(2026, 1, 31),
            "complete_through": date(2026, 1, 31),
            "balance_as_of": date(2026, 1, 1),
            "header_balance_cents": 0,
        },
        {
            "account_id": account_id,
            "status": "committed",
            "section_id": "synthetic-section-end",
            "import_file_id": "synthetic-import-end",
            "section_index": 1,
            "coverage_from": date(2026, 2, 1),
            "coverage_through": date(2026, 2, 28),
            "complete_through": date(2026, 2, 28),
            "balance_as_of": date(2026, 2, 28),
            "header_balance_cents": 0,
        },
    ]
    transactions = [
        {"account_id": account_id, "booked_on": date(2026, 1, 2), "amount_cents": -(10**13)}
        for _ in range(1_000)
    ]
    monkeypatch.setattr(
        accounts_api, "load_quality_snapshot", lambda *_args: (sections, transactions)
    )

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/accounts/{account_id}/balance-checks",
            params={"period_start": "2026-01-01", "period_end": "2026-02-28"},
        )
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["recalculated_balance_cents"] is None
    assert item["delta_cents"] is None
    assert item["total_out_of_range"] is True
