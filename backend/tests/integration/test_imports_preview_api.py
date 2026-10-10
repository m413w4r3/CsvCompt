from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.db.models import Account
from app.db.session import write_transaction
from app.importers.account_identity import bank_fingerprint
from app.importers.credit_agricole import parse_export
from app.main import create_app
from app.services.app_secrets import ensure_hmac_key

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _ready_client(
    tmp_path: Path, *, max_upload_bytes: int = 20 * 1024 * 1024
) -> tuple[TestClient, Path]:
    database = tmp_path / "ready.sqlite"
    settings = Settings(
        database_url=f"sqlite:///{database}",
        data_dir=tmp_path / "data",
        max_upload_bytes=max_upload_bytes,
        _env_file=None,
    )
    upgrade_to_head(settings)
    return TestClient(create_app(settings)), database


def _upload(client: TestClient, data: bytes, content_type: str = "text/csv"):
    return client.post(
        "/api/v1/imports/preview",
        files={"file": ("never-stored-name.csv", data, content_type)},
    )


def _table_counts(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            if row[0] != "alembic_version"
        ]
        return {
            table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }


@pytest.mark.integration
def test_CSV_05_preview_multiple_and_cancel_leave_business_tables_unchanged(tmp_path: Path) -> None:
    client, database = _ready_client(tmp_path)
    source = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    before = _table_counts(database)
    first = _upload(client, source).json()
    second = _upload(client, source + b"\n").json()
    client.delete(f"/api/v1/imports/preview/{first['preview_id']}").raise_for_status()
    after = _table_counts(database)
    assert {key: after[key] for key in before if key != "app_settings"} == {
        key: before[key] for key in before if key != "app_settings"
    }
    assert after["app_settings"] == before["app_settings"] + 1
    assert client.get(f"/api/v1/imports/preview/{second['preview_id']}").status_code == 200
    assert client.get(f"/api/v1/imports/preview/{first['preview_id']}").status_code == 410


@pytest.mark.integration
def test_preview_fixture_totals_and_safe_response(tmp_path: Path) -> None:
    client, _database = _ready_client(tmp_path)
    source = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    response = _upload(client, source)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "previewed"
    assert result["reconciliation"] is None
    assert result["sections"][0]["operation_count"] == result["operation_count"]
    assert result["sections"][0]["net_cents"] == (
        result["sections"][0]["debit_total_cents"] + result["sections"][0]["credit_total_cents"]
    )
    assert result["sections"][0]["account_mapping"]["status"] == "unknown"
    encoded = response.text
    assert b"Compte" not in response.content
    assert b"0001" not in response.content
    assert "raw_label" not in encoded


@pytest.mark.integration
def test_IMP_11_provisional_day_is_reported_without_blocking(tmp_path: Path) -> None:
    client, _database = _ready_client(tmp_path)
    result = _upload(client, (FIXTURES / "provisional_day_A.csv").read_bytes()).json()
    section = result["sections"][0]
    assert section["provisional_days"] == ["2026-10-09"]
    assert section["complete_through"] == "2026-10-08"


@pytest.mark.integration
def test_IMP_12_out_of_period_resolution_and_IMP_13_duplicate_section_blocking(
    tmp_path: Path,
) -> None:
    client, _database = _ready_client(tmp_path)
    engine = client.app.state.db_engine
    out_of_period = (FIXTURES / "adversarial/out_of_period_line.csv").read_bytes()
    parsed = parse_export(out_of_period)
    fingerprint = bank_fingerprint(
        ensure_hmac_key(engine), parsed.sections[0].account_number_canonical
    )
    with write_transaction(engine) as session:
        session.add(
            Account(
                alias="Compte synthétique connu",
                account_role="current",
                bank_fingerprint=fingerprint,
                fingerprint_key_version=1,
            )
        )
    result = _upload(client, out_of_period).json()
    assert not result["can_commit"]
    assert any(item["code"] == "OUT_OF_PERIOD" for item in result["rejections"])
    response = client.patch(
        f"/api/v1/imports/{result['preview_id']}/rows/1/multiplicity",
        json={"expected_version": 1, "action": "create_occurrence"},
    )
    assert response.status_code == 200
    assert response.json()["can_commit"] is True
    duplicate = _upload(
        client, (FIXTURES / "adversarial/same_account_two_sections.csv").read_bytes()
    ).json()
    assert not duplicate["can_commit"]
    assert any(item["code"] == "DUPLICATE_ACCOUNT_SECTION" for item in duplicate["rejections"])


@pytest.mark.integration
def test_all_adversarial_synthetic_fixtures_return_preview_without_500(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client, _database = _ready_client(tmp_path)
    manifest = json.loads((FIXTURES / "manifest.yaml").read_text(encoding="utf-8"))["fixtures"]
    for fixture in sorted((FIXTURES / "adversarial").glob("*.csv")):
        response = _upload(client, fixture.read_bytes())
        assert response.status_code == 200, fixture.name
        payload = response.json()
        assert "can_commit" in payload
        assert payload["can_commit"] is False, fixture.name
        expected_codes = {
            anomaly["code"] for anomaly in manifest[f"adversarial/{fixture.name}"]["anomalies"]
        }
        actual_codes = {
            item.get("detail_code") or item["code"]
            for item in (*payload["warnings"], *payload["rejections"])
        }
        assert expected_codes <= actual_codes, fixture.name
        assert all("raw_label" not in rejection for rejection in payload["rejections"])
        parser_result = parse_export(fixture.read_bytes())
        for parsed_section in parser_result.sections:
            if parsed_section.account_number_canonical is not None:
                number = parsed_section.account_number_canonical.value
                assert number not in response.text
                assert number not in caplog.text
        for parsed_row in parser_result.rows:
            assert parsed_row.raw_label not in response.text
            assert parsed_row.raw_label not in caplog.text


@pytest.mark.integration
def test_ambiguous_duplicate_row_index_requires_section_over_http(tmp_path: Path) -> None:
    client, _database = _ready_client(tmp_path)
    payload = {
        "status": "previewed",
        "encoding": "utf-8",
        "file_sha256": "a" * 64,
        "generated_on": "2026-03-01",
        "generation_date_source": "header_download",
        "operation_count": 2,
        "sections": [
            {
                "section_index": index,
                "account_mapping": {
                    "status": "known",
                    "account_id": f"synthetic-account-{index}",
                    "alias": f"Compte synthétique {index}",
                },
                "account_resolution": "known",
                "account_id": f"synthetic-account-{index}",
                "account_alias": f"Compte synthétique {index}",
                "period_from": "2026-03-01",
                "period_to": "2026-03-31",
                "coverage_from": "2026-03-01",
                "coverage_through": "2026-03-31",
                "complete_through": "2026-02-28",
                "operation_count": 1,
                "debit_total_cents": -100,
                "credit_total_cents": 0,
                "net_cents": -100,
                "balance_as_of": None,
                "header_balance_cents": None,
                "min_date": "2026-03-15",
                "max_date": "2026-03-15",
                "provisional_days": [],
                "anomaly_counts": {"OUT_OF_PERIOD": 1},
            }
            for index in (1, 2)
        ],
        "warnings": [],
        "rejections": [
            {
                "code": "OUT_OF_PERIOD",
                "severity": "blocking",
                "section": index,
                "logical_row_idx": 1,
                "field": "booked_on",
                "message": "La date de l’opération est hors période.",
            }
            for index in (1, 2)
        ],
        "can_commit": False,
        "already_imported": False,
        "import_file_id": None,
        "reconciliation": None,
        "_resolutions": {"account_mapping": {}, "coverage": {}, "multiplicity": {}},
        "_out_of_period_rows": [{"section": 1, "row": 1}, {"section": 2, "row": 1}],
        "_period_missing_sections": [],
        "_structural_rejection": False,
    }
    saved = client.app.state.import_preview_store.create(payload, b"synthetic")
    route = f"/api/v1/imports/{saved['preview_id']}/rows/1/multiplicity"
    ambiguous = client.patch(route, json={"expected_version": 1, "action": "create_occurrence"})
    assert ambiguous.status_code == 409
    assert ambiguous.json()["error"]["code"] == "MULTIPLICITY_AMBIGUOUS"
    first = client.patch(
        route,
        json={"expected_version": 1, "action": "create_occurrence", "section_index": 1},
    )
    assert first.status_code == 200
    assert first.json()["version"] == 2
    assert len(first.json()["rejections"]) == 1
    assert first.json()["can_commit"] is False
    second = client.patch(
        route,
        json={"expected_version": 2, "action": "link_existing", "section_index": 2},
    )
    assert second.status_code == 200
    assert second.json()["version"] == 3
    assert second.json()["rejections"] == []
    assert second.json()["can_commit"] is True


@pytest.mark.integration
def test_non_adversarial_fixtures_match_manifest_aggregates(tmp_path: Path) -> None:
    client, _database = _ready_client(tmp_path)
    manifest = json.loads((FIXTURES / "manifest.yaml").read_text(encoding="utf-8"))["fixtures"]
    for relative_path, oracle in manifest.items():
        if relative_path.startswith("adversarial/"):
            continue
        response = _upload(client, (FIXTURES / relative_path).read_bytes())
        assert response.status_code == 200, relative_path
        result = response.json()
        assert len(result["sections"]) == len(oracle["sections"]), relative_path
        assert result["operation_count"] == sum(
            expected["operation_count"] for expected in oracle["sections"]
        ), relative_path
        for actual, expected in zip(result["sections"], oracle["sections"], strict=True):
            assert actual["operation_count"] == expected["operation_count"], relative_path
            assert actual["debit_total_cents"] == -expected["debit_cents"], relative_path
            assert actual["credit_total_cents"] == expected["credit_cents"], relative_path
            assert actual["min_date"] == expected["min_date"], relative_path
            assert actual["max_date"] == expected["max_date"], relative_path
            assert actual["header_balance_cents"] == expected["header_balance_cents"], relative_path
        assert result["can_commit"] is False if result["sections"] else True


@pytest.mark.integration
def test_known_hmac_mapping_and_versioned_new_mapping_then_coverage(tmp_path: Path) -> None:
    client, database = _ready_client(tmp_path)
    engine = client.app.state.db_engine
    source = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    parsed = parse_export(source)
    key = ensure_hmac_key(engine)
    fingerprint = bank_fingerprint(key, parsed.sections[0].account_number_canonical)
    with write_transaction(engine) as session:
        session.add(
            Account(
                alias="Compte synthétique connu",
                account_role="current",
                bank_fingerprint=fingerprint,
                fingerprint_key_version=1,
            )
        )
    known = _upload(client, source).json()
    assert known["sections"][0]["account_mapping"]["status"] == "known"
    assert known["can_commit"] is True

    no_period = _upload(
        client,
        (FIXTURES / "adversarial/section_without_period.csv").read_bytes(),
    ).json()
    mapped = client.patch(
        f"/api/v1/imports/{no_period['preview_id']}/sections/1/account-mapping",
        json={
            "expected_version": 1,
            "new_account": {"alias": "Compte projeté", "account_role": "current"},
        },
    )
    assert mapped.status_code == 200
    assert mapped.json()["version"] == 2
    covered = client.patch(
        f"/api/v1/imports/{no_period['preview_id']}/sections/1/coverage",
        json={
            "expected_version": 2,
            "coverage_from": "2026-03-01",
            "coverage_through": "2026-03-31",
        },
    )
    assert covered.status_code == 200
    assert covered.json()["version"] == 3
    assert covered.json()["can_commit"] is True


@pytest.mark.integration
def test_ensure_hmac_key_is_idempotent_under_thread_concurrency(tmp_path: Path) -> None:
    _client, database = _ready_client(tmp_path)
    from app.db.session import create_sqlite_engine

    engine = create_sqlite_engine(f"sqlite:///{database}")
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            keys = list(executor.map(lambda _index: ensure_hmac_key(engine), range(2)))
        assert keys[0] == keys[1]
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT count(*), min(is_secret) FROM app_settings WHERE setting_key=?",
                ("import.account_hmac_key.v1",),
            ).fetchone()
        assert row == (1, 1)
    finally:
        engine.dispose()


@pytest.mark.integration
def test_preview_version_conflict_size_media_and_database_readiness(tmp_path: Path) -> None:
    missing_settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'missing.sqlite'}",
        data_dir=tmp_path / "missing-data",
        _env_file=None,
    )
    with TestClient(create_app(missing_settings)) as missing_client:
        response = _upload(missing_client, b"synthetic")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DATABASE_NOT_READY"

    client, _database = _ready_client(tmp_path / "ready", max_upload_bytes=20_000)
    assert _upload(client, b"x" * 20_001).json()["error"]["code"] == "FILE_TOO_LARGE"
    assert (
        _upload(client, b"text", "application/pdf").json()["error"]["code"]
        == "UNSUPPORTED_MEDIA_TYPE"
    )
    result = _upload(client, (FIXTURES / "ca_2026_simple.csv").read_bytes()).json()
    response = client.patch(
        f"/api/v1/imports/{result['preview_id']}/sections/1/account-mapping",
        json={
            "expected_version": 99,
            "new_account": {"alias": "Courant fictif", "account_role": "current"},
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"
