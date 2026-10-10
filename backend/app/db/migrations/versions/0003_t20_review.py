"""Ajoute des lots et des liens d'annulation append-only pour T20."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
from pathlib import Path

from alembic import op
from sqlalchemy.engine import Connection

revision = "0003_t20_review"
down_revision = "0002_t17_refund_safety"
branch_labels = None
depends_on = None
_BACKFILL_BOUNDARY = "-- MIGRATION_BACKFILL_BOUNDARY"


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
        raise RuntimeError("SQL de migration T20 incomplet")


def _decode_snapshot(raw: object) -> object | None:
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _rule_version(*snapshots: object) -> int | None:
    for raw in snapshots:
        value = _decode_snapshot(raw)
        if isinstance(value, dict):
            version = value.get("rule_version")
            if type(version) is int and version > 0:
                return version
    return None


def _allocation_signature(raw: object) -> tuple[tuple[object, ...], ...] | None:
    value = _decode_snapshot(raw) if isinstance(raw, str) else raw
    if isinstance(value, dict):
        value = value.get("allocations")
    if not isinstance(value, list):
        return None
    signature: list[tuple[object, ...]] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        signature.append(
            tuple(
                item.get(key)
                for key in (
                    "flow_kind",
                    "category_id",
                    "amount_cents",
                    "origin",
                    "beneficiary",
                    "cadence",
                    "flexibility",
                    "acquisition",
                )
            )
        )
    return tuple(sorted(signature, key=repr))


def _legacy_decision_id(
    connection: Connection, transaction_id: str, execution_after_json: object
) -> str | None:
    execution_signature = _allocation_signature(execution_after_json)
    if execution_signature is None:
        return None
    rows = connection.exec_driver_sql(
        """SELECT decision_id, after_json FROM classification_decisions
           WHERE transaction_id=? AND action IN ('confirm','edit','split')
             AND after_json IS NOT NULL
           ORDER BY created_at_utc DESC, decision_id DESC""",
        (transaction_id,),
    ).fetchall()
    for decision_id, after_json in rows:
        if _allocation_signature(after_json) == execution_signature:
            return str(decision_id)
    return None


def _backfill_legacy_executions(connection: Connection) -> None:
    """Keep old traces reversible individually; never infer a historical batch."""
    rows = connection.exec_driver_sql(
        """SELECT rule_execution_id, transaction_id, before_json, after_json
           FROM rule_executions ORDER BY executed_at_utc, rule_execution_id"""
    ).fetchall()
    for execution_id, transaction_id, before_json, after_json in rows:
        version = _rule_version(after_json, before_json)
        batch_id = str(uuid.uuid4())
        scope_digest = hashlib.sha256(f"legacy-singleton:{execution_id}".encode()).hexdigest()
        decision_id = _legacy_decision_id(connection, str(transaction_id), after_json)
        connection.exec_driver_sql(
            """UPDATE rule_executions
               SET batch_id=?, rule_version=?, batch_scope_sha256=?,
                   classification_decision_id=?
               WHERE rule_execution_id=?""",
            (batch_id, version, scope_digest, decision_id, execution_id),
        )


def upgrade() -> None:
    connection = op.get_bind()
    sql_path = Path(__file__).parents[1] / "sql" / "0003_t20_review.sql"
    sql = sql_path.read_text(encoding="utf-8")
    if sql.count(_BACKFILL_BOUNDARY) != 1:
        raise RuntimeError("frontière de backfill T20 absente ou dupliquée")
    schema_sql, indexes_and_guards_sql = sql.split(_BACKFILL_BOUNDARY, maxsplit=1)
    _execute_statements(connection, schema_sql)
    _backfill_legacy_executions(connection)
    _execute_statements(connection, indexes_and_guards_sql)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_verified_backup() -> None:
    if os.environ.get("COMPTES_ALLOW_DATA_LOSS_DOWNGRADE") != "1":
        raise RuntimeError(
            "downgrade T20 refusé; exige COMPTES_ALLOW_DATA_LOSS_DOWNGRADE=1 "
            "et une sauvegarde vérifiée"
        )
    raw_path = os.environ.get("COMPTES_DOWNGRADE_BACKUP")
    if not raw_path:
        raise RuntimeError("downgrade T20 exige COMPTES_DOWNGRADE_BACKUP")
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
        raise RuntimeError("downgrade T20 refusé : sauvegarde illisible") from error
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
        raise RuntimeError("downgrade T20 refusé : sauvegarde non vérifiée")


def downgrade() -> None:
    _require_verified_backup()
    connection = op.get_bind()
    for name in (
        "trg_rule_executions_insert_guard",
        "trg_rule_executions_batch_consistency",
        "trg_rule_executions_update_guard",
        "trg_rule_executions_delete_forbidden",
        "trg_classification_decisions_undo_guard",
        "trg_classification_decisions_delete_forbidden",
    ):
        connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
    for name in (
        "ux_rule_executions_batch_transaction",
        "ux_rule_executions_classification_decision",
        "ux_rule_executions_undo_decision",
        "ux_classification_decisions_undo_target",
        "ix_rule_executions_batch",
    ):
        connection.exec_driver_sql(f'DROP INDEX "{name}"')
    for column in (
        "undone_by_decision_id",
        "classification_decision_id",
        "batch_scope_sha256",
        "rule_version",
        "batch_id",
    ):
        connection.exec_driver_sql(f'ALTER TABLE rule_executions DROP COLUMN "{column}"')
    connection.exec_driver_sql(
        'ALTER TABLE classification_decisions DROP COLUMN "transaction_version"'
    )
