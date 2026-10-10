from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.db.models import Account, Transaction
from app.db.session import write_transaction
from app.importers.account_identity import bank_fingerprint
from app.importers.credit_agricole import parse_export
from app.importers.normalize import normalize_label
from app.main import create_app
from app.services.app_secrets import ensure_hmac_key

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _ready_client(
    tmp_path: Path,
    *,
    max_upload_bytes: int = 20 * 1024 * 1024,
    max_preview_anomalies: int = 1000,
) -> tuple[TestClient, Path]:
    database = tmp_path / "ready.sqlite"
    settings = Settings(
        database_url=f"sqlite:///{database}",
        data_dir=tmp_path / "data",
        max_upload_bytes=max_upload_bytes,
        max_preview_anomalies=max_preview_anomalies,
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


def _table_contents(database: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    with sqlite3.connect(database) as connection:
        tables = sorted(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        snapshot: dict[str, tuple[tuple[object, ...], ...]] = {}
        for table in tables:
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            order = ", ".join(f'"{column}"' for column in columns)
            rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}').fetchall()
            snapshot[table] = tuple(tuple(value for value in row) for row in rows)
        return snapshot


def _two_section_preview(client: TestClient) -> dict[str, Any]:
    def section(index: int) -> dict[str, Any]:
        return {
            "section_index": index,
            "account_mapping": {"status": "unknown", "account_id": None, "alias": None},
            "account_resolution": "unknown",
            "account_id": None,
            "account_alias": None,
            "period_from": "2026-01-01",
            "period_to": "2026-01-31",
            "coverage_from": "2026-01-01",
            "coverage_through": "2026-01-31",
            "complete_through": "2026-01-31",
            "operation_count": 0,
            "debit_total_cents": 0,
            "credit_total_cents": 0,
            "net_cents": 0,
            "balance_as_of": None,
            "header_balance_cents": None,
            "min_date": None,
            "max_date": None,
            "provisional_days": [],
            "anomaly_counts": {},
        }

    return client.app.state.import_preview_store.create(
        {
            "status": "previewed",
            "encoding": "utf-8",
            "file_sha256": "a" * 64,
            "generated_on": "2026-01-31",
            "generation_date_source": "header_download",
            "operation_count": 0,
            "sections": [section(1), section(2)],
            "warnings": [],
            "rejections": [],
            "anomaly_counts": {},
            "anomaly_summary": {
                "warning_count": 0,
                "rejection_count": 0,
                "stored_warning_count": 0,
                "stored_rejection_count": 0,
                "omitted_warning_count": 0,
                "omitted_rejection_count": 0,
            },
            "already_imported": False,
            "import_file_id": None,
            "can_commit": False,
            "reconciliation": None,
            "_canonical_sections": [1, 2],
            "_initial_known_sections": [],
            "_fingerprints": {1: None, 2: None},
            "_resolutions": {"account_mapping": {}, "coverage": {}, "multiplicity": {}},
            "_period_missing_sections": [],
            "_out_of_period_rows": [],
            "_structural_rejection": False,
            "_unresolvable_blocking_count": 0,
            "_out_of_period_omitted": 0,
        },
        b"synthetic two-section preview",
    )


@pytest.mark.integration
def test_CSV_05_preview_multiple_and_cancel_leave_business_tables_unchanged(tmp_path: Path) -> None:
    client, database = _ready_client(tmp_path)
    ensure_hmac_key(client.app.state.db_engine)
    with write_transaction(client.app.state.db_engine) as session:
        session.add(Account(alias="Préexistant synthétique", account_role="current"))
    before = _table_contents(database)
    source = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    first = _upload(client, source).json()
    second = _upload(client, source + b"\n").json()
    patch_response = client.patch(
        f"/api/v1/imports/{second['preview_id']}/sections/1/account-mapping",
        json={
            "expected_version": second["version"],
            "new_account": {"alias": "Aperçu synthétique", "account_role": "current"},
        },
    )
    assert patch_response.status_code == 200
    client.delete(f"/api/v1/imports/preview/{first['preview_id']}").raise_for_status()
    client.delete(f"/api/v1/imports/preview/{second['preview_id']}").raise_for_status()
    after = _table_contents(database)
    assert after == before
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
    assert any(item["code"] == "ACCOUNT_MAPPING_REQUIRED" for item in result["rejections"])
    encoded = response.text
    assert b"Compte" not in response.content
    assert b"0001" not in response.content
    assert "raw_label" not in encoded
    resolved = client.patch(
        f"/api/v1/imports/{result['preview_id']}/sections/1/account-mapping",
        json={
            "expected_version": result["version"],
            "new_account": {"alias": "Compte aperçu", "account_role": "current"},
        },
    )
    assert resolved.status_code == 200
    assert not any(
        item["code"] == "ACCOUNT_MAPPING_REQUIRED" for item in resolved.json()["rejections"]
    )
    assert resolved.json()["can_commit"] is True


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
def test_preview_caps_stored_anomaly_details_and_keeps_aggregate_counts(tmp_path: Path) -> None:
    client, _database = _ready_client(tmp_path, max_preview_anomalies=1)
    result = _upload(
        client, (FIXTURES / "adversarial/interleaved_blank_lines.csv").read_bytes()
    ).json()
    assert len(result["warnings"]) == 1
    assert result["anomaly_counts"]["INTERLEAVED_BLANK_LINES"] == 2
    assert result["anomaly_summary"]["warning_count"] == 2
    assert result["anomaly_summary"]["omitted_warning_count"] == 1
    assert result["can_commit"] is False


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
        "anomaly_counts": {"OUT_OF_PERIOD": 2},
        "anomaly_summary": {
            "warning_count": 0,
            "rejection_count": 2,
            "stored_warning_count": 0,
            "stored_rejection_count": 2,
            "omitted_warning_count": 0,
            "omitted_rejection_count": 0,
        },
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
    ambiguous_link = client.patch(
        route,
        json={
            "expected_version": 1,
            "action": "link_existing",
            "transaction_id": "550e8400-e29b-41d4-a716-446655440000",
        },
    )
    assert ambiguous_link.status_code == 409
    assert ambiguous_link.json()["error"]["code"] == "MULTIPLICITY_AMBIGUOUS"
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
        json={"expected_version": 2, "action": "create_occurrence", "section_index": 2},
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

    coverage_root = tmp_path / "coverage"
    coverage_root.mkdir()
    coverage_client, _ = _ready_client(coverage_root)
    no_period = _upload(
        coverage_client,
        (FIXTURES / "adversarial/section_without_period.csv").read_bytes(),
    ).json()
    mapped = coverage_client.patch(
        f"/api/v1/imports/{no_period['preview_id']}/sections/1/account-mapping",
        json={
            "expected_version": 1,
            "new_account": {"alias": "Compte projeté", "account_role": "current"},
        },
    )
    assert mapped.status_code == 200
    assert mapped.json()["version"] == 2
    covered = coverage_client.patch(
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
    pre_spool = client.post(
        "/api/v1/imports/preview",
        content=b"synthetic body",
        headers={
            "Content-Type": "multipart/form-data; boundary=synthetic",
            "Content-Length": str(20_000 + 65_537),
        },
    )
    assert pre_spool.status_code == 413
    assert pre_spool.json()["error"]["code"] == "FILE_TOO_LARGE"
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


@pytest.mark.integration
def test_M_01_known_mapping_is_locked_and_fingerprint_conflicts_are_rejected(
    tmp_path: Path,
) -> None:
    client, _database = _ready_client(tmp_path)
    engine = client.app.state.db_engine
    source = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    parsed = parse_export(source)
    fingerprint = bank_fingerprint(
        ensure_hmac_key(engine), parsed.sections[0].account_number_canonical
    )
    with write_transaction(engine) as session:
        known_account = Account(
            alias="Compte connu synthétique",
            account_role="current",
            bank_fingerprint=fingerprint,
            fingerprint_key_version=1,
        )
        different_account = Account(
            alias="Compte différent synthétique",
            account_role="savings",
            bank_fingerprint="d" * 64,
            fingerprint_key_version=1,
        )
        session.add_all([known_account, different_account])
    preview = _upload(client, source).json()
    assert preview["sections"][0]["account_mapping"]["status"] == "known"
    for mapping in (
        {"account_id": known_account.account_id},
        {"account_id": different_account.account_id},
        {"new_account": {"alias": "Nouveau", "account_role": "current"}},
    ):
        response = client.patch(
            f"/api/v1/imports/{preview['preview_id']}/sections/1/account-mapping",
            json={"expected_version": 1, **mapping},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ACCOUNT_MAPPING_LOCKED"

    conflict_root = tmp_path / "conflicts"
    conflict_root.mkdir()
    conflict_client, _ = _ready_client(conflict_root)
    unresolved = _upload(conflict_client, source).json()
    conflict_engine = conflict_client.app.state.db_engine
    with write_transaction(conflict_engine) as session:
        different_account = Account(
            alias="Compte différent synthétique",
            account_role="savings",
            bank_fingerprint="d" * 64,
            fingerprint_key_version=1,
        )
        session.add(different_account)
    conflict = conflict_client.patch(
        f"/api/v1/imports/{unresolved['preview_id']}/sections/1/account-mapping",
        json={"expected_version": 1, "account_id": different_account.account_id},
    )
    assert conflict.status_code == 422
    assert conflict.json()["error"]["code"] == "ACCOUNT_FINGERPRINT_CONFLICT"
    expected_fingerprint = bank_fingerprint(
        ensure_hmac_key(conflict_engine), parsed.sections[0].account_number_canonical
    )
    with write_transaction(conflict_engine) as session:
        session.add(
            Account(
                alias="Compte lié après aperçu",
                account_role="current",
                bank_fingerprint=expected_fingerprint,
                fingerprint_key_version=1,
            )
        )
    linked_new = conflict_client.patch(
        f"/api/v1/imports/{unresolved['preview_id']}/sections/1/account-mapping",
        json={
            "expected_version": 1,
            "new_account": {"alias": "Nouveau compte", "account_role": "current"},
        },
    )
    assert linked_new.status_code == 409
    assert linked_new.json()["error"]["code"] == "ACCOUNT_FINGERPRINT_CONFLICT"


@pytest.mark.integration
def test_M_02_missing_canonical_account_number_is_not_resolvable(tmp_path: Path) -> None:
    client, _database = _ready_client(tmp_path)
    source = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    malformed_account = source.replace(
        b"Compte de D\xe9p\xf4t carte n\xb0 0001;", b"Compte de Depot;"
    )
    result = _upload(client, malformed_account).json()
    assert not result["can_commit"]
    assert any(item["code"] == "MISSING_ACCOUNT_NUMBER" for item in result["rejections"])
    response = client.patch(
        f"/api/v1/imports/{result['preview_id']}/sections/1/account-mapping",
        json={
            "expected_version": 1,
            "new_account": {"alias": "Compte synthétique", "account_role": "current"},
        },
    )
    assert response.status_code == 422
    assert not client.get(f"/api/v1/imports/preview/{result['preview_id']}").json()["can_commit"]


@pytest.mark.integration
def test_M_03_coverage_is_restricted_and_contains_all_rows(tmp_path: Path) -> None:
    client, _database = _ready_client(tmp_path)
    missing_period = _upload(
        client, (FIXTURES / "adversarial/section_without_period.csv").read_bytes()
    ).json()
    route = f"/api/v1/imports/{missing_period['preview_id']}/sections/1/coverage"
    invalid = client.patch(
        route,
        json={
            "expected_version": 1,
            "coverage_from": "2026-03-11",
            "coverage_through": "2026-03-31",
        },
    )
    assert invalid.status_code == 422
    accepted = client.patch(
        route,
        json={
            "expected_version": 1,
            "coverage_from": "2026-03-01",
            "coverage_through": "2026-03-31",
        },
    )
    assert accepted.status_code == 200
    declared_period = _upload(client, (FIXTURES / "ca_2026_simple.csv").read_bytes()).json()
    declared_before = (
        declared_period["sections"][0]["period_from"],
        declared_period["sections"][0]["period_to"],
    )
    forbidden = client.patch(
        f"/api/v1/imports/{declared_period['preview_id']}/sections/1/coverage",
        json={
            "expected_version": 1,
            "coverage_from": "2025-01-01",
            "coverage_through": "2026-12-31",
        },
    )
    assert forbidden.status_code == 422
    unchanged = client.get(f"/api/v1/imports/preview/{declared_period['preview_id']}").json()
    assert (
        unchanged["sections"][0]["period_from"],
        unchanged["sections"][0]["period_to"],
    ) == declared_before


@pytest.mark.integration
def test_M_05_link_existing_requires_strict_same_account_identity_and_is_audited(
    tmp_path: Path,
) -> None:
    client, _database = _ready_client(tmp_path)
    engine = client.app.state.db_engine
    raw = (FIXTURES / "adversarial/out_of_period_line.csv").read_bytes()
    parsed = parse_export(raw)
    section = parsed.sections[0]
    row = section.rows[0]
    fingerprint = bank_fingerprint(ensure_hmac_key(engine), section.account_number_canonical)
    identity_label = normalize_label(row.raw_label).identity_label.value
    with write_transaction(engine) as session:
        account = Account(
            alias="Compte liaison synthétique",
            account_role="current",
            bank_fingerprint=fingerprint,
            fingerprint_key_version=1,
        )
        session.add(account)
        session.flush()
        other_account = Account(alias="Autre compte synthétique", account_role="current")
        session.add(other_account)
        session.flush()
        transaction = Transaction(
            account_id=account.account_id,
            booked_on=row.booked_on,
            amount_cents=row.amount_cents,
            raw_label="Libellé synthétique",
            identity_label=identity_label,
            identity_key_sha256="a" * 64,
            occurrence_rank=1,
        )
        wrong_identity = Transaction(
            account_id=account.account_id,
            booked_on=row.booked_on,
            amount_cents=row.amount_cents + 1,
            raw_label="Autre libellé synthétique",
            identity_label=identity_label,
            identity_key_sha256="b" * 64,
            occurrence_rank=1,
        )
        wrong_account = Transaction(
            account_id=other_account.account_id,
            booked_on=row.booked_on,
            amount_cents=row.amount_cents,
            raw_label="Libellé synthétique distinct",
            identity_label=identity_label,
            identity_key_sha256="c" * 64,
            occurrence_rank=1,
        )
        session.add_all([transaction, wrong_identity, wrong_account])
    result = _upload(client, raw).json()
    endpoint = f"/api/v1/imports/{result['preview_id']}/rows/{row.logical_row_idx}/multiplicity"
    bad = client.patch(
        endpoint,
        json={
            "expected_version": 1,
            "action": "link_existing",
            "transaction_id": wrong_identity.transaction_id,
        },
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    wrong_account_response = client.patch(
        endpoint,
        json={
            "expected_version": 1,
            "action": "link_existing",
            "transaction_id": wrong_account.transaction_id,
        },
    )
    assert wrong_account_response.status_code == 422
    linked = client.patch(
        endpoint,
        json={
            "expected_version": 1,
            "action": "link_existing",
            "transaction_id": transaction.transaction_id,
        },
    )
    assert linked.status_code == 200
    assert linked.json()["can_commit"] is True
    stored = client.app.state.import_preview_store.get(result["preview_id"])
    decision = stored["_decision_history"][-1]
    assert decision["actor"] == "local_user"
    assert decision["at_utc"].endswith("Z")
    missing_target = client.patch(
        endpoint,
        json={"expected_version": 2, "action": "link_existing"},
    )
    assert missing_target.status_code == 422


@pytest.mark.integration
def test_alias_rules_and_single_file_field_are_enforced(tmp_path: Path) -> None:
    client, _database = _ready_client(tmp_path)
    source = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    preview = _upload(client, source).json()
    for alias in (
        "1234 5678 9012",
        "".join(("FR", "76 1234 5678 9012 3456 7890 123")),
        "<b>Compte</b>",
    ):
        response = client.patch(
            f"/api/v1/imports/{preview['preview_id']}/sections/1/account-mapping",
            json={
                "expected_version": 1,
                "new_account": {"alias": alias, "account_role": "current"},
            },
        )
        assert response.status_code == 422
    duplicate_files = client.post(
        "/api/v1/imports/preview",
        files=[
            ("file", ("one.csv", source, "text/csv")),
            ("file", ("two.csv", source, "text/csv")),
        ],
    )
    assert duplicate_files.status_code == 422


@pytest.mark.integration
def test_m_01_can_commit_recalculates_after_one_mapping_patch(tmp_path: Path) -> None:
    client, _database = _ready_client(tmp_path)
    preview = _two_section_preview(client)
    with write_transaction(client.app.state.db_engine) as session:
        account_a = Account(alias="Compte synthétique A", account_role="current")
        account_b = Account(alias="Compte synthétique B", account_role="savings")
        session.add_all([account_a, account_b])
        session.flush()
    url = f"/api/v1/imports/{preview['preview_id']}/sections"
    client.patch(
        f"{url}/1/account-mapping",
        json={"expected_version": 1, "account_id": account_a.account_id},
    )
    second = client.patch(
        f"{url}/2/account-mapping",
        json={"expected_version": 2, "account_id": account_a.account_id},
    )
    assert second.status_code == 200
    assert any(item["code"] == "DUPLICATE_ACCOUNT_SECTION" for item in second.json()["rejections"])
    resolved = client.patch(
        f"{url}/2/account-mapping",
        json={"expected_version": 3, "account_id": account_b.account_id},
    )
    assert resolved.status_code == 200
    assert resolved.json()["rejections"] == []
    assert resolved.json()["can_commit"] is True
    assert client.get(f"/api/v1/imports/preview/{preview['preview_id']}").json()["can_commit"]


@pytest.mark.integration
def test_m_13_alias_conflicts_with_existing_and_other_preview_sections(tmp_path: Path) -> None:
    client, _database = _ready_client(tmp_path)
    with write_transaction(client.app.state.db_engine) as session:
        session.add(Account(alias="Alias existant", account_role="current"))
    preview = _two_section_preview(client)
    endpoint = f"/api/v1/imports/{preview['preview_id']}/sections"
    existing = client.patch(
        f"{endpoint}/1/account-mapping",
        json={
            "expected_version": 1,
            "new_account": {"alias": " Alias Existant ", "account_role": "current"},
        },
    )
    assert existing.status_code == 409
    first = client.patch(
        f"{endpoint}/1/account-mapping",
        json={
            "expected_version": 1,
            "new_account": {"alias": "Alias aperçu", "account_role": "current"},
        },
    )
    assert first.status_code == 200
    duplicate = client.patch(
        f"{endpoint}/2/account-mapping",
        json={
            "expected_version": 2,
            "new_account": {"alias": " alias aperçu ", "account_role": "savings"},
        },
    )
    assert duplicate.status_code == 409


@pytest.mark.integration
def test_m_03_health_remains_responsive_during_synthetic_10_mib_preview(tmp_path: Path) -> None:
    client, _database = _ready_client(tmp_path)
    data = b"x" * (10 * 1024 * 1024)

    async def exercise() -> tuple[int, float, int]:
        async with AsyncClient(
            transport=ASGITransport(app=client.app), base_url="http://127.0.0.1"
        ) as async_client:
            upload = asyncio.create_task(
                async_client.post(
                    "/api/v1/imports/preview",
                    files={"file": ("synthetic.csv", data, "text/csv")},
                )
            )
            await asyncio.sleep(0.01)
            started = time.perf_counter()
            health = await async_client.get("/api/v1/health")
            elapsed = time.perf_counter() - started
            preview_response = await upload
            return health.status_code, elapsed, preview_response.status_code

    health_status, elapsed, preview_status = asyncio.run(exercise())
    assert health_status == 200
    assert elapsed < 1.0
    assert preview_status == 200
