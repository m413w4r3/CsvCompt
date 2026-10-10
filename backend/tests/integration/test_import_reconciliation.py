from __future__ import annotations

import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.importers.credit_agricole import parse_export
from app.importers.normalize import normalize_label
from app.main import create_app
from app.services.import_commit import commit_preview

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _ready_app(tmp_path: Path, name: str) -> tuple[object, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / f"{name}.sqlite"
    data_dir = tmp_path / f"{name}-data"
    settings = Settings(database_url=f"sqlite:///{database}", data_dir=data_dir, _env_file=None)
    upgrade_to_head(settings)
    return create_app(settings), database, data_dir


def _upload(client: TestClient, filename: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/imports/preview",
        files={"file": ("ignored.csv", (FIXTURES / filename).read_bytes(), "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _map_first(client: TestClient, preview: dict[str, object]) -> dict[str, object]:
    mapped = client.patch(
        f"/api/v1/imports/{preview['preview_id']}/sections/1/account-mapping",
        json={
            "expected_version": preview["version"],
            "new_account": {"alias": "Compte chevauchement synthétique", "account_role": "current"},
        },
    )
    assert mapped.status_code == 200, mapped.text
    return mapped.json()


def _commit(client: TestClient, preview: dict[str, object]) -> dict[str, object]:
    response = client.post(
        f"/api/v1/imports/{preview['preview_id']}/commit",
        headers={"Idempotency-Key": str(uuid4())},
        json={"expected_version": preview["version"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _signature(database: Path) -> list[tuple[str, int, str, int]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            """SELECT booked_on, amount_cents, identity_label, occurrence_rank
               FROM transactions
               ORDER BY booked_on, amount_cents, identity_label, occurrence_rank"""
        ).fetchall()


def _expected_maximum(filenames: tuple[str, ...]) -> Counter[tuple[str, int, str]]:
    counts_by_file: list[Counter[tuple[str, int, str]]] = []
    for filename in filenames:
        parsed = parse_export((FIXTURES / filename).read_bytes())
        counts: Counter[tuple[str, int, str]] = Counter()
        for row in parsed.rows:
            counts[
                (
                    row.booked_on.isoformat(),
                    row.amount_cents,
                    normalize_label(row.raw_label).identity_label.value,
                )
            ] += 1
        counts_by_file.append(counts)
    keys = set().union(*(set(counts) for counts in counts_by_file))
    return Counter({key: max(counts[key] for counts in counts_by_file) for key in keys})


@pytest.mark.integration
def test_IMP_02_IMP_03_IMP_04_IMP_09_overlap_and_multiplicity_keep_all_sources(
    tmp_path: Path,
) -> None:
    app, database, _data_dir = _ready_app(tmp_path, "overlap")
    raw_a = (FIXTURES / "overlap_A.csv").read_bytes()
    raw_b = (FIXTURES / "overlap_B.csv").read_bytes()
    expected = _expected_maximum(("overlap_A.csv", "overlap_B.csv"))
    with TestClient(app) as client:
        first = _map_first(client, _upload(client, "overlap_A.csv"))
        first_result = _commit(client, first)
        assert first_result["created_transaction_count"] == 21

        second = _upload(client, "overlap_B.csv")
        assert second["sections"][0]["account_resolution"] == "known"
        second_result = _commit(client, second)
        assert second_result["created_transaction_count"] == sum(expected.values()) - 21

        repeated_b = _commit(client, _upload(client, "overlap_B.csv"))
        assert repeated_b["status"] == "no_op"
        assert repeated_b["created_transaction_count"] == 0

        actual_signature = _signature(database)
        actual = Counter(
            {
                (booked_on, amount, label): count
                for (booked_on, amount, label), count in Counter(
                    (booked_on, amount, label)
                    for booked_on, amount, label, _rank in actual_signature
                ).items()
            }
        )
        assert actual == expected
        assert any(amount == -1123 for _, amount, _ in actual)
        assert [
            rank
            for day, amount, label, rank in actual_signature
            if (day, amount, label)
            == next(key for key, multiplicity in expected.items() if multiplicity == 4)
        ] == [1, 2, 3, 4]

        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT count(*) FROM source_rows").fetchone()[0] == len(
                parse_export(raw_a).rows
            ) + len(parse_export(raw_b).rows)
            assert (
                connection.execute(
                    "SELECT count(*) FROM source_rows WHERE import_file_id=?",
                    (repeated_b["import_file_id"],),
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    """SELECT count(*) FROM transactions t WHERE NOT EXISTS
                   (SELECT 1 FROM source_rows s WHERE s.transaction_id=t.transaction_id)"""
                ).fetchone()[0]
                == 0
            )

        # B contains a genuinely older date than its latest date; no max(booked_on) cutoff is used.
        parsed_b = parse_export(raw_b)
        assert min(row.booked_on for row in parsed_b.rows) < max(
            row.booked_on for row in parsed_b.rows
        )
        assert any(
            day == min(row.booked_on for row in parsed_b.rows).isoformat() for day, *_ in actual
        )


@pytest.mark.integration
def test_IMP_05_B_then_reversed_C_matches_C_then_B_transaction_signature(tmp_path: Path) -> None:
    signatures: list[list[tuple[str, int, str, int]]] = []
    for name, first_filename, second_filename in (
        ("forward", "overlap_B.csv", "overlap_C_reversed_order.csv"),
        ("reverse", "overlap_C_reversed_order.csv", "overlap_B.csv"),
    ):
        app, database, _data_dir = _ready_app(tmp_path, name)
        with TestClient(app) as client:
            first = _map_first(client, _upload(client, first_filename))
            _commit(client, first)
            _commit(client, _upload(client, second_filename))
        signatures.append(_signature(database))
    assert signatures[0] == signatures[1]
    expected = _expected_maximum(("overlap_B.csv", "overlap_C_reversed_order.csv"))
    assert Counter((day, amount, label) for day, amount, label, _rank in signatures[0]) == expected
    for (day, amount, label), count in expected.items():
        ranks = [rank for d, a, text, rank in signatures[0] if (d, a, text) == (day, amount, label)]
        assert ranks == list(range(1, count + 1))


@pytest.mark.integration
def test_IMP_07_real_threads_serialize_same_hash_and_two_overlapping_files(tmp_path: Path) -> None:

    # Same exact content: two real worker threads race through separate previews.
    app, database, data_dir = _ready_app(tmp_path / "same-hash", "same")
    with TestClient(app) as client:
        _commit(client, _map_first(client, _upload(client, "overlap_A.csv")))
        previews = [_upload(client, "overlap_B.csv") for _ in range(2)]
        store = app.state.import_preview_store
        same_hash_barrier = Barrier(2)

        def commit_preview_in_thread(preview: dict[str, object]) -> dict[str, object]:
            same_hash_barrier.wait(timeout=5)
            return commit_preview(
                str(preview["preview_id"]),
                int(preview["version"]),
                str(uuid4()),
                "thread_worker",
                engine=app.state.db_engine,
                store=store,
                data_dir=data_dir,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            same_hash_results = list(pool.map(commit_preview_in_thread, previews))
        assert sorted(result["status"] for result in same_hash_results) == ["committed", "no_op"]
        with sqlite3.connect(database) as connection:
            transactions_after_same_hash = connection.execute(
                "SELECT count(*) FROM transactions"
            ).fetchone()[0]
            assert connection.execute("SELECT count(*) FROM source_rows").fetchone()[0] == 21 + 19
            assert (
                connection.execute(
                    "SELECT count(*) FROM import_files WHERE status='no_op'"
                ).fetchone()[0]
                == 1
            )

    # Different but overlapping contents: two real threads serialize under WAL/BEGIN IMMEDIATE.
    app, database, data_dir = _ready_app(tmp_path / "overlap-files", "overlapping")
    with TestClient(app) as client:
        _commit(client, _map_first(client, _upload(client, "overlap_A.csv")))
        previews = [
            _upload(client, filename)
            for filename in ("overlap_B.csv", "overlap_C_reversed_order.csv")
        ]
        store = app.state.import_preview_store
        overlap_barrier = Barrier(2)

        def commit_distinct_in_thread(preview: dict[str, object]) -> dict[str, object]:
            overlap_barrier.wait(timeout=5)
            return commit_preview(
                str(preview["preview_id"]),
                int(preview["version"]),
                str(uuid4()),
                "thread_worker",
                engine=app.state.db_engine,
                store=store,
                data_dir=data_dir,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            overlap_results = list(pool.map(commit_distinct_in_thread, previews))
        assert [result["status"] for result in overlap_results] == ["committed", "committed"]
        expected = _expected_maximum(
            ("overlap_A.csv", "overlap_B.csv", "overlap_C_reversed_order.csv")
        )
        signature = _signature(database)
        assert Counter((day, amount, label) for day, amount, label, _rank in signature) == expected
        with sqlite3.connect(database) as connection:
            assert (
                connection.execute("SELECT count(*) FROM source_rows").fetchone()[0] == 21 + 19 + 34
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM import_files WHERE status='committed'"
                ).fetchone()[0]
                == 3
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM import_files WHERE status='no_op'"
                ).fetchone()[0]
                == 0
            )
        assert transactions_after_same_hash == sum(
            _expected_maximum(("overlap_A.csv", "overlap_B.csv")).values()
        )


@pytest.mark.integration
def test_IMP_08_signed_amounts_and_case_variants_stay_distinct_with_audit_candidate(
    tmp_path: Path,
) -> None:
    app, database, _data_dir = _ready_app(tmp_path, "strict-identity")
    with TestClient(app) as client:
        _commit(client, _map_first(client, _upload(client, "ca_2026_simple.csv")))
        with sqlite3.connect(database) as connection:
            signs = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT amount_cents FROM transactions WHERE abs(amount_cents)=1123"
                )
            }
        assert signs == {-1123, 1123}
        _commit(client, _upload(client, "overlap_C_reversed_order.csv"))
        with sqlite3.connect(database) as connection:
            candidates = connection.execute(
                "SELECT count(*) FROM import_reconciliation_candidates "
                "WHERE reason_code='near_label'"
            ).fetchone()[0]
        assert candidates > 0


@pytest.mark.integration
def test_IMP_12_link_existing_resolves_only_an_equal_strict_identity(tmp_path: Path) -> None:
    app, database, _data_dir = _ready_app(tmp_path, "link-existing")
    with TestClient(app) as client:
        _commit(client, _map_first(client, _upload(client, "provisional_day_A.csv")))
        different_amount_csv = (
            (FIXTURES / "provisional_day_A.csv").read_bytes().replace(b"11,23;;", b"12,34;;", 1)
        )
        different_amount_preview_response = client.post(
            "/api/v1/imports/preview",
            files={"file": ("synthetic.csv", different_amount_csv, "text/csv")},
        )
        assert different_amount_preview_response.status_code == 200
        different_amount_preview = different_amount_preview_response.json()
        assert different_amount_preview["sections"][0]["account_resolution"] == "known"
        _commit(client, different_amount_preview)
        with sqlite3.connect(database) as connection:
            existing_id = connection.execute(
                "SELECT transaction_id FROM transactions WHERE amount_cents=-1123"
            ).fetchone()[0]
            wrong_identity_id = connection.execute(
                "SELECT transaction_id FROM transactions WHERE amount_cents=-1234"
            ).fetchone()[0]
        assert wrong_identity_id != existing_id
        outside = _upload(client, "provisional_day_B.csv")
        missing_target = client.patch(
            f"/api/v1/imports/{outside['preview_id']}/rows/1/multiplicity",
            json={
                "expected_version": outside["version"],
                "action": "link_existing",
                "transaction_id": None,
            },
        )
        assert missing_target.status_code == 422
        wrong_target = client.patch(
            f"/api/v1/imports/{outside['preview_id']}/rows/1/multiplicity",
            json={
                "expected_version": outside["version"],
                "action": "link_existing",
                "transaction_id": str(uuid4()),
            },
        )
        assert wrong_target.status_code == 422
        different_identity_target = client.patch(
            f"/api/v1/imports/{outside['preview_id']}/rows/1/multiplicity",
            json={
                "expected_version": outside["version"],
                "action": "link_existing",
                "transaction_id": wrong_identity_id,
            },
        )
        assert different_identity_target.status_code == 422
        created = client.patch(
            f"/api/v1/imports/{outside['preview_id']}/rows/1/multiplicity",
            json={
                "expected_version": outside["version"],
                "action": "create_occurrence",
            },
        )
        assert created.status_code == 200, created.text
        linked = client.patch(
            f"/api/v1/imports/{outside['preview_id']}/rows/1/multiplicity",
            json={
                "expected_version": created.json()["version"],
                "action": "link_existing",
                "transaction_id": existing_id,
            },
        )
        assert linked.status_code == 200, linked.text
        committed = _commit(client, linked.json())
        assert committed["created_transaction_count"] == 0
        assert committed["linked_source_row_count"] == 1
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 2
            assert connection.execute("SELECT count(*) FROM source_rows").fetchone()[0] == 3
            assert (
                connection.execute(
                    "SELECT count(DISTINCT transaction_id) FROM source_rows"
                ).fetchone()[0]
                == 2
            )
            audit_rows = connection.execute(
                "SELECT action, actor, before_json, after_json, created_at_utc "
                "FROM audit_events WHERE target_type='import_row' ORDER BY created_at_utc"
            ).fetchall()
            assert len(audit_rows) == 2
            assert all(row[0] == "import.preview.multiplicity_resolution" for row in audit_rows)
            assert all(row[1] == "local_user" and row[4] is not None for row in audit_rows)
            assert '"resolution":null' in audit_rows[0][2]
            assert '"method":"create_occurrence"' in audit_rows[0][3]
            assert '"action":"create_occurrence"' in audit_rows[1][2]
            assert '"method":"link_existing"' in audit_rows[1][3]
            assert existing_id in audit_rows[1][3]


@pytest.mark.integration
def test_IMP_11_IMP_12_provisional_day_and_explicit_out_of_period_decision(tmp_path: Path) -> None:
    app, database, _data_dir = _ready_app(tmp_path, "provisional")
    with TestClient(app) as client:
        provisional_a = _map_first(client, _upload(client, "provisional_day_A.csv"))
        assert provisional_a["sections"][0]["complete_through"] == "2026-10-08"
        assert _commit(client, provisional_a)["created_transaction_count"] == 1

        outside = _upload(client, "provisional_day_B.csv")
        assert outside["can_commit"] is False
        assert any(item["code"] == "OUT_OF_PERIOD" for item in outside["rejections"])
        # The human chooses an additional occurrence for the day outside the declared window.
        decision = client.patch(
            f"/api/v1/imports/{outside['preview_id']}/rows/1/multiplicity",
            json={"expected_version": outside["version"], "action": "create_occurrence"},
        )
        assert decision.status_code == 200, decision.text
        committed = _commit(client, decision.json())
        assert committed["created_transaction_count"] == 1
        with sqlite3.connect(database) as connection:
            ranks = connection.execute(
                "SELECT occurrence_rank FROM transactions ORDER BY occurrence_rank"
            ).fetchall()
            assert ranks == [(1,), (2,)]
            assert (
                connection.execute(
                    "SELECT count(*) FROM import_rejections "
                    "WHERE code='OUT_OF_PERIOD' AND resolution_status='resolved'"
                ).fetchone()[0]
                == 1
            )
            audit = connection.execute(
                "SELECT after_json FROM audit_events "
                "WHERE target_type='import_row' "
                "AND action='import.preview.multiplicity_resolution'"
            ).fetchone()
            assert audit is not None and '"method":"create_occurrence"' in audit[0]


@pytest.mark.integration
def test_missing_period_resolution_preserves_declared_period_and_handles_empty_header_section(
    tmp_path: Path,
) -> None:
    app, database, _data_dir = _ready_app(tmp_path, "coverage-resolution")
    source = (FIXTURES / "adversarial/section_without_period.csv").read_bytes()
    text = source.decode("cp1252")
    lines = text.splitlines(keepends=True)
    header_index = next(index for index, line in enumerate(lines) if line.startswith("Date;"))
    empty_section = "".join(lines[: header_index + 1]).encode("cp1252")

    with TestClient(app) as client:
        declared = _upload(client, "ca_2026_simple.csv")
        changed_declared_range = client.patch(
            f"/api/v1/imports/{declared['preview_id']}/sections/1/coverage",
            json={
                "expected_version": declared["version"],
                "coverage_from": "2026-01-01",
                "coverage_through": "2026-04-30",
            },
        )
        assert changed_declared_range.status_code == 422
        stored_declared = app.state.import_preview_store.get(str(declared["preview_id"]))
        assert stored_declared["sections"][0]["period_from"] == "2026-01-01"
        assert stored_declared["sections"][0]["period_to"] == "2026-02-28"
        assert stored_declared["sections"][0]["coverage_from"] == "2026-01-01"

        missing_period = client.post(
            "/api/v1/imports/preview",
            files={"file": ("missing-period.csv", source, "text/csv")},
        )
        assert missing_period.status_code == 200
        out_of_bounds = client.patch(
            f"/api/v1/imports/{missing_period.json()['preview_id']}/sections/1/coverage",
            json={
                "expected_version": missing_period.json()["version"],
                "coverage_from": "2026-03-11",
                "coverage_through": "2026-03-31",
            },
        )
        assert out_of_bounds.status_code == 422

        uploaded = client.post(
            "/api/v1/imports/preview",
            files={"file": ("empty-synthetic.csv", empty_section, "text/csv")},
        )
        assert uploaded.status_code == 200, uploaded.text
        empty_preview = uploaded.json()
        assert empty_preview["operation_count"] == 0
        assert empty_preview["generation_date_source"] == "header_download"
        mapped = client.patch(
            f"/api/v1/imports/{empty_preview['preview_id']}/sections/1/account-mapping",
            json={
                "expected_version": empty_preview["version"],
                "new_account": {
                    "alias": "Compte vide synthétique",
                    "account_role": "current",
                },
            },
        )
        assert mapped.status_code == 200, mapped.text
        covered = client.patch(
            f"/api/v1/imports/{empty_preview['preview_id']}/sections/1/coverage",
            json={
                "expected_version": mapped.json()["version"],
                "coverage_from": "2026-03-01",
                "coverage_through": "2026-03-31",
            },
        )
        assert covered.status_code == 200, covered.text
        assert covered.json()["sections"][0]["period_from"] is None
        assert covered.json()["sections"][0]["coverage_from"] == "2026-03-01"
        committed = _commit(client, covered.json())
        assert committed["new_source_row_count"] == 0
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM source_rows").fetchone()[0] == 0
            section = connection.execute(
                "SELECT coverage_from, coverage_through, complete_through "
                "FROM import_account_sections WHERE import_file_id=?",
                (committed["import_file_id"],),
            ).fetchone()
            coverage_audit = connection.execute(
                "SELECT before_json, after_json FROM audit_events "
                "WHERE import_file_id=? "
                "AND action='import.preview.coverage_resolution'",
                (committed["import_file_id"],),
            ).fetchone()
        assert section == ("2026-03-01", "2026-03-31", "2026-03-31")
        assert coverage_audit is not None
        assert '"coverage_from":null' in coverage_audit[0]
        assert '"method":"set_coverage"' in coverage_audit[1]


@pytest.mark.integration
def test_IMP_13_distinct_sections_keep_accounts_isolated_and_create_accounts_atomically(
    tmp_path: Path,
) -> None:
    app, database, _data_dir = _ready_app(tmp_path, "sections")
    raw = (FIXTURES / "ca_2026_simple.csv").read_bytes().decode("cp1252")
    lines = raw.splitlines(keepends=True)
    first_section = next(index for index, line in enumerate(lines) if line.startswith("MADAME"))
    first_account = next(index for index, line in enumerate(lines) if line.startswith("Compte de"))
    preamble = "".join(lines[:first_section])
    first = "".join(lines[first_section:]).rstrip("\r\n")
    second = "".join(lines[first_account:]).replace("n° 0001", "n° 0002", 1).rstrip("\r\n")
    two_account_export = (preamble + first + "\n" + second + "\n").encode("cp1252")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/imports/preview",
            files={"file": ("two-account-synthetic.csv", two_account_export, "text/csv")},
        )
        assert response.status_code == 200, response.text
        preview = response.json()
        for section_index in (1, 2):
            result = client.patch(
                f"/api/v1/imports/{preview['preview_id']}/sections/{section_index}/account-mapping",
                json={
                    "expected_version": preview["version"],
                    "new_account": {
                        "alias": f"Compte synthétique section {section_index}",
                        "account_role": "current",
                    },
                },
            )
            assert result.status_code == 200, result.text
            preview = result.json()
        committed = _commit(client, preview)
        assert committed["status"] == "committed"
        assert len({section["account_id"] for section in committed["sections"]}) == 2
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT count(*) FROM accounts").fetchone()[0] == 2
            assert (
                connection.execute(
                    "SELECT count(DISTINCT account_id) FROM transactions"
                ).fetchone()[0]
                == 2
            )
            assert (
                connection.execute("SELECT count(*) FROM source_rows").fetchone()[0]
                == preview["operation_count"]
            )
