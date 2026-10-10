from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.main import create_app


@pytest.mark.integration
@pytest.mark.opt_in_real_data
def test_opt_in_real_csv_preview_aggregates_only(tmp_path: Path) -> None:
    csv_path = os.environ.get("COMPTES_REAL_CSV")
    if not csv_path:
        pytest.skip("COMPTES_REAL_CSV absent: test réel opt-in ignoré.")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'real-opt-in.sqlite'}",
        data_dir=tmp_path / "private-data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/imports/preview",
            files={"file": ("redacted.csv", Path(csv_path).read_bytes(), "text/csv")},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["operation_count"] == 1024
    assert payload["sections"][0]["min_date"] == "2026-01-01"
    assert payload["sections"][0]["max_date"] == "2026-10-09"
    assert payload["sections"][0]["account_mapping"]["status"] == "unknown"
    assert payload["can_commit"] is False
