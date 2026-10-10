"""Protège les snapshots de décision et durcit les liens d'annulation T20."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

from alembic import op
from sqlalchemy.engine import Connection

revision = "0004_t20_snapshot_guards"
down_revision = "0003_t20_review"
branch_labels = None
depends_on = None


def _execute_statements(connection: Connection, sql: str) -> None:
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                connection.exec_driver_sql(statement)
            pending = ""
    if pending.strip():
        raise RuntimeError("SQL de migration T20-0004 incomplet")


def upgrade() -> None:
    sql_path = Path(__file__).parents[1] / "sql" / "0004_t20_snapshot_guards.sql"
    _execute_statements(op.get_bind(), sql_path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_verified_backup() -> None:
    if os.environ.get("COMPTES_ALLOW_DATA_LOSS_DOWNGRADE") != "1":
        raise RuntimeError(
            "downgrade T20-0004 refusé; exige COMPTES_ALLOW_DATA_LOSS_DOWNGRADE=1 "
            "et une sauvegarde vérifiée"
        )
    raw_path = os.environ.get("COMPTES_DOWNGRADE_BACKUP")
    if not raw_path:
        raise RuntimeError("downgrade T20-0004 exige COMPTES_DOWNGRADE_BACKUP")
    backup_path = Path(raw_path).expanduser()
    manifest_path = backup_path.with_suffix(".manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        backup_mode = stat.S_IMODE(backup_path.stat().st_mode)
        manifest_mode = stat.S_IMODE(manifest_path.stat().st_mode)
        with sqlite3.connect(f"{backup_path.resolve().as_uri()}?mode=ro", uri=True) as backup:
            integrity = backup.execute("PRAGMA integrity_check").fetchone()
            revision_table = backup.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
            ).fetchone()
            backup_revision = (
                None
                if revision_table is None
                else backup.execute("SELECT version_num FROM alembic_version").fetchone()
            )
    except (OSError, sqlite3.Error, ValueError, TypeError) as error:
        raise RuntimeError("downgrade T20-0004 refusé : sauvegarde illisible") from error
    if (
        backup_mode != 0o600
        or manifest_mode != 0o600
        or manifest.get("backup_file") != backup_path.name
        or manifest.get("source_revision") != revision
        or manifest.get("sha256") != _sha256(backup_path)
        or manifest.get("integrity_check") != "ok"
        or backup_revision != (revision,)
        or manifest.get("file_size_bytes") != backup_path.stat().st_size
        or integrity != ("ok",)
    ):
        raise RuntimeError("downgrade T20-0004 refusé : sauvegarde non vérifiée")


def downgrade() -> None:
    _require_verified_backup()
    connection = op.get_bind()
    connection.exec_driver_sql('DROP TRIGGER "trg_rule_executions_update_guard"')
    connection.exec_driver_sql('DROP TRIGGER "trg_classification_decisions_undo_guard"')
    connection.exec_driver_sql(
        'ALTER TABLE classification_decisions DROP COLUMN "before_json_sha256"'
    )
    connection.exec_driver_sql(
        'ALTER TABLE classification_decisions DROP COLUMN "after_json_sha256"'
    )
    _execute_statements(
        connection,
        """CREATE TRIGGER trg_rule_executions_update_guard
BEFORE UPDATE ON rule_executions
WHEN OLD.undone_by_decision_id IS NOT NULL OR
     NEW.undone_by_decision_id IS NULL OR
     NEW.rule_execution_id IS NOT OLD.rule_execution_id OR
     NEW.rule_id IS NOT OLD.rule_id OR
     NEW.transaction_id IS NOT OLD.transaction_id OR
     NEW.outcome IS NOT OLD.outcome OR
     NEW.before_json IS NOT OLD.before_json OR
     NEW.after_json IS NOT OLD.after_json OR
     NEW.dry_run IS NOT OLD.dry_run OR
     NEW.audit_event_id IS NOT OLD.audit_event_id OR
     NEW.executed_at_utc IS NOT OLD.executed_at_utc OR
     NEW.batch_id IS NOT OLD.batch_id OR
     NEW.rule_version IS NOT OLD.rule_version OR
     NEW.batch_scope_sha256 IS NOT OLD.batch_scope_sha256 OR
     NEW.classification_decision_id IS NOT OLD.classification_decision_id OR
     NOT EXISTS (
         SELECT 1 FROM classification_decisions d
         WHERE d.decision_id = NEW.undone_by_decision_id
           AND d.transaction_id = NEW.transaction_id
           AND d.action = 'undo'
     )
BEGIN
    SELECT RAISE(ABORT, 'rule_execution_immutable');
END;

CREATE TRIGGER trg_classification_decisions_undo_guard
BEFORE UPDATE ON classification_decisions
WHEN OLD.undone_by_decision_id IS NOT NULL OR
     NEW.undone_by_decision_id IS NULL OR
     NEW.decision_id IS NOT OLD.decision_id OR
     NEW.transaction_id IS NOT OLD.transaction_id OR
     NEW.suggestion_id IS NOT OLD.suggestion_id OR
     NEW.action IS NOT OLD.action OR
     NEW.actor IS NOT OLD.actor OR
     NEW.before_json IS NOT OLD.before_json OR
     NEW.after_json IS NOT OLD.after_json OR
     NEW.transaction_version IS NOT OLD.transaction_version OR
     NEW.created_at_utc IS NOT OLD.created_at_utc OR
     NOT EXISTS (
         SELECT 1 FROM classification_decisions d
         WHERE d.decision_id = NEW.undone_by_decision_id
           AND d.transaction_id = NEW.transaction_id
           AND d.action = 'undo'
     )
BEGIN
    SELECT RAISE(ABORT, 'classification_decision_undo_link_invalid');
END;""",
    )
