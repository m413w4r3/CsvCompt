from __future__ import annotations

import calendar
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.main import create_app

pytestmark = [pytest.mark.integration, pytest.mark.opt_in_real_data]


def test_real_2026_csv_quality_is_aggregated_only(tmp_path: Path) -> None:
    configured_path = os.environ.get("COMPTES_REAL_CSV")
    if not configured_path:
        pytest.skip("COMPTES_REAL_CSV absent : contrôle qualité réel opt-in ignoré.")

    csv_path = Path(configured_path)
    assert csv_path.is_file()

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'quality-real-opt-in.sqlite'}",
        data_dir=tmp_path / "private-data",
        _env_file=None,
    )
    upgrade_to_head(settings)

    with TestClient(create_app(settings)) as client:
        preview_response = client.post(
            "/api/v1/imports/preview",
            files={"file": ("quality-opt-in.csv", csv_path.read_bytes(), "text/csv")},
        )
        assert preview_response.status_code == 200
        preview = preview_response.json()
        assert len(preview["sections"]) == 1

        mapping_response = client.patch(
            f"/api/v1/imports/{preview['preview_id']}/sections/1/account-mapping",
            json={
                "expected_version": preview["version"],
                "new_account": {
                    "alias": "Compte contrôle qualité opt-in",
                    "account_role": "current",
                },
            },
        )
        assert mapping_response.status_code == 200
        mapping = mapping_response.json()

        commit_response = client.post(
            f"/api/v1/imports/{preview['preview_id']}/commit",
            headers={"Idempotency-Key": str(uuid4())},
            json={"expected_version": mapping["version"]},
        )
        assert commit_response.status_code == 200
        committed = commit_response.json()
        assert len(committed["sections"]) == 1
        account_id = committed["sections"][0]["account_id"]

        annual_coverage_response = client.get(
            f"/api/v1/accounts/{account_id}/coverage",
            params={"period_start": "2026-01-01", "period_end": "2026-10-09"},
        )
        assert annual_coverage_response.status_code == 200
        annual_coverage = annual_coverage_response.json()["coverage"]
        assert annual_coverage["status"] == "partial"
        assert annual_coverage["covered_from"] == "2026-01-01"
        assert annual_coverage["covered_through"] == "2026-10-08"
        assert len(annual_coverage["sections"]) == 1
        assert annual_coverage["sections"][0]["covered_from"] == "2026-01-01"
        assert annual_coverage["sections"][0]["covered_through"] == "2026-10-09"
        assert annual_coverage["sections"][0]["generated_on"] == "2026-10-09"
        assert annual_coverage["sections"][0]["complete_through"] == "2026-10-08"
        assert annual_coverage["provisional_days"] == ["2026-10-09"]

        monthly_completeness: list[str] = []
        for month in range(1, 10):
            month_start = date(2026, month, 1)
            month_end = date(2026, month, calendar.monthrange(2026, month)[1])
            response = client.get(
                f"/api/v1/accounts/{account_id}/coverage",
                params={
                    "period_start": month_start.isoformat(),
                    "period_end": month_end.isoformat(),
                },
            )
            assert response.status_code == 200
            monthly_completeness.append(response.json()["period"]["completeness"])
        assert monthly_completeness == ["complete"] * 9

        october_response = client.get(
            f"/api/v1/accounts/{account_id}/coverage",
            params={"period_start": "2026-10-01", "period_end": "2026-10-31"},
        )
        assert october_response.status_code == 200
        assert october_response.json()["period"]["completeness"] == "partial"

        before_start_response = client.get(
            f"/api/v1/accounts/{account_id}/coverage",
            params={"period_start": "2025-04-01", "period_end": "2025-12-31"},
        )
        assert before_start_response.status_code == 200
        before_start = before_start_response.json()["coverage"]
        assert before_start["status"] == "not_started"
        assert before_start["missing_days"] == []

        balance_response = client.get(
            f"/api/v1/accounts/{account_id}/balance-checks",
            params={"period_start": "2026-01-01", "period_end": "2026-10-09"},
        )
        assert balance_response.status_code == 200
        checks = balance_response.json()["items"]
        assert len(checks) == 1
        assert checks[0]["status"] == "not_comparable"
        assert checks[0]["reason"] == "balance_after_complete_through"
