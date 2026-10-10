from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.core.config import Settings
from app.db import migrate
from app.db.migrate import MigrationError, current_revision, upgrade_to_head
from app.services import backups


def _id() -> str:
    return str(uuid4())


def _settings(tmp_path: Path, name: str) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / f'{name}.sqlite'}",
        data_dir=tmp_path / f"{name}-data",
        _env_file=None,
    )


def _at_0003(settings: Settings) -> tuple[str, str, str, str]:
    config = Config(str(migrate.ALEMBIC_INI))
    config.attributes["settings"] = settings
    config.attributes["operation"] = "upgrade"
    command.upgrade(config, "0003_t20_review")

    account_id, transaction_id, decision_id, execution_id = (_id() for _ in range(4))
    rule_id, audit_id, batch_id = (_id() for _ in range(3))
    identity = "migration 0004 synthetic"
    snapshot = json.dumps([], separators=(",", ":"))
    scope_hash = "a" * 64
    database_path = Path(settings.database_url.removeprefix("sqlite:///"))
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO accounts(account_id, alias, account_role) VALUES (?, ?, 'current')",
            (account_id, "Compte synthétique 0004"),
        )
        connection.execute(
            "INSERT INTO categories(category_id, label, taxonomy_version) "
            "VALUES ('groceries', 'Catégorie synthétique', 1)"
        )
        connection.execute(
            "INSERT INTO category_flow_kinds(category_id, flow_kind) "
            "VALUES ('groceries', 'expense')"
        )
        connection.execute(
            """INSERT INTO transactions
               (transaction_id, account_id, booked_on, amount_cents, raw_label,
                identity_label, identity_key_sha256, occurrence_rank)
               VALUES (?, ?, '2026-05-04', -1250, 'Libellé synthétique 0004', ?, ?, 1)""",
            (transaction_id, account_id, identity, "b" * 64),
        )
        connection.execute(
            """INSERT INTO rules
               (rule_id, name, scope_kind, scope_json, flow_kind, category_id, effect_json)
               VALUES (?, 'Règle synthétique 0004', 'exact_payee', '{}',
                       'expense', 'groceries', '{}')""",
            (rule_id,),
        )
        connection.execute(
            """INSERT INTO classification_decisions
               (decision_id, transaction_id, action, actor, before_json, after_json,
                transaction_version)
               VALUES (?, ?, 'confirm', 'test-synthetic', ?, ?, 2)""",
            (decision_id, transaction_id, snapshot, snapshot),
        )
        connection.execute(
            """INSERT INTO audit_events
               (audit_event_id, target_type, target_id, action, actor, before_json, after_json)
               VALUES (?, 'transaction', ?, 'rule.applied', 'test-synthetic', ?, ?)""",
            (audit_id, transaction_id, snapshot, snapshot),
        )
        connection.execute(
            """INSERT INTO rule_executions
               (rule_execution_id, rule_id, transaction_id, outcome, before_json, after_json,
                dry_run, audit_event_id, batch_id, rule_version, batch_scope_sha256,
                classification_decision_id)
               VALUES (?, ?, ?, 'applied', ?, ?, 0, ?, ?, 1, ?, ?)""",
            (
                execution_id,
                rule_id,
                transaction_id,
                snapshot,
                snapshot,
                audit_id,
                batch_id,
                scope_hash,
                decision_id,
            ),
        )
    return transaction_id, decision_id, execution_id, audit_id


@pytest.mark.integration
def test_MIG_0004_populated_0003_upgrade_preserves_rows_and_backup_restores_schema(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, "populated-0004")
    transaction_id, decision_id, execution_id, _audit_id = _at_0003(settings)
    database = Path(settings.database_url.removeprefix("sqlite:///"))

    result = upgrade_to_head(settings)

    assert result.revision == "0004_t20_snapshot_guards"
    assert result.backup is not None
    assert current_revision(settings.database_url) == "0004_t20_snapshot_guards"
    assert result.backup.manifest_path.is_file()
    with sqlite3.connect(result.backup.database_path) as copied:
        assert copied.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0003_t20_review",
        )
        assert copied.execute(
            "SELECT decision_id, before_json, after_json "
            "FROM classification_decisions WHERE decision_id=?",
            (decision_id,),
        ).fetchone() == (decision_id, "[]", "[]")

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT transaction_id FROM classification_decisions WHERE decision_id=?",
            (decision_id,),
        ).fetchone() == (transaction_id,)
        assert connection.execute(
            "SELECT classification_decision_id FROM rule_executions WHERE rule_execution_id=?",
            (execution_id,),
        ).fetchone() == (decision_id,)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(classification_decisions)")
        }
        assert {"before_json_sha256", "after_json_sha256"} <= columns
        assert connection.execute(
            "SELECT before_json_sha256, after_json_sha256 FROM classification_decisions "
            "WHERE decision_id=?",
            (decision_id,),
        ).fetchone() == (None, None)

    backup_path = tmp_path / "full-0004-backup.cfbackup"
    full_backup = backups.create_backup(
        database, settings.data_dir, backup_path, retention_count=None
    )
    restored_database = tmp_path / "restored-0004" / "current" / "database.sqlite"
    restored_data = tmp_path / "restored-0004" / "current" / "data"
    restored = backups.restore_backup(
        full_backup.path,
        restored_database,
        restored_data,
        active_database_path=database,
        active_data_dir=settings.data_dir,
    )
    assert restored.verification.schema_version == "0004_t20_snapshot_guards"
    with sqlite3.connect(restored_database) as connection:
        assert connection.execute(
            "SELECT before_json_sha256, after_json_sha256 FROM classification_decisions "
            "WHERE decision_id=?",
            (decision_id,),
        ).fetchone() == (None, None)
        assert {
            row[1] for row in connection.execute("PRAGMA table_info(classification_decisions)")
        } >= {
            "before_json_sha256",
            "after_json_sha256",
        }


@pytest.mark.integration
def test_MIG_0004_failure_after_first_alter_rolls_back_atomically(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, "rollback-0004")
    _at_0003(settings)
    database = Path(settings.database_url.removeprefix("sqlite:///"))

    def fail_on_second_added_column(
        _connection, _cursor, statement: str, _parameters, _context, _executemany
    ) -> None:
        if "ADD COLUMN after_json_sha256" in statement:
            raise RuntimeError("synthetic 0004 injected failure")

    event.listen(Engine, "before_cursor_execute", fail_on_second_added_column)
    try:
        with pytest.raises(MigrationError, match="La migration Alembic a échoué") as failure:
            upgrade_to_head(settings)
        assert "synthetic 0004 injected failure" in str(failure.value.__cause__)
    finally:
        event.remove(Engine, "before_cursor_execute", fail_on_second_added_column)

    assert current_revision(settings.database_url) == "0003_t20_review"
    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(classification_decisions)")
        }
        assert "before_json_sha256" not in columns
        assert "after_json_sha256" not in columns
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
