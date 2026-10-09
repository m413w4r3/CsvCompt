from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from app.importers.credit_agricole import parse_export

pytestmark = [pytest.mark.integration, pytest.mark.opt_in_real_data]


def test_CSV_06_real_2026_aggregate_only() -> None:
    configured = os.environ.get("COMPTES_REAL_CSV")
    if not configured:
        pytest.skip("COMPTES_REAL_CSV non défini ; test réel opt-in ignoré")

    path = Path(configured)
    result = parse_export(path.read_bytes())
    assert len(result.sections) == 1
    assert result.operation_count == 1024
    assert min(row.booked_on for row in result.rows) == date(2026, 1, 1)
    assert max(row.booked_on for row in result.rows) == date(2026, 10, 9)
    assert not result.has_blocking_anomalies

    profile_script = Path(__file__).resolve().parents[3] / "scripts" / "real_csv_profile.py"
    process = subprocess.run(
        [sys.executable, "-I", str(profile_script), configured, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, "aggregate profile failed"
    profile = json.loads(process.stdout)
    parser_net = sum(row.amount_cents for row in result.rows)
    assert parser_net == profile["consistency"]["net_flow_cents"]
    assert process.stderr == ""
    assert all(len(row.raw_label) < 8 or row.raw_label not in process.stdout for row in result.rows)
    assert all(
        section.account_number_canonical is None
        or len(section.account_number_canonical.value) < 4
        or section.account_number_canonical.value not in process.stdout
        for section in result.sections
    )
