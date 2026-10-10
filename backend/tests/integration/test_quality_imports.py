from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.db.models import Transaction
from app.db.session import session_scope
from app.main import create_app
from app.services import import_preview_store as import_preview_store_module
from app.services.app_secrets import ensure_hmac_key
from app.services.balance_checks import BalanceCheck
from tests.helpers.privacy import assert_payload_has_no_account_numbers

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _ready_app(tmp_path: Path, name: str):
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / f"{name}.sqlite"
    settings = Settings(
        database_url=f"sqlite:///{database}",
        data_dir=directory / f"{name}-data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    return create_app(settings)


def _force_uuid4_values_containing_0001(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = 0

    def deterministic_uuid4() -> UUID:
        nonlocal counter
        counter += 1
        return UUID(f"00000001-0000-4000-8000-{counter:012x}")

    monkeypatch.setattr(import_preview_store_module.uuid, "uuid4", deterministic_uuid4)


def _upload(client: TestClient, fixture: str) -> dict[str, object]:
    return _upload_bytes(client, (FIXTURES / fixture).read_bytes())


def _upload_bytes(client: TestClient, raw: bytes) -> dict[str, object]:
    response = client.post(
        "/api/v1/imports/preview",
        files={"file": ("synthetic.csv", raw, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _map_account(
    client: TestClient,
    preview: dict[str, object],
    section_index: int,
    alias: str,
    role: str,
) -> dict[str, object]:
    response = client.patch(
        f"/api/v1/imports/{preview['preview_id']}/sections/{section_index}/account-mapping",
        json={
            "expected_version": preview["version"],
            "new_account": {"alias": alias, "account_role": role},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _commit(client: TestClient, preview: dict[str, object]):
    return client.post(
        f"/api/v1/imports/{preview['preview_id']}/commit",
        headers={"Idempotency-Key": str(uuid4())},
        json={"expected_version": preview["version"]},
    )


def _resolve_out_of_period_to_existing(
    app, client: TestClient, preview: dict[str, object], account_id: str
) -> dict[str, object]:
    with session_scope(app.state.db_engine) as session:
        transaction_id = session.scalar(
            select(Transaction.transaction_id)
            .where(Transaction.account_id == account_id)
            .order_by(Transaction.transaction_id)
            .limit(1)
        )
    assert transaction_id is not None
    response = client.patch(
        f"/api/v1/imports/{preview['preview_id']}/rows/1/multiplicity",
        json={
            "expected_version": preview["version"],
            "action": "link_existing",
            "transaction_id": transaction_id,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("followup_fixture", "day_nine_status"),
    [
        ("provisional_day_B.csv", "partial"),
        ("provisional_day_B_overlap.csv", "complete"),
    ],
    ids=["nonoverlap-leaves-day-missing", "overlap-completes-provisional-day"],
)
def test_ADR_024_imported_provisional_day_requires_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    followup_fixture: str,
    day_nine_status: str,
) -> None:
    _force_uuid4_values_containing_0001(monkeypatch)
    app = _ready_app(tmp_path, "provisional")
    with TestClient(app) as client:
        initial = _map_account(
            client, _upload(client, "provisional_day_A.csv"), 1, "Courant test", "current"
        )
        first_commit = _commit(client, initial)
        assert first_commit.status_code == 200, first_commit.text
        account_id = first_commit.json()["sections"][0]["account_id"]

        followup = _upload(client, followup_fixture)
        if followup_fixture == "provisional_day_B.csv":
            followup = _resolve_out_of_period_to_existing(app, client, followup, account_id)
        second_commit = _commit(client, followup)
        assert second_commit.status_code == 200, second_commit.text

        day_nine = client.get(
            f"/api/v1/accounts/{account_id}/coverage",
            params={"period_start": "2026-10-01", "period_end": "2026-10-09"},
        )
        assert day_nine.status_code == 200, day_nine.text
        assert day_nine.json()["coverage"]["status"] == day_nine_status
        if day_nine_status == "partial":
            assert day_nine.json()["coverage"]["missing_days"] == [
                {"from": "2026-10-09", "through": "2026-10-09"}
            ]
        else:
            assert day_nine.json()["coverage"]["complete_day_count"] == 9

        whole_month = client.get(
            f"/api/v1/accounts/{account_id}/coverage",
            params={"period_start": "2026-10-01", "period_end": "2026-10-31"},
        )
        assert whole_month.status_code == 200, whole_month.text
        assert whole_month.json()["coverage"]["status"] == "partial"
        assert "bank_fingerprint" not in whole_month.text
        assert_payload_has_no_account_numbers(whole_month.json(), whole_month.text, {"0001"})

        balances = client.get(
            f"/api/v1/accounts/{account_id}/balance-checks",
            params={"period_start": "2026-10-01", "period_end": "2026-10-31"},
        )
        assert balances.status_code == 200, balances.text
        assert balances.json()["items"]
        assert all(item["status"] == "not_comparable" for item in balances.json()["items"])
        assert "bank_fingerprint" not in balances.text
        assert_payload_has_no_account_numbers(balances.json(), balances.text, {"0001"})


@pytest.mark.integration
def test_IMP_10_multi_account_coverage_and_account_list_keep_current_and_pea_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_uuid4_values_containing_0001(monkeypatch)
    app = _ready_app(tmp_path, "multi-account")
    with TestClient(app) as client:
        fixture_bytes = (FIXTURES / "ca_2026_simple.csv").read_bytes()
        needle = "n° 0001;".encode("cp1252")
        assert fixture_bytes.count(needle) == 1
        pea_bytes = fixture_bytes.replace(needle, "n° 0002;".encode("cp1252"), 1)
        current_preview = _map_account(
            client,
            _upload_bytes(client, fixture_bytes),
            1,
            "Courant familial test",
            "current",
        )
        current_commit = _commit(client, current_preview)
        assert current_commit.status_code == 200, current_commit.text
        current_id = current_commit.json()["sections"][0]["account_id"]
        pea_preview = _map_account(client, _upload_bytes(client, pea_bytes), 1, "PEA test", "pea")
        pea_commit = _commit(client, pea_preview)
        assert pea_commit.status_code == 200, pea_commit.text
        pea_id = pea_commit.json()["sections"][0]["account_id"]

        current = client.get(
            f"/api/v1/accounts/{current_id}/coverage",
            params={"period_start": "2026-01-01", "period_end": "2026-04-30"},
        )
        pea = client.get(
            f"/api/v1/accounts/{pea_id}/coverage",
            params={"period_start": "2026-01-01", "period_end": "2026-04-30"},
        )
        assert current.status_code == pea.status_code == 200
        assert current.json()["coverage"]["operation_count"] == 40
        assert pea.json()["coverage"]["operation_count"] == 40
        assert current.json()["accounts"] == [current_id]
        assert pea.json()["accounts"] == [pea_id]

        listed = client.get("/api/v1/accounts")
        assert listed.status_code == 200, listed.text
        items = {item["account_id"]: item for item in listed.json()["items"]}
        assert items[current_id]["coverage"]["operation_count"] == 40
        assert items[pea_id]["coverage"]["operation_count"] == 40
        assert "bank_fingerprint" not in listed.text
        assert "account_fingerprint" not in listed.text
        assert_payload_has_no_account_numbers(listed.json(), listed.text, {"0001"})


@pytest.mark.integration
def test_KPI_09_overlapping_import_order_does_not_change_coverage_or_balance_status(
    tmp_path: Path,
) -> None:
    def run_order(name: str, fixture_order: tuple[str, str]) -> dict[str, object]:
        app = _ready_app(tmp_path / name, name)
        with TestClient(app) as client:
            first_preview = _map_account(
                client,
                _upload(client, fixture_order[0]),
                1,
                "Courant ordre test",
                "current",
            )
            first_commit = _commit(client, first_preview)
            assert first_commit.status_code == 200, first_commit.text
            account_id = first_commit.json()["sections"][0]["account_id"]
            second_preview = _upload(client, fixture_order[1])
            second_commit = _commit(client, second_preview)
            assert second_commit.status_code == 200, second_commit.text
            coverage = client.get(
                f"/api/v1/accounts/{account_id}/coverage",
                params={"period_start": "2026-01-01", "period_end": "2026-05-30"},
            )
            balances = client.get(
                f"/api/v1/accounts/{account_id}/balance-checks",
                params={"period_start": "2026-01-01", "period_end": "2026-05-30"},
            )
            assert coverage.status_code == balances.status_code == 200
            checks = [
                (
                    row["status"],
                    row["start_balance_as_of"],
                    row["end_balance_as_of"],
                    row["recalculated_balance_cents"],
                    row["delta_cents"],
                )
                for row in balances.json()["items"]
            ]
            result = coverage.json()["coverage"]
            return {
                "status": result["status"],
                "covered_from": result["covered_from"],
                "covered_through": result["covered_through"],
                "complete_day_count": result["complete_day_count"],
                "missing_days": result["missing_days"],
                "operation_count": result["operation_count"],
                "checks": checks,
            }

    forward = run_order("order-forward", ("overlap_A.csv", "overlap_B.csv"))
    reverse = run_order("order-reverse", ("overlap_B.csv", "overlap_A.csv"))
    assert forward == reverse
    # Un mismatch éventuel rend le statut unknown sans effacer la couverture.
    expected_status = (
        "unknown" if any(check[0] == "mismatch" for check in forward["checks"]) else "complete"
    )
    assert forward["status"] == expected_status
    assert forward["complete_day_count"] == 150
    assert forward["missing_days"] == []


@pytest.mark.integration
def test_scenario_famille_2026_fixture_is_committed_and_read_as_coverage(tmp_path: Path) -> None:
    app = _ready_app(tmp_path, "family-scenario")
    with TestClient(app) as client:
        preview = _map_account(
            client,
            _upload(client, "scenario_famille_2026.csv"),
            1,
            "Scénario familial test",
            "current",
        )
        committed = _commit(client, preview)
        assert committed.status_code == 200, committed.text
        account_id = committed.json()["sections"][0]["account_id"]
        result = client.get(
            f"/api/v1/accounts/{account_id}/coverage",
            params={"period_start": "2026-01-01", "period_end": "2026-12-31"},
        )
        assert result.status_code == 200, result.text
        assert result.json()["coverage"]["operation_count"] > 0
        assert result.json()["coverage"]["status"] == "partial"

        pre_start = client.get(
            f"/api/v1/accounts/{account_id}/coverage",
            params={"period_start": "2025-04-01", "period_end": "2025-12-31"},
        )
        assert pre_start.status_code == 200, pre_start.text
        assert pre_start.json()["coverage"]["status"] == "not_started"
        assert pre_start.json()["coverage"]["missing_days"] == []
        # completeness a trois états; coverage.status distingue le pré-démarrage d'une alerte.
        assert pre_start.json()["period"]["completeness"] == "not_started"


@pytest.mark.integration
def test_balance_check_cursor_paginates_rejects_tampering_and_detects_stale_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.api.v1.accounts as accounts_api

    app = _ready_app(tmp_path, "balance-cursor")
    with TestClient(app) as client:
        preview = _map_account(
            client,
            _upload(client, "ca_2026_simple.csv"),
            1,
            "Compte pagination test",
            "current",
        )
        committed = _commit(client, preview)
        assert committed.status_code == 200, committed.text
        account_id = committed.json()["sections"][0]["account_id"]
        ensure_hmac_key(app.state.db_engine)

        checks = [
            BalanceCheck(
                balance_check_id=f"synthetic-check-{index}",
                account_id=account_id,
                status="match",
                start_balance_as_of=date(2026, index, 1),
                end_balance_as_of=date(2026, index + 1, 1),
                start_balance_cents=10_000 + index,
                end_balance_cents=10_001 + index,
                recalculated_balance_cents=10_001 + index,
                delta_cents=0,
                start_section_id=f"synthetic-start-{index}",
                end_section_id=f"synthetic-end-{index}",
                input_snapshot_sha256="a" * 64,
                version=1,
            )
            for index in range(1, 4)
        ]
        monkeypatch.setattr(
            accounts_api, "calculate_balance_checks", lambda *_args, **_kwargs: checks
        )

        original_loader = accounts_api.load_quality_snapshot
        snapshot_changed = False

        def load_snapshot(session, account_ids):
            sections, transactions = original_loader(session, account_ids)
            if snapshot_changed:
                transactions = [
                    *transactions,
                    {
                        "transaction_id": "synthetic-snapshot-change",
                        "account_id": account_id,
                        "booked_on": date(2026, 6, 1),
                        "amount_cents": 1,
                    },
                ]
            return sections, transactions

        monkeypatch.setattr(accounts_api, "load_quality_snapshot", load_snapshot)
        params = {"period_start": "2026-01-01", "period_end": "2026-12-31", "limit": 1}
        first = client.get(f"/api/v1/accounts/{account_id}/balance-checks", params=params)
        assert first.status_code == 200, first.text
        first_payload = first.json()
        assert [item["balance_check_id"] for item in first_payload["items"]] == [
            "synthetic-check-1"
        ]
        cursor = first_payload["page"]["next_cursor"]
        assert cursor

        second = client.get(
            f"/api/v1/accounts/{account_id}/balance-checks",
            params={**params, "cursor": cursor},
        )
        assert second.status_code == 200, second.text
        assert [item["balance_check_id"] for item in second.json()["items"]] == [
            "synthetic-check-2"
        ]

        encoded, signature = cursor.rsplit(".", 1)
        replacement = "A" if signature[0] != "A" else "B"
        tampered = f"{encoded}.{replacement}{signature[1:]}"
        invalid = client.get(
            f"/api/v1/accounts/{account_id}/balance-checks",
            params={**params, "cursor": tampered},
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "CURSOR_INVALID"

        snapshot_changed = True
        stale = client.get(
            f"/api/v1/accounts/{account_id}/balance-checks",
            params={**params, "cursor": cursor},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "CURSOR_STALE"
