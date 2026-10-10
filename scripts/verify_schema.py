#!/usr/bin/env python3
"""Vérification contractuelle SQLite T02, sans dépendance externe ni donnée réelle."""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "docs" / "examples" / "schema.sql"
FROZEN_0001_HASHES = {
    "backend/app/db/migrations/sql/0001_initial.sql": (
        "b3ee949ad9aedddd6bb6c07ac0b1c1cad41709d8da0d861df676e1db7c49b257"
    ),
    "backend/app/db/migrations/versions/0001_initial.py": (
        "3d5fa08337a46a336f65e71b48fcdedd8866dca2ebf9e6a093905190c15e4d92"
    ),
}
ACCOUNT_ID = "00000000-0000-4000-8000-000000000001"
ACCOUNT_B_ID = "00000000-0000-4000-8000-000000000002"
UUID_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "accounts": ("account_id",),
    "allocations": ("allocation_id",),
    "audit_events": ("audit_event_id",),
    "backup_manifests": ("backup_manifest_id",),
    "balance_checks": ("balance_check_id",),
    "budget_accounts": ("budget_id", "account_id"),
    "budgets": ("budget_id",),
    "classification_decisions": ("decision_id",),
    "classification_suggestions": ("suggestion_id",),
    "import_account_sections": ("import_file_id",),
    "import_files": ("import_file_id",),
    "import_reconciliation_candidates": ("reconciliation_candidate_id",),
    "import_rejections": ("import_rejection_id",),
    "insight_fact_transactions": ("insight_fact_id", "transaction_id"),
    "insight_facts": ("insight_fact_id",),
    "insights": ("insight_id",),
    "llm_runs": ("llm_run_id",),
    "payee_aliases": ("payee_alias_id",),
    "payees": ("payee_id",),
    "refund_link_allocation_parts": ("transaction_link_id", "allocation_id"),
    "rule_executions": ("rule_execution_id",),
    "rules": ("rule_id",),
    "source_rows": ("source_row_id",),
    "transaction_links": ("transaction_link_id",),
    "transactions": ("transaction_id",),
}


class VerificationError(RuntimeError):
    pass


def new_id() -> str:
    return str(uuid.uuid4())


def open_connection(
    database: str | Path = ":memory:",
    schema_path: Path | None = None,
    *,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(database), isolation_level=None, timeout=5, check_same_thread=check_same_thread
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    if schema_path is not None:
        connection.executescript(schema_path.read_text(encoding="utf-8"))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    recursive_triggers = connection.execute("PRAGMA recursive_triggers").fetchone()[0]
    if foreign_keys != 1 or recursive_triggers != 1:
        connection.close()
        raise VerificationError("chaque connexion doit activer foreign_keys et recursive_triggers")
    return connection


def connect(schema_path: Path) -> sqlite3.Connection:
    return open_connection(":memory:", schema_path)


def report(label: str) -> None:
    print(f"OK {label}")


def expect_integrity_error(
    label: str,
    action: Callable[[], object],
    expected: str | tuple[str, ...] | None = None,
) -> None:
    try:
        action()
    except sqlite3.IntegrityError as error:
        expected_values = (expected,) if isinstance(expected, str) else expected
        if expected is not None and not any(
            candidate in str(error) for candidate in expected_values or ()
        ):
            raise VerificationError(
                f"{label}: erreur SQLite inattendue {str(error)!r}, attendu {expected!r}"
            ) from error
        report(label)
        return
    raise VerificationError(f"contrainte absente ou non appliquée : {label}")


def expect_python_rejection(label: str, action: Callable[[], object]) -> None:
    try:
        action()
    except (TypeError, ValueError):
        report(label)
        return
    raise VerificationError(f"validation Python absente : {label}")


def require_cents(value: object) -> int:
    """Garde du futur service : bool, float et texte ne sont pas des centimes entiers."""
    if type(value) is not int:
        raise TypeError("un montant doit être un int Python strict")
    return value


def insert_account(
    connection: sqlite3.Connection,
    account_id: str = ACCOUNT_ID,
    account_role: str = "current",
) -> None:
    connection.execute(
        "INSERT INTO accounts(account_id, alias, account_role) VALUES (?, ?, ?)",
        (account_id, "Compte synthétique", account_role),
    )


def insert_import(
    connection: sqlite3.Connection,
    *,
    digest: str | None = None,
    status: str = "previewed",
    duplicate_of: str | None = None,
    archive_relpath: str | None = None,
    archive_storage_mode: str | None = None,
    archive_permissions_octal: str | None = None,
    received_on: str = "2026-03-31",
    generation_date: str | None = None,
    generation_date_source: str = "header_download",
    parsed_operation_count: int = 0,
) -> str:
    import_id = new_id()
    digest = digest or hashlib.sha256(import_id.encode("ascii")).hexdigest()
    if status == "no_op":
        if (
            archive_relpath is None
            and archive_storage_mode is None
            and archive_permissions_octal is None
        ):
            pass
    else:
        archive_relpath = archive_relpath or f"data/originals/{import_id}.bin"
        archive_storage_mode = archive_storage_mode or "raw"
        archive_permissions_octal = archive_permissions_octal or "0600"
    generation_date = generation_date or received_on
    connection.execute(
        """INSERT INTO import_files
           (import_file_id, file_sha256, archive_relpath, archive_storage_mode,
            archive_permissions_octal, file_size_bytes, received_on, generation_date,
            generation_date_source, parsed_operation_count, status, duplicate_of_import_file_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            import_id,
            digest,
            archive_relpath,
            archive_storage_mode,
            archive_permissions_octal,
            128,
            received_on,
            generation_date,
            generation_date_source,
            parsed_operation_count,
            status,
            duplicate_of,
        ),
    )
    return import_id


def complete_through(coverage_through: str, generated_on: str) -> str:
    through = date.fromisoformat(coverage_through)
    generation = date.fromisoformat(generated_on)
    return min(through, generation - timedelta(days=1)).isoformat()


def insert_section(
    connection: sqlite3.Connection,
    import_id: str,
    account_id: str | None = ACCOUNT_ID,
    *,
    section_index: int = 1,
    coverage_from: str | None = "2026-03-01",
    coverage_through: str | None = "2026-03-31",
    complete_through_value: str | None = None,
    balance_as_of: str | None = None,
    header_balance_cents: int | None = None,
) -> None:
    generation = connection.execute(
        "SELECT generation_date FROM import_files WHERE import_file_id = ?", (import_id,)
    ).fetchone()
    if generation is None:
        raise VerificationError("section sans fichier parent")
    if complete_through_value is None and coverage_through is not None:
        complete_through_value = complete_through(coverage_through, generation[0])
    connection.execute(
        """INSERT INTO import_account_sections
           (import_file_id, section_index, account_id, coverage_from, coverage_through, complete_through,
            balance_as_of, header_balance_cents)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            import_id,
            section_index,
            account_id,
            coverage_from,
            coverage_through,
            complete_through_value,
            balance_as_of,
            header_balance_cents,
        ),
    )


Identity = tuple[str, str, int, str]


def identity_digest(identity: Identity) -> str:
    account_id, booked_on, amount_cents, identity_label = identity
    parts = (account_id, booked_on, str(amount_cents), identity_label)
    framed = b"".join(
        len(part.encode("utf-8")).to_bytes(4, "big") + part.encode("utf-8") for part in parts
    )
    return hashlib.sha256(framed).hexdigest()


def insert_transaction(
    connection: sqlite3.Connection,
    *,
    identity: Identity,
    occurrence_rank: int,
    transaction_id: str | None = None,
    amount_sql: str | None = None,
) -> str:
    account_id, booked_on, amount_cents, identity_label = identity
    transaction_id = transaction_id or new_id()
    if amount_sql is None:
        amount = require_cents(amount_cents)
        sql = "?"
    else:
        # Uniquement pour les essais qui contournent volontairement le binder strict.
        amount = None
        sql = amount_sql
    parameters = (
        (
            transaction_id,
            account_id,
            booked_on,
            amount,
            identity_label,
            identity_label,
            identity_digest(identity),
            occurrence_rank,
        )
        if amount_sql is None
        else (
            transaction_id,
            account_id,
            booked_on,
            identity_label,
            identity_label,
            identity_digest(identity),
            occurrence_rank,
        )
    )
    connection.execute(
        f"""INSERT INTO transactions
            (transaction_id, account_id, booked_on, amount_cents, raw_label,
             identity_label, identity_key_sha256, occurrence_rank)
            VALUES (?, ?, ?, {sql}, ?, ?, ?, ?)""",
        parameters,
    )
    return transaction_id


def insert_source(
    connection: sqlite3.Connection,
    import_id: str,
    transaction_id: str,
    logical_row_idx: int,
    *,
    section_index: int = 1,
    source_row_id: str | None = None,
    identity_key_sha256: str | None = None,
) -> str:
    source_row_id = source_row_id or new_id()
    row_digest = hashlib.sha256(f"synthetic-row:{import_id}:{logical_row_idx}".encode()).hexdigest()
    if identity_key_sha256 is None:
        found = connection.execute(
            "SELECT identity_key_sha256 FROM transactions WHERE transaction_id=?", (transaction_id,)
        ).fetchone()
        identity_key_sha256 = found[0] if found else "0" * 64
    connection.execute(
        """INSERT INTO source_rows
           (source_row_id, import_file_id, section_index, logical_row_idx, row_sha256,
            identity_key_sha256, transaction_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            source_row_id,
            import_id,
            section_index,
            logical_row_idx,
            row_digest,
            identity_key_sha256,
            transaction_id,
        ),
    )
    return source_row_id


def commit_import(connection: sqlite3.Connection, import_id: str) -> None:
    connection.execute(
        "UPDATE import_files SET status = 'committed', finished_at_utc = strftime('%Y-%m-%dT%H:%M:%fZ','now'), version=version+1 WHERE import_file_id = ?",
        (import_id,),
    )


def create_committed_empty(connection: sqlite3.Connection, digest: str | None = None) -> str:
    import_id = insert_import(connection, digest=digest)
    insert_section(connection, import_id)
    commit_import(connection, import_id)
    return import_id


def add_category(connection: sqlite3.Connection, category_id: str, flow_kind: str) -> None:
    connection.execute(
        "INSERT INTO categories(category_id, label, taxonomy_version) VALUES (?, ?, 1)",
        (category_id, category_id),
    )
    connection.execute(
        "INSERT INTO category_flow_kinds(category_id, flow_kind) VALUES (?, ?)",
        (category_id, flow_kind),
    )


def seed_financial_categories(connection: sqlite3.Connection) -> None:
    mappings = (
        ("groceries", "expense"),
        ("income_refund", "refund"),
        ("income_salary_a", "income_recurring"),
        ("income_other_test", "income_other"),
        ("flow_savings_out", "transfer_savings"),
        ("flow_savings_in", "transfer_savings"),
        ("flow_owned_transfer", "transfer_owned"),
        ("flow_personal_a", "allocation_personal"),
        ("flow_loan_disbursement", "loan_disbursement"),
        ("flow_loan_principal", "loan_repayment"),
        ("flow_capital_improvement", "capital_event"),
        ("flow_cash_unallocated", "cash_withdrawal_pending"),
        ("flow_intermediary_unresolved", "intermediary_pending"),
        ("flow_reconciliation", "reconciliation"),
    )
    for category_id, flow_kind in mappings:
        add_category(connection, category_id, flow_kind)


def insert_allocation(
    connection: sqlite3.Connection,
    transaction_id: str,
    *,
    category_id: str,
    flow_kind: str,
    amount_cents: object,
    allocation_id: str | None = None,
) -> str:
    amount = require_cents(amount_cents)
    allocation_id = allocation_id or new_id()
    connection.execute(
        """INSERT INTO allocations
           (allocation_id, transaction_id, category_id, flow_kind, amount_cents, origin)
           VALUES (?, ?, ?, ?, ?, 'manual')""",
        (allocation_id, transaction_id, category_id, flow_kind, amount),
    )
    return allocation_id


def insert_classification_decision(
    connection: sqlite3.Connection, transaction_id: str, decision_id: str | None = None
) -> str:
    decision_id = decision_id or new_id()
    connection.execute(
        """INSERT INTO classification_decisions
           (decision_id, transaction_id, action, actor, before_json, after_json)
           VALUES (?, ?, 'undo', 'verify_schema', '{}', '{}')""",
        (decision_id, transaction_id),
    )
    return decision_id


def _schema_prorated_parts(
    connection: sqlite3.Connection, target_id: str, amount_cents: int
) -> list[tuple[str, int]]:
    rows = list(
        connection.execute(
            """SELECT allocation_id, category_id, -amount_cents
               FROM allocations WHERE transaction_id=? AND flow_kind='expense'
                 AND superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL
               ORDER BY category_id, allocation_id""",
            (target_id,),
        )
    )
    category_weights: dict[str, int] = defaultdict(int)
    for _allocation_id, category_id, capacity in rows:
        category_weights[str(category_id)] += int(capacity)
    denominator = sum(category_weights.values())
    ordered_categories = sorted(
        category_weights, key=lambda category_id: (-category_weights[category_id], category_id)
    )
    category_parts: dict[str, int] = {}
    remaining = amount_cents
    for index, category_id in enumerate(ordered_categories):
        if index == len(ordered_categories) - 1:
            part = remaining
        else:
            weight = category_weights[category_id]
            part = (2 * amount_cents * weight + denominator) // (2 * denominator)
            remaining -= part
        category_parts[category_id] = part

    result: list[tuple[str, int]] = []
    for category_id, part in category_parts.items():
        category_rows = sorted(
            (
                (str(allocation_id), int(capacity))
                for allocation_id, row_category, capacity in rows
                if row_category == category_id
            ),
            key=lambda item: item[0],
        )
        if part > sum(capacity for _allocation_id, capacity in category_rows):
            raise VerificationError("répartition proratisée dépasse une capacité synthétique")
        unassigned = part
        for allocation_id, capacity in category_rows:
            allocated = min(unassigned, capacity)
            if allocated:
                result.append((allocation_id, allocated))
                unassigned -= allocated
            if unassigned == 0:
                break
        if unassigned:
            raise VerificationError("parts proratisées synthétiques incomplètes")
    return result


def insert_refund_link(
    connection: sqlite3.Connection,
    source_id: str,
    target_id: str,
    amount_cents: object,
    *,
    status: str = "confirmed",
    link_id: str | None = None,
    source_allocation_id: str | None = None,
    target_allocation_id: str | None = None,
) -> str:
    amount = require_cents(amount_cents)
    link_id = link_id or new_id()
    if source_allocation_id is None:
        row = connection.execute(
            """SELECT allocation_id FROM allocations
               WHERE transaction_id=? AND flow_kind='refund'
                 AND superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL
               ORDER BY allocation_id LIMIT 1""",
            (source_id,),
        ).fetchone()
        source_allocation_id = None if row is None else str(row[0])
    materialize_prorata = status == "confirmed" and target_allocation_id is None
    insert_status = "proposed" if status in ("confirmed", "rejected") else status
    connection.execute("SAVEPOINT insert_refund_link")
    try:
        connection.execute(
            """INSERT INTO transaction_links
               (transaction_link_id, source_transaction_id, target_transaction_id,
                link_kind, amount_cents, source_allocation_id, target_allocation_id, status)
               VALUES (?, ?, ?, 'refund_of', ?, ?, ?, ?)""",
            (
                link_id,
                source_id,
                target_id,
                amount,
                source_allocation_id,
                target_allocation_id,
                insert_status,
            ),
        )
        if materialize_prorata:
            connection.executemany(
                """INSERT INTO refund_link_allocation_parts
                   (transaction_link_id, allocation_id, amount_cents) VALUES (?, ?, ?)""",
                [
                    (link_id, allocation_id, part_cents)
                    for allocation_id, part_cents in _schema_prorated_parts(
                        connection, target_id, amount
                    )
                ],
            )
        if status in ("confirmed", "rejected"):
            connection.execute(
                "UPDATE transaction_links SET status=?, version=version+1 "
                "WHERE transaction_link_id=?",
                (status, link_id),
            )
        connection.execute("RELEASE SAVEPOINT insert_refund_link")
    except BaseException:
        connection.execute("ROLLBACK TO SAVEPOINT insert_refund_link")
        connection.execute("RELEASE SAVEPOINT insert_refund_link")
        raise
    return link_id


def verify_connection_pragmas_and_replace(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise VerificationError("SEC-06 foreign_keys non actif sur connexion 1")
        if connection.execute("PRAGMA recursive_triggers").fetchone()[0] != 1:
            raise VerificationError("SEC-06 recursive_triggers non actif sur connexion 1")
        # Une seconde connexion indépendante doit appliquer les mêmes PRAGMA.
        second = connect(schema_path)
        try:
            if second.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise VerificationError("SEC-06 foreign_keys non actif sur connexion 2")
            if second.execute("PRAGMA recursive_triggers").fetchone()[0] != 1:
                raise VerificationError("SEC-06 recursive_triggers non actif sur connexion 2")
        finally:
            second.close()
        insert_account(connection)
        iid = insert_import(connection, parsed_operation_count=1)
        insert_section(connection, iid)
        unprovenanced = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-11", -1234, "Sans source synthétique"),
            occurrence_rank=1,
        )
        expect_integrity_error(
            "SEC-06 DELETE d'une transaction sans provenance rejeté par trigger",
            lambda: connection.execute(
                "DELETE FROM transactions WHERE transaction_id = ?", (unprovenanced,)
            ),
            "transaction_source_immutable",
        )
        expect_integrity_error(
            "SEC-06 INSERT OR REPLACE sur transaction sans provenance rejeté",
            lambda: connection.execute(
                "INSERT OR REPLACE INTO transactions(transaction_id, account_id, booked_on, amount_cents, raw_label, identity_label, identity_key_sha256, occurrence_rank) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (unprovenanced, ACCOUNT_ID, "2026-03-11", -1234, "Autre", "Autre", "0" * 64),
            ),
            "transaction_source_immutable",
        )
        tid = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-12", -1234, "Paiement synthétique"),
            occurrence_rank=1,
        )
        sid = insert_source(connection, iid, tid, 1)
        expect_integrity_error(
            "ADR-019.b UPDATE d'un champ source transaction rejeté",
            lambda: connection.execute(
                "UPDATE transactions SET raw_label='Modifié', version=version+1 WHERE transaction_id = ?",
                (tid,),
            ),
            "transaction_source_immutable",
        )
        expect_integrity_error(
            "SEC-06 DELETE d'une transaction source rejeté",
            lambda: connection.execute("DELETE FROM transactions WHERE transaction_id = ?", (tid,)),
            "transaction_source_immutable",
        )
        expect_integrity_error(
            "ADR-019.b UPDATE d'une source_row rejeté",
            lambda: connection.execute(
                "UPDATE source_rows SET row_sha256=? WHERE source_row_id=?", ("1" * 64, sid)
            ),
            "source_row_immutable",
        )
        expect_integrity_error(
            "SEC-06 DELETE d'une source_row rejeté",
            lambda: connection.execute("DELETE FROM source_rows WHERE source_row_id = ?", (sid,)),
            "source_row_immutable",
        )
        expect_integrity_error(
            "SEC-06 INSERT OR REPLACE sur source_rows rejeté",
            lambda: connection.execute(
                """INSERT OR REPLACE INTO source_rows
                   (source_row_id, import_file_id, section_index, logical_row_idx, row_sha256,
                    identity_key_sha256, transaction_id) VALUES (?, ?, 1, 1, ?, ?, ?)""",
                (
                    sid,
                    iid,
                    hashlib.sha256(b"replace").hexdigest(),
                    connection.execute(
                        "SELECT identity_key_sha256 FROM transactions WHERE transaction_id=?",
                        (tid,),
                    ).fetchone()[0],
                    tid,
                ),
            ),
            "source_row_immutable",
        )
        expect_integrity_error(
            "SEC-06 INSERT OR REPLACE sur transactions rejeté",
            lambda: connection.execute(
                "INSERT OR REPLACE INTO transactions(transaction_id, account_id, booked_on, amount_cents, raw_label, identity_label, identity_key_sha256, occurrence_rank) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (tid, ACCOUNT_ID, "2026-03-12", -1234, "Autre", "Autre", "0" * 64),
            ),
            "transaction_source_immutable",
        )
        report("SEC-06 chaque connexion vérifiée avec les deux PRAGMA")
    finally:
        connection.close()


def verify_primary_key_timestamp_and_version_guards(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            columns = connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
            table_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            table_sql = (
                "" if table_sql_row is None or table_sql_row[0] is None else table_sql_row[0]
            )
            for column in columns:
                name = column[1]
                if column[5] and column[3] != 1:
                    raise VerificationError(f"PK NOT NULL absent: {table}.{name}")
                if name in UUID_PRIMARY_KEYS.get(table, ()):
                    if column[2].upper() != "TEXT" or column[3] != 1 or column[5] == 0:
                        raise VerificationError(f"UUIDv4 PK mal typée: {table}.{name}")
                    if f"length({name}) = 36" not in table_sql or f"{name} GLOB '" not in table_sql:
                        raise VerificationError(f"UUIDv4 CHECK absent: {table}.{name}")
                if name.endswith("_utc"):
                    if (
                        "strftime('%Y-%m-%dT%H:%M:%fZ'," not in table_sql
                        or f"= {name}" not in table_sql
                    ):
                        raise VerificationError(f"horodatage UTC strict absent: {table}.{name}")

            if any(column[1] == "version" for column in columns):
                trigger = f"trg_{table}_version_increment"
                if (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
                    ).fetchone()
                    is None
                ):
                    raise VerificationError(f"trigger de version absent: {table}")

        insert_account(connection)
        expect_integrity_error(
            "ADR-025 PK UUIDv4 invalide rejetée",
            lambda: insert_account(connection, "not-a-uuid"),
        )
        expect_integrity_error(
            "ADR-025 PK UUID NULL rejetée",
            lambda: connection.execute(
                "INSERT INTO accounts(account_id, alias, account_role) VALUES (NULL, ?, ?)",
                ("Compte sans identifiant", "current"),
            ),
        )
        expect_integrity_error(
            "m-11 horodatage non canonique rejeté",
            lambda: connection.execute(
                "INSERT INTO accounts(account_id, alias, account_role, created_at_utc) "
                "VALUES (?, ?, ?, ?)",
                (new_id(), "Horodatage invalide", "current", "2026-10-10T10:00:00Z"),
            ),
        )
        default_time = connection.execute(
            "SELECT created_at_utc FROM accounts WHERE account_id=?", (ACCOUNT_ID,)
        ).fetchone()[0]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", default_time) is None:
            raise VerificationError(f"DEFAULT UTC non canonique: {default_time!r}")

        expect_integrity_error(
            "m-12 version inchangée refusée par trigger",
            lambda: connection.execute(
                "UPDATE accounts SET alias='Version inchangée' WHERE account_id=?", (ACCOUNT_ID,)
            ),
            "version_increment_required",
        )
        expect_integrity_error(
            "m-12 saut de version refusé par trigger",
            lambda: connection.execute(
                "UPDATE accounts SET version=version+2 WHERE account_id=?", (ACCOUNT_ID,)
            ),
            "version_increment_required",
        )
        connection.execute(
            "UPDATE accounts SET alias='Version suivante', version=version+1 WHERE account_id=?",
            (ACCOUNT_ID,),
        )
        if (
            connection.execute(
                "SELECT version FROM accounts WHERE account_id=?", (ACCOUNT_ID,)
            ).fetchone()[0]
            != 2
        ):
            raise VerificationError("m-12 incrément de version valide non appliqué")
        report(
            "ADR-025 PK non-nulles/UUIDv4, m-11 UTC milliseconde et m-12 version optimiste vérifiés"
        )
    finally:
        connection.close()


def verify_foreign_key(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        expect_integrity_error(
            "FK-01 compte inexistant rejeté",
            lambda: insert_transaction(
                connection,
                identity=("missing-account", "2026-03-12", -1234, "Label synthétique"),
                occurrence_rank=1,
            ),
        )
    finally:
        connection.close()


def verify_occurrence_rank_guards(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        identity = (ACCOUNT_ID, "2026-03-12", -1234, "Paiement synthétique")
        insert_transaction(connection, identity=identity, occurrence_rank=1)
        expect_integrity_error(
            "IMP-03 rang 1 en doublon rejeté",
            lambda: insert_transaction(connection, identity=identity, occurrence_rank=1),
            "UNIQUE constraint failed",
        )
        insert_transaction(connection, identity=identity, occurrence_rank=2)
        gap_identity = (ACCOUNT_ID, "2026-03-13", -1234, "Trou de rang")
        expect_integrity_error(
            "IMP-14 rang 3 sans rang 2 rejeté",
            lambda: insert_transaction(connection, identity=gap_identity, occurrence_rank=3),
            "occurrence_rank_not_contiguous",
        )
        report("IMP-03 rangs 1 et 2 acceptés")
    finally:
        connection.close()


WHITE_SPACE_RANGES = (
    (0x0009, 0x000D),
    (0x0020, 0x0020),
    (0x0085, 0x0085),
    (0x00A0, 0x00A0),
    (0x1680, 0x1680),
    (0x2000, 0x200A),
    (0x2028, 0x2029),
    (0x202F, 0x202F),
    (0x205F, 0x205F),
    (0x3000, 0x3000),
)


def is_unicode_white_space(character: str) -> bool:
    value = ord(character)
    return any(start <= value <= end for start, end in WHITE_SPACE_RANGES)


def normalize_identity_label(value: str) -> tuple[str, bool]:
    normalized = unicodedata.normalize("NFC", value)
    contains_format = any(unicodedata.category(char) == "Cf" for char in normalized)
    output: list[str] = []
    pending_space = False
    for character in normalized:
        if is_unicode_white_space(character):
            pending_space = bool(output)
            continue
        if pending_space:
            output.append(" ")
            pending_space = False
        output.append(character)
    return "".join(output), contains_format


def verify_identity_label(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        identity, cf_warning = normalize_identity_label("\u00a0E\u0301\t PAIEMENT\u200b\n")
        if identity != "É PAIEMENT\u200b" or not cf_warning:
            raise VerificationError("ADR-019.g normalisation White_Space/NFC/Cf incorrecte")
        if is_unicode_white_space("\u001c"):
            raise VerificationError("U+001C ne fait pas partie de la propriété Unicode White_Space")
        if not is_unicode_white_space("\u0085"):
            raise VerificationError("U+0085 doit appartenir à la propriété Unicode White_Space")
        report("ADR-019.g NFC + Unicode White_Space ; Cf averti et conservé")
        expect_integrity_error(
            "ADR-019.g espace ASCII de bord rejeté par le schéma",
            lambda: insert_transaction(
                connection,
                identity=(ACCOUNT_ID, "2026-03-12", -100, " Libellé "),
                occurrence_rank=1,
            ),
            "CHECK constraint failed",
        )
        expect_integrity_error(
            "ADR-019.g double espace ASCII rejeté par le schéma",
            lambda: insert_transaction(
                connection,
                identity=(ACCOUNT_ID, "2026-03-12", -100, "Libellé  double"),
                occurrence_rank=1,
            ),
            "CHECK constraint failed",
        )
        tid = insert_transaction(
            connection, identity=(ACCOUNT_ID, "2026-03-12", -100, identity), occurrence_rank=1
        )
        stored = connection.execute(
            "SELECT identity_key_sha256 FROM transactions WHERE transaction_id = ?", (tid,)
        ).fetchone()[0]
        if stored != identity_digest((ACCOUNT_ID, "2026-03-12", -100, identity)):
            raise VerificationError("hash technique de identity_key incorrect")
    finally:
        connection.close()


def verify_amount_guards(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        expect_integrity_error(
            "INV-02 montant SQLite REAL non entier rejeté",
            lambda: insert_transaction(
                connection,
                identity=(ACCOUNT_ID, "2026-03-12", 0, "REAL"),
                occurrence_rank=1,
                amount_sql="123.5",
            ),
            "CHECK constraint failed",
        )
        expect_integrity_error(
            "ADR-019.i montant zéro rejeté",
            lambda: insert_transaction(
                connection,
                identity=(ACCOUNT_ID, "2026-03-12", 0, "Montant zéro"),
                occurrence_rank=1,
            ),
            "CHECK constraint failed",
        )
        expect_integrity_error(
            "ADR-019.b amount_cents float SQLite rejeté avant commit",
            lambda: insert_transaction(
                connection,
                identity=(ACCOUNT_ID, "2026-03-13", 0, "Montant REAL"),
                occurrence_rank=1,
                amount_sql="123.5",
            ),
            "CHECK constraint failed",
        )
        expect_python_rejection(
            "ADR-019.h float intégral rejeté avant binding", lambda: require_cents(1123.0)
        )
        expect_python_rejection(
            "ADR-019.h texte décimal rejeté avant binding", lambda: require_cents("12.00")
        )
        expect_python_rejection("ADR-019.h bool rejeté avant binding", lambda: require_cents(True))
        if require_cents(-1123) != -1123:
            raise VerificationError("binder strict a modifié un montant entier")
    finally:
        connection.close()


def verify_archive_metadata(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        iid = insert_import(connection, status="rejected")
        relpath, storage_mode, permissions = connection.execute(
            "SELECT archive_relpath, archive_storage_mode, archive_permissions_octal FROM import_files WHERE import_file_id = ?",
            (iid,),
        ).fetchone()
        if (relpath, storage_mode, permissions) != (f"data/originals/{iid}.bin", "raw", "0600"):
            raise VerificationError("INV-11 archive brute/chemin/permissions incorrects")
        expect_integrity_error(
            "INV-11 chemin d'archive hors du répertoire autorisé rejeté",
            lambda: insert_import(connection, archive_relpath="../private.bin"),
            "CHECK constraint failed",
        )
        expect_integrity_error(
            "INV-11 mode de stockage non raw rejeté",
            lambda: insert_import(connection, archive_storage_mode="compressed"),
            "CHECK constraint failed",
        )
        expect_integrity_error(
            "INV-11 permissions autres que 0600 rejetées",
            lambda: insert_import(connection, archive_permissions_octal="0644"),
            "CHECK constraint failed",
        )
        committed = create_committed_empty(
            connection, hashlib.sha256(b"archive-less source").hexdigest()
        )
        digest = connection.execute(
            "SELECT file_sha256 FROM import_files WHERE import_file_id=?", (committed,)
        ).fetchone()[0]
        noop = insert_import(connection, status="no_op", duplicate_of=committed, digest=digest)
        archive_fields = connection.execute(
            "SELECT archive_relpath, archive_storage_mode, archive_permissions_octal FROM import_files WHERE import_file_id=?",
            (noop,),
        ).fetchone()
        if archive_fields != (None, None, None):
            raise VerificationError("n-09 no_op a conservé une référence ou métadonnée d'archive")
        expect_integrity_error(
            "n-09 un no_op ne peut conserver des octets archivés",
            lambda: insert_import(
                connection,
                status="no_op",
                duplicate_of=committed,
                digest=digest,
                archive_relpath=f"data/originals/{new_id()}.bin",
            ),
            "CHECK constraint failed",
        )
        report("INV-11 data/originals, archive raw, permissions attendues 0600")
    finally:
        connection.close()


def verify_noop_archive_constraint(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        committed = create_committed_empty(
            connection, hashlib.sha256(b"no-op archive proof").hexdigest()
        )
        digest = connection.execute(
            "SELECT file_sha256 FROM import_files WHERE import_file_id=?", (committed,)
        ).fetchone()[0]
        expect_integrity_error(
            "n-09 un no_op ne peut conserver des octets archivés",
            lambda: insert_import(
                connection,
                status="no_op",
                duplicate_of=committed,
                digest=digest,
                archive_relpath=f"data/originals/{new_id()}.bin",
            ),
            "CHECK constraint failed",
        )
    finally:
        connection.close()


def verify_section_and_provenance_guards(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        insert_account(connection, ACCOUNT_B_ID)
        iid = insert_import(connection, parsed_operation_count=1)
        insert_section(connection, iid, ACCOUNT_ID)
        expect_integrity_error(
            "IMP-13 second section mappée au même compte rejetée",
            lambda: insert_section(connection, iid, ACCOUNT_ID, section_index=2),
            "UNIQUE constraint failed",
        )
        tid = insert_transaction(
            connection,
            identity=(ACCOUNT_B_ID, "2026-03-12", -100, "Mauvais compte"),
            occurrence_rank=1,
        )
        expect_integrity_error(
            "IMP-14 source_row compte différent de la section rejetée",
            lambda: insert_source(connection, iid, tid, 1),
            "source_row_account_mismatch",
        )

        ok_tid = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-12", -100, "Même transaction"),
            occurrence_rank=1,
        )
        expect_integrity_error(
            "n-08 source_row identity_key_sha256 doit égaler la transaction cible",
            lambda: insert_source(connection, iid, ok_tid, 1, identity_key_sha256="0" * 64),
            "source_row_identity_key_mismatch",
        )
        insert_source(connection, iid, ok_tid, 1)
        expect_integrity_error(
            "IMP-14 une source par fichier et transaction imposée",
            lambda: insert_source(connection, iid, ok_tid, 2),
            "UNIQUE constraint failed: source_rows.import_file_id, source_rows.transaction_id",
        )
        connection.execute("SAVEPOINT source_row_unique")
        another_tid = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-13", -101, "Collision de ligne"),
            occurrence_rank=1,
        )
        expect_integrity_error(
            "IMP-14 logical_row_idx unique par section et fichier",
            lambda: insert_source(connection, iid, another_tid, 1),
            "UNIQUE constraint failed: source_rows.import_file_id, source_rows.section_index, source_rows.logical_row_idx",
        )
        connection.execute("ROLLBACK TO source_row_unique")
        connection.execute("RELEASE source_row_unique")

        import_b = insert_import(connection, parsed_operation_count=1)
        insert_section(connection, import_b, ACCOUNT_B_ID)
        insert_source(connection, import_b, tid, 1)
        commit_import(connection, import_b)
        commit_import(connection, iid)

        original = iid
        noop = insert_import(
            connection,
            status="no_op",
            duplicate_of=original,
            digest=connection.execute(
                "SELECT file_sha256 FROM import_files WHERE import_file_id=?", (original,)
            ).fetchone()[0],
        )
        if (
            connection.execute(
                "SELECT count(*) FROM source_rows WHERE import_file_id=?", (noop,)
            ).fetchone()[0]
            != 0
        ):
            raise VerificationError("IMP-01 un no_op ne doit jamais apporter de source_row")
        report("IMP-01 tentative no_op conservée avec 0 source_row")
        extra = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-14", -101, "Après commit synthétique"),
            occurrence_rank=1,
        )
        expect_integrity_error(
            "ADR-019.b capacité de provenance d'un import commité fermée",
            lambda: insert_source(connection, iid, extra, 2),
            "source_row_import_already_complete",
        )
        rejected = insert_import(connection)
        insert_section(connection, rejected)
        connection.execute(
            "UPDATE import_files SET status='rejected', version=version+1 WHERE import_file_id=?",
            (rejected,),
        )
        expect_integrity_error(
            "INV-01 source_row d'un import rejected rejetée",
            lambda: insert_source(connection, rejected, ok_tid, 1),
            "source_row_import_status_invalid",
        )
        pending_reject = insert_import(connection, parsed_operation_count=1)
        insert_section(connection, pending_reject)
        pending_tx = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-15", -101, "Source avant rejet"),
            occurrence_rank=1,
        )
        insert_source(connection, pending_reject, pending_tx, 1)
        expect_integrity_error(
            "n-06 import avec source_row ne peut pas devenir rejected",
            lambda: connection.execute(
                "UPDATE import_files SET status='rejected', version=version+1 WHERE import_file_id=?",
                (pending_reject,),
            ),
            "rejected_import_cannot_have_source_rows",
        )
    finally:
        connection.close()


def verify_import_status_and_immutability(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        iid = insert_import(connection, parsed_operation_count=1)
        insert_section(connection, iid)
        tid = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-12", -100, "Import commité"),
            occurrence_rank=1,
        )
        insert_source(connection, iid, tid, 1)
        rejection_id = new_id()
        connection.execute(
            """INSERT INTO import_rejections
               (import_rejection_id, import_file_id, section_index, code, message, severity)
               VALUES (?, ?, 1, 'SYNTHETIC_WARNING', 'Avertissement synthétique', 'warning')""",
            (rejection_id, iid),
        )
        commit_import(connection, iid)
        expect_integrity_error(
            "ADR-019.d métadonnées de fichier commitées immuables",
            lambda: connection.execute(
                "UPDATE import_files SET file_sha256=?, version=version+1 WHERE import_file_id=?",
                ("0" * 64, iid),
            ),
            "committed_import_immutable",
        )
        expect_integrity_error(
            "ADR-019.d import commité non supprimable",
            lambda: connection.execute("DELETE FROM import_files WHERE import_file_id=?", (iid,)),
            "import_history_immutable",
        )
        expect_integrity_error(
            "ADR-019.d couverture/mapping de section commité immuable",
            lambda: connection.execute(
                "UPDATE import_account_sections SET coverage_through='2026-04-01' WHERE import_file_id=?",
                (iid,),
            ),
            "committed_import_section_immutable",
        )
        expect_integrity_error(
            "n-07 décision d'aperçu d'un import commité non modifiable",
            lambda: connection.execute(
                "UPDATE import_rejections SET message='Réécriture' WHERE import_rejection_id=?",
                (rejection_id,),
            ),
            "committed_import_rejection_immutable",
        )
        expect_integrity_error(
            "n-07 décision d'aperçu d'un import commité non supprimable",
            lambda: connection.execute(
                "DELETE FROM import_rejections WHERE import_rejection_id=?", (rejection_id,)
            ),
            "committed_import_rejection_immutable",
        )
        expect_integrity_error(
            "n-07 ajout de décision après commit interdit",
            lambda: connection.execute(
                """INSERT INTO import_rejections
                   (import_rejection_id, import_file_id, code, message, severity)
                   VALUES (?, ?, 'LATE', 'Tardif', 'warning')""",
                (new_id(), iid),
            ),
            "import_rejection_requires_previewed_file",
        )
        empty_committed = create_committed_empty(
            connection, hashlib.sha256(b"empty committed section").hexdigest()
        )
        expect_integrity_error(
            "ADR-019.d section vide d'un import commité non supprimable",
            lambda: connection.execute(
                "DELETE FROM import_account_sections WHERE import_file_id=?", (empty_committed,)
            ),
            "committed_import_section_immutable",
        )
        expect_integrity_error(
            "ADR-019.d section commité non supprimable",
            lambda: connection.execute(
                "DELETE FROM import_account_sections WHERE import_file_id=?", (iid,)
            ),
            "committed_import_section_immutable",
        )
        expect_integrity_error(
            "ADR-019.d INSERT direct au statut committed interdit",
            lambda: insert_import(connection, status="committed"),
            "import_must_start_previewed",
        )
        expect_integrity_error(
            "ADR-017 aucun statut d'import partial",
            lambda: insert_import(connection, status="partial"),
            "CHECK constraint failed",
        )
        incomplete_count = insert_import(connection, parsed_operation_count=1)
        insert_section(connection, incomplete_count)
        expect_integrity_error(
            "ADR-019.c nombre de source_rows égal au nombre de lignes admises",
            lambda: commit_import(connection, incomplete_count),
            "committed_import_source_count_mismatch",
        )
        connection.execute("SAVEPOINT no_source_guard")
        no_source = insert_import(connection, parsed_operation_count=0)
        insert_section(connection, no_source)
        insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-20", -100, "Transaction sans provenance"),
            occurrence_rank=1,
        )
        expect_integrity_error(
            "ADR-019.c aucune transaction commise sans provenance",
            lambda: commit_import(connection, no_source),
            "transaction_without_source_row",
        )
        connection.execute("ROLLBACK TO no_source_guard")
        connection.execute("RELEASE no_source_guard")
        preview = insert_import(connection)
        insert_section(connection, preview, coverage_from=None, coverage_through=None)
        expect_integrity_error(
            "ADR-017/018 commit sans plage résolue rejeté",
            lambda: commit_import(connection, preview),
            "committed_import_section_unresolved",
        )
        unresolved = insert_import(connection)
        insert_section(connection, unresolved)
        connection.execute(
            """INSERT INTO import_rejections
               (import_rejection_id, import_file_id, section_index, logical_row_idx, code, message,
                severity, requires_resolution, resolution_status)
               VALUES (?, ?, 1, 1, 'OUT_OF_PERIOD', 'Date à résoudre', 'warning', 1, 'pending')""",
            (new_id(), unresolved),
        )
        expect_integrity_error(
            "IMP-12 OUT_OF_PERIOD non résolue bloque le fichier entier",
            lambda: commit_import(connection, unresolved),
            "committed_import_has_unresolved_rejection",
        )
        connection.execute(
            """UPDATE import_rejections SET resolution_status='resolved', resolution_action='create_occurrence',
               resolved_by='user', resolved_at_utc='2026-10-10T10:00:00.000Z' WHERE import_file_id=?""",
            (unresolved,),
        )
        out_of_period = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-02-28", -90, "Ligne hors période synthétique"),
            occurrence_rank=1,
        )
        insert_source(connection, unresolved, out_of_period, 1)
        connection.execute(
            "UPDATE import_files SET parsed_operation_count=1, version=version+1 WHERE import_file_id=?",
            (unresolved,),
        )
        commit_import(connection, unresolved)
        report("IMP-12 décision d'aperçu auditée débloque le commit intégral")
        invalid_noop = hashlib.sha256(b"other content").hexdigest()
        expect_integrity_error(
            "ADR-019.d no_op doit viser un hash commité identique",
            lambda: insert_import(
                connection, status="no_op", digest=invalid_noop, duplicate_of=iid
            ),
            "no_op_must_reference_committed_same_hash",
        )
        transition = insert_import(connection)
        expect_integrity_error(
            "ADR-019.d transition previewed vers no_op interdite",
            lambda: connection.execute(
                "UPDATE import_files SET status='no_op', version=version+1 WHERE import_file_id=?",
                (transition,),
            ),
            "import_status_transition_invalid",
        )
    finally:
        connection.close()


def verify_import_generation_fields(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        header = insert_import(
            connection,
            received_on="2026-04-01",
            generation_date="2026-03-31",
            generation_date_source="header_download",
        )
        insert_section(connection, header, coverage_through="2026-03-31")
        got = connection.execute(
            "SELECT generation_date, generation_date_source FROM import_files WHERE import_file_id=?",
            (header,),
        ).fetchone()
        ct = connection.execute(
            "SELECT complete_through FROM import_account_sections WHERE import_file_id=?", (header,)
        ).fetchone()[0]
        if got != ("2026-03-31", "header_download") or ct != "2026-03-30":
            raise VerificationError("IMP-11 generated_on/complete_through du préambule incorrect")
        fallback = insert_import(
            connection,
            received_on="2026-04-01",
            generation_date="2026-04-01",
            generation_date_source="received_on_fallback",
            parsed_operation_count=1,
        )
        insert_section(
            connection,
            fallback,
            coverage_from="2026-03-01",
            coverage_through="2026-04-05",
            complete_through_value="2026-04-01",
        )
        fallback_tx = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-04-02", -250, "Dernier jour provisoire"),
            occurrence_rank=1,
        )
        insert_source(connection, fallback, fallback_tx, 1)
        commit_import(connection, fallback)
        generated, source = connection.execute(
            "SELECT generation_date, generation_date_source FROM import_files WHERE import_file_id=?",
            (fallback,),
        ).fetchone()
        fallback_through = connection.execute(
            "SELECT complete_through FROM import_account_sections WHERE import_file_id=?",
            (fallback,),
        ).fetchone()[0]
        if (generated, source, fallback_through) != (
            "2026-04-01",
            "received_on_fallback",
            "2026-04-01",
        ):
            raise VerificationError("IMP-11 date de réception de repli incorrecte")
        report(
            "IMP-11 date de repli : dernier jour avec opérations provisoire, complete_through veille"
        )
    finally:
        connection.close()


def verify_fallback_complete_through_guard(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        import_id = insert_import(
            connection,
            received_on="2026-04-01",
            generation_date="2026-04-01",
            generation_date_source="received_on_fallback",
            parsed_operation_count=1,
        )
        insert_section(
            connection,
            import_id,
            coverage_from="2026-03-01",
            coverage_through="2026-04-05",
            complete_through_value="2026-04-02",
        )
        transaction_id = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-04-02", -250, "Dernier jour à garder provisoire"),
            occurrence_rank=1,
        )
        insert_source(connection, import_id, transaction_id, 1)
        expect_integrity_error(
            "IMP-11 complete_through repli ne peut inclure le dernier jour avec opérations",
            lambda: commit_import(connection, import_id),
            "committed_import_section_unresolved",
        )
    finally:
        connection.close()


def coverage_union_covers(intervals: Iterable[tuple[str, str]], start: str, end: str) -> bool:
    """Oracle de complétude : l'union d'intervalles fermés couvre chaque jour demandé."""
    cursor = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if cursor > last:
        return True
    ordered = sorted((date.fromisoformat(a), date.fromisoformat(b)) for a, b in intervals)
    for interval_start, interval_end in ordered:
        if interval_end < cursor:
            continue
        if interval_start > cursor:
            return False
        cursor = max(cursor, interval_end + timedelta(days=1))
        if cursor > last:
            return True
    return False


def compute_balance_delta(
    start_date: str,
    start_cents: int,
    end_date: str,
    end_cents: int,
    start_complete_through: str,
    end_complete_through: str,
    coverage: Iterable[tuple[str, str]],
    transactions: Iterable[tuple[str, int]],
) -> int | None:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if start > date.fromisoformat(start_complete_through) or end > date.fromisoformat(
        end_complete_through
    ):
        return None
    if start >= end or not coverage_union_covers(
        coverage, (start + timedelta(days=1)).isoformat(), end_date
    ):
        return None
    recalculated = start_cents + sum(
        amount for booked_on, amount in transactions if start_date < booked_on <= end_date
    )
    return end_cents - recalculated


def completeness_after_balance_check(is_covered: bool, check_status: str | None) -> str:
    if check_status == "mismatch":
        return "unknown"
    return "complete" if is_covered and check_status != "not_comparable" else "partial"


def canonical_balance_snapshot_hash(
    section_inputs: Iterable[tuple[str, str, str, str, int]],
    transaction_inputs: Iterable[tuple[str, int]],
) -> str:
    records = ["section\0" + "\0".join(map(str, row)) for row in sorted(section_inputs)]
    records.extend(
        "transaction\0" + booked_on + "\0" + str(amount)
        for booked_on, amount in sorted(transaction_inputs)
    )
    encoded = b"".join(
        len(record.encode("utf-8")).to_bytes(4, "big") + record.encode("utf-8")
        for record in records
    )
    return hashlib.sha256(encoded).hexdigest()


def verify_balance_checks(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        first = insert_import(connection, parsed_operation_count=1, generation_date="2026-04-01")
        insert_section(
            connection,
            first,
            coverage_from="2026-03-01",
            coverage_through="2026-03-31",
            complete_through_value="2026-03-31",
            balance_as_of="2026-03-15",
            header_balance_cents=10000,
        )
        debit = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-20", -1000, "Débit de solde synthétique"),
            occurrence_rank=1,
        )
        insert_source(connection, first, debit, 1)
        commit_import(connection, first)

        second = insert_import(connection, parsed_operation_count=0, generation_date="2026-04-16")
        insert_section(
            connection,
            second,
            coverage_from="2026-03-16",
            coverage_through="2026-04-15",
            complete_through_value="2026-04-15",
            balance_as_of="2026-04-15",
            header_balance_cents=9000,
        )
        commit_import(connection, second)
        calculated_delta = compute_balance_delta(
            "2026-03-15",
            10000,
            "2026-04-15",
            9000,
            "2026-03-31",
            "2026-04-15",
            [("2026-03-01", "2026-03-31"), ("2026-03-16", "2026-04-15")],
            [("2026-03-20", -1000)],
        )
        reversed_delta = compute_balance_delta(
            "2026-03-15",
            10000,
            "2026-04-15",
            9000,
            "2026-03-31",
            "2026-04-15",
            [("2026-03-16", "2026-04-15"), ("2026-03-01", "2026-03-31")],
            [("2026-03-20", -1000)],
        )
        if calculated_delta != 0 or reversed_delta != calculated_delta:
            raise VerificationError(
                "ADR-024.1/2 union des jours et solde dépendent de l'ordre des imports"
            )
        section_facts = [
            (first, "2026-03-01", "2026-03-31", "2026-03-15", 10000),
            (second, "2026-03-16", "2026-04-15", "2026-04-15", 9000),
        ]
        snapshot_a = canonical_balance_snapshot_hash(section_facts, [("2026-03-20", -1000)])
        snapshot_b = canonical_balance_snapshot_hash(
            list(reversed(section_facts)), [("2026-03-20", -1000)]
        )
        if snapshot_a != snapshot_b:
            raise VerificationError(
                "ADR-024.2 empreinte des entrées de solde dépend de l'ordre d'import"
            )

        check_id = new_id()
        snapshot = hashlib.sha256(b"ordered balance inputs").hexdigest()
        connection.execute(
            """INSERT INTO balance_checks
               (balance_check_id, account_id, start_import_file_id, start_section_index,
                end_import_file_id, end_section_index, status, recalculated_balance_cents,
                delta_cents, input_snapshot_sha256)
               VALUES (?, ?, ?, 1, ?, 1, 'match', 9000, 0, ?)""",
            (check_id, ACCOUNT_ID, first, second, snapshot),
        )
        expect_integrity_error(
            "ADR-024.2 balance_checks append-only UPDATE",
            lambda: connection.execute(
                "UPDATE balance_checks SET delta_cents=1, version=version+1 WHERE balance_check_id=?",
                (check_id,),
            ),
            "balance_check_append_only",
        )
        expect_integrity_error(
            "ADR-024.2 balance_checks append-only DELETE",
            lambda: connection.execute(
                "DELETE FROM balance_checks WHERE balance_check_id=?", (check_id,)
            ),
            "balance_check_append_only",
        )
        mismatch_file = insert_import(
            connection, parsed_operation_count=0, generation_date="2026-04-16"
        )
        insert_section(
            connection,
            mismatch_file,
            coverage_from="2026-03-16",
            coverage_through="2026-04-15",
            complete_through_value="2026-04-15",
            balance_as_of="2026-04-15",
            header_balance_cents=9001,
        )
        commit_import(connection, mismatch_file)
        connection.execute(
            """INSERT INTO balance_checks
               (balance_check_id, account_id, start_import_file_id, start_section_index,
                end_import_file_id, end_section_index, status, recalculated_balance_cents,
                delta_cents, input_snapshot_sha256)
               VALUES (?, ?, ?, 1, ?, 1, 'mismatch', 9000, 1, ?)""",
            (
                new_id(),
                ACCOUNT_ID,
                first,
                mismatch_file,
                hashlib.sha256(b"mismatch snapshot").hexdigest(),
            ),
        )
        mismatch_status = connection.execute(
            "SELECT status FROM balance_checks WHERE end_import_file_id=?", (mismatch_file,)
        ).fetchone()[0]
        if completeness_after_balance_check(True, mismatch_status) != "unknown":
            raise VerificationError("ADR-018.3 un mismatch réel doit rendre le mois unknown")
        report("ADR-018.3 mismatch de balance rend la complétude du mois unknown")
        expect_integrity_error(
            "ADR-024.3 delta de balance_check doit correspondre aux en-têtes",
            lambda: connection.execute(
                """INSERT INTO balance_checks
                   (balance_check_id, account_id, start_import_file_id, start_section_index,
                    end_import_file_id, end_section_index, status, recalculated_balance_cents,
                    delta_cents, input_snapshot_sha256)
                   VALUES (?, ?, ?, 1, ?, 1, 'match', 8999, 0, ?)""",
                (new_id(), ACCOUNT_ID, first, second, hashlib.sha256(b"bad delta").hexdigest()),
            ),
            "balance_check_not_comparable_inputs",
        )

        late = insert_import(connection, parsed_operation_count=0, generation_date="2026-04-16")
        insert_section(
            connection,
            late,
            coverage_from="2026-03-01",
            coverage_through="2026-04-15",
            complete_through_value="2026-04-15",
            balance_as_of="2026-04-16",
            header_balance_cents=9000,
        )
        commit_import(connection, late)
        expect_integrity_error(
            "ADR-024.2 solde postérieur à complete_through non comparable",
            lambda: connection.execute(
                """INSERT INTO balance_checks
                   (balance_check_id, account_id, start_import_file_id, start_section_index,
                    end_import_file_id, end_section_index, status, recalculated_balance_cents,
                    delta_cents, input_snapshot_sha256)
                   VALUES (?, ?, ?, 1, ?, 1, 'match', 9000, 0, ?)""",
                (new_id(), ACCOUNT_ID, first, late, hashlib.sha256(b"late balance").hexdigest()),
            ),
            "balance_check_not_comparable_inputs",
        )
        gap = [("2026-03-01", "2026-03-30"), ("2026-04-01", "2026-04-15")]
        if coverage_union_covers(gap, "2026-03-01", "2026-04-15"):
            raise VerificationError("ADR-024.1 une journée absente a été déclarée complète")
        report(
            "ADR-024.1/2 couverture par union et balance_checks append-only, versionnés par empreinte"
        )
    finally:
        connection.close()


def verify_balance_date_cutoff_guard(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        start = insert_import(connection, parsed_operation_count=0, generation_date="2026-04-01")
        insert_section(
            connection,
            start,
            coverage_from="2026-03-01",
            coverage_through="2026-03-31",
            complete_through_value="2026-03-31",
            balance_as_of="2026-03-15",
            header_balance_cents=10000,
        )
        commit_import(connection, start)
        late = insert_import(connection, parsed_operation_count=0, generation_date="2026-04-16")
        insert_section(
            connection,
            late,
            coverage_from="2026-03-16",
            coverage_through="2026-04-15",
            complete_through_value="2026-04-15",
            balance_as_of="2026-04-16",
            header_balance_cents=10000,
        )
        commit_import(connection, late)
        expect_integrity_error(
            "ADR-024.2 balance_as_of postérieur à complete_through refusé en match",
            lambda: connection.execute(
                """INSERT INTO balance_checks
                   (balance_check_id, account_id, start_import_file_id, start_section_index,
                    end_import_file_id, end_section_index, status, recalculated_balance_cents,
                    delta_cents, input_snapshot_sha256)
                   VALUES (?, ?, ?, 1, ?, 1, 'match', 10000, 0, ?)""",
                (
                    new_id(),
                    ACCOUNT_ID,
                    start,
                    late,
                    hashlib.sha256(b"ineligible balance date").hexdigest(),
                ),
            ),
            "balance_check_not_comparable_inputs",
        )
    finally:
        connection.close()


def verify_noop_and_committed_hash(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        digest = hashlib.sha256(b"synthetic same file").hexdigest()
        original = create_committed_empty(connection, digest)
        no_op = insert_import(connection, status="no_op", digest=digest, duplicate_of=original)
        expect_integrity_error(
            "ADR-019.d historique no_op non supprimable",
            lambda: connection.execute("DELETE FROM import_files WHERE import_file_id=?", (no_op,)),
            "import_history_immutable",
        )
        duplicate = insert_import(connection, digest=digest)
        insert_section(connection, duplicate)
        expect_integrity_error(
            "IMP-01 hash de fichier commité dupliqué rejeté",
            lambda: commit_import(connection, duplicate),
            "UNIQUE constraint failed: import_files.file_sha256",
        )
        report("IMP-01 no_op référence le commit initial et ne réserve aucun second commit")
    finally:
        connection.close()


def verify_rule_audit_foreign_key(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        seed_financial_categories(connection)
        tid = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-12", -100, "Règle synthétique"),
            occurrence_rank=1,
        )
        rule_id = new_id()
        connection.execute(
            """INSERT INTO rules (rule_id, name, scope_kind, scope_json, flow_kind, category_id, effect_json)
               VALUES (?, 'Règle synthétique', 'exact_payee', '{}', 'expense', 'groceries', '{}')""",
            (rule_id,),
        )
        expect_integrity_error(
            "FK rule_executions.audit_event_id orphelin rejeté",
            lambda: connection.execute(
                """INSERT INTO rule_executions
                   (rule_execution_id, rule_id, transaction_id, outcome, audit_event_id)
                   VALUES (?, ?, ?, 'applied', 'missing-audit-event')""",
                (new_id(), rule_id, tid),
            ),
            "FOREIGN KEY constraint failed",
        )
    finally:
        connection.close()


def verify_transaction_flow_not_stored(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(transactions)")}
        if "flow_kind" in columns:
            raise VerificationError("ADR-020 flow_kind ne doit pas être stocké sur transactions")
        report("ADR-020 aucun flow_kind stocké sur transactions")
    finally:
        connection.close()


def verify_allocations(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        seed_financial_categories(connection)
        debit = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-12", -1000, "Débit synthétique"),
            occurrence_rank=1,
        )
        credit = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-13", 1000, "Crédit synthétique"),
            occurrence_rank=1,
        )
        old = insert_allocation(
            connection, debit, category_id="groceries", flow_kind="expense", amount_cents=-700
        )
        expect_integrity_error(
            "ADR-019.e allocation de signe différent rejetée",
            lambda: insert_allocation(
                connection, debit, category_id="groceries", flow_kind="expense", amount_cents=1
            ),
            "allocation_sign_must_match_transaction",
        )
        expect_integrity_error(
            "ADR-019.e allocation confirmée au-delà du montant rejetée",
            lambda: insert_allocation(
                connection, debit, category_id="groceries", flow_kind="expense", amount_cents=-301
            ),
            "allocation_total_exceeds_transaction",
        )
        expect_integrity_error(
            "ADR-019.e catégorie flow_savings_out positive rejetée",
            lambda: insert_allocation(
                connection,
                credit,
                category_id="flow_savings_out",
                flow_kind="transfer_savings",
                amount_cents=100,
            ),
            "allocation_category_direction_invalid",
        )
        insert_allocation(
            connection,
            credit,
            category_id="income_salary_a",
            flow_kind="income_recurring",
            amount_cents=900,
        )
        retract_transaction = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-14", -1000, "Débit à retirer synthétique"),
            occurrence_rank=1,
        )
        retractable = insert_allocation(
            connection,
            retract_transaction,
            category_id="groceries",
            flow_kind="expense",
            amount_cents=-700,
        )
        wrong_transaction_decision = insert_classification_decision(connection, credit)
        expect_integrity_error(
            "ADR-027 retrait d'allocation exige une décision de la même transaction",
            lambda: connection.execute(
                """UPDATE allocations SET retracted_at_utc='2026-03-14T12:00:00.000Z',
                   retracted_by_decision_id=?, version=version+1 WHERE allocation_id=?""",
                (wrong_transaction_decision, retractable),
            ),
            "allocation_retraction_decision_transaction_mismatch",
        )
        same_transaction_decision = insert_classification_decision(connection, retract_transaction)
        connection.execute(
            """UPDATE allocations SET retracted_at_utc='2026-03-14T12:00:00.000Z',
               retracted_by_decision_id=?, version=version+1 WHERE allocation_id=?""",
            (same_transaction_decision, retractable),
        )
        expect_integrity_error(
            "ADR-027 décision de retrait référencée non supprimable",
            lambda: connection.execute(
                "DELETE FROM classification_decisions WHERE decision_id=?",
                (same_transaction_decision,),
            ),
            "FOREIGN KEY constraint failed",
        )
        expect_integrity_error(
            "ADR-027 allocation retirée immuable",
            lambda: connection.execute(
                "UPDATE allocations SET amount_cents=-600, version=version+1 WHERE allocation_id=?",
                (retractable,),
            ),
            "allocation_update_requires_audited_supersession",
        )
        expect_integrity_error(
            "ADR-027 retrait initial direct interdit",
            lambda: connection.execute(
                """INSERT INTO allocations
                   (allocation_id, transaction_id, category_id, flow_kind, amount_cents, origin,
                    retracted_at_utc, retracted_by_decision_id)
                   VALUES (?, ?, 'groceries', 'expense', -1, 'manual',
                           '2026-03-14T12:00:00.000Z', ?)""",
                (new_id(), retract_transaction, same_transaction_decision),
            ),
            "allocation_retraction_requires_update",
        )
        try:
            insert_allocation(
                connection,
                retract_transaction,
                category_id="groceries",
                flow_kind="expense",
                amount_cents=-1000,
            )
        except sqlite3.IntegrityError as error:
            raise VerificationError(
                "ADR-027 retrait audité, immuable et exclu du plafond/somme actifs"
            ) from error
        active_after_retraction = connection.execute(
            """SELECT sum(amount_cents) FROM allocations WHERE transaction_id=?
               AND superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL""",
            (retract_transaction,),
        ).fetchone()[0]
        if active_after_retraction != -1000:
            raise VerificationError(
                "ADR-027 allocation retirée encore comprise dans la somme active"
            )
        report("ADR-027 retrait audité, immuable et exclu du plafond/somme actifs")
        expect_integrity_error(
            "ADR-019.b allocation amount_cents REAL rejeté",
            lambda: connection.execute(
                """INSERT INTO allocations
                   (allocation_id, transaction_id, category_id, flow_kind, amount_cents, origin)
                   VALUES (?, ?, 'groceries', 'expense', -0.5, 'manual')""",
                (new_id(), debit),
            ),
            "CHECK constraint failed",
        )
        expect_integrity_error(
            "ADR-019.e UPDATE en place d'allocation confirmée rejeté",
            lambda: connection.execute(
                "UPDATE allocations SET origin='rule', version=version+1 WHERE allocation_id=?",
                (old,),
            ),
            "allocation_update_requires_audited_supersession",
        )
        expect_integrity_error(
            "ADR-019.e DELETE d'allocation confirmée rejeté",
            lambda: connection.execute("DELETE FROM allocations WHERE allocation_id=?", (old,)),
            "allocation_delete_forbidden",
        )
        expect_integrity_error(
            "SEC-06 REPLACE d'allocation confirmée rejeté",
            lambda: connection.execute(
                """INSERT OR REPLACE INTO allocations
                   (allocation_id, transaction_id, category_id, flow_kind, amount_cents, origin)
                   VALUES (?, ?, 'groceries', 'expense', -100, 'manual')""",
                (old, debit),
            ),
            "allocation_delete_forbidden",
        )
        replacement_id = new_id()
        audit_id = new_id()
        mismatched_supersession_audit = new_id()
        connection.execute(
            """INSERT INTO audit_events(audit_event_id, target_type, target_id, action, actor, before_json, after_json)
               VALUES (?, 'allocation', ?, 'allocation.supersede', 'verify_schema', '{}', '{}')""",
            (mismatched_supersession_audit, old),
        )
        expect_integrity_error(
            "ADR-027 supersession d'allocation exige la même transaction",
            lambda: connection.execute(
                """UPDATE allocations SET superseded_by_allocation_id=?,
                   superseded_by_audit_event_id=?, version=version+1 WHERE allocation_id=?""",
                (retractable, mismatched_supersession_audit, old),
            ),
            "allocation_supersession_transaction_mismatch",
        )
        connection.execute(
            """INSERT INTO audit_events(audit_event_id, target_type, target_id, action, actor, before_json, after_json)
               VALUES (?, 'allocation', ?, 'allocation.supersede', 'verify_schema', '{}', '{}')""",
            (audit_id, old),
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE allocations SET superseded_by_allocation_id=?, superseded_by_audit_event_id=?, version=version+1
               WHERE allocation_id=?""",
            (replacement_id, audit_id, old),
        )
        insert_allocation(
            connection,
            debit,
            category_id="groceries",
            flow_kind="expense",
            amount_cents=-900,
            allocation_id=replacement_id,
        )
        connection.execute("COMMIT")
        active = connection.execute(
            """SELECT sum(amount_cents) FROM allocations WHERE transaction_id=?
               AND superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL""",
            (debit,),
        ).fetchone()[0]
        if active != -900:
            raise VerificationError(
                "ADR-019.e supersession auditée ne remplace pas l'allocation active"
            )
        report("ADR-019.e supersession versionnée/auditée ; allocation active correcte")
        expect_integrity_error(
            "ADR-027 allocation supersédée immuable",
            lambda: connection.execute(
                "UPDATE allocations SET amount_cents=-701, version=version+1 WHERE allocation_id=?",
                (old,),
            ),
            "allocation_update_requires_audited_supersession",
        )
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


def verify_refund_links(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection, account_role="current")
        insert_account(connection, ACCOUNT_B_ID, account_role="personal")
        seed_financial_categories(connection)
        credit = insert_transaction(
            connection,
            identity=(ACCOUNT_B_ID, "2026-04-02", 20000, "Avoir synthétique"),
            occurrence_rank=1,
        )
        purchase_a = insert_transaction(
            connection,
            identity=(ACCOUNT_B_ID, "2026-03-30", -20000, "Achat synthétique A"),
            occurrence_rank=1,
        )
        purchase_b = insert_transaction(
            connection,
            identity=(ACCOUNT_B_ID, "2026-02-20", -5000, "Achat synthétique B"),
            occurrence_rank=1,
        )
        source_allocation = insert_allocation(
            connection, credit, category_id="income_refund", flow_kind="refund", amount_cents=10000
        )
        second_source_allocation = insert_allocation(
            connection, credit, category_id="income_refund", flow_kind="refund", amount_cents=5000
        )
        insert_allocation(
            connection,
            credit,
            category_id="income_other_test",
            flow_kind="income_other",
            amount_cents=5000,
        )
        grocery_allocation = insert_allocation(
            connection,
            purchase_a,
            category_id="groceries",
            flow_kind="expense",
            amount_cents=-10000,
        )
        add_category(connection, "restaurants", "expense")
        insert_allocation(
            connection,
            purchase_a,
            category_id="restaurants",
            flow_kind="expense",
            amount_cents=-5000,
        )
        cross_account_target = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-29", -1000, "Cible autre compte"),
            occurrence_rank=1,
        )
        insert_allocation(
            connection,
            cross_account_target,
            category_id="groceries",
            flow_kind="expense",
            amount_cents=-1000,
        )
        expect_integrity_error(
            "MONEY-13 lien SQL inter-comptes refusé",
            lambda: insert_refund_link(
                connection,
                credit,
                cross_account_target,
                1,
                status="proposed",
                source_allocation_id=source_allocation,
            ),
            "refund_link_direction_invalid",
        )
        mixed_credit = insert_transaction(
            connection,
            identity=(ACCOUNT_B_ID, "2026-04-06", 18000, "Avoir test plafonds combinés"),
            occurrence_rank=1,
        )
        mixed_source_allocation = insert_allocation(
            connection,
            mixed_credit,
            category_id="income_refund",
            flow_kind="refund",
            amount_cents=18000,
        )
        mixed_purchase = insert_transaction(
            connection,
            identity=(ACCOUNT_B_ID, "2026-03-31", -20000, "Achat test plafonds combinés"),
            occurrence_rank=1,
        )
        mixed_grocery_allocation = insert_allocation(
            connection,
            mixed_purchase,
            category_id="groceries",
            flow_kind="expense",
            amount_cents=-10000,
        )
        insert_allocation(
            connection,
            mixed_purchase,
            category_id="restaurants",
            flow_kind="expense",
            amount_cents=-10000,
        )
        insert_refund_link(
            connection,
            mixed_credit,
            mixed_purchase,
            10000,
            source_allocation_id=mixed_source_allocation,
        )
        expect_integrity_error(
            "MONEY-11 SQL lien prorata puis ciblé dépasse le plafond d'allocation",
            lambda: insert_refund_link(
                connection,
                mixed_credit,
                mixed_purchase,
                8000,
                source_allocation_id=mixed_source_allocation,
                target_allocation_id=mixed_grocery_allocation,
            ),
            "refund_link_target_allocation_cap_exceeded",
        )
        mixed_parts = dict(
            connection.execute(
                """SELECT a.category_id, sum(p.amount_cents)
                   FROM refund_link_allocation_parts p
                   JOIN allocations a ON a.allocation_id=p.allocation_id
                   JOIN transaction_links l ON l.transaction_link_id=p.transaction_link_id
                   WHERE l.target_transaction_id=? AND l.status='confirmed'
                   GROUP BY a.category_id""",
                (mixed_purchase,),
            )
        )
        mixed_caps = dict(
            connection.execute(
                """SELECT category_id, -sum(amount_cents) FROM allocations
                   WHERE transaction_id=? AND flow_kind='expense'
                   AND superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL
                   GROUP BY category_id""",
                (mixed_purchase,),
            )
        )
        mixed_net = {
            category_id: cap - int(mixed_parts.get(category_id, 0))
            for category_id, cap in mixed_caps.items()
        }
        if mixed_parts != {"groceries": 5000, "restaurants": 5000}:
            raise VerificationError("MONEY-11 les parts SQL ne respectent pas le split ADR-007")
        if any(amount < 0 for amount in mixed_net.values()) or sum(mixed_net.values()) != 10000:
            raise VerificationError("MONEY-11 le refus SQL a laissé un net catégorie négatif")
        report("MONEY-11 parts ADR-007 et plafonds combinés prorata/ciblé au niveau SQL")
        insert_allocation(
            connection, purchase_b, category_id="groceries", flow_kind="expense", amount_cents=-5000
        )
        confirmed_link = insert_refund_link(
            connection,
            credit,
            purchase_a,
            10000,
            source_allocation_id=source_allocation,
            target_allocation_id=grocery_allocation,
        )
        try:
            insert_refund_link(
                connection,
                credit,
                purchase_a,
                1,
                status="proposed",
                source_allocation_id=second_source_allocation,
                target_allocation_id=grocery_allocation,
            )
        except sqlite3.IntegrityError as error:
            raise VerificationError("UNIQUE active link identity per allocation") from error
        insert_refund_link(
            connection,
            credit,
            purchase_b,
            5000,
            source_allocation_id=second_source_allocation,
        )
        expect_integrity_error(
            "ADR-027 une allocation liée ne peut pas être retirée",
            lambda: connection.execute(
                """UPDATE allocations SET retracted_at_utc='2026-04-02T12:00:00.000Z',
                   retracted_by_decision_id=?, version=version+1 WHERE allocation_id=?""",
                (insert_classification_decision(connection, credit), source_allocation),
            ),
            "allocation_referenced_by_confirmed_refund_link",
        )
        target_c = insert_transaction(
            connection,
            identity=(ACCOUNT_B_ID, "2026-01-10", -5000, "Achat synthétique C"),
            occurrence_rank=1,
        )
        target_c_allocation = insert_allocation(
            connection, target_c, category_id="groceries", flow_kind="expense", amount_cents=-5000
        )
        expect_integrity_error(
            "ADR-027 plafond de source par allocation refund",
            lambda: insert_refund_link(
                connection,
                credit,
                target_c,
                1,
                source_allocation_id=source_allocation,
                target_allocation_id=target_c_allocation,
            ),
            "refund_link_source_cap_exceeded",
        )
        second_credit = insert_transaction(
            connection,
            identity=(ACCOUNT_B_ID, "2026-04-03", 10000, "Autre avoir"),
            occurrence_rank=1,
        )
        second_credit_source = insert_allocation(
            connection,
            second_credit,
            category_id="income_refund",
            flow_kind="refund",
            amount_cents=10000,
        )
        expect_integrity_error(
            "ADR-024.3 plafond de l'allocation expense ciblée",
            lambda: insert_refund_link(
                connection, second_credit, purchase_a, 4000, target_allocation_id=grocery_allocation
            ),
            "refund_link_target_allocation_cap_exceeded",
        )
        proposed_target_overflow = insert_refund_link(
            connection,
            second_credit,
            purchase_a,
            4000,
            status="proposed",
            source_allocation_id=second_credit_source,
            target_allocation_id=grocery_allocation,
        )
        expect_integrity_error(
            "ADR-027 plafond par allocation cible lors de confirmation",
            lambda: connection.execute(
                "UPDATE transaction_links SET status='confirmed', version=version+1 WHERE transaction_link_id=?",
                (proposed_target_overflow,),
            ),
            "refund_link_target_allocation_cap_exceeded",
        )
        expect_integrity_error(
            "ADR-024.3 somme liée plafonnée par les allocations expense, distincte du débit brut",
            lambda: insert_refund_link(connection, second_credit, purchase_a, 5001),
            "refund_link_target_cap_exceeded",
        )
        not_refund = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-04-04", 100, "Crédit non avoir"),
            occurrence_rank=1,
        )
        not_refund_allocation = insert_allocation(
            connection,
            not_refund,
            category_id="income_other_test",
            flow_kind="income_other",
            amount_cents=100,
        )
        direction_target = insert_transaction(
            connection,
            identity=(ACCOUNT_B_ID, "2026-01-11", -100, "Cible de sens synthétique"),
            occurrence_rank=1,
        )
        insert_allocation(
            connection,
            direction_target,
            category_id="groceries",
            flow_kind="expense",
            amount_cents=-100,
        )
        expect_integrity_error(
            "MONEY-10 source sans allocation refund rejetée",
            lambda: insert_refund_link(
                connection,
                not_refund,
                direction_target,
                1,
                source_allocation_id=not_refund_allocation,
            ),
            "refund_link_direction_invalid",
        )
        rejected = insert_refund_link(
            connection,
            second_credit,
            target_c,
            1000,
            status="rejected",
            source_allocation_id=second_credit_source,
        )
        try:
            proposed = insert_refund_link(
                connection,
                second_credit,
                target_c,
                1000,
                status="proposed",
                source_allocation_id=second_credit_source,
            )
        except sqlite3.IntegrityError as error:
            raise VerificationError(
                "ADR-027 le lien rejected bloque une nouvelle proposition"
            ) from error
        statuses = connection.execute(
            """SELECT status FROM transaction_links WHERE transaction_link_id IN (?, ?)
               ORDER BY status""",
            (rejected, proposed),
        ).fetchall()
        if statuses != [("proposed",), ("rejected",)]:
            raise VerificationError("ADR-027 le lien rejeté bloque une nouvelle proposition")
        report("ADR-027 lien rejected conservé sans bloquer une proposition active")
        proposed_source_overflow = insert_refund_link(
            connection,
            credit,
            target_c,
            1,
            status="proposed",
            source_allocation_id=second_source_allocation,
        )
        connection.executemany(
            """INSERT INTO refund_link_allocation_parts
               (transaction_link_id, allocation_id, amount_cents) VALUES (?, ?, ?)""",
            [
                (proposed_source_overflow, allocation_id, part_cents)
                for allocation_id, part_cents in _schema_prorated_parts(connection, target_c, 1)
            ],
        )
        expect_integrity_error(
            "ADR-027 plafond source par allocation lors de confirmation",
            lambda: connection.execute(
                "UPDATE transaction_links SET status='confirmed', version=version+1 WHERE transaction_link_id=?",
                (proposed_source_overflow,),
            ),
            "refund_link_source_cap_exceeded",
        )
        proposed_wrong_source = insert_refund_link(
            connection,
            second_credit,
            purchase_b,
            1,
            status="proposed",
            source_allocation_id=second_credit_source,
        )
        expect_integrity_error(
            "ADR-027 UPDATE d'un lien exige son allocation refund active",
            lambda: connection.execute(
                "UPDATE transaction_links SET source_allocation_id=?, version=version+1 WHERE transaction_link_id=?",
                (not_refund_allocation, proposed_wrong_source),
            ),
            "refund_link_direction_invalid",
        )
        inactive_source_credit = insert_transaction(
            connection,
            identity=(ACCOUNT_B_ID, "2026-04-05", 1000, "Avoir retiré synthétique"),
            occurrence_rank=1,
        )
        inactive_source_allocation = insert_allocation(
            connection,
            inactive_source_credit,
            category_id="income_refund",
            flow_kind="refund",
            amount_cents=1000,
        )
        inactive_source_decision = insert_classification_decision(
            connection, inactive_source_credit
        )
        connection.execute(
            """UPDATE allocations SET retracted_at_utc='2026-04-05T12:00:00.000Z',
               retracted_by_decision_id=?, version=version+1 WHERE allocation_id=?""",
            (inactive_source_decision, inactive_source_allocation),
        )
        expect_integrity_error(
            "ADR-027 liens refusés vers une allocation refund retirée",
            lambda: insert_refund_link(
                connection,
                inactive_source_credit,
                target_c,
                1,
                status="proposed",
                source_allocation_id=inactive_source_allocation,
            ),
            "refund_link_direction_invalid",
        )
        target_d = insert_transaction(
            connection,
            identity=(ACCOUNT_B_ID, "2026-01-12", -1000, "Achat avec part retirée"),
            occurrence_rank=1,
        )
        inactive_target_allocation = insert_allocation(
            connection,
            target_d,
            category_id="groceries",
            flow_kind="expense",
            amount_cents=-400,
        )
        insert_allocation(
            connection,
            target_d,
            category_id="groceries",
            flow_kind="expense",
            amount_cents=-600,
        )
        inactive_target_decision = insert_classification_decision(connection, target_d)
        connection.execute(
            """UPDATE allocations SET retracted_at_utc='2026-01-12T12:00:00.000Z',
               retracted_by_decision_id=?, version=version+1 WHERE allocation_id=?""",
            (inactive_target_decision, inactive_target_allocation),
        )
        expect_integrity_error(
            "ADR-027 liens refusés vers une allocation expense retirée",
            lambda: insert_refund_link(
                connection,
                second_credit,
                target_d,
                1,
                status="proposed",
                source_allocation_id=second_credit_source,
                target_allocation_id=inactive_target_allocation,
            ),
            "refund_link_direction_invalid",
        )
        expect_integrity_error(
            "ADR-027 FK de source_allocation_id",
            lambda: connection.execute(
                """INSERT INTO transaction_links
                   (transaction_link_id, source_transaction_id, target_transaction_id,
                    link_kind, amount_cents, source_allocation_id, status)
                   VALUES (?, ?, ?, 'related', 1, 'missing-source-allocation', 'proposed')""",
                (new_id(), second_credit, target_c),
            ),
            "FOREIGN KEY constraint failed",
        )
        expect_integrity_error(
            "ADR-024.3 FK de target_allocation_id",
            lambda: connection.execute(
                """INSERT INTO transaction_links
                   (transaction_link_id, source_transaction_id, target_transaction_id,
                    link_kind, amount_cents, source_allocation_id, target_allocation_id, status)
                   VALUES (?, ?, ?, 'related', 1, ?, 'missing-allocation', 'proposed')""",
                (new_id(), second_credit, target_c, second_credit_source),
            ),
            "FOREIGN KEY constraint failed",
        )
        source_date, target_account = connection.execute(
            """SELECT s.booked_on, t.account_id FROM transaction_links l
               JOIN transactions s ON s.transaction_id=l.source_transaction_id
               JOIN transactions t ON t.transaction_id=l.target_transaction_id
               WHERE l.link_kind='refund_of' AND l.status='confirmed' AND l.source_transaction_id=?
               ORDER BY t.booked_on DESC LIMIT 1""",
            (credit,),
        ).fetchone()
        if source_date != "2026-04-02" or target_account != ACCOUNT_B_ID:
            raise VerificationError("MONEY-10 date d'avoir ou compte cible absent du drilldown")
        expect_integrity_error(
            "n-05 lien refund confirmé modifiable uniquement par supersession",
            lambda: connection.execute(
                "UPDATE transaction_links SET amount_cents=9999, version=version+1 WHERE transaction_link_id=?",
                (confirmed_link,),
            ),
            "confirmed_transaction_link_supersession_required",
        )
        replacement = new_id()
        audit_id = new_id()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """UPDATE transaction_links
                   SET superseded_by_transaction_link_id=?, superseded_by_audit_event_id=?, version=version+1
                   WHERE transaction_link_id=?""",
                (replacement, audit_id, confirmed_link),
            )
            connection.execute(
                """INSERT INTO transaction_links
                   (transaction_link_id, source_transaction_id, target_transaction_id, link_kind,
                    amount_cents, source_allocation_id, target_allocation_id, status)
                   VALUES (?, ?, ?, 'refund_of', 9000, ?, ?, 'proposed')""",
                (replacement, credit, purchase_a, source_allocation, grocery_allocation),
            )
            connection.execute(
                """INSERT INTO audit_events
                   (audit_event_id, target_type, target_id, action, actor, before_json, after_json)
                   VALUES (?, 'transaction_link', ?, 'supersede', 'user', '{}', '{}')""",
                (audit_id, confirmed_link),
            )
            connection.execute(
                "UPDATE transaction_links SET status='confirmed', version=version+1 "
                "WHERE transaction_link_id=?",
                (replacement,),
            )
            connection.execute("COMMIT")
        except sqlite3.IntegrityError as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise VerificationError(
                "n-11 UPDATE ancien lien puis INSERT remplaçant requiert la FK différée"
            ) from error
        report(
            "n-11 UPDATE ancien lien puis INSERT remplaçant : FK différée, audit et version validés"
        )
        # Mars seul garde l'achat ; avril seul porte l'effet du remboursement.
        march_cost = -connection.execute(
            "SELECT sum(amount_cents) FROM allocations WHERE transaction_id=? AND flow_kind='expense'",
            (purchase_a,),
        ).fetchone()[0]
        april_credit_effect = -connection.execute(
            """SELECT sum(amount_cents) FROM transaction_links
               WHERE source_transaction_id=? AND status='confirmed'
                 AND superseded_by_transaction_link_id IS NULL""",
            (credit,),
        ).fetchone()[0]
        if march_cost != 15000 or april_credit_effect != -14000:
            raise VerificationError(
                "MONEY-10 remboursement inter-mois ne suit pas la date de l'avoir"
            )
        expect_integrity_error(
            "ADR-021 lien de remboursement confirmé non supprimable",
            lambda: connection.execute(
                "DELETE FROM transaction_links WHERE transaction_link_id=?", (confirmed_link,)
            ),
            "confirmed_transaction_link_immutable",
        )
        expect_integrity_error(
            "SEC-06 REPLACE d'un lien de remboursement confirmé rejeté",
            lambda: connection.execute(
                """INSERT OR REPLACE INTO transaction_links
                   (transaction_link_id, source_transaction_id, target_transaction_id, link_kind,
                    amount_cents, source_allocation_id, status)
                   VALUES (?, ?, ?, 'refund_of', 1, ?, 'proposed')""",
                (confirmed_link, credit, purchase_a, source_allocation),
            ),
            "confirmed_transaction_link_immutable",
        )
        report(
            "MONEY-10 refund_of inter-mois/inter-comptes, proposed ignoré, plafonds source/cible"
        )
    finally:
        connection.close()


def verify_insight_facts_and_idempotency(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        insight_columns = {row[1] for row in connection.execute("PRAGMA table_info(insights)")}
        if "metric_definition_id" in insight_columns:
            raise VerificationError("F-04 metric_definition_id doit être porté par insight_facts")
        iid = new_id()
        connection.execute(
            "INSERT INTO insights(insight_id, narrative, generated_by) VALUES (?, 'Synthèse synthétique', 'deterministic')",
            (iid,),
        )
        fact_id = new_id()
        connection.execute(
            """INSERT INTO insight_facts
               (insight_fact_id, insight_id, fact_ordinal, metric_definition_id, period_start,
                period_end, value_kind, raw_cents, filters_json, coverage_json)
               VALUES (?, ?, 1, 'consumption_net.v1', '2026-03-01', '2026-03-31', 'cents', 1200, '{}', '{}')""",
            (fact_id, iid),
        )
        expect_integrity_error(
            "F-04 metric ID d'insight hors registre rejeté",
            lambda: connection.execute(
                """INSERT INTO insight_facts
                   (insight_fact_id, insight_id, fact_ordinal, metric_definition_id, period_start,
                    period_end, value_kind, raw_cents, filters_json, coverage_json)
                   VALUES (?, ?, 2, 'invented_metric.v1', '2026-03-01', '2026-03-31', 'cents', 1, '{}', '{}')""",
                (new_id(), iid),
            ),
            "CHECK constraint failed",
        )
        key = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO idempotency_records
               (idempotency_key, http_method, canonical_path, request_sha256, response_status, response_json)
               VALUES (?, 'POST', '/api/v1/imports/x/commit', ?, 200, '{}')""",
            (key, hashlib.sha256(b"request").hexdigest()),
        )
        created, expires = connection.execute(
            "SELECT created_at_utc, expires_at_utc FROM idempotency_records WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        days = connection.execute(
            "SELECT julianday(?) - julianday(?)", (expires, created)
        ).fetchone()[0]
        if abs(days - 7) > 0.00001:
            raise VerificationError(f"ADR-023 rétention idempotence inattendue : {days} jours")
        report("ADR-023 faits insight normalisés et idempotence expirant après 7 jours")
    finally:
        connection.close()


def verify_backup_hmac_key_flag(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        values = (new_id(), "1", "data/backups/synthetic.zip", "a" * 64, 4096, "verified")
        expect_integrity_error(
            "ADR-023 backup sans clé HMAC rejeté",
            lambda: connection.execute(
                """INSERT INTO backup_manifests
                   (backup_manifest_id, schema_version, archive_relpath, archive_sha256,
                    file_size_bytes, includes_hmac_key, status)
                   VALUES (?, ?, ?, ?, ?, 0, ?)""",
                values,
            ),
            "CHECK constraint failed",
        )
        manifest_id = new_id()
        connection.execute(
            """INSERT INTO backup_manifests
               (backup_manifest_id, schema_version, archive_relpath, archive_sha256,
                file_size_bytes, includes_hmac_key, status)
               VALUES (?, '1', 'data/backups/synthetic.zip', ?, 4096, 1, 'verified')""",
            (manifest_id, "b" * 64),
        )
        key_included, permissions = connection.execute(
            "SELECT includes_hmac_key, archive_permissions_octal FROM backup_manifests WHERE backup_manifest_id=?",
            (manifest_id,),
        ).fetchone()
        if (key_included, permissions) != (1, "0600"):
            raise VerificationError("ADR-023 backup doit inclure HMAC et permissions 0600")
        report("ADR-023 manifeste backup exige la clé HMAC et le mode 0600")
    finally:
        connection.close()


def verify_personal_mode_oracle(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection, account_role="current")
        insert_account(connection, ACCOUNT_B_ID, account_role="personal")
        seed_financial_categories(connection)
        allowance = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-03-01", -25000, "Enveloppe synthétique"),
            occurrence_rank=1,
        )
        personal_credit = insert_transaction(
            connection,
            identity=(ACCOUNT_B_ID, "2026-03-02", 25000, "Virement propre synthétique"),
            occurrence_rank=1,
        )
        personal_spend = insert_transaction(
            connection,
            identity=(ACCOUNT_B_ID, "2026-03-03", -18000, "Dépense personnelle synthétique"),
            occurrence_rank=1,
        )
        insert_allocation(
            connection,
            allowance,
            category_id="flow_personal_a",
            flow_kind="allocation_personal",
            amount_cents=-25000,
        )
        insert_allocation(
            connection,
            personal_credit,
            category_id="flow_owned_transfer",
            flow_kind="transfer_owned",
            amount_cents=25000,
        )
        insert_allocation(
            connection,
            personal_spend,
            category_id="groceries",
            flow_kind="expense",
            amount_cents=-18000,
        )
        envelope_consumption = connection.execute(
            """SELECT coalesce(-sum(a.amount_cents),0) FROM allocations a JOIN transactions t USING(transaction_id)
               JOIN accounts ac USING(account_id) WHERE a.flow_kind='expense' AND ac.account_role<>'personal'
               AND a.superseded_by_allocation_id IS NULL"""
        ).fetchone()[0]
        envelope_allowance = -connection.execute(
            "SELECT coalesce(sum(amount_cents),0) FROM allocations WHERE flow_kind='allocation_personal'"
        ).fetchone()[0]
        observed_consumption = -connection.execute(
            """SELECT coalesce(sum(a.amount_cents),0) FROM allocations a JOIN transactions t USING(transaction_id)
               JOIN accounts ac USING(account_id) WHERE a.flow_kind='expense' AND ac.account_role='personal'
               AND a.superseded_by_allocation_id IS NULL"""
        ).fetchone()[0]
        observed_allowance = 0
        unspent = 25000 - 18000
        if (
            envelope_consumption,
            envelope_allowance,
            observed_consumption,
            observed_allowance,
            unspent,
        ) != (0, 25000, 18000, 0, 7000):
            raise VerificationError("KPI-08 envelope/observed double compte ou reliquat incorrect")
        report(
            "KPI-08 compte personnel importé : envelope (0,25000), observed (18000,0), reliquat 7000"
        )
    finally:
        connection.close()


def verify_import_rollback(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection)
        connection.execute("BEGIN IMMEDIATE")
        iid = insert_import(connection, parsed_operation_count=2)
        insert_section(connection, iid)
        identity = (ACCOUNT_ID, "2026-03-12", -100, "Rollback synthétique")
        tid = insert_transaction(connection, identity=identity, occurrence_rank=1)
        insert_source(connection, iid, tid, 1)
        try:
            # Déclenche une erreur après une transaction et une provenance déjà écrites.
            insert_transaction(connection, identity=identity, occurrence_rank=3)
        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK")
        else:
            connection.execute("ROLLBACK")
            raise VerificationError("IMP-06 la faute au milieu du commit n'a pas échoué")
        counts = (
            connection.execute("SELECT count(*) FROM import_files").fetchone()[0],
            connection.execute("SELECT count(*) FROM transactions").fetchone()[0],
            connection.execute("SELECT count(*) FROM source_rows").fetchone()[0],
        )
        if counts != (0, 0, 0):
            raise VerificationError(f"IMP-06 rollback incomplet : {counts}")
        report(
            "IMP-06 erreur SQL au milieu du commit : fichier/transaction/source_rows tous annulés"
        )
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


def verify_refund_parts_and_state(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        insert_account(connection, account_role="current")
        seed_financial_categories(connection)
        credit = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-05-01", 3000, "Avoir parts synthétique"),
            occurrence_rank=1,
        )
        purchase = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-04-01", -3000, "Achat parts synthétique"),
            occurrence_rank=1,
        )
        source = insert_allocation(
            connection, credit, category_id="income_refund", flow_kind="refund", amount_cents=3000
        )
        target = insert_allocation(
            connection, purchase, category_id="groceries", flow_kind="expense", amount_cents=-3000
        )
        try:
            confirmed = insert_refund_link(
                connection, credit, purchase, 1000, source_allocation_id=source
            )
        except sqlite3.IntegrityError as error:
            raise VerificationError(
                "MONEY-18 somme des parts égale au montant du lien confirmé: "
                f"attribution valide rejetée ({error})"
            ) from error
        part = connection.execute(
            "SELECT amount_cents FROM refund_link_allocation_parts "
            "WHERE transaction_link_id=? AND allocation_id=?",
            (confirmed, target),
        ).fetchone()
        total = connection.execute(
            "SELECT sum(amount_cents) FROM refund_link_allocation_parts "
            "WHERE transaction_link_id=?",
            (confirmed,),
        ).fetchone()[0]
        if part != (1000,) or total != 1000:
            raise VerificationError("MONEY-18 part matérialisée positive et somme égale au lien")
        expect_integrity_error(
            "MONEY-18 modification d'une part matérialisée interdite",
            lambda: connection.execute(
                "UPDATE refund_link_allocation_parts SET amount_cents=amount_cents+1 "
                "WHERE transaction_link_id=? AND allocation_id=?",
                (confirmed, target),
            ),
            "refund_link_part_immutable",
        )
        expect_integrity_error(
            "MONEY-18 suppression d'une part matérialisée interdite",
            lambda: connection.execute(
                "DELETE FROM refund_link_allocation_parts "
                "WHERE transaction_link_id=? AND allocation_id=?",
                (confirmed, target),
            ),
            "refund_link_part_delete_forbidden",
        )

        second_credit = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-05-02", 1000, "Deuxième avoir parts synthétique"),
            occurrence_rank=1,
        )
        second_source = insert_allocation(
            connection,
            second_credit,
            category_id="income_refund",
            flow_kind="refund",
            amount_cents=1000,
        )
        proposed = insert_refund_link(
            connection,
            second_credit,
            purchase,
            100,
            status="proposed",
            source_allocation_id=second_source,
        )
        expect_integrity_error(
            "MONEY-18 signe strictement positif des parts",
            lambda: connection.execute(
                "INSERT INTO refund_link_allocation_parts "
                "(transaction_link_id, allocation_id, amount_cents) VALUES (?, ?, 0)",
                (proposed, target),
            ),
            "CHECK constraint failed",
        )
        expect_integrity_error(
            "MONEY-18 somme des parts égale au montant du lien confirmé",
            lambda: connection.execute(
                "UPDATE transaction_links SET status='confirmed', version=version+1 "
                "WHERE transaction_link_id=?",
                (proposed,),
            ),
            "refund_link_attribution_total_mismatch",
        )

        target_with_inactive = insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-04-02", -1000, "Achat allocation inactive synthétique"),
            occurrence_rank=1,
        )
        inactive = insert_allocation(
            connection,
            target_with_inactive,
            category_id="groceries",
            flow_kind="expense",
            amount_cents=-400,
        )
        active = insert_allocation(
            connection,
            target_with_inactive,
            category_id="groceries",
            flow_kind="expense",
            amount_cents=-600,
        )
        decision = insert_classification_decision(connection, target_with_inactive)
        connection.execute(
            """UPDATE allocations SET retracted_at_utc='2026-04-02T12:00:00.000Z',
               retracted_by_decision_id=?, version=version+1 WHERE allocation_id=?""",
            (decision, inactive),
        )
        stale_target_link = insert_refund_link(
            connection,
            second_credit,
            target_with_inactive,
            100,
            status="proposed",
            source_allocation_id=second_source,
        )
        expect_integrity_error(
            "MONEY-18 une part ne peut pas cibler une allocation inactive",
            lambda: connection.execute(
                "INSERT INTO refund_link_allocation_parts "
                "(transaction_link_id, allocation_id, amount_cents) VALUES (?, ?, 1)",
                (stale_target_link, inactive),
            ),
            "refund_link_part_target_invalid",
        )
        if active == inactive:
            raise VerificationError("MONEY-18 fixture allocations actives/inactives distinctes")

        rejected = insert_refund_link(
            connection,
            second_credit,
            target_with_inactive,
            100,
            status="rejected",
            source_allocation_id=second_source,
            target_allocation_id=active,
        )
        def update_status(link_id: str, new_status: str) -> None:
            connection.execute(
                "UPDATE transaction_links SET status=?, version=version+1 "
                "WHERE transaction_link_id=?",
                (new_status, link_id),
            )

        for old_status, new_status in (
            ("rejected", "confirmed"),
            ("rejected", "proposed"),
        ):
            expect_integrity_error(
                f"MONEY-19 transition {old_status} → {new_status} interdite",
                partial(update_status, rejected, new_status),
                "refund_link_status_transition_invalid",
            )
        for new_status in ("proposed", "rejected"):
            expect_integrity_error(
                f"MONEY-19 transition confirmed → {new_status} interdite",
                partial(update_status, confirmed, new_status),
                (
                    "refund_link_status_transition_invalid",
                    "confirmed_transaction_link_supersession_required",
                ),
            )
        expect_integrity_error(
            "MONEY-19 INSERT d'un refund directement confirmé interdit",
            lambda: connection.execute(
                """INSERT INTO transaction_links
                   (transaction_link_id, source_transaction_id, target_transaction_id,
                    link_kind, amount_cents, source_allocation_id, target_allocation_id, status)
                   VALUES (?, ?, ?, 'refund_of', 100, ?, ?, 'confirmed')""",
                (
                    new_id(),
                    second_credit,
                    target_with_inactive,
                    second_source,
                    active,
                ),
            ),
            "refund_link_initial_status_invalid",
        )
        report("MONEY-18 gardes SQL de parts: somme, allocation active, signe et immutabilité")
        report("MONEY-19 machine d'états SQL des liens refund")
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


def verify_two_connection_noop(schema_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="verify-schema-imp07-") as temp_dir:
        db_path = Path(temp_dir) / "synthetic.sqlite"
        init = open_connection(db_path, schema_path)
        insert_account(init)
        init.close()
        first = open_connection(db_path)
        second = open_connection(db_path, check_same_thread=False)
        digest = hashlib.sha256(b"concurrent synthetic file").hexdigest()
        second_started = threading.Event()
        second_acquired = threading.Event()
        result: list[str] = []
        failure: list[BaseException] = []
        try:
            first.execute("BEGIN IMMEDIATE")
            if (
                first.execute(
                    "SELECT 1 FROM import_files WHERE file_sha256=? AND status='committed'",
                    (digest,),
                ).fetchone()
                is not None
            ):
                raise VerificationError("IMP-07 hash synthétique déjà présent avant la course")

            def second_writer() -> None:
                try:
                    second_started.set()
                    second.execute("BEGIN IMMEDIATE")
                    second_acquired.set()
                    original = second.execute(
                        "SELECT import_file_id FROM import_files WHERE file_sha256=? AND status='committed'",
                        (digest,),
                    ).fetchone()
                    if original is None:
                        raise VerificationError(
                            "IMP-07 la seconde connexion n'a pas vu le commit initial"
                        )
                    no_op = insert_import(
                        second, digest=digest, status="no_op", duplicate_of=original[0]
                    )
                    second.execute(
                        """INSERT INTO audit_events(audit_event_id, import_file_id, target_type, target_id, action, actor)
                           VALUES (?, ?, 'import', ?, 'import.no_op', 'verify_schema')""",
                        (new_id(), no_op, original[0]),
                    )
                    second.execute("COMMIT")
                    result.append("no_op")
                except BaseException as error:
                    failure.append(error)
                    if second.in_transaction:
                        second.execute("ROLLBACK")

            thread = threading.Thread(target=second_writer, daemon=True)
            thread.start()
            if not second_started.wait(2):
                raise VerificationError("IMP-07 seconde connexion n'a pas démarré")
            # Tant que la première garde le verrou, BEGIN IMMEDIATE de la seconde attend.
            if second_acquired.wait(0.1):
                raise VerificationError(
                    "IMP-07 la seconde connexion a lu avant d'obtenir le verrou"
                )
            original = insert_import(first, digest=digest)
            insert_section(first, original)
            commit_import(first, original)
            first.execute("COMMIT")
            if not second_acquired.wait(4):
                raise VerificationError(
                    "IMP-07 seconde connexion bloquée après libération du verrou"
                )
            thread.join(timeout=4)
            if thread.is_alive() or failure:
                raise VerificationError(f"IMP-07 erreur du second écrivain : {failure}")
            count = first.execute(
                "SELECT count(*) FROM import_files WHERE file_sha256=? AND status='committed'",
                (digest,),
            ).fetchone()[0]
            noops = first.execute(
                "SELECT count(*) FROM import_files WHERE file_sha256=? AND status='no_op'",
                (digest,),
            ).fetchone()[0]
            if (count, noops, result) != (1, 1, ["no_op"]):
                raise VerificationError(
                    f"IMP-07 cardinalité concurrente incorrecte : {count}, {noops}, {result}"
                )
            report(
                "IMP-07 deux connexions : hash relu sous BEGIN IMMEDIATE, seconde tentative no_op"
            )
        finally:
            if first.in_transaction:
                first.execute("ROLLBACK")
            if second.in_transaction:
                second.execute("ROLLBACK")
            first.close()
            second.close()


def rows_for(groups: Iterable[tuple[str, str, int, str, int]]) -> list[Identity]:
    result: list[Identity] = []
    for booked_on, label, amount, _tag, multiplicity in groups:
        result.extend([(ACCOUNT_ID, booked_on, amount, label)] * multiplicity)
    return result


def run_export(
    connection: sqlite3.Connection,
    *,
    content: bytes,
    rows: list[Identity],
    file_label: str,
) -> tuple[int, bool]:
    """Oracle miniature du service : lookup du SHA sous le verrou, puis max multiplicité."""
    digest = hashlib.sha256(content).hexdigest()
    connection.execute("BEGIN IMMEDIATE")
    existing = connection.execute(
        "SELECT import_file_id FROM import_files WHERE file_sha256=? AND status='committed'",
        (digest,),
    ).fetchone()
    if existing:
        no_op_id = insert_import(
            connection, digest=digest, status="no_op", duplicate_of=existing[0]
        )
        connection.execute(
            """INSERT INTO audit_events(audit_event_id, import_file_id, target_type, target_id, action, actor)
               VALUES (?, ?, 'import', ?, 'import.no_op', 'verify_schema')""",
            (new_id(), no_op_id, existing[0]),
        )
        connection.execute("COMMIT")
        return connection.execute("SELECT count(*) FROM transactions").fetchone()[0], True

    import_id = insert_import(connection, digest=digest, parsed_operation_count=len(rows))
    insert_section(
        connection, import_id, ACCOUNT_ID, coverage_from="2026-01-01", coverage_through="2026-12-31"
    )
    occurrences: dict[Identity, list[int]] = defaultdict(list)
    for logical_row_idx, identity in enumerate(rows, start=1):
        occurrences[identity].append(logical_row_idx)

    for identity in sorted(occurrences):
        ranks = [
            row[0]
            for row in connection.execute(
                """SELECT occurrence_rank FROM transactions WHERE account_id=? AND booked_on=?
               AND amount_cents=? AND identity_label=? ORDER BY occurrence_rank""",
                identity,
            )
        ]
        if ranks and (len(ranks) != max(ranks) or ranks != list(range(1, max(ranks) + 1))):
            raise VerificationError("IMP-14 n_existing count != max : rangs non contigus")
        n_existing = max(ranks, default=0)
        n_new = len(occurrences[identity])
        known = {
            rank: transaction_id
            for rank, transaction_id in connection.execute(
                """SELECT occurrence_rank, transaction_id FROM transactions WHERE account_id=? AND booked_on=?
                   AND amount_cents=? AND identity_label=?""",
                identity,
            )
        }
        for rank in range(1, max(n_existing, n_new) + 1):
            transaction_id = known.get(rank)
            if transaction_id is None:
                transaction_id = insert_transaction(
                    connection, identity=identity, occurrence_rank=rank
                )
            if rank <= n_new:
                logical_row_idx = occurrences[identity][rank - 1]
                # Le hash de ligne est synthétique ; aucune donnée bancaire n'est utilisée.
                insert_source(connection, import_id, transaction_id, logical_row_idx)
    connection.execute(
        """INSERT INTO audit_events(audit_event_id, import_file_id, target_type, target_id, action, actor)
           VALUES (?, ?, 'import', ?, 'import.commit', 'verify_schema')""",
        (new_id(), import_id, import_id),
    )
    commit_import(connection, import_id)
    connection.execute("COMMIT")
    return connection.execute("SELECT count(*) FROM transactions").fetchone()[0], False


def transaction_signature(connection: sqlite3.Connection) -> list[tuple[str, str, int, str, int]]:
    return connection.execute(
        """SELECT account_id, booked_on, amount_cents, identity_label, occurrence_rank
           FROM transactions ORDER BY account_id, booked_on, amount_cents, identity_label, occurrence_rank"""
    ).fetchall()


def verify_IMP_01_to_IMP_05_and_IMP_09_order_independence(schema_path: Path) -> None:
    a_rows = rows_for(
        [
            ("2026-03-12", "Paiement répété", -1123, "x", 2),
            ("2026-03-12", "Crédit similaire", 1123, "y", 1),
        ]
    )
    b_rows = rows_for(
        [
            ("2026-03-12", "Paiement répété", -1123, "x", 3),
            ("2026-03-12", "Crédit similaire", 1123, "y", 1),
            ("2026-03-13", "Paiement distinct", -50, "z", 1),
            ("2026-01-10", "Opération ancienne nouvellement exportée", -75, "old", 1),
        ]
    )
    a_bytes = b"synthetic export A"
    b_bytes = b"synthetic export B"
    forward = connect(schema_path)
    reverse = connect(schema_path)
    try:
        insert_account(forward)
        insert_account(reverse)
        observed: list[int] = []
        seen: set[bytes] = set()
        sequence = (
            ("A", a_bytes, a_rows),
            ("B", b_bytes, b_rows),
            ("A", a_bytes, a_rows),
            ("B", b_bytes, b_rows),
            ("A", a_bytes, a_rows),
        )
        for label, content, rows in sequence:
            count, was_noop = run_export(forward, content=content, rows=rows, file_label=label)
            observed.append(count)
            expected = 3 if label == "A" and not observed[:-1] else 6
            if count != expected or was_noop != (content in seen):
                raise VerificationError(f"IMP-01..05 séquence A/B/A/B/A invalide après {label}")
            seen.add(content)
        b_reverse = list(reversed(b_rows))
        a_reverse = list(reversed(a_rows))
        run_export(reverse, content=b_bytes, rows=b_reverse, file_label="B reversed")
        run_export(reverse, content=a_bytes, rows=a_reverse, file_label="A reversed")
        expected_signature = [
            (ACCOUNT_ID, "2026-01-10", -75, "Opération ancienne nouvellement exportée", 1),
            (ACCOUNT_ID, "2026-03-12", -1123, "Paiement répété", 1),
            (ACCOUNT_ID, "2026-03-12", -1123, "Paiement répété", 2),
            (ACCOUNT_ID, "2026-03-12", -1123, "Paiement répété", 3),
            (ACCOUNT_ID, "2026-03-12", 1123, "Crédit similaire", 1),
            (ACCOUNT_ID, "2026-03-13", -50, "Paiement distinct", 1),
        ]
        if observed != [3, 6, 6, 6, 6] or transaction_signature(forward) != expected_signature:
            raise VerificationError(f"IMP-01..05 cardinalité ou identité inattendue : {observed}")
        if transaction_signature(forward) != transaction_signature(reverse):
            raise VerificationError("IMP-05 A/B et B/A ou ordre de lignes divergent")
        if forward.execute("SELECT count(*) FROM source_rows").fetchone()[0] != 9:
            raise VerificationError("IMP-02 occurrences A+B : attendu 9 source_rows")
        if forward.execute(
            "SELECT count(*) FROM transactions WHERE NOT EXISTS (SELECT 1 FROM source_rows s WHERE s.transaction_id=transactions.transaction_id)"
        ).fetchone()[0]:
            raise VerificationError("IMP-02 une transaction n'a aucune source")
        report("IMP-01..05 A/B/A/B/A → transactions [3, 6, 6, 6, 6], rangs 1..3")
        report("IMP-05 B/A et ordre des lignes inversé → même signature")
        report("IMP-09 opération ancienne dans B importée malgré une date maximale plus récente")
        report("IMP-02 provenance par occurrence : 9 source_rows pour 6 transactions")
    finally:
        forward.close()
        reverse.close()


def remove_sql_object(text: str, kind: str, name: str) -> str:
    if kind == "trigger":
        pattern = rf"(?ims)^CREATE TRIGGER {re.escape(name)}\b.*?^END;\s*"
    elif kind == "index":
        pattern = rf"(?ims)^CREATE (?:UNIQUE )?INDEX {re.escape(name)}\b.*?;\s*"
    else:
        raise ValueError(kind)
    changed, count = re.subn(pattern, "", text, count=1)
    if count != 1:
        raise VerificationError(f"preuve temporaire : objet SQL introuvable {kind} {name}")
    return changed


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise VerificationError(f"preuve temporaire : cible ambiguë pour {label}")
    return text.replace(old, new, 1)


def mutate_trigger_clause(text: str, trigger_name: str, old: str, new: str, label: str) -> str:
    start = text.index(f"CREATE TRIGGER {trigger_name}")
    end = text.index("\nEND;", start) + len("\nEND;")
    prefix, trigger, suffix = text[:start], text[start:end], text[end:]
    return prefix + replace_once(trigger, old, new, label) + suffix


def mutate_active_allocation_total_filter(text: str) -> str:
    old = "AND a.superseded_by_allocation_id IS NULL\n                            AND a.retracted_at_utc IS NULL"
    start = text.index("CREATE TRIGGER trg_allocations_total_cap")
    end = text.index("\nEND;", start) + len("\nEND;")
    prefix, trigger, suffix = text[:start], text[start:end], text[end:]
    if trigger.count(old) != 2:
        raise VerificationError("preuve temporaire : filtre actif allocation cap ambigu")
    return prefix + trigger.replace(old, "AND a.superseded_by_allocation_id IS NULL") + suffix


def mutate_refund_source_active_filter(text: str) -> str:
    return mutate_trigger_clause(
        text,
        "trg_refund_links_confirmed_direction_insert",
        "AND src.retracted_at_utc IS NULL",
        "",
        "active refund source filter",
    )


def mutate_refund_target_active_filter(text: str) -> str:
    return mutate_trigger_clause(
        text,
        "trg_refund_links_confirmed_direction_insert",
        "AND a.superseded_by_allocation_id IS NULL\n                AND a.retracted_at_utc IS NULL",
        "AND a.superseded_by_allocation_id IS NULL",
        "active refund target filter",
    )


def remove_refund_target_cap_guards(text: str) -> str:
    for trigger_name in (
        "trg_refund_links_target_cap_insert",
        "trg_refund_links_target_cap_update",
        "trg_refund_links_confirmed_allocation_caps_insert",
        "trg_refund_links_confirmed_allocation_caps_update",
    ):
        text = remove_sql_object(text, "trigger", trigger_name)
    return text


def remove_refund_target_allocation_insert_guards(text: str) -> str:
    for trigger_name in (
        "trg_refund_links_target_allocation_cap_insert",
        "trg_refund_links_confirmed_allocation_caps_insert",
    ):
        text = remove_sql_object(text, "trigger", trigger_name)
    return text


def remove_refund_target_allocation_update_guards(text: str) -> str:
    for trigger_name in (
        "trg_refund_links_target_allocation_cap_update",
        "trg_refund_links_confirmed_allocation_caps_update",
    ):
        text = remove_sql_object(text, "trigger", trigger_name)
    return text


def mutate_transaction_amount_clause(text: str, old: str, new: str, label: str) -> str:
    start = text.index("CREATE TABLE transactions (")
    end = text.index("\n);", start)
    prefix, table, suffix = text[:start], text[start:end], text[end:]
    return prefix + replace_once(table, old, new, label) + suffix


def mutate_table_clause(text: str, table_name: str, old: str, new: str, label: str) -> str:
    start = text.index(f"CREATE TABLE {table_name} (")
    end = text.index("\n);", start)
    prefix, table, suffix = text[:start], text[start:end], text[end:]
    return prefix + replace_once(table, old, new, label) + suffix


def remove_uuid_primary_key_check(text: str, table_name: str, column_name: str) -> str:
    start = text.index(f"CREATE TABLE {table_name} (")
    end = text.index("\n);", start)
    prefix, table, suffix = text[:start], text[start:end], text[end:]
    pattern = re.compile(
        rf"({re.escape(column_name)} TEXT NOT NULL PRIMARY KEY) "
        rf"CHECK \(length\({re.escape(column_name)}\) = 36 AND "
        rf"{re.escape(column_name)} GLOB '[^']+'\)"
    )
    mutated, count = pattern.subn(r"\1", table, count=1)
    if count != 1:
        raise VerificationError(
            f"preuve temporaire : UUIDv4 CHECK introuvable {table_name}.{column_name}"
        )
    return prefix + mutated + suffix


def verify_constraint_mutation_proofs(schema_path: Path) -> None:
    source = schema_path.read_text(encoding="utf-8")
    mutations: list[tuple[str, Callable[[str], str], Callable[[Path], None], str]] = [
        (
            "UUIDv4 primary key",
            lambda s: remove_uuid_primary_key_check(s, "accounts", "account_id"),
            verify_primary_key_timestamp_and_version_guards,
            "UUIDv4 CHECK absent: accounts.account_id",
        ),
        (
            "strict UTC timestamp",
            lambda s: mutate_table_clause(
                s,
                "accounts",
                "CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),",
                "CHECK (1 = 1),",
                "created_at_utc CHECK",
            ),
            verify_primary_key_timestamp_and_version_guards,
            "horodatage UTC strict absent: accounts.created_at_utc",
        ),
        (
            "optimistic version trigger",
            lambda s: remove_sql_object(s, "trigger", "trg_accounts_version_increment"),
            verify_primary_key_timestamp_and_version_guards,
            "trigger de version absent: accounts",
        ),
        (
            "rank contiguous",
            lambda s: remove_sql_object(s, "trigger", "trg_transactions_occurrence_contiguous"),
            verify_occurrence_rank_guards,
            "IMP-14 rang 3 sans rang 2",
        ),
        (
            "foreign key account",
            lambda s: mutate_transaction_amount_clause(
                s,
                "account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE RESTRICT,",
                "account_id TEXT NOT NULL,",
                "transaction account FK",
            ),
            verify_foreign_key,
            "FK-01 compte inexistant",
        ),
        (
            "source/account match",
            lambda s: remove_sql_object(s, "trigger", "trg_source_rows_account_matches_section"),
            verify_section_and_provenance_guards,
            "IMP-14 source_row compte différent",
        ),
        (
            "source/import status",
            lambda s: remove_sql_object(s, "trigger", "trg_source_rows_import_status"),
            verify_section_and_provenance_guards,
            "INV-01 source_row d'un import rejected",
        ),
        (
            "no sources on rejected import",
            lambda s: remove_sql_object(
                s, "trigger", "trg_import_files_reject_requires_no_sources"
            ),
            verify_section_and_provenance_guards,
            "n-06 import avec source_row ne peut pas devenir rejected",
        ),
        (
            "source identity hash equality",
            lambda s: remove_sql_object(
                s, "trigger", "trg_source_rows_identity_matches_transaction"
            ),
            verify_section_and_provenance_guards,
            "n-08 source_row identity_key_sha256 doit égaler la transaction cible",
        ),
        (
            "source same-transaction uniqueness",
            lambda s: replace_once(
                s,
                "    UNIQUE (import_file_id, section_index, logical_row_idx),\n    UNIQUE (import_file_id, transaction_id)\n",
                "    UNIQUE (import_file_id, section_index, logical_row_idx)\n",
                "UNIQUE source import/transaction",
            ),
            verify_section_and_provenance_guards,
            "IMP-14 une source par fichier",
        ),
        (
            "source same-row uniqueness",
            lambda s: replace_once(
                s,
                "    UNIQUE (import_file_id, section_index, logical_row_idx),\n    UNIQUE (import_file_id, transaction_id)\n",
                "    UNIQUE (import_file_id, transaction_id)\n",
                "UNIQUE source logical row",
            ),
            verify_section_and_provenance_guards,
            "IMP-14 logical_row_idx unique",
        ),
        (
            "source committed capacity",
            lambda s: remove_sql_object(s, "trigger", "trg_source_rows_import_capacity"),
            verify_section_and_provenance_guards,
            "ADR-019.b capacité de provenance",
        ),
        (
            "unique account section",
            lambda s: remove_sql_object(s, "index", "uq_import_sections_file_account"),
            verify_section_and_provenance_guards,
            "IMP-13 second section mappée",
        ),
        (
            "committed file hash unique",
            lambda s: remove_sql_object(s, "index", "uq_import_files_committed_sha256"),
            verify_noop_and_committed_hash,
            "IMP-01 hash de fichier commité dupliqué",
        ),
        (
            "import commit guard",
            lambda s: remove_sql_object(s, "trigger", "trg_import_files_commit_guard"),
            verify_import_status_and_immutability,
            "ADR-019.c nombre de source_rows",
        ),
        (
            "fallback complete-through boundary",
            lambda s: remove_sql_object(s, "trigger", "trg_import_files_commit_guard"),
            verify_fallback_complete_through_guard,
            "IMP-11 complete_through repli ne peut inclure le dernier jour avec opérations",
        ),
        (
            "direct commit forbidden",
            lambda s: remove_sql_object(s, "trigger", "trg_import_files_no_direct_commit"),
            verify_import_status_and_immutability,
            "ADR-019.d INSERT direct au statut committed",
        ),
        (
            "no_op reference validated",
            lambda s: remove_sql_object(s, "trigger", "trg_import_files_noop_validate"),
            verify_import_status_and_immutability,
            "ADR-019.d no_op doit viser",
        ),
        (
            "committed import immutable",
            lambda s: remove_sql_object(s, "trigger", "trg_import_files_committed_immutable"),
            verify_import_status_and_immutability,
            "ADR-019.d métadonnées de fichier",
        ),
        (
            "import file no delete",
            lambda s: remove_sql_object(s, "trigger", "trg_import_files_no_delete"),
            verify_noop_and_committed_hash,
            "ADR-019.d historique no_op",
        ),
        (
            "committed section immutable",
            lambda s: remove_sql_object(
                s, "trigger", "trg_import_sections_committed_immutable_update"
            ),
            verify_import_status_and_immutability,
            "ADR-019.d couverture/mapping",
        ),
        (
            "committed section no delete",
            lambda s: remove_sql_object(
                s, "trigger", "trg_import_sections_committed_immutable_delete"
            ),
            verify_import_status_and_immutability,
            "ADR-019.d section vide",
        ),
        (
            "preview decision insert cutoff",
            lambda s: remove_sql_object(
                s, "trigger", "trg_import_rejections_insert_previewed_only"
            ),
            verify_import_status_and_immutability,
            "n-07 ajout de décision après commit interdit",
        ),
        (
            "preview decision update immutable",
            lambda s: remove_sql_object(
                s, "trigger", "trg_import_rejections_committed_immutable_update"
            ),
            verify_import_status_and_immutability,
            "n-07 décision d'aperçu d'un import commité non modifiable",
        ),
        (
            "preview decision delete immutable",
            lambda s: remove_sql_object(
                s, "trigger", "trg_import_rejections_committed_immutable_delete"
            ),
            verify_import_status_and_immutability,
            "n-07 décision d'aperçu d'un import commité non supprimable",
        ),
        (
            "transaction source update immutable",
            lambda s: remove_sql_object(s, "trigger", "trg_transactions_source_immutable"),
            verify_connection_pragmas_and_replace,
            "ADR-019.b UPDATE d'un champ source transaction",
        ),
        (
            "transaction no delete",
            lambda s: remove_sql_object(s, "trigger", "trg_transactions_no_delete"),
            verify_connection_pragmas_and_replace,
            "SEC-06 DELETE d'une transaction sans provenance",
        ),
        (
            "source row update immutable",
            lambda s: remove_sql_object(s, "trigger", "trg_source_rows_immutable"),
            verify_connection_pragmas_and_replace,
            "ADR-019.b UPDATE d'une source_row",
        ),
        (
            "source row no delete",
            lambda s: remove_sql_object(s, "trigger", "trg_source_rows_no_delete"),
            verify_connection_pragmas_and_replace,
            "SEC-06 DELETE d'une source_row",
        ),
        (
            "transaction sign",
            lambda s: remove_sql_object(s, "trigger", "trg_allocations_transaction_sign"),
            verify_allocations,
            "ADR-019.e allocation de signe différent",
        ),
        (
            "allocation total cap",
            lambda s: remove_sql_object(s, "trigger", "trg_allocations_total_cap"),
            verify_allocations,
            "ADR-019.e allocation confirmée au-delà",
        ),
        (
            "retracted allocations excluded from cap",
            mutate_active_allocation_total_filter,
            verify_allocations,
            "ADR-027 retrait audité, immuable et exclu du plafond/somme actifs",
        ),
        (
            "allocation inserted active only",
            lambda s: remove_sql_object(s, "trigger", "trg_allocations_insert_active_only"),
            verify_allocations,
            "ADR-027 retrait initial direct interdit",
        ),
        (
            "allocation retraction decision transaction",
            lambda s: remove_sql_object(
                s, "trigger", "trg_allocations_retraction_decision_same_transaction"
            ),
            verify_allocations,
            "ADR-027 retrait d'allocation exige une décision de la même transaction",
        ),
        (
            "allocation retraction decision foreign key",
            lambda s: mutate_table_clause(
                s,
                "allocations",
                "retracted_by_decision_id TEXT REFERENCES classification_decisions(decision_id) ON DELETE RESTRICT,",
                "retracted_by_decision_id TEXT,",
                "allocation retraction decision foreign key",
            ),
            verify_allocations,
            "ADR-027 décision de retrait référencée non supprimable",
        ),
        (
            "allocation referenced by confirmed links immutable",
            lambda s: remove_sql_object(s, "trigger", "trg_allocations_referenced_links_immutable"),
            verify_refund_links,
            "ADR-027 une allocation liée ne peut pas être retirée",
        ),
        (
            "category direction",
            lambda s: remove_sql_object(s, "trigger", "trg_allocations_category_direction_insert"),
            verify_allocations,
            "ADR-019.e catégorie flow_savings_out",
        ),
        (
            "allocation supersession existing target transaction",
            lambda s: remove_sql_object(
                s, "trigger", "trg_allocations_supersession_existing_target_match"
            ),
            verify_allocations,
            "ADR-027 supersession d'allocation exige la même transaction",
        ),
        (
            "allocation update immutability",
            lambda s: remove_sql_object(s, "trigger", "trg_allocations_update_only_supersede"),
            verify_allocations,
            "ADR-027 allocation retirée immuable",
        ),
        (
            "allocation no delete",
            lambda s: remove_sql_object(s, "trigger", "trg_allocations_no_delete"),
            verify_allocations,
            "ADR-019.e DELETE d'allocation confirmée",
        ),
        (
            "refund direction",
            lambda s: remove_sql_object(
                s, "trigger", "trg_refund_links_confirmed_direction_insert"
            ),
            verify_refund_links,
            "contrainte absente ou non appliquée : MONEY-13 lien SQL inter-comptes refusé",
        ),
        (
            "refund direction update",
            lambda s: remove_sql_object(
                s, "trigger", "trg_refund_links_confirmed_direction_update"
            ),
            verify_refund_links,
            "ADR-027 UPDATE d'un lien exige son allocation refund active",
        ),
        (
            "refund source active",
            mutate_refund_source_active_filter,
            verify_refund_links,
            "ADR-027 liens refusés vers une allocation refund retirée",
        ),
        (
            "refund target active",
            mutate_refund_target_active_filter,
            verify_refund_links,
            "ADR-027 liens refusés vers une allocation expense retirée",
        ),
        (
            "refund source cap on confirmation",
            lambda s: remove_sql_object(s, "trigger", "trg_refund_links_source_cap_update"),
            verify_refund_links,
            "ADR-027 plafond de source par allocation refund",
        ),
        (
            "refund target cap",
            remove_refund_target_cap_guards,
            verify_refund_links,
            "MONEY-11 SQL lien prorata puis ciblé dépasse le plafond d'allocation",
        ),
        (
            "refund combined allocation caps",
            lambda s: remove_sql_object(
                remove_sql_object(
                    s,
                    "trigger",
                    "trg_refund_links_confirmed_allocation_caps_insert",
                ),
                "trigger",
                "trg_refund_links_confirmed_allocation_caps_update",
            ),
            verify_refund_links,
            "MONEY-11 SQL lien prorata puis ciblé dépasse le plafond d'allocation",
        ),
        (
            "refund cross-account scope",
            lambda s: mutate_trigger_clause(
                s,
                "trg_refund_links_confirmed_direction_insert",
                "          AND s.account_id = t.account_id\n",
                "",
                "refund account scope guard",
            ),
            verify_refund_links,
            "contrainte absente ou non appliquée : MONEY-13 lien SQL inter-comptes refusé",
        ),
        (
            "refund target allocation cap update",
            remove_refund_target_allocation_update_guards,
            verify_refund_links,
            "MONEY-11 SQL lien prorata puis ciblé dépasse le plafond d'allocation",
        ),
        (
            "confirmed refund link no delete",
            lambda s: remove_sql_object(s, "trigger", "trg_confirmed_transaction_links_no_delete"),
            verify_refund_links,
            "ADR-021 lien de remboursement confirmé non supprimable",
        ),
        (
            "refund part exact sum",
            lambda s: mutate_trigger_clause(
                s,
                "trg_refund_links_confirmed_attribution_update",
                "), 0) <> NEW.amount_cents",
                "), 0) = NEW.amount_cents",
                "refund attribution exact sum",
            ),
            verify_refund_parts_and_state,
            "MONEY-18 somme des parts égale au montant du lien confirmé",
        ),
        (
            "refund part active target",
            lambda s: mutate_trigger_clause(
                s,
                "trg_refund_link_allocation_parts_insert_guard",
                "          AND a.amount_cents < 0 "
                "AND a.superseded_by_allocation_id IS NULL\n"
                "          AND a.retracted_at_utc IS NULL",
                "          AND a.amount_cents < 0",
                "refund part active target filter",
            ),
            verify_refund_parts_and_state,
            "MONEY-18 une part ne peut pas cibler une allocation inactive",
        ),
        (
            "refund part positive integer sign",
            lambda s: mutate_table_clause(
                s,
                "refund_link_allocation_parts",
                "amount_cents INTEGER NOT NULL CHECK "
                "(typeof(amount_cents) = 'integer' AND amount_cents > 0),",
                "amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer'),",
                "refund part positive integer",
            ),
            verify_refund_parts_and_state,
            "MONEY-18 signe strictement positif des parts",
        ),
        (
            "refund part update forbidden",
            lambda s: remove_sql_object(
                s, "trigger", "trg_refund_link_allocation_parts_update_forbidden"
            ),
            verify_refund_parts_and_state,
            "MONEY-18 modification d'une part matérialisée interdite",
        ),
        (
            "refund part delete forbidden",
            lambda s: remove_sql_object(
                s, "trigger", "trg_refund_link_allocation_parts_delete_forbidden"
            ),
            verify_refund_parts_and_state,
            "MONEY-18 suppression d'une part matérialisée interdite",
        ),
        (
            "refund state transitions",
            lambda s: remove_sql_object(s, "trigger", "trg_refund_links_status_transition"),
            verify_refund_parts_and_state,
            "MONEY-19 transition rejected → confirmed interdite",
        ),
        (
            "refund initial status",
            lambda s: remove_sql_object(s, "trigger", "trg_refund_links_initial_status"),
            verify_refund_parts_and_state,
            "MONEY-19 INSERT d'un refund directement confirmé interdit",
        ),
        (
            "confirmed refund link supersession only",
            lambda s: remove_sql_object(
                s, "trigger", "trg_confirmed_transaction_links_supersede_only"
            ),
            verify_refund_links,
            "n-05 lien refund confirmé modifiable uniquement par supersession",
        ),
        (
            "refund target allocation FK",
            lambda s: mutate_table_clause(
                s,
                "transaction_links",
                "target_allocation_id TEXT REFERENCES allocations(allocation_id) ON DELETE RESTRICT,",
                "target_allocation_id TEXT,",
                "target allocation foreign key",
            ),
            verify_refund_links,
            "ADR-024.3 FK de target_allocation_id",
        ),
        (
            "refund source allocation FK",
            lambda s: mutate_table_clause(
                s,
                "transaction_links",
                "source_allocation_id TEXT REFERENCES allocations(allocation_id) ON DELETE RESTRICT,",
                "source_allocation_id TEXT,",
                "source allocation foreign key",
            ),
            verify_refund_links,
            "ADR-027 FK de source_allocation_id",
        ),
        (
            "refund active index excludes rejected rows",
            lambda s: replace_once(
                s,
                "WHERE status IN ('proposed', 'confirmed') AND superseded_by_transaction_link_id IS NULL;",
                "WHERE superseded_by_transaction_link_id IS NULL;",
                "active link status filter",
            ),
            verify_refund_links,
            "ADR-027 le lien rejected bloque une nouvelle proposition",
        ),
        (
            "refund active index distinguishes source allocations",
            lambda s: replace_once(
                s,
                "ON transaction_links(source_transaction_id, source_allocation_id, target_transaction_id, link_kind,",
                "ON transaction_links(source_transaction_id, target_transaction_id, link_kind,",
                "active link source allocation index key",
            ),
            verify_refund_links,
            "UNIQUE active link identity per allocation",
        ),
        (
            "link supersession deferred FK",
            lambda s: mutate_table_clause(
                s,
                "transaction_links",
                "superseded_by_transaction_link_id TEXT REFERENCES transaction_links(transaction_link_id)\n        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,",
                "superseded_by_transaction_link_id TEXT REFERENCES transaction_links(transaction_link_id)\n        ON DELETE RESTRICT,",
                "deferred supersession foreign key",
            ),
            verify_refund_links,
            "n-11 UPDATE ancien lien puis INSERT remplaçant",
        ),
        (
            "link supersession audit deferred FK",
            lambda s: mutate_table_clause(
                s,
                "transaction_links",
                "superseded_by_audit_event_id TEXT REFERENCES audit_events(audit_event_id)\n        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,",
                "superseded_by_audit_event_id TEXT REFERENCES audit_events(audit_event_id)\n        ON DELETE RESTRICT,",
                "deferred supersession audit foreign key",
            ),
            verify_refund_links,
            "n-11 UPDATE ancien lien puis INSERT remplaçant",
        ),
        (
            "balance check input validation",
            lambda s: remove_sql_object(s, "trigger", "trg_balance_checks_validate_insert"),
            verify_balance_checks,
            "ADR-024.3 delta de balance_check",
        ),
        (
            "balance check date cutoff",
            lambda s: remove_sql_object(s, "trigger", "trg_balance_checks_validate_insert"),
            verify_balance_date_cutoff_guard,
            "ADR-024.2 balance_as_of postérieur à complete_through refusé en match",
        ),
        (
            "balance check update append-only",
            lambda s: remove_sql_object(s, "trigger", "trg_balance_checks_immutable_update"),
            verify_balance_checks,
            "ADR-024.2 balance_checks append-only UPDATE",
        ),
        (
            "balance check delete append-only",
            lambda s: remove_sql_object(s, "trigger", "trg_balance_checks_immutable_delete"),
            verify_balance_checks,
            "ADR-024.2 balance_checks append-only DELETE",
        ),
        (
            "zero transaction rejected",
            lambda s: mutate_transaction_amount_clause(
                s, "AND amount_cents <> 0", "AND amount_cents = amount_cents", "zero amount CHECK"
            ),
            verify_amount_guards,
            "ADR-019.i montant zéro",
        ),
        (
            "integer transaction cents",
            lambda s: mutate_transaction_amount_clause(
                s, "typeof(amount_cents) = 'integer' AND ", "", "integer amount CHECK"
            ),
            verify_amount_guards,
            "INV-02 montant SQLite REAL non entier",
        ),
        (
            "identity trim",
            lambda s: replace_once(
                s,
                "length(identity_label) > 0 AND identity_label = trim(identity_label)\n        AND instr(identity_label, '  ') = 0",
                "length(identity_label) > 0 AND instr(identity_label, '  ') = 0",
                "identity trim CHECK",
            ),
            verify_identity_label,
            "ADR-019.g espace ASCII de bord",
        ),
        (
            "identity double-space",
            lambda s: replace_once(
                s,
                "AND instr(identity_label, '  ') = 0",
                "AND instr(identity_label, ' ') >= 0",
                "identity double-space CHECK",
            ),
            verify_identity_label,
            "ADR-019.g double espace ASCII",
        ),
        (
            "insight metric registry",
            lambda s: re.sub(
                r"    metric_definition_id TEXT NOT NULL CHECK \(metric_definition_id IN \(.*?\n    \)\),",
                "    metric_definition_id TEXT NOT NULL,",
                s,
                count=1,
                flags=re.S,
            ),
            verify_insight_facts_and_idempotency,
            "F-04 metric ID d'insight",
        ),
        (
            "idempotency seven days",
            lambda s: replace_once(
                s, "created_at_utc, '+7 days'", "created_at_utc, '+6 days'", "idempotency expiry"
            ),
            verify_insight_facts_and_idempotency,
            "ADR-023 rétention idempotence",
        ),
        (
            "backup HMAC key required",
            lambda s: mutate_table_clause(
                s,
                "backup_manifests",
                "includes_hmac_key INTEGER NOT NULL DEFAULT 1 CHECK (includes_hmac_key = 1),",
                "includes_hmac_key INTEGER NOT NULL DEFAULT 1,",
                "backup HMAC flag",
            ),
            verify_backup_hmac_key_flag,
            "ADR-023 backup sans clé HMAC",
        ),
        (
            "no_op archive forbidden",
            lambda s: mutate_table_clause(
                s,
                "import_files",
                "    CHECK (\n        (status = 'no_op' AND archive_relpath IS NULL\n            AND archive_storage_mode IS NULL AND archive_permissions_octal IS NULL)\n        OR (status <> 'no_op' AND archive_relpath IS NOT NULL\n            AND archive_relpath = 'data/originals/' || import_file_id || '.bin'\n            AND archive_storage_mode = 'raw' AND archive_permissions_octal = '0600')\n    )",
                "    CHECK (1 = 1)",
                "no_op archive CHECK",
            ),
            verify_noop_archive_constraint,
            "n-09 un no_op ne peut conserver des octets archivés",
        ),
        (
            "archive relative path",
            lambda s: replace_once(
                s,
                "AND archive_relpath = 'data/originals/' || import_file_id || '.bin'\n            AND archive_storage_mode",
                "AND archive_storage_mode",
                "archive path check",
            ),
            verify_archive_metadata,
            "INV-11 chemin d'archive hors",
        ),
        (
            "archive raw storage",
            lambda s: replace_once(
                s,
                "AND archive_storage_mode = 'raw' AND archive_permissions_octal = '0600'",
                "AND archive_permissions_octal = '0600'",
                "archive raw check",
            ),
            verify_archive_metadata,
            "INV-11 mode de stockage non raw",
        ),
        (
            "archive permissions",
            lambda s: replace_once(
                s,
                "AND archive_storage_mode = 'raw' AND archive_permissions_octal = '0600'",
                "AND archive_storage_mode = 'raw'",
                "archive permissions check",
            ),
            verify_archive_metadata,
            "INV-11 permissions autres que 0600",
        ),
        (
            "import status enum",
            lambda s: mutate_table_clause(
                s,
                "import_files",
                "status TEXT NOT NULL CHECK (status IN ('previewed', 'committed', 'rejected', 'no_op')),",
                "status TEXT NOT NULL,",
                "import status enum",
            ),
            verify_import_status_and_immutability,
            "ADR-017 aucun statut d'import partial",
        ),
        (
            "transaction flow not stored",
            lambda s: mutate_transaction_amount_clause(
                s,
                "    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer' AND amount_cents <> 0),",
                "    flow_kind TEXT,\n    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer' AND amount_cents <> 0),",
                "stored transaction flow",
            ),
            verify_transaction_flow_not_stored,
            "flow_kind ne doit pas être stocké sur transactions",
        ),
    ]
    with tempfile.TemporaryDirectory(prefix="verify-schema-proof-") as temp_dir:
        root = Path(temp_dir)
        for number, (name, mutate, focused_test, expected_failure) in enumerate(mutations, start=1):
            mutant = root / f"mutant-{number:02d}.sql"
            mutant.write_text(mutate(source), encoding="utf-8")
            try:
                focused_test(mutant)
            except VerificationError as error:
                if expected_failure not in str(error):
                    raise VerificationError(
                        f"preuve de régression {name}: échec hors garde-fou attendu ({error})"
                    ) from error
                report(f"preuve /tmp : retrait {name} détecté par {expected_failure}")
            else:
                raise VerificationError(
                    f"preuve de régression {name}: le test est resté vert sans sa contrainte"
                )

    script_source = Path(__file__).read_text(encoding="utf-8")
    binder_start = script_source.index("def require_cents(")
    binder_end = script_source.index("\n\ndef insert_account(", binder_start)
    mutant_binder = replace_once(
        script_source[binder_start:binder_end],
        "if type(value) is not int:",
        "if False:",
        "strict int binder",
    )
    mutant_source = script_source[:binder_start] + mutant_binder + script_source[binder_end:]
    with tempfile.TemporaryDirectory(prefix="verify-schema-python-proof-") as temp_dir:
        mutant_script = Path(temp_dir) / "verify_schema_mutant.py"
        mutant_script.write_text(mutant_source, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(mutant_script),
                "--schema",
                str(schema_path),
                "--skip-mutation-proofs",
                "--skip-migration-history",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        combined = result.stdout + result.stderr
        if (
            result.returncode == 0
            or "ADR-019.h float intégral rejeté avant binding" not in combined
        ):
            raise VerificationError(
                "preuve ADR-019.h: retirer type(value) is int n'a pas fait échouer son test"
            )
        report("preuve /tmp : retrait du binder ADR-019.h détecté par le test strict-int")


def verify_schema(
    schema_path: Path, *, prove_mutations: bool = True, verify_migrations: bool = True
) -> None:
    # Charge seul le schéma avant les scénarios : une erreur de syntaxe est signalée sans ambiguïté.
    connection = connect(schema_path)
    connection.close()
    tests: tuple[Callable[[Path], None], ...] = (
        verify_connection_pragmas_and_replace,
        verify_primary_key_timestamp_and_version_guards,
        verify_foreign_key,
        verify_occurrence_rank_guards,
        verify_identity_label,
        verify_amount_guards,
        verify_archive_metadata,
        verify_noop_archive_constraint,
        verify_section_and_provenance_guards,
        verify_import_status_and_immutability,
        verify_import_generation_fields,
        verify_fallback_complete_through_guard,
        verify_balance_checks,
        verify_balance_date_cutoff_guard,
        verify_noop_and_committed_hash,
        verify_rule_audit_foreign_key,
        verify_transaction_flow_not_stored,
        verify_allocations,
        verify_refund_links,
        verify_refund_parts_and_state,
        verify_insight_facts_and_idempotency,
        verify_backup_hmac_key_flag,
        verify_personal_mode_oracle,
        verify_import_rollback,
        verify_two_connection_noop,
        verify_IMP_01_to_IMP_05_and_IMP_09_order_independence,
    )
    for test in tests:
        test(schema_path)
    if verify_migrations:
        verify_migration_history_and_head_schema()
    if prove_mutations:
        verify_constraint_mutation_proofs(schema_path)


def verify_migration_history_and_head_schema() -> None:
    for relative_path, expected_digest in FROZEN_0001_HASHES.items():
        actual = hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()
        if actual != expected_digest:
            raise VerificationError(
                f"MIG-FREEZE-01 migration gelée, créer 0003 : {relative_path} a changé"
            )
    report("MIG-FREEZE-01 les deux fichiers de 0001 sont gelés")

    # Exercise the actual linear Alembic chain in a temporary database. The
    # runner creates the required verified backup between 0001 and 0002.
    sys.path.insert(0, str(ROOT / "backend"))
    try:
        from alembic import command
        from alembic.config import Config

        from app.core.config import Settings
        from app.db.migrate import upgrade_to_head

        with tempfile.TemporaryDirectory(prefix="verify-schema-migration-") as temp_dir:
            base = Path(temp_dir)
            database = base / "migration.sqlite"
            settings = Settings(
                database_url=f"sqlite:///{database}",
                data_dir=base / "private-data",
                _env_file=None,
            )
            config = Config(str(ROOT / "alembic.ini"))
            config.attributes["settings"] = settings
            config.attributes["operation"] = "upgrade"
            command.upgrade(config, "0001_initial")
            result = upgrade_to_head(settings)
            if result.revision != "0002_t17_refund_safety" or result.backup is None:
                raise VerificationError("MIG-0002 chaîne 0001 → 0002 ou sauvegarde absente")

            expected = connect(DEFAULT_SCHEMA)
            migrated = sqlite3.connect(database)
            try:
                def normalized_objects(
                    connection: sqlite3.Connection,
                ) -> tuple[tuple[str, str, str, str | None], ...]:
                    rows = connection.execute(
                        "SELECT type, name, tbl_name, sql FROM sqlite_master "
                        "WHERE type IN ('table','index','trigger') "
                        "AND name NOT LIKE 'sqlite_%' AND name <> 'alembic_version'"
                    ).fetchall()
                    return tuple(
                        sorted(
                            (
                                kind,
                                name,
                                table,
                                None if sql is None else re.sub(r"\s+", " ", sql).strip(),
                            )
                            for kind, name, table, sql in rows
                        )
                    )

                if normalized_objects(migrated) != normalized_objects(expected):
                    raise VerificationError(
                        "MIG-0002 sqlite_master après 0001 → 0002 diverge de schema.sql"
                    )
                integrity = migrated.execute("PRAGMA integrity_check").fetchone()
                foreign_keys = migrated.execute("PRAGMA foreign_key_check").fetchall()
                if integrity != ("ok",) or foreign_keys:
                    raise VerificationError(
                        "MIG-0002 migrated database integrity_check/foreign_key_check invalide"
                    )
            finally:
                expected.close()
                migrated.close()
    except VerificationError:
        raise
    except Exception as error:
        raise VerificationError(f"MIG-0002 échec de migration temporaire : {error}") from error
    report("MIG-0002 la chaîne 0001 → 0002 égale docs/examples/schema.sql")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA,
        help="copie alternative de schema.sql, utile pour vérifier qu'une régression échoue",
    )
    parser.add_argument(
        "--skip-mutation-proofs",
        action="store_true",
        help="réservé à l'exécution rapide d'une copie mutante par un outil externe",
    )
    parser.add_argument(
        "--skip-migration-history",
        action="store_true",
        help="réservé à l'exécution d'un script mutant copié hors du dépôt",
    )
    args = parser.parse_args()
    try:
        verify_schema(
            args.schema,
            prove_mutations=not args.skip_mutation_proofs,
            verify_migrations=not args.skip_migration_history,
        )
    except (OSError, sqlite3.Error, VerificationError) as error:
        print(f"ÉCHEC verify_schema: {error}", file=sys.stderr)
        return 1
    print("PASS verify_schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
