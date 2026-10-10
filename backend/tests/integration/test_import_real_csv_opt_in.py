from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.main import create_app

pytestmark = [pytest.mark.integration, pytest.mark.opt_in_real_data]


def test_CSV_06_import_commit_real_csv_aggregates_only(tmp_path: Path) -> None:
    configured = os.environ.get("COMPTES_REAL_CSV")
    if not configured:
        pytest.skip("COMPTES_REAL_CSV absent: commit réel opt-in ignoré.")

    path = Path(configured)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'real-commit-opt-in.sqlite'}",
        data_dir=tmp_path / "private-data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    profile_script = Path(__file__).resolve().parents[3] / "scripts" / "real_csv_profile.py"
    profile_result = subprocess.run(
        [sys.executable, "-I", str(profile_script), str(path), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert profile_result.returncode == 0, "aggregate profile failed"
    profile = json.loads(profile_result.stdout)
    assert profile["sections"][0]["operation_count"] == 1024

    with TestClient(create_app(settings)) as client:
        preview_response = client.post(
            "/api/v1/imports/preview",
            files={"file": ("local-upload.csv", path.read_bytes(), "text/csv")},
        )
        assert preview_response.status_code == 200
        preview = preview_response.json()
        mapping = client.patch(
            f"/api/v1/imports/{preview['preview_id']}/sections/1/account-mapping",
            json={
                "expected_version": preview["version"],
                "new_account": {"alias": "Compte réel opt-in", "account_role": "current"},
            },
        )
        assert mapping.status_code == 200
        commit = client.post(
            f"/api/v1/imports/{preview['preview_id']}/commit",
            headers={"Idempotency-Key": str(uuid4())},
            json={"expected_version": mapping.json()["version"]},
        )
        assert commit.status_code == 200, "commit réel opt-in failed"
        assert commit.json()["created_transaction_count"] == 1024
        assert commit.json()["new_source_row_count"] == 1024

        repeated_preview = client.post(
            "/api/v1/imports/preview",
            files={"file": ("repeat.csv", path.read_bytes(), "text/csv")},
        ).json()
        repeated = client.post(
            f"/api/v1/imports/{repeated_preview['preview_id']}/commit",
            headers={"Idempotency-Key": str(uuid4())},
            json={"expected_version": repeated_preview["version"]},
        )
        assert repeated.status_code == 200
        assert repeated.json()["status"] == "no_op"
        assert repeated.json()["created_transaction_count"] == 0

    with sqlite3.connect(tmp_path / "real-commit-opt-in.sqlite") as connection:
        aggregates = connection.execute(
            """SELECT count(*), sum(amount_cents), count(DISTINCT identity_key_sha256)
               FROM transactions"""
        ).fetchone()
        assert aggregates[0] == 1024
        assert aggregates[1] == profile["consistency"]["net_flow_cents"]
        assert connection.execute("SELECT count(*) FROM source_rows").fetchone()[0] == 1024
        duplicate_groups = connection.execute(
            """SELECT count(*) FROM (
                   SELECT account_id, booked_on, amount_cents, identity_label
                   FROM transactions GROUP BY account_id, booked_on, amount_cents, identity_label
                   HAVING count(*) > 1
               )"""
        ).fetchone()[0]
        max_multiplicity = connection.execute(
            """SELECT max(multiplicity) FROM (
                   SELECT count(*) AS multiplicity FROM transactions
                   GROUP BY account_id, booked_on, amount_cents, identity_label
               )"""
        ).fetchone()[0]
        assert duplicate_groups == profile["duplicate_groups"] == 2
        assert max_multiplicity == profile["duplicate_max_multiplicity"] == 4
