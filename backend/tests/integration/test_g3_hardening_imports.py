from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.errors import AppError
from app.db.errors import DatabaseBusyError
from app.db.migrate import upgrade_to_head
from app.db.models import Account, AuditEvent
from app.db.session import write_transaction
from app.main import create_app
from app.services import import_archive, import_commit
from app.services import imports as import_service
from app.services.import_archive import purge_orphaned_archives
from app.services.imports import _recalculate

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _ready(tmp_path: Path, name: str = "g3"):
    database = tmp_path / f"{name}.sqlite"
    data_dir = tmp_path / f"{name}-data"
    settings = Settings(database_url=f"sqlite:///{database}", data_dir=data_dir, _env_file=None)
    upgrade_to_head(settings)
    return create_app(settings), database, data_dir


def _upload(client: TestClient, raw: bytes) -> dict[str, object]:
    response = client.post(
        "/api/v1/imports/preview",
        files={"file": ("synthetic.csv", raw, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _map_new_account(
    client: TestClient, preview: dict[str, object], alias: str
) -> dict[str, object]:
    response = client.patch(
        f"/api/v1/imports/{preview['preview_id']}/sections/1/account-mapping",
        json={
            "expected_version": preview["version"],
            "new_account": {"alias": alias, "account_role": "current"},
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


def _commit_service(client: TestClient, preview: dict[str, object], data_dir: Path) -> None:
    import_commit.commit_preview(
        str(preview["preview_id"]),
        int(preview["version"]),
        str(uuid4()),
        "synthetic_g3_test",
        engine=client.app.state.db_engine,
        store=client.app.state.import_preview_store,
        data_dir=data_dir,
    )


@pytest.mark.integration
def test_IMP_12_out_of_period_api_lists_strict_candidates_and_link_keeps_rank(
    tmp_path: Path,
) -> None:
    app, database, _data_dir = _ready(tmp_path, "link-candidate")
    out_of_period = (FIXTURES / "adversarial/out_of_period_line.csv").read_bytes()
    in_period = out_of_period.replace(b"15/03/2026", b"20/03/2026")
    with TestClient(app) as client:
        first = _map_new_account(client, _upload(client, in_period), "Compte candidat synthétique")
        committed = _commit(client, first)
        assert committed.status_code == 200, committed.text
        assert committed.json()["created_transaction_count"] == 1

        preview = _upload(client, out_of_period)
        assert preview["rejections"][0]["code"] == "OUT_OF_PERIOD"
        groups = preview["out_of_period_candidates"]
        assert len(groups) == 1
        assert set(groups[0]["candidates"][0]) == {
            "transaction_id",
            "occurrence_rank",
            "imported_at_utc",
        }
        candidate = groups[0]["candidates"][0]
        assert candidate["occurrence_rank"] == 1
        assert candidate["imported_at_utc"].endswith("Z")

        linked = client.patch(
            f"/api/v1/imports/{preview['preview_id']}/rows/1/multiplicity",
            json={
                "expected_version": preview["version"],
                "action": "link_existing",
                "transaction_id": candidate["transaction_id"],
                "section_index": 1,
            },
        )
        assert linked.status_code == 200, linked.text
        assert linked.json()["can_commit"] is True
        result = _commit(client, linked.json())
        assert result.status_code == 200, result.text
        assert result.json()["created_transaction_count"] == 0
        assert result.json()["new_source_row_count"] == 1
        rejections = client.get(f"/api/v1/imports/{result.json()['import_file_id']}/rejections")
        assert rejections.status_code == 200, rejections.text
        assert rejections.json()["items"][0]["resolution_status"] == "resolved"

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_rows").fetchone()[0] == 2
        assert connection.execute("SELECT occurrence_rank FROM transactions").fetchone()[0] == 1


@pytest.mark.integration
def test_IMP_12_out_of_period_create_occurrence_adds_only_the_next_rank(
    tmp_path: Path,
) -> None:
    app, database, _data_dir = _ready(tmp_path, "create-next-rank")
    out_of_period = (FIXTURES / "adversarial/out_of_period_line.csv").read_bytes()
    in_period = out_of_period.replace(b"15/03/2026", b"20/03/2026")
    with TestClient(app) as client:
        first = _map_new_account(client, _upload(client, in_period), "Compte rang synthétique")
        assert _commit(client, first).status_code == 200

        preview = _upload(client, out_of_period)
        resolved = client.patch(
            f"/api/v1/imports/{preview['preview_id']}/rows/1/multiplicity",
            json={
                "expected_version": preview["version"],
                "action": "create_occurrence",
                "section_index": 1,
            },
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["can_commit"] is True
        result = _commit(client, resolved.json())
        assert result.status_code == 200, result.text
        assert result.json()["created_transaction_count"] == 1

    with sqlite3.connect(database) as connection:
        ranks = [row[0] for row in connection.execute("SELECT occurrence_rank FROM transactions")]
        assert sorted(ranks) == [1, 2]
        assert connection.execute("SELECT count(*) FROM source_rows").fetchone()[0] == 2


@pytest.mark.integration
def test_IMP_12_out_of_period_create_occurrence_starts_new_identity_at_rank_one(
    tmp_path: Path,
) -> None:
    app, database, _data_dir = _ready(tmp_path, "create-new-out-of-period-identity")
    raw = (FIXTURES / "adversarial/out_of_period_line.csv").read_bytes()
    with TestClient(app) as client:
        preview = _map_new_account(
            client, _upload(client, raw), "Compte nouvelle identité synthétique"
        )
        assert preview["rejections"][0]["code"] == "OUT_OF_PERIOD"
        resolved = client.patch(
            f"/api/v1/imports/{preview['preview_id']}/rows/1/multiplicity",
            json={
                "expected_version": preview["version"],
                "action": "create_occurrence",
                "section_index": 1,
            },
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["can_commit"] is True
        committed = _commit(client, resolved.json())
        assert committed.status_code == 200, committed.text
        assert committed.json()["created_transaction_count"] == 1

    with sqlite3.connect(database) as connection:
        ranks = [row[0] for row in connection.execute("SELECT occurrence_rank FROM transactions")]
    assert ranks == [1]


@pytest.mark.integration
def test_IMP_12_duplicate_link_target_is_blocked_in_preview_and_before_archive(
    tmp_path: Path,
) -> None:
    account_id = "synthetic-account"
    preview = {
        "sections": [
            {
                "section_index": 1,
                "account_mapping": {
                    "status": "known",
                    "account_id": account_id,
                    "alias": "Compte synthétique",
                },
            }
        ],
        "_resolutions": {
            "account_mapping": {},
            "coverage": {},
            "multiplicity": {
                "1:1": {"action": "link_existing", "transaction_id": "same-target"},
                "1:2": {"action": "link_existing", "transaction_id": "same-target"},
            },
        },
        "_out_of_period_rows": [{"section": 1, "row": 1}, {"section": 1, "row": 2}],
        "_period_missing_sections": [],
        "_canonical_sections": [1],
        "_structural_rejection": False,
        "_unresolvable_blocking_count": 0,
        "_out_of_period_omitted": 0,
        "rejections": [],
    }
    _recalculate(preview)
    assert preview["can_commit"] is False
    assert any(item["code"] == "MULTIPLICITY_RESOLUTION_INVALID" for item in preview["rejections"])

    app, _database, data_dir = _ready(tmp_path, "duplicate-link")
    first_row = SimpleNamespace(
        section=SimpleNamespace(ordinal=1),
        row=SimpleNamespace(logical_row_idx=1),
        identity_key=(account_id, "2026-03-16", -1860, "identity"),
        out_of_period=True,
    )
    second_row = SimpleNamespace(
        section=SimpleNamespace(ordinal=1),
        row=SimpleNamespace(logical_row_idx=2),
        identity_key=(account_id, "2026-03-16", -1860, "identity"),
        out_of_period=True,
    )
    with TestClient(app) as client, pytest.raises(AppError) as error:
        import_commit._validate_link_targets_before_archive(
            client.app.state.db_engine, preview, [first_row, second_row]
        )
    assert error.value.code == "MULTIPLICITY_RESOLUTION_INVALID"
    assert not list((data_dir / "originals").glob("*.bin"))


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["database_busy", "integrity", "runtime"])
def test_IMP_06_MAJ_02_precommit_failures_remove_unreferenced_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    app, _database, data_dir = _ready(tmp_path, f"failure-{failure}")
    raw = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    with TestClient(app) as client:
        preview = _map_new_account(client, _upload(client, raw), "Compte échec synthétique")

        if failure == "database_busy":

            def busy(_engine):
                raise DatabaseBusyError()

            monkeypatch.setattr(import_commit, "write_transaction", busy)
            expected = DatabaseBusyError
        elif failure == "integrity":

            def integrity(*_args, **_kwargs):
                raise IntegrityError("synthetic", {}, RuntimeError("constraint"))

            monkeypatch.setattr(import_commit, "_commit_in_transaction", integrity)
            expected = AppError
        else:
            original = import_commit.save_response

            def fail_after_save(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("synthetic_response_failure")

            monkeypatch.setattr(import_commit, "save_response", fail_after_save)
            expected = RuntimeError

        with pytest.raises(expected):
            _commit_service(client, preview, data_dir)

        assert list((data_dir / "originals").glob("*.bin")) == []
        with client.app.state.db_engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM import_files").scalar_one() == 0


@pytest.mark.integration
def test_IMP_06_MAJ_02_startup_purges_simulated_kill_orphan_with_private_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _database, data_dir = _ready(tmp_path, "startup-purge")
    originals = data_dir / "originals"
    originals.mkdir(parents=True, mode=0o700)
    orphan = originals / f"{uuid4()}.bin"
    orphan.write_bytes(b"synthetic raw bytes")
    old = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
    import os

    os.utime(orphan, (old, old))
    future = datetime.now(UTC) + timedelta(hours=26)

    class FutureDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return future if tz else future.replace(tzinfo=None)

    monkeypatch.setattr(import_archive, "datetime", FutureDateTime)
    with TestClient(app):
        assert not orphan.exists()

    with app.state.db_engine.connect() as connection:
        event = connection.execute(
            select(AuditEvent.action, AuditEvent.after_json).where(
                AuditEvent.action == "import.archive_orphan_purge_intent"
            )
        ).one()
    assert event.action == "import.archive_orphan_purge_intent"
    event_payload = json.loads(event.after_json)
    assert event_payload == {"archive_count": 1, "temporary_count": 0}
    assert "originals" not in event.after_json
    assert ".bin" not in event.after_json


@pytest.mark.integration
def test_IMP_06_MAJ_02_orphan_sweep_uses_ctime_when_mtime_is_old(
    tmp_path: Path,
) -> None:
    app, _database, data_dir = _ready(tmp_path, "callable-purge")
    originals = data_dir / "originals"
    originals.mkdir(parents=True, mode=0o700)
    orphan = originals / f"{uuid4()}.bin"
    recent = originals / f"{uuid4()}.bin"
    orphan.write_bytes(b"synthetic aged bytes")
    recent.write_bytes(b"synthetic recent bytes")
    old = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
    import os

    os.utime(orphan, (old, old))

    assert purge_orphaned_archives(app.state.db_engine, data_dir) == 0
    assert orphan.exists() and recent.exists()
    assert (
        purge_orphaned_archives(
            app.state.db_engine, data_dir, now=datetime.now(UTC) + timedelta(hours=26)
        )
        == 2
    )
    assert not orphan.exists()
    assert not recent.exists()


@pytest.mark.integration
def test_IMP_01_no_op_attempt_does_not_leave_a_second_archive(tmp_path: Path) -> None:
    app, _database, data_dir = _ready(tmp_path, "no-op-archive")
    raw = (FIXTURES / "overlap_A.csv").read_bytes()
    with TestClient(app) as client:
        first = _map_new_account(client, _upload(client, raw), "Compte no-op synthétique")
        committed = _commit(client, first)
        assert committed.status_code == 200
        original_archive = data_dir / "originals" / f"{committed.json()['import_file_id']}.bin"
        assert original_archive.exists()

        no_op = _commit(client, _upload(client, raw))
        assert no_op.status_code == 200
        assert no_op.json()["status"] == "no_op"
        assert no_op.json()["archive_retained"] is False
        assert list((data_dir / "originals").glob("*.bin")) == [original_archive]


@pytest.mark.integration
def test_IMP_01_same_idempotency_key_replays_after_preview_gone_410(tmp_path: Path) -> None:
    app, _database, data_dir = _ready(tmp_path, "replay-after-preview-gone")
    raw = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    with TestClient(app) as client:
        preview = _map_new_account(client, _upload(client, raw), "Compte rejeu synthétique")
        key = str(uuid4())
        first = _commit(client, preview, key)
        assert first.status_code == 200, first.text
        assert not client.app.state.import_preview_store._path(str(preview["preview_id"])).exists()
        expired = client.get(f"/api/v1/imports/preview/{preview['preview_id']}")
        assert expired.status_code == 410

        replay = _commit(client, preview, key)
        assert replay.status_code == 200, replay.text
        assert replay.json() == first.json()
        assert len(list((data_dir / "originals").glob("*.bin"))) == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("conflict", "alias", "change_account_number"),
    [
        ("ACCOUNT_FINGERPRINT_CONFLICT", "Secondaire synthétique", False),
        ("ACCOUNT_ALIAS_CONFLICT", "courant synthétique", True),
    ],
)
def test_IMP_06_commit_rechecks_account_identity_conflicts_without_orphan(
    tmp_path: Path,
    conflict: str,
    alias: str,
    change_account_number: bool,
) -> None:
    app, _database, data_dir = _ready(tmp_path, f"account-{conflict.lower()}")
    first_raw = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    if change_account_number:
        second_raw = first_raw.replace(b"0001", b"0002")
    else:
        second_raw = first_raw.replace(b"09/10/2026", b"10/10/2026")
    with TestClient(app) as client:
        first = _map_new_account(client, _upload(client, first_raw), "Courant synthétique")
        second = _map_new_account(client, _upload(client, second_raw), alias)
        first_commit = _commit(client, first)
        assert first_commit.status_code == 200, first_commit.text

        second_commit = _commit(client, second)
        assert second_commit.status_code == 409, second_commit.text
        assert second_commit.json()["error"]["code"] == conflict
        refreshed = client.get(f"/api/v1/imports/preview/{second['preview_id']}").json()
        assert refreshed["can_commit"] is False
        assert any(item["code"] == conflict for item in refreshed["rejections"])
        assert len(list((data_dir / "originals").glob("*.bin"))) == 1


@pytest.mark.integration
def test_IMP_06_existing_account_fingerprint_race_is_a_dedicated_conflict(
    tmp_path: Path,
) -> None:
    app, _database, data_dir = _ready(tmp_path, "existing-account-fingerprint-race")
    first_raw = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    second_raw = first_raw.replace(b"09/10/2026", b"10/10/2026")
    with TestClient(app) as client:
        with write_transaction(client.app.state.db_engine) as session:
            first_account = Account(alias="Compte existant A", account_role="current")
            second_account = Account(alias="Compte existant B", account_role="savings")
            session.add_all([first_account, second_account])
            session.flush()
            first_account_id = first_account.account_id
            second_account_id = second_account.account_id

        first = _upload(client, first_raw)
        second = _upload(client, second_raw)
        first = client.patch(
            f"/api/v1/imports/{first['preview_id']}/sections/1/account-mapping",
            json={"expected_version": first["version"], "account_id": first_account_id},
        ).json()
        second = client.patch(
            f"/api/v1/imports/{second['preview_id']}/sections/1/account-mapping",
            json={"expected_version": second["version"], "account_id": second_account_id},
        ).json()

        first_commit = _commit(client, first)
        assert first_commit.status_code == 200, first_commit.text
        second_commit = _commit(client, second)
        assert second_commit.status_code == 409, second_commit.text
        assert second_commit.json()["error"]["code"] == "ACCOUNT_FINGERPRINT_CONFLICT"

        refreshed = client.get(f"/api/v1/imports/preview/{second['preview_id']}").json()
        assert refreshed["can_commit"] is False
        assert any(
            item["code"] == "ACCOUNT_FINGERPRINT_CONFLICT" for item in refreshed["rejections"]
        )
        assert len(list((data_dir / "originals").glob("*.bin"))) == 1

    with client.app.state.db_engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT alias, bank_fingerprint FROM accounts ORDER BY alias"
        ).all()
    assert rows[0][1] is not None
    assert rows[1][1] is None


@pytest.mark.integration
def test_IMP_06_existing_account_fingerprint_change_blocks_preview_visibly(
    tmp_path: Path,
) -> None:
    app, _database, data_dir = _ready(tmp_path, "existing-account-fingerprint-change")
    raw = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    with TestClient(app) as client:
        with write_transaction(client.app.state.db_engine) as session:
            account = Account(alias="Compte modifié synthétique", account_role="current")
            session.add(account)
            session.flush()
            account_id = account.account_id

        preview = _upload(client, raw)
        mapped = client.patch(
            f"/api/v1/imports/{preview['preview_id']}/sections/1/account-mapping",
            json={"expected_version": preview["version"], "account_id": account_id},
        )
        assert mapped.status_code == 200, mapped.text

        with write_transaction(client.app.state.db_engine) as session:
            changed_account = session.get(Account, account_id)
            assert changed_account is not None
            changed_account.bank_fingerprint = "f" * 64
            changed_account.fingerprint_key_version = 1
            changed_account.version += 1

        committed = _commit(client, mapped.json())
        assert committed.status_code == 409, committed.text
        assert committed.json()["error"]["code"] == "ACCOUNT_FINGERPRINT_CONFLICT"
        refreshed = client.get(f"/api/v1/imports/preview/{preview['preview_id']}").json()
        assert refreshed["can_commit"] is False
        assert any(
            item["code"] == "ACCOUNT_FINGERPRINT_CONFLICT" for item in refreshed["rejections"]
        )
        assert list((data_dir / "originals").glob("*.bin")) == []


@pytest.mark.integration
def test_IMP_11_preview_patch_during_marked_commit_is_409(tmp_path: Path) -> None:
    app, _database, _data_dir = _ready(tmp_path, "patch-during-commit")
    raw = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    with TestClient(app) as client:
        preview = _upload(client, raw)
        store = client.app.state.import_preview_store
        store.begin_commit(str(preview["preview_id"]), int(preview["version"]))
        try:
            response = client.patch(
                f"/api/v1/imports/{preview['preview_id']}/sections/1/account-mapping",
                json={
                    "expected_version": preview["version"],
                    "new_account": {
                        "alias": "Compte concurrence synthétique",
                        "account_role": "current",
                    },
                },
            )
        finally:
            store.finish_commit(str(preview["preview_id"]))
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "IMPORT_COMMIT_IN_PROGRESS"


@pytest.mark.integration
def test_G3_MAX_IMPORT_OPERATION_COUNT_has_dedicated_api_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _database, _data_dir = _ready(tmp_path, "operation-limit")
    monkeypatch.setattr(
        import_service,
        "parse_export",
        lambda *_args, **_kwargs: SimpleNamespace(operation_count=4_001),
    )
    raw = (FIXTURES / "ca_2026_simple.csv").read_bytes()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/imports/preview",
            files={"file": ("synthetic.csv", raw, "text/csv")},
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "TOO_MANY_OPERATIONS"
    assert "4000" in response.json()["error"]["message"]
