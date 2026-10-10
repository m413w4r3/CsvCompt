from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.main import create_app


@pytest.mark.contract
def test_API_import_preview_example_fields_and_error_envelope(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'contract.sqlite'}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    fixture = Path(__file__).parents[1] / "fixtures" / "ca_2026_simple.csv"
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/imports/preview",
            files={"file": ("example.csv", fixture.read_bytes(), "text/csv")},
        )
        assert response.status_code == 200
        payload = response.json()
        assert {
            "preview_id",
            "version",
            "status",
            "encoding",
            "file_sha256",
            "generated_on",
            "generation_date_source",
            "sections",
            "warnings",
            "rejections",
            "can_commit",
            "expires_at_utc",
            "reconciliation",
        } <= payload.keys()
        assert payload["status"] == "previewed"
        assert payload["reconciliation"] is None
        assert client.get("/api/v1/imports/preview/not-a-uuid").status_code == 422
