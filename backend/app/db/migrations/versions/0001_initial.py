"""Crée à l'identique le schéma SQLite v1 gelé au gate G0."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

from alembic import op
from sqlalchemy.engine import Connection

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None
OWNED_TABLES = (
    "accounts",
    "import_files",
    "import_account_sections",
    "balance_checks",
    "import_rejections",
    "payees",
    "payee_aliases",
    "categories",
    "category_flow_kinds",
    "transactions",
    "source_rows",
    "import_reconciliation_candidates",
    "allocations",
    "classification_suggestions",
    "classification_decisions",
    "rules",
    "rule_executions",
    "transaction_links",
    "audit_events",
    "app_settings",
    "llm_runs",
    "budgets",
    "budget_accounts",
    "insights",
    "insight_facts",
    "insight_fact_transactions",
    "backup_manifests",
    "idempotency_records",
)


def _execute_schema(connection: Connection, sql: str) -> None:
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                connection.exec_driver_sql(statement)
            pending = ""
    if pending.strip():
        raise RuntimeError("Le SQL de migration initiale se termine par une instruction incomplète")


def upgrade() -> None:
    sql_path = Path(__file__).parents[1] / "sql" / "0001_initial.sql"
    _execute_schema(op.get_bind(), sql_path.read_text(encoding="utf-8"))


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_explicit_backup(expected_revision: str) -> None:
    raw_path = os.environ.get("COMPTES_DOWNGRADE_BACKUP")
    if not raw_path:
        raise RuntimeError(
            "downgrade avec données exige COMPTES_DOWNGRADE_BACKUP "
            "pointant vers une sauvegarde vérifiée"
        )
    backup_path = Path(raw_path).expanduser()
    manifest_path = backup_path.with_suffix(".manifest.json")
    try:
        backup_mode = stat.S_IMODE(backup_path.stat().st_mode)
        manifest_mode = stat.S_IMODE(manifest_path.stat().st_mode)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        digest = _sha256_file(backup_path)
        backup = sqlite3.connect(f"{backup_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            backup.execute("PRAGMA foreign_keys=ON")
            backup.execute("PRAGMA recursive_triggers=ON")
            integrity = backup.execute("PRAGMA integrity_check").fetchone()
            version_table = backup.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
            ).fetchone()
            backup_revision = (
                None
                if version_table is None
                else backup.execute("SELECT version_num FROM alembic_version").fetchone()
            )
        finally:
            backup.close()
    except (OSError, sqlite3.Error, ValueError, TypeError) as error:
        raise RuntimeError(
            "downgrade refusé : sauvegarde explicite absente ou illisible"
        ) from error
    if (
        backup_mode != 0o600
        or manifest_mode != 0o600
        or manifest.get("backup_file") != backup_path.name
        or manifest.get("source_revision") != expected_revision
        or manifest.get("sha256") != digest
        or manifest.get("integrity_check") != "ok"
        or backup_revision != (expected_revision,)
        or manifest.get("file_size_bytes") != backup_path.stat().st_size
        or integrity != ("ok",)
    ):
        raise RuntimeError(
            "downgrade refusé : sauvegarde explicite non vérifiée ou permissions incorrectes"
        )


def downgrade() -> None:
    connection = op.get_bind()
    tables = [
        table
        for table in OWNED_TABLES
        if connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).first()
    ]
    populated = [
        table
        for table in tables
        if connection.exec_driver_sql(f"SELECT 1 FROM {_quote(table)} LIMIT 1").first() is not None
    ]
    if populated:
        if os.environ.get("COMPTES_ALLOW_DATA_LOSS_DOWNGRADE") != "1":
            raise RuntimeError(
                "downgrade refusé : des données métier existent; créez et vérifiez une sauvegarde, "
                "puis définissez explicitement COMPTES_ALLOW_DATA_LOSS_DOWNGRADE=1 et "
                "COMPTES_DOWNGRADE_BACKUP"
            )
        current_revision: str = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        _verify_explicit_backup(str(current_revision))

    triggers = [
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name IN (" + ", ".join("?" for _ in tables) + ")",
            tuple(tables),
        )
    ]
    for trigger in triggers:
        connection.exec_driver_sql(f"DROP TRIGGER {_quote(trigger)}")

    remaining = set(tables)
    dependencies: dict[str, set[str]] = {}
    for table in remaining:
        references = {
            row[2]
            for row in connection.exec_driver_sql(f"PRAGMA foreign_key_list({_quote(table)})")
            if row[2] in remaining and row[2] != table
        }
        dependencies[table] = references

    while remaining:
        # Retire les tables qui ne sont référencées par aucune autre table restante.
        leaves = sorted(
            table
            for table in remaining
            if not any(table in dependencies[child] for child in remaining if child != table)
        )
        if not leaves:
            # Les éventuels cycles n'ont pas de données à préserver après le garde ci-dessus.
            if populated:
                raise RuntimeError("downgrade refusé : dépendances cycliques malgré la sauvegarde")
            leaves = [sorted(remaining)[0]]
        for table in leaves:
            connection.exec_driver_sql(f"DROP TABLE {_quote(table)}")
            remaining.remove(table)
