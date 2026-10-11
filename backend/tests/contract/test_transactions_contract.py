from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.mark.contract
def test_API_TXN_01_transaction_routes_and_error_envelope(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'transactions-contract.sqlite'}",
        enable_api_docs=True,
        _env_file=None,
    )
    with TestClient(create_app(settings)) as client:
        schema = client.get("/openapi.json").json()
        assert "/api/v1/transactions" in schema["paths"]
        assert "/api/v1/transactions/{transaction_id}" in schema["paths"]
        invalid = client.get("/api/v1/transactions?limit=101")
        assert invalid.status_code == 422
        assert set(invalid.json()) == {"error"}
        assert set(invalid.json()["error"]) == {"code", "message", "details"}
