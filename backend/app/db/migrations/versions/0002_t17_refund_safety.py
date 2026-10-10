"""Ajoute les parts bornées des remboursements et leurs garde-fous SQL.

La révision 0001 est gelée. Cette révision reconstruit classification_decisions
pour ajouter l'action ``clear`` et installe les garanties T17 sur les liens.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections import defaultdict
from pathlib import Path

from alembic import op
from sqlalchemy.engine import Connection

revision = "0002_t17_refund_safety"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_BACKFILL_BOUNDARY = "-- MIGRATION_BACKFILL_BOUNDARY"
_REPLACED_TRIGGERS = (
    "trg_allocations_referenced_links_immutable",
    "trg_refund_links_confirmed_direction_insert",
    "trg_refund_links_confirmed_direction_update",
    "trg_transaction_link_supersession_replacement",
)
_ADDED_TRIGGERS = (
    "trg_refund_link_allocation_parts_delete_forbidden",
    "trg_refund_link_allocation_parts_insert_guard",
    "trg_refund_link_allocation_parts_update_forbidden",
    "trg_refund_links_confirmed_allocation_caps_insert",
    "trg_refund_links_confirmed_allocation_caps_update",
    "trg_refund_links_confirmed_attribution_insert",
    "trg_refund_links_confirmed_attribution_update",
    "trg_refund_links_status_transition",
    "trg_transaction_link_supersession_replacement_confirmation",
    "trg_refund_links_initial_status",
)


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
        raise RuntimeError("SQL de migration incomplet")


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise RuntimeError(f"{field} doit rester un entier SQLite")
    return value


def _bounded_prorata(
    amount_cents: int, weights: dict[str, int], capacities: dict[str, int]
) -> dict[str, int]:
    """Largest-remainder prorata, integer only, with iterative capacity caps."""
    if amount_cents <= 0 or amount_cents > sum(capacities.values()):
        raise RuntimeError("REFUND_PRORATA_INFEASIBLE: capacité restante insuffisante")
    result = {category_id: 0 for category_id in weights}
    remaining = amount_cents
    active = {key for key in weights if capacities[key] > 0}
    while remaining and active:
        denominator = sum(weights[key] for key in active)
        capped = {key for key in active if remaining * weights[key] > capacities[key] * denominator}
        if capped:
            for key in sorted(capped):
                part = capacities[key]
                result[key] = part
                remaining -= part
                active.remove(key)
            continue
        floors: dict[str, int] = {}
        remainders: dict[str, int] = {}
        for key in active:
            floors[key], remainders[key] = divmod(remaining * weights[key], denominator)
        residual = remaining - sum(floors.values())
        result.update(floors)
        recipients = sorted(
            (key for key in active if floors[key] < capacities[key]),
            key=lambda key: (-remainders[key], key),
        )
        if residual > len(recipients):
            raise RuntimeError("REFUND_PRORATA_INFEASIBLE: résidu entier non attribuable")
        for key in recipients[:residual]:
            result[key] += 1
        remaining = 0
    if remaining or sum(result.values()) != amount_cents:
        raise RuntimeError("REFUND_PRORATA_INFEASIBLE: résidu entier non attribuable")
    if any(part < 0 or part > capacities[key] for key, part in result.items()):
        raise RuntimeError("REFUND_PRORATA_INFEASIBLE: plafond de catégorie dépassé")
    return result


def _backfill_confirmed_prorata(connection: Connection) -> None:
    """Freeze current active confirmed prorata links in stable creation order.

    Superseded links remain historical and are excluded from accounting. For an
    active pre-0002 prorata link, an unresolvable or insufficient current
    capacity stops the migration instead of inventing or losing cents.
    """
    links = connection.exec_driver_sql(
        """SELECT transaction_link_id, target_transaction_id, amount_cents
           FROM transaction_links
           WHERE link_kind='refund_of' AND status='confirmed'
             AND target_allocation_id IS NULL
             AND superseded_by_transaction_link_id IS NULL
           ORDER BY created_at_utc, transaction_link_id"""
    ).fetchall()
    for link_id, target_id, amount in links:
        allocation_rows = connection.exec_driver_sql(
            """SELECT allocation_id, category_id, amount_cents
               FROM allocations
               WHERE transaction_id=? AND flow_kind='expense' AND amount_cents < 0
                 AND superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL
               ORDER BY allocation_id""",
            (target_id,),
        ).fetchall()
        if not allocation_rows:
            raise RuntimeError(
                "REFUND_PRORATA_INFEASIBLE: lien confirmé sans allocation expense active"
            )
        remaining_by_allocation: dict[str, int] = {}
        gross_by_category: dict[str, int] = defaultdict(int)
        remaining_by_category: dict[str, int] = defaultdict(int)
        for allocation_id, category_id, gross_signed in allocation_rows:
            gross = -_integer(gross_signed, "allocation amount_cents")
            direct_used = _integer(
                connection.exec_driver_sql(
                    """SELECT COALESCE(sum(amount_cents),0) FROM transaction_links
                       WHERE target_allocation_id=? AND link_kind='refund_of'
                         AND status='confirmed'
                         AND superseded_by_transaction_link_id IS NULL""",
                    (allocation_id,),
                ).scalar_one(),
                "lien direct confirmé",
            )
            parts_used = _integer(
                connection.exec_driver_sql(
                    """SELECT COALESCE(sum(p.amount_cents),0)
                       FROM refund_link_allocation_parts p
                       JOIN transaction_links l ON l.transaction_link_id=p.transaction_link_id
                       WHERE p.allocation_id=? AND l.link_kind='refund_of'
                         AND l.status='confirmed'
                         AND l.superseded_by_transaction_link_id IS NULL""",
                    (allocation_id,),
                ).scalar_one(),
                "parts de remboursements confirmés",
            )
            remaining = max(0, gross - direct_used - parts_used)
            remaining_by_allocation[str(allocation_id)] = remaining
            gross_by_category[str(category_id)] += gross
            remaining_by_category[str(category_id)] += remaining
        category_parts = _bounded_prorata(
            _integer(amount, "montant du lien"),
            dict(gross_by_category),
            dict(remaining_by_category),
        )
        allocations_by_category: dict[str, list[str]] = defaultdict(list)
        for allocation_id, category_id, _gross in allocation_rows:
            allocations_by_category[str(category_id)].append(str(allocation_id))
        for category_id, part_total in category_parts.items():
            left = part_total
            for allocation_id in sorted(allocations_by_category[category_id]):
                part = min(left, remaining_by_allocation[allocation_id])
                if part:
                    connection.exec_driver_sql(
                        """INSERT INTO refund_link_allocation_parts
                           (transaction_link_id, allocation_id, amount_cents) VALUES (?, ?, ?)""",
                        (link_id, allocation_id, part),
                    )
                    left -= part
                if left == 0:
                    break
            if left:
                raise RuntimeError("REFUND_PRORATA_INFEASIBLE: plafond d'allocation insuffisant")


def _schema_objects(sql_path: Path) -> dict[tuple[str, str], str]:
    database = sqlite3.connect(":memory:")
    try:
        database.executescript(sql_path.read_text(encoding="utf-8"))
        return {
            (str(kind), str(name)): str(sql)
            for kind, name, sql in database.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
            if sql is not None
        }
    finally:
        database.close()


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
            "downgrade 0002 exige COMPTES_DOWNGRADE_BACKUP vers une sauvegarde vérifiée"
        )
    path = Path(raw_path).expanduser()
    manifest_path = path.with_suffix(".manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        digest = _sha256_file(path)
        mode = stat.S_IMODE(path.stat().st_mode)
        manifest_mode = stat.S_IMODE(manifest_path.stat().st_mode)
        backup = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            integrity = backup.execute("PRAGMA integrity_check").fetchone()
            version = backup.execute("SELECT version_num FROM alembic_version").fetchone()
        finally:
            backup.close()
    except (OSError, sqlite3.Error, ValueError, TypeError) as error:
        raise RuntimeError("downgrade 0002 refusé : sauvegarde illisible") from error
    if (
        mode != 0o600
        or manifest_mode != 0o600
        or manifest.get("backup_file") != path.name
        or manifest.get("source_revision") != expected_revision
        or manifest.get("sha256") != digest
        or manifest.get("integrity_check") != "ok"
        or manifest.get("file_size_bytes") != path.stat().st_size
        or integrity != ("ok",)
        or version != (expected_revision,)
    ):
        raise RuntimeError("downgrade 0002 refusé : sauvegarde non vérifiée ou mal protégée")


def upgrade() -> None:
    connection = op.get_bind()
    sql_path = Path(__file__).parents[1] / "sql" / "0002_t17_refund_safety.sql"
    sql = sql_path.read_text(encoding="utf-8")
    if sql.count(_BACKFILL_BOUNDARY) != 1:
        raise RuntimeError("frontière de backfill de la migration 0002 invalide")
    before_backfill, after_backfill = sql.split(_BACKFILL_BOUNDARY, maxsplit=1)
    _execute_statements(connection, before_backfill)
    _backfill_confirmed_prorata(connection)
    _execute_statements(connection, after_backfill)


def downgrade() -> None:
    if os.environ.get("COMPTES_ALLOW_DATA_LOSS_DOWNGRADE") != "1":
        raise RuntimeError(
            "downgrade 0002 refusé : définir explicitement "
            "COMPTES_ALLOW_DATA_LOSS_DOWNGRADE=1 avec une sauvegarde vérifiée"
        )
    _verify_explicit_backup(revision)
    connection = op.get_bind()
    reference_path = Path(__file__).parents[1] / "sql" / "0001_initial.sql"
    original = _schema_objects(reference_path)
    for trigger in (*_ADDED_TRIGGERS, *_REPLACED_TRIGGERS):
        connection.exec_driver_sql(f"DROP TRIGGER {_quote(trigger)}")
    connection.exec_driver_sql('DROP TRIGGER "trg_classification_decisions_version_increment"')
    connection.exec_driver_sql('DROP INDEX "ix_classification_decisions_transaction"')
    connection.exec_driver_sql('DROP INDEX "ix_refund_link_allocation_parts_allocation"')
    connection.exec_driver_sql("DROP TABLE refund_link_allocation_parts")

    columns = (
        "decision_id",
        "transaction_id",
        "suggestion_id",
        "action",
        "actor",
        "before_json",
        "after_json",
        "undone_by_decision_id",
        "version",
        "created_at_utc",
    )
    quoted_columns = ", ".join(_quote(column) for column in columns)
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    connection.exec_driver_sql(
        "ALTER TABLE classification_decisions RENAME TO classification_decisions_0002"
    )
    connection.exec_driver_sql(original[("table", "classification_decisions")])
    connection.exec_driver_sql(
        f"""INSERT INTO classification_decisions ({quoted_columns})
            SELECT decision_id, transaction_id, suggestion_id,
                   CASE WHEN action='clear' THEN 'undo' ELSE action END,
                   actor, before_json, after_json, undone_by_decision_id,
                   version, created_at_utc
            FROM classification_decisions_0002"""
    )
    connection.exec_driver_sql("DROP TABLE classification_decisions_0002")
    connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
    connection.exec_driver_sql(original[("index", "ix_classification_decisions_transaction")])
    decision_version_trigger = original[
        ("trigger", "trg_classification_decisions_version_increment")
    ]
    connection.exec_driver_sql(decision_version_trigger)
    for trigger in _REPLACED_TRIGGERS:
        connection.exec_driver_sql(original[("trigger", trigger)])
