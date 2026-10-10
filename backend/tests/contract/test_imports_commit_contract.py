from __future__ import annotations

from pathlib import Path

import pytest

from app.api.v1.imports import CommitResponse
from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.main import create_app


@pytest.mark.contract
def test_import_commit_and_read_routes_have_typed_response_contracts(tmp_path: Path) -> None:
    database = tmp_path / "contract.sqlite"
    settings = Settings(
        database_url=f"sqlite:///{database}", data_dir=tmp_path / "contract-data", _env_file=None
    )
    upgrade_to_head(settings)
    app = create_app(settings)
    schema = app.openapi()

    assert "CommitResponse" in schema["components"]["schemas"]
    assert "ImportSummaryPage" in schema["components"]["schemas"]
    assert "ImportDetail" in schema["components"]["schemas"]
    assert "RejectionPage" in schema["components"]["schemas"]
    commit_operation = schema["paths"]["/api/v1/imports/{preview_id}/commit"]["post"]
    assert any(
        parameter["name"] == "Idempotency-Key" and parameter["required"]
        for parameter in commit_operation["parameters"]
    )
    assert "requestBody" in commit_operation
    assert "response_model" not in commit_operation

    with pytest.raises(ValueError):
        CommitResponse.model_validate(
            {
                "import_file_id": "not-a-uuid",
                "status": "partial",
                "no_op": False,
                "duplicate_of_import_file_id": None,
                "file_sha256": "0" * 64,
                "created_transaction_count": 0,
                "new_source_row_count": 0,
                "linked_source_row_count": 0,
                "archive_retained": False,
                "generation_date_source": "header_download",
                "sections": [],
            }
        )

    assert "/api/v1/imports/{import_file_id}" in schema["paths"]
    assert "/api/v1/imports/{import_file_id}/rejections" in schema["paths"]
    assert "delete" not in schema["paths"]["/api/v1/imports/{import_file_id}"]
