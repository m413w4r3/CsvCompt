from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.main import create_app


def test_accounts_quality_routes_have_typed_read_only_contracts(tmp_path: Path) -> None:
    database = tmp_path / "accounts-contract.sqlite"
    settings = Settings(
        database_url=f"sqlite:///{database}",
        data_dir=tmp_path / "accounts-contract-data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    application = create_app(settings)

    with TestClient(application) as client:
        accounts = client.get("/api/v1/accounts")
        assert accounts.status_code == 200
        assert accounts.json() == {"items": []}

        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/accounts" in paths
        assert "/api/v1/accounts/{account_id}/coverage" in paths
        assert "/api/v1/accounts/{account_id}/balance-checks" in paths

        missing_period = client.get(f"/api/v1/accounts/{uuid4()}/coverage")
        assert missing_period.status_code == 422
        assert set(missing_period.json()) == {"error"}
        assert set(missing_period.json()["error"]) == {"code", "message", "details"}

        missing_balance_period = client.get(f"/api/v1/accounts/{uuid4()}/balance-checks")
        assert missing_balance_period.status_code == 422
        assert missing_balance_period.json()["error"]["code"] == "VALIDATION_ERROR"

        invalid_account = client.get(
            "/api/v1/accounts/not-a-uuid/coverage",
            params={"period_start": "2026-01-01", "period_end": "2026-01-31"},
        )
        assert invalid_account.status_code == 422
        assert invalid_account.json()["error"]["code"] == "VALIDATION_ERROR"
