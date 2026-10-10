from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.main import create_app

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _ready_app(tmp_path: Path, name: str = "commit") -> tuple[object, Path, Path]:
    database = tmp_path / f"{name}.sqlite"
    data_dir = tmp_path / f"{name}-data"
    settings = Settings(database_url=f"sqlite:///{database}", data_dir=data_dir, _env_file=None)
    upgrade_to_head(settings)
    return create_app(settings), database, data_dir


def _upload(client: TestClient, raw_bytes: bytes) -> dict[str, object]:
    response = client.post(
        "/api/v1/imports/preview",
        files={"file": ("name-from-client-is-discarded.csv", raw_bytes, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _map_new_account(client: TestClient, preview: dict[str, object]) -> dict[str, object]:
    preview_id = str(preview["preview_id"])
    response = client.patch(
        f"/api/v1/imports/{preview_id}/sections/1/account-mapping",
        json={
            "expected_version": preview["version"],
            "new_account": {"alias": "Compte synthétique de test", "account_role": "current"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _commit(client: TestClient, preview: dict[str, object], key: str | None = None):
    return client.post(
        f"/api/v1/imports/{preview['preview_id']}/commit",
        headers={"Idempotency-Key": key or str(uuid4())},
        json={"expected_version": preview["version"]},
    )


def _table_snapshot(database: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    with sqlite3.connect(database) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            if row[0] != "alembic_version"
        ]
        snapshot: dict[str, tuple[tuple[object, ...], ...]] = {}
        for table in tables:
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            order = ", ".join(f'"{column}"' for column in columns)
            snapshot[table] = tuple(connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}'))
        return snapshot


@pytest.mark.integration
def test_IMP_01_same_file_three_times_is_one_commit_then_two_traced_no_ops(
    tmp_path: Path,
) -> None:
    app, database, data_dir = _ready_app(tmp_path)
    raw = (FIXTURES / "overlap_A.csv").read_bytes()
    with TestClient(app) as client:
        first = _map_new_account(client, _upload(client, raw))
        first_key = str(uuid4())
        committed = _commit(client, first, first_key)
        assert committed.status_code == 200, committed.text
        committed_payload = committed.json()
        assert committed_payload["status"] == "committed"
        assert committed_payload["created_transaction_count"] == 21
        assert committed_payload["new_source_row_count"] == 21
        assert committed_payload["archive_retained"] is True

        replay = _commit(client, first, first_key)
        assert replay.status_code == 200
        assert replay.json() == committed_payload

        no_op_ids: list[str] = []
        for _ in range(2):
            repeated = _upload(client, raw)
            result = _commit(client, repeated)
            assert result.status_code == 200, result.text
            assert result.json()["status"] == "no_op"
            assert result.json()["created_transaction_count"] == 0
            assert result.json()["new_source_row_count"] == 0
            assert result.json()["archive_retained"] is False
            no_op_ids.append(result.json()["import_file_id"])

        archive = data_dir / "originals" / f"{committed_payload['import_file_id']}.bin"
        assert archive.read_bytes() == raw
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == committed_payload["file_sha256"]
        assert archive.stat().st_mode & 0o777 == 0o600
        assert (data_dir / "originals").stat().st_mode & 0o777 == 0o700

        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 21
            assert connection.execute("SELECT count(*) FROM source_rows").fetchone()[0] == 21
            assert (
                connection.execute(
                    "SELECT count(*) FROM import_files WHERE status='no_op'"
                ).fetchone()[0]
                == 2
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM source_rows WHERE import_file_id IN (?, ?)", no_op_ids
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT archive_relpath FROM import_files WHERE import_file_id=?",
                    (no_op_ids[0],),
                ).fetchone()[0]
                is None
            )

        response_text = client.get(f"/api/v1/imports/{committed_payload['import_file_id']}").text
        assert "archive_relpath" not in response_text
        assert "originals/" not in response_text
        assert "raw_label" not in response_text
        assert (
            client.get(
                f"/api/v1/imports/{committed_payload['import_file_id']}/rejections"
            ).status_code
            == 200
        )
        assert client.get("/api/v1/imports?status=committed").status_code == 200
        first_page = client.get("/api/v1/imports?status=no_op&limit=1")
        assert first_page.status_code == 200
        page = first_page.json()
        assert len(page["items"]) == 1
        assert page["page"]["next_cursor"]
        second_page = client.get(
            "/api/v1/imports",
            params={"status": "no_op", "limit": 1, "cursor": page["page"]["next_cursor"]},
        )
        assert second_page.status_code == 200, second_page.text
        assert len(second_page.json()["items"]) == 1
        encoded_cursor, cursor_signature = page["page"]["next_cursor"].rsplit(".", 1)
        replacement = "A" if cursor_signature[0] != "A" else "B"
        tampered = f"{encoded_cursor}.{replacement}{cursor_signature[1:]}"
        assert (
            client.get(
                "/api/v1/imports", params={"status": "no_op", "limit": 1, "cursor": tampered}
            ).status_code
            == 422
        )


@pytest.mark.integration
def test_IMP_06_mid_commit_failure_rolls_back_rows_and_new_account_but_keeps_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import import_commit

    app, database, data_dir = _ready_app(tmp_path, "rollback")
    raw = (FIXTURES / "scenario_famille_2026.csv").read_bytes()
    with TestClient(app) as client:
        preview = _map_new_account(client, _upload(client, raw))
        before = _table_snapshot(database)
        original_insert = import_commit._new_transaction
        calls = 0

        def fail_after_first(session, *, key, rank, raw_label):
            nonlocal calls
            calls += 1
            transaction = original_insert(session, key=key, rank=rank, raw_label=raw_label)
            if calls == 2:
                raise RuntimeError("synthetic_insert_fault")
            return transaction

        monkeypatch.setattr(import_commit, "_new_transaction", fail_after_first)
        with pytest.raises(RuntimeError, match="synthetic_insert_fault"):
            import_commit.commit_preview(
                str(preview["preview_id"]),
                int(preview["version"]),
                str(uuid4()),
                "synthetic_tester",
                engine=app.state.db_engine,
                store=app.state.import_preview_store,
                data_dir=data_dir,
            )
        after = _table_snapshot(database)
        assert after == before
        archives = list((data_dir / "originals").glob("*.bin"))
        assert len(archives) == 1
        assert archives[0].read_bytes() == raw
        assert archives[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.integration
def test_api_idempotency_key_conflict_is_normalized_and_does_not_write(tmp_path: Path) -> None:
    app, database, _data_dir = _ready_app(tmp_path, "idem-conflict")
    raw = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    with TestClient(app) as client:
        preview = _map_new_account(client, _upload(client, raw))
        key = str(uuid4())
        response = _commit(client, preview, key)
        assert response.status_code == 200
        before = _table_snapshot(database)
        conflict = client.post(
            f"/api/v1/imports/{preview['preview_id']}/commit",
            headers={"Idempotency-Key": key},
            json={"expected_version": 1},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert "raw_label" not in conflict.text
        assert _table_snapshot(database) == before


@pytest.mark.integration
def test_legacy_targetless_link_is_rejected_before_archive_or_database_writes(
    tmp_path: Path,
) -> None:
    app, database, data_dir = _ready_app(tmp_path, "legacy-link")
    with TestClient(app) as client:
        first = _map_new_account(
            client, _upload(client, (FIXTURES / "provisional_day_A.csv").read_bytes())
        )
        first_commit = _commit(client, first)
        assert first_commit.status_code == 200
        outside = _upload(client, (FIXTURES / "provisional_day_B.csv").read_bytes())
        preview_store = app.state.import_preview_store
        stored = preview_store.get(str(outside["preview_id"]))
        stored.pop("_preview_protocol_version", None)
        legacy = preview_store.update(str(outside["preview_id"]), stored)
        decision = client.patch(
            f"/api/v1/imports/{outside['preview_id']}/rows/1/multiplicity",
            json={"expected_version": legacy["version"], "action": "link_existing"},
        )
        assert decision.status_code == 200
        archive_count_before = len(list((data_dir / "originals").glob("*.bin")))
        before = _table_snapshot(database)
        rejected = client.post(
            f"/api/v1/imports/{outside['preview_id']}/commit",
            headers={"Idempotency-Key": str(uuid4())},
            json={"expected_version": decision.json()["version"]},
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "MULTIPLICITY_RESOLUTION_INVALID"
        assert len(list((data_dir / "originals").glob("*.bin"))) == archive_count_before
        assert _table_snapshot(database) == before


@pytest.mark.integration
def test_INV_02_scenario_famille_2026_commits_manifest_totals_in_integer_cents(
    tmp_path: Path,
) -> None:
    import json

    from app.importers.credit_agricole import parse_export

    app, database, _data_dir = _ready_app(tmp_path, "family-scenario")
    raw = (FIXTURES / "scenario_famille_2026.csv").read_bytes()
    manifest = json.loads((FIXTURES / "manifest.yaml").read_text(encoding="utf-8"))
    expected_section = manifest["fixtures"]["scenario_famille_2026.csv"]["sections"][0]
    parsed = parse_export(raw)
    expected_net = expected_section["credit_cents"] - expected_section["debit_cents"]
    assert sum(row.amount_cents for row in parsed.rows) == expected_net
    with TestClient(app) as client:
        preview = _map_new_account(client, _upload(client, raw))
        committed = _commit(client, preview)
        assert committed.status_code == 200, committed.text
        assert committed.json()["created_transaction_count"] == 150
        assert committed.json()["new_source_row_count"] == 150
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT sum(amount_cents) FROM transactions").fetchone()[0]
            == expected_net
        )
        assert connection.execute("SELECT count(*) FROM source_rows").fetchone()[0] == 150
        assert connection.execute("SELECT count(*) FROM allocations").fetchone()[0] == 0
        source_labels = connection.execute(
            """SELECT t.raw_label FROM transactions t
               JOIN source_rows s ON s.transaction_id=t.transaction_id
               WHERE s.import_file_id=? ORDER BY s.logical_row_idx""",
            (committed.json()["import_file_id"],),
        ).fetchall()
    assert [item[0] for item in source_labels] == [row.raw_label for row in parsed.rows]


@pytest.mark.integration
def test_idempotency_records_expire_on_application_startup(tmp_path: Path) -> None:
    app, database, _data_dir = _ready_app(tmp_path, "expired-idem")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO idempotency_records
               (idempotency_key,http_method,canonical_path,request_sha256,response_status,response_json,created_at_utc)
               VALUES ('expired-key','POST','/expired',?,200,'{}','2026-10-01T00:00:00.000Z')""",
            ("a" * 64,),
        )
        connection.commit()
    with TestClient(app):
        pass
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM idempotency_records WHERE idempotency_key='expired-key'"
            ).fetchone()[0]
            == 0
        )
