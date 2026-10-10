from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config

from app.core.config import Settings
from app.db.migrate import create_verified_backup, current_revision, upgrade_to_head
from app.db.models import Account, Transaction
from app.db.session import create_sqlite_engine, write_transaction
from app.services.allocations import AllocationPart, allocate

ROOT = Path(__file__).resolve().parents[3]
FROZEN_0001_SQL_SHA256 = "b3ee949ad9aedddd6bb6c07ac0b1c1cad41709d8da0d861df676e1db7c49b257"
FROZEN_0001_PY_SHA256 = "3d5fa08337a46a336f65e71b48fcdedd8866dca2ebf9e6a093905190c15e4d92"


def _objects(database: Path) -> tuple[tuple[str, str, str, str | None], ...]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger') "
            "AND name NOT LIKE 'sqlite_%' AND name <> 'alembic_version'"
        ).fetchall()
    return tuple(
        sorted(
            (
                str(kind),
                str(name),
                str(table),
                None if sql is None else re.sub(r"\s+", " ", str(sql)).strip(),
            )
            for kind, name, table, sql in rows
        )
    )


def _snapshot_rows(database: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    with sqlite3.connect(database) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name <> 'alembic_version' ORDER BY name"
            )
        ]
        return {
            table: tuple(
                sorted(
                    (tuple(row) for row in connection.execute(f' SELECT * FROM "{table}"')),
                    key=repr,
                )
            )
            for table in tables
        }


def _insert_old_status_links(
    database: Path,
    *,
    source_transaction_id: str,
    source_allocation_id: str,
    target_transaction_id: str,
    status: str,
    amount_cents: int,
) -> str:
    link_id = str(uuid4())
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO transaction_links
               (transaction_link_id, source_transaction_id, target_transaction_id,
                link_kind, amount_cents, source_allocation_id, target_allocation_id, status)
               VALUES (?, ?, ?, 'refund_of', ?, ?, NULL, ?)""",
            (
                link_id,
                source_transaction_id,
                target_transaction_id,
                amount_cents,
                source_allocation_id,
                status,
            ),
        )
    return link_id


def _create_old_revision(settings: Settings) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    config.attributes["operation"] = "upgrade"
    command.upgrade(config, "0001_initial")


def _seed_old_categories(database: Path) -> None:
    categories = (
        ("groceries", "expense"),
        ("restaurants_dining", "expense"),
        ("income_refund", "refund"),
    )
    with sqlite3.connect(database) as connection:
        for category_id, flow_kind in categories:
            connection.execute(
                "INSERT INTO categories(category_id, label, taxonomy_version) VALUES (?, ?, 1)",
                (category_id, f"Catégorie {category_id}"),
            )
            connection.execute(
                "INSERT INTO category_flow_kinds(category_id, flow_kind) VALUES (?, ?)",
                (category_id, flow_kind),
            )


@pytest.mark.integration
def test_MIG_FREEZE_01_published_0001_files_are_pinned() -> None:
    sql_path = ROOT / "backend/app/db/migrations/sql/0001_initial.sql"
    python_path = ROOT / "backend/app/db/migrations/versions/0001_initial.py"
    assert hashlib.sha256(sql_path.read_bytes()).hexdigest() == FROZEN_0001_SQL_SHA256, (
        "migration gelée, créer 0003 : SHA-256 de 0001_initial.sql différent"
    )
    assert hashlib.sha256(python_path.read_bytes()).hexdigest() == FROZEN_0001_PY_SHA256, (
        "migration gelée, créer 0003 : SHA-256 de 0001_initial.py différent"
    )


@pytest.mark.integration
def test_MIG_0002_nonempty_0001_upgrade_preserves_rows_and_guards(tmp_path: Path) -> None:
    database = tmp_path / "preexisting-0001.sqlite"
    settings = Settings(
        database_url=f"sqlite:///{database}", data_dir=tmp_path / "private-data", _env_file=None
    )
    _create_old_revision(settings)
    _seed_old_categories(database)
    engine = create_sqlite_engine(settings.database_url)
    try:
        with write_transaction(engine) as session:
            account = Account(alias="Compte migration synthétique", account_role="current")
            session.add(account)
            session.flush()
            account_id = account.account_id
            specs = [
                ("ACHAT MIGRATION", -10_000),
                ("AVOIR PROPOSITION", 3_000),
                ("AVOIR CONFIRME", 2_000),
                ("AVOIR REJETE", 1_000),
                ("AVOIR RETRAIT", 500),
                ("AVOIR PLAFOND", 20_000),
            ]
            transactions: dict[str, Transaction] = {}
            for label, amount in specs:
                row = Transaction(
                    account_id=account_id,
                    booked_on=date(2026, 2, 3),
                    amount_cents=amount,
                    raw_label="Opération synthétique de migration",
                    identity_label=label,
                    identity_key_sha256=hashlib.sha256(label.encode()).hexdigest(),
                    occurrence_rank=1,
                )
                session.add(row)
                session.flush()
                transactions[label] = row
        purchase = transactions["ACHAT MIGRATION"]
        allocate(
            engine,
            purchase.transaction_id,
            [
                AllocationPart("expense", "groceries", -7_000),
                AllocationPart("expense", "restaurants_dining", -3_000),
            ],
            mode="complete",
            origin="manual",
            actor="migration-fixture",
            expected_version=1,
        )
        allocate(
            engine,
            purchase.transaction_id,
            [
                AllocationPart("expense", "groceries", -6_000),
                AllocationPart("expense", "restaurants_dining", -4_000),
            ],
            mode="complete",
            origin="manual",
            actor="migration-fixture",
            expected_version=2,
        )
        source_allocations: dict[str, str] = {}
        for label, _amount in specs[1:]:
            state = allocate(
                engine,
                transactions[label].transaction_id,
                [AllocationPart("refund", "income_refund", transactions[label].amount_cents)],
                mode="complete",
                origin="manual",
                actor="migration-fixture",
                expected_version=1,
            )
            source_allocations[label] = state.allocations[0].allocation_id
        engine.dispose()

        # Les écritures historiques couvrent les statuts qui existaient à 0001;
        # elles passent par les CHECK et triggers 0001 avant l'upgrade.
        _insert_old_status_links(
            database,
            source_transaction_id=transactions["AVOIR PROPOSITION"].transaction_id,
            source_allocation_id=source_allocations["AVOIR PROPOSITION"],
            target_transaction_id=purchase.transaction_id,
            status="proposed",
            amount_cents=1_500,
        )
        _insert_old_status_links(
            database,
            source_transaction_id=transactions["AVOIR CONFIRME"].transaction_id,
            source_allocation_id=source_allocations["AVOIR CONFIRME"],
            target_transaction_id=purchase.transaction_id,
            status="confirmed",
            amount_cents=1_000,
        )
        _insert_old_status_links(
            database,
            source_transaction_id=transactions["AVOIR REJETE"].transaction_id,
            source_allocation_id=source_allocations["AVOIR REJETE"],
            target_transaction_id=purchase.transaction_id,
            status="rejected",
            amount_cents=500,
        )

        with sqlite3.connect(database) as connection:
            # decision + retraction row retained through the classification table rebuild.
            retracted_id = source_allocations["AVOIR RETRAIT"]
            retracted_decision_id = str(uuid4())
            connection.execute(
                """INSERT INTO classification_decisions
                   (decision_id, transaction_id, action, actor, before_json)
                   VALUES (?, ?, 'undo', 'migration-fixture', '{}')""",
                (retracted_decision_id, transactions["AVOIR RETRAIT"].transaction_id),
            )
            connection.execute(
                """UPDATE allocations SET retracted_at_utc='2026-02-04T00:00:00.000Z',
                          retracted_by_decision_id=?, version=version+1
                   WHERE allocation_id=?""",
                (retracted_decision_id, retracted_id),
            )
            import_id = str(uuid4())
            connection.execute(
                """INSERT INTO import_files
                   (import_file_id, file_sha256, archive_relpath, archive_storage_mode,
                    archive_permissions_octal, file_size_bytes, received_on, generation_date,
                    generation_date_source, parsed_operation_count, status)
                   VALUES (?, ?, ?, 'raw', '0600', 1, '2026-02-03', '2026-02-03',
                           'header_download', ?, 'previewed')""",
                (import_id, "a" * 64, f"data/originals/{import_id}.bin", len(specs)),
            )
            connection.execute(
                """INSERT INTO import_account_sections
                   (import_file_id, section_index, account_id, coverage_from,
                    coverage_through, complete_through)
                   VALUES (?, 1, ?, '2026-02-01', '2026-02-28', '2026-02-02')""",
                (import_id, account_id),
            )
            for index, (label, _amount) in enumerate(specs, start=1):
                transaction = transactions[label]
                connection.execute(
                    """INSERT INTO source_rows
                       (source_row_id, import_file_id, section_index, logical_row_idx,
                        row_sha256, identity_key_sha256, transaction_id)
                       VALUES (?, ?, 1, ?, ?, ?, ?)""",
                    (
                        str(uuid4()),
                        import_id,
                        index,
                        hashlib.sha256(f"row-{index}".encode()).hexdigest(),
                        transaction.identity_key_sha256,
                        transaction.transaction_id,
                    ),
                )
            connection.execute(
                """UPDATE import_files SET status='committed',
                   finished_at_utc='2026-02-03T00:00:00.000Z', version=version+1
                   WHERE import_file_id=?""",
                (import_id,),
            )
        rows_before = _snapshot_rows(database)

        result = upgrade_to_head(settings)
        assert result.revision == "0002_t17_refund_safety"
        assert result.backup is not None
        assert stat.S_IMODE(result.backup.database_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.backup.manifest_path.stat().st_mode) == 0o600
        backup_manifest = json.loads(result.backup.manifest_path.read_text(encoding="utf-8"))
        assert backup_manifest["source_revision"] == "0001_initial"
        assert backup_manifest["integrity_check"] == "ok"

        rows_after = _snapshot_rows(database)
        assert {name: rows_after[name] for name in rows_before} == rows_before
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "0002_t17_refund_safety",
            )
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute(
                "SELECT sum(amount_cents) FROM refund_link_allocation_parts"
            ).fetchone() == (1_000,)
            assert (
                connection.execute("SELECT count(*) FROM refund_link_allocation_parts").fetchone()[
                    0
                ]
                >= 2
            )
            assert connection.execute("SELECT count(*) FROM source_rows").fetchone() == (
                len(specs),
            )

            # Les gardes de parts et d'états sont actives sur le fichier migré.
            confirmed_link = connection.execute(
                "SELECT transaction_link_id FROM transaction_links WHERE status='confirmed'"
            ).fetchone()[0]
            part = connection.execute(
                "SELECT allocation_id FROM refund_link_allocation_parts "
                "WHERE transaction_link_id=?",
                (confirmed_link,),
            ).fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError, match="refund_link_part_immutable"):
                connection.execute(
                    "UPDATE refund_link_allocation_parts SET amount_cents=amount_cents+1 "
                    "WHERE transaction_link_id=? AND allocation_id=?",
                    (confirmed_link, part),
                )
            with pytest.raises(sqlite3.IntegrityError, match="refund_link_part_delete_forbidden"):
                connection.execute(
                    "DELETE FROM refund_link_allocation_parts WHERE transaction_link_id=? "
                    "AND allocation_id=?",
                    (confirmed_link, part),
                )
            proposed_link = connection.execute(
                "SELECT transaction_link_id FROM transaction_links WHERE status='proposed'"
            ).fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                connection.execute(
                    "INSERT INTO refund_link_allocation_parts "
                    "(transaction_link_id, allocation_id, amount_cents) VALUES (?, ?, 0)",
                    (proposed_link, part),
                )
            superseded_allocation = connection.execute(
                "SELECT allocation_id FROM allocations WHERE transaction_id=? "
                "AND category_id='groceries' AND superseded_by_allocation_id IS NOT NULL LIMIT 1",
                (purchase.transaction_id,),
            ).fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError, match="refund_link_part_target_invalid"):
                connection.execute(
                    "INSERT INTO refund_link_allocation_parts "
                    "(transaction_link_id, allocation_id, amount_cents) VALUES (?, ?, 1)",
                    (proposed_link, superseded_allocation),
                )
            with pytest.raises(
                sqlite3.IntegrityError, match="refund_link_attribution_total_mismatch"
            ):
                connection.execute(
                    "UPDATE transaction_links SET status='confirmed', version=version+1 "
                    "WHERE transaction_link_id=?",
                    (proposed_link,),
                )
            groceries = connection.execute(
                "SELECT allocation_id FROM allocations WHERE transaction_id=? "
                "AND category_id='groceries' AND superseded_by_allocation_id IS NULL "
                "AND retracted_at_utc IS NULL",
                (purchase.transaction_id,),
            ).fetchone()[0]
            cap_link_id = str(uuid4())
            connection.execute(
                """INSERT INTO transaction_links
                   (transaction_link_id, source_transaction_id, target_transaction_id,
                    link_kind, amount_cents, source_allocation_id, target_allocation_id, status)
                   VALUES (?, ?, ?, 'refund_of', 5500, ?, ?, 'proposed')""",
                (
                    cap_link_id,
                    transactions["AVOIR PLAFOND"].transaction_id,
                    purchase.transaction_id,
                    source_allocations["AVOIR PLAFOND"],
                    groceries,
                ),
            )
            with pytest.raises(
                sqlite3.IntegrityError, match="refund_link_target_allocation_cap_exceeded"
            ):
                connection.execute(
                    "UPDATE transaction_links SET status='confirmed', version=version+1 "
                    "WHERE transaction_link_id=?",
                    (cap_link_id,),
                )
            with pytest.raises(
                sqlite3.IntegrityError, match="refund_link_status_transition_invalid"
            ):
                connection.execute(
                    "UPDATE transaction_links SET status='confirmed', version=version+1 "
                    "WHERE status='rejected'"
                )

        reference_db = tmp_path / "head-reference.sqlite"
        with sqlite3.connect(reference_db) as connection:
            connection.executescript(
                (ROOT / "docs/examples/schema.sql").read_text(encoding="utf-8")
            )
        assert _objects(database) == _objects(reference_db)
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MIG_0002_downgrade_requires_explicit_verified_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "downgrade.sqlite"
    settings = Settings(
        database_url=f"sqlite:///{database}", data_dir=tmp_path / "private-data", _env_file=None
    )
    assert upgrade_to_head(settings).revision == "0002_t17_refund_safety"
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    config.attributes["operation"] = "downgrade"
    monkeypatch.delenv("COMPTES_ALLOW_DATA_LOSS_DOWNGRADE", raising=False)
    monkeypatch.delenv("COMPTES_DOWNGRADE_BACKUP", raising=False)
    with pytest.raises(RuntimeError, match="COMPTES_ALLOW_DATA_LOSS_DOWNGRADE=1"):
        command.downgrade(config, "0001_initial")
    assert current_revision(settings.database_url) == "0002_t17_refund_safety"

    backup = create_verified_backup(
        database,
        tmp_path / "verified-backups",
        source_revision="0002_t17_refund_safety",
    )
    monkeypatch.setenv("COMPTES_ALLOW_DATA_LOSS_DOWNGRADE", "1")
    monkeypatch.setenv("COMPTES_DOWNGRADE_BACKUP", str(backup.database_path))
    command.downgrade(config, "0001_initial")
    assert current_revision(settings.database_url) == "0001_initial"
    base_reference = tmp_path / "frozen-base.sqlite"
    with sqlite3.connect(base_reference) as connection:
        connection.executescript(
            (ROOT / "backend/app/db/migrations/sql/0001_initial.sql").read_text(encoding="utf-8")
        )
    assert _objects(database) == _objects(base_reference)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
