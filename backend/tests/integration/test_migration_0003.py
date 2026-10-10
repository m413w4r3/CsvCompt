from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config

from app.core.config import Settings
from app.db import migrate
from app.db.migrate import current_revision, upgrade_to_head


@pytest.mark.integration
def test_MIG_0003_existing_0002_rule_trace_is_preserved_and_backfilled(tmp_path: Path) -> None:
    database = tmp_path / "existing-0002.sqlite"
    settings = Settings(
        database_url=f"sqlite:///{database}", data_dir=tmp_path / "private-data", _env_file=None
    )
    config = Config(str(migrate.ALEMBIC_INI))
    config.attributes["settings"] = settings
    config.attributes["operation"] = "upgrade"
    config.attributes["pre_upgrade_backup_checked"] = True
    command.upgrade(config, "0002_t17_refund_safety")

    account_id = str(uuid4())
    transaction_id = str(uuid4())
    rule_id = str(uuid4())
    decision_id = str(uuid4())
    audit_id = str(uuid4())
    execution_id = str(uuid4())
    normalized = "migration rule trace synthetic"
    rule_version = 2
    allocation = {
        "flow_kind": "expense",
        "category_id": "groceries",
        "amount_cents": -1_250,
        "origin": "rule",
        "beneficiary": "unknown",
        "cadence": "unknown",
        "flexibility": "unknown",
        "acquisition": "unknown",
    }
    before_json = json.dumps(
        {"rule_version": rule_version, "allocations": []},
        sort_keys=True,
        separators=(",", ":"),
    )
    after_decision = json.dumps([allocation], sort_keys=True, separators=(",", ":"))
    after_execution = json.dumps(
        {"rule_version": rule_version, "outcome": "applied", "allocations": [allocation]},
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO categories(category_id, label, taxonomy_version) VALUES (?, ?, 1)",
            ("groceries", "Catégorie synthétique"),
        )
        connection.execute(
            "INSERT INTO category_flow_kinds(category_id, flow_kind) "
            "VALUES ('groceries', 'expense')"
        )
        connection.execute(
            "INSERT INTO accounts(account_id, alias, account_role) VALUES (?, ?, 'current')",
            (account_id, "Compte migration synthétique"),
        )
        connection.execute(
            """INSERT INTO transactions
               (transaction_id, account_id, booked_on, amount_cents, raw_label,
                identity_label, identity_key_sha256, occurrence_rank)
               VALUES (?, ?, '2026-05-01', -1250, ?, ?, ?, 1)""",
            (
                transaction_id,
                account_id,
                "Libellé synthétique de migration",
                normalized,
                hashlib.sha256(normalized.encode()).hexdigest(),
            ),
        )
        connection.execute(
            """INSERT INTO rules
               (rule_id, name, scope_kind, scope_json, flow_kind, category_id,
                effect_json, priority, is_approved, is_active, approved_by, version)
               VALUES (?, 'Règle synthétique', 'label_pattern', '{}', 'expense',
                       'groceries', '{}', 100, 1, 1, 'migration-synthetic', ?)""",
            (rule_id, rule_version),
        )
        connection.execute(
            """INSERT INTO classification_decisions
               (decision_id, transaction_id, action, actor, before_json, after_json)
               VALUES (?, ?, 'confirm', 'migration-synthetic', '[]', ?)""",
            (decision_id, transaction_id, after_decision),
        )
        connection.execute(
            """INSERT INTO audit_events
               (audit_event_id, target_type, target_id, action, actor, before_json, after_json)
               VALUES (?, 'transaction', ?, 'rule.applied', 'migration-synthetic', ?, ?)""",
            (audit_id, transaction_id, before_json, after_execution),
        )
        connection.execute(
            """INSERT INTO rule_executions
               (rule_execution_id, rule_id, transaction_id, outcome, before_json,
                after_json, audit_event_id)
               VALUES (?, ?, ?, 'applied', ?, ?, ?)""",
            (execution_id, rule_id, transaction_id, before_json, after_execution, audit_id),
        )

    result = upgrade_to_head(settings)
    assert result.revision == "0004_t20_snapshot_guards"
    assert result.backup is not None
    assert current_revision(settings.database_url) == "0004_t20_snapshot_guards"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """SELECT rule_execution_id, transaction_id, before_json, after_json,
                      batch_id, rule_version, batch_scope_sha256,
                      classification_decision_id, undone_by_decision_id
               FROM rule_executions WHERE rule_execution_id=?""",
            (execution_id,),
        ).fetchone()
        assert row is not None
        assert row[:4] == (execution_id, transaction_id, before_json, after_execution)
        assert row[4] and row[4] != execution_id
        assert row[5] == rule_version
        assert len(row[6]) == 64
        assert row[7] == decision_id and row[8] is None
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
