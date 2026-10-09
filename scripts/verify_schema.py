#!/usr/bin/env python3
"""Vérification exécutable du schéma SQLite de référence (stdlib uniquement)."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "docs" / "examples" / "schema.sql"
ACCOUNT_ID = "00000000-0000-4000-8000-000000000001"


class VerificationError(RuntimeError):
    pass


def new_id() -> str:
    return str(uuid.uuid4())


def connect(schema_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", isolation_level=None, timeout=5)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(schema_path.read_text(encoding="utf-8"))
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise VerificationError("PRAGMA foreign_keys n'est pas actif")
    return connection


def report(label: str) -> None:
    print(f"OK {label}")


def expect_integrity_error(label: str, action) -> None:
    try:
        action()
    except sqlite3.IntegrityError:
        report(label)
        return
    raise VerificationError(f"contrainte absente ou non appliquée : {label}")


def insert_account(connection: sqlite3.Connection, account_id: str = ACCOUNT_ID) -> None:
    connection.execute(
        "INSERT INTO accounts(account_id, alias, account_role) VALUES (?, ?, ?)",
        (account_id, "Compte synthétique", "current"),
    )


def insert_import(
    connection: sqlite3.Connection,
    *,
    digest: str | None = None,
    status: str = "committed",
    duplicate_of: str | None = None,
    archive_relpath: str | None = None,
    archive_storage_mode: str = "raw",
    archive_permissions_octal: str = "0600",
) -> str:
    import_id = new_id()
    digest = digest or hashlib.sha256(import_id.encode("ascii")).hexdigest()
    archive_relpath = archive_relpath or f"data/originals/{import_id}.bin"
    connection.execute(
        """INSERT INTO import_files
           (import_file_id, file_sha256, archive_relpath, archive_storage_mode,
            archive_permissions_octal, file_size_bytes, status, duplicate_of_import_file_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (import_id, digest, archive_relpath, archive_storage_mode,
         archive_permissions_octal, 128, status, duplicate_of),
    )
    return import_id


def verify_archive_metadata(connection: sqlite3.Connection) -> None:
    import_id = insert_import(connection, status="rejected")
    relpath, storage_mode, permissions = connection.execute(
        """SELECT archive_relpath, archive_storage_mode, archive_permissions_octal
           FROM import_files WHERE import_file_id = ?""",
        (import_id,),
    ).fetchone()
    if (relpath, storage_mode, permissions) != (
        f"data/originals/{import_id}.bin", "raw", "0600"
    ):
        raise VerificationError("INV-11 mode d'archive raw ou permissions 0600 incorrects")
    report("INV-11 chemin relatif data/originals, mode raw et permissions 0600")
    expect_integrity_error(
        "INV-11 chemin d'archive hors du répertoire autorisé rejeté",
        lambda: insert_import(connection, archive_relpath="../private.csv"),
    )
    expect_integrity_error(
        "INV-11 mode de stockage autre que raw rejeté en v1",
        lambda: insert_import(connection, archive_storage_mode="compressed"),
    )
    expect_integrity_error(
        "INV-11 permissions d'archive différentes de 0600 rejetées",
        lambda: insert_import(connection, archive_permissions_octal="0644"),
    )


def insert_section(
    connection: sqlite3.Connection,
    import_id: str,
    account_id: str = ACCOUNT_ID,
) -> None:
    connection.execute(
        """INSERT INTO import_account_sections
           (import_file_id, section_index, account_id, coverage_from, coverage_through)
           VALUES (?, 1, ?, '2026-01-01', '2026-12-31')""",
        (import_id, account_id),
    )


def identity_digest(identity: tuple[str, str, int, str]) -> str:
    # Le futur service emploie la même forme non ambiguë : UTF-8 avec
    # préfixe de longueur sur chaque composant, montant signé en base 10.
    account_id, booked_on, amount_cents, identity_label = identity
    parts = (account_id, booked_on, str(amount_cents), identity_label)
    framed = b"".join(
        len(part.encode("utf-8")).to_bytes(4, "big") + part.encode("utf-8")
        for part in parts
    )
    return hashlib.sha256(framed).hexdigest()


def insert_transaction(
    connection: sqlite3.Connection,
    *,
    identity: tuple[str, str, int, str],
    occurrence_rank: int,
    transaction_id: str | None = None,
    amount_sql: str | None = None,
) -> str:
    account_id, booked_on, amount_cents, identity_label = identity
    transaction_id = transaction_id or new_id()
    amount = str(amount_cents) if amount_sql is None else amount_sql
    connection.execute(
        f"""INSERT INTO transactions
            (transaction_id, account_id, booked_on, amount_cents, raw_label,
             identity_label, identity_key_sha256, occurrence_rank)
            VALUES (?, ?, ?, {amount}, ?, ?, ?, ?)""",
        (
            transaction_id,
            account_id,
            booked_on,
            identity_label,
            identity_label,
            identity_digest(identity),
            occurrence_rank,
        ),
    )
    return transaction_id


def verify_foreign_key(connection: sqlite3.Connection) -> None:
    expect_integrity_error(
        "FK-01 compte inexistant rejeté",
        lambda: insert_transaction(
            connection,
            identity=("missing-account", "2026-02-03", -1234, "Label synthétique"),
            occurrence_rank=1,
        ),
    )


def verify_occurrence_uniqueness(connection: sqlite3.Connection) -> None:
    insert_account(connection)
    identity = (ACCOUNT_ID, "2026-02-03", -1234, "Paiement synthétique")
    insert_transaction(connection, identity=identity, occurrence_rank=1)
    expect_integrity_error(
        "IMP-03 occurrence rank 1 en double rejeté",
        lambda: insert_transaction(connection, identity=identity, occurrence_rank=1),
    )
    insert_transaction(connection, identity=identity, occurrence_rank=2)
    report("IMP-03 même identité avec ranks 1 et 2 acceptée")


def verify_immutable_source(connection: sqlite3.Connection) -> None:
    import_id = insert_import(connection)
    insert_section(connection, import_id)
    identity = (ACCOUNT_ID, "2026-02-04", -250, "Libellé source immuable")
    transaction_id = insert_transaction(connection, identity=identity, occurrence_rank=1)
    source_id = new_id()
    connection.execute(
        """INSERT INTO source_rows
           (source_row_id, import_file_id, section_index, logical_row_idx, row_sha256, transaction_id)
           VALUES (?, ?, 1, 1, ?, ?)""",
        (source_id, import_id, hashlib.sha256(b"synthetic row").hexdigest(), transaction_id),
    )
    expect_integrity_error(
        "INV-01 UPDATE du libellé source transaction rejeté",
        lambda: connection.execute(
            "UPDATE transactions SET raw_label = 'modified' WHERE transaction_id = ?",
            (transaction_id,),
        ),
    )
    expect_integrity_error(
        "INV-01 UPDATE de source_rows rejeté",
        lambda: connection.execute(
            "UPDATE source_rows SET logical_row_idx = 2 WHERE source_row_id = ?",
            (source_id,),
        ),
    )


def verify_integer_cents(connection: sqlite3.Connection) -> None:
    expect_integrity_error(
        "INV-02 montant SQLite REAL refusé",
        lambda: insert_transaction(
            connection,
            identity=(ACCOUNT_ID, "2026-02-05", 0, "Montant non entier"),
            occurrence_rank=1,
            amount_sql="123.5",
        ),
    )


def verify_committed_hash_unique(connection: sqlite3.Connection) -> None:
    digest = hashlib.sha256(b"same synthetic file").hexdigest()
    insert_import(connection, digest=digest)
    expect_integrity_error(
        "IMP-01 hash de fichier commité en doublon rejeté",
        lambda: insert_import(connection, digest=digest),
    )


def verify_rule_audit_foreign_key(connection: sqlite3.Connection) -> None:
    rule_account_id = "00000000-0000-4000-8000-000000000002"
    insert_account(connection, account_id=rule_account_id)
    identity = (rule_account_id, "2026-02-06", -100, "Règle synthétique")
    transaction_id = insert_transaction(connection, identity=identity, occurrence_rank=1)
    connection.execute(
        "INSERT INTO categories(category_id, label, taxonomy_version) VALUES ('groceries', 'Courses', 1)"
    )
    connection.execute(
        "INSERT INTO category_flow_kinds(category_id, flow_kind) VALUES ('groceries', 'expense')"
    )
    rule_id = new_id()
    connection.execute(
        """INSERT INTO rules
           (rule_id, name, scope_kind, scope_json, flow_kind, category_id, effect_json)
           VALUES (?, 'Règle synthétique', 'exact_payee', '{}', 'expense', 'groceries', '{}')""",
        (rule_id,),
    )
    expect_integrity_error(
        "FK rule_executions.audit_event_id orphelin rejeté",
        lambda: connection.execute(
            """INSERT INTO rule_executions
               (rule_execution_id, rule_id, transaction_id, outcome, audit_event_id)
               VALUES (?, ?, ?, 'applied', 'missing-audit-event')""",
            (new_id(), rule_id, transaction_id),
        ),
    )


Identity = tuple[str, str, int, str]


def rows_for(groups: Iterable[tuple[str, str, int, str, int]]) -> list[Identity]:
    result: list[Identity] = []
    for date, label, amount, _tag, multiplicity in groups:
        result.extend([(ACCOUNT_ID, date, amount, label)] * multiplicity)
    return result


def run_export(
    connection: sqlite3.Connection,
    *,
    content: bytes,
    rows: list[Identity],
    file_label: str,
) -> tuple[int, bool]:
    """Oracle de démonstration du service de réconciliation, pas le service applicatif."""
    digest = hashlib.sha256(content).hexdigest()
    existing = connection.execute(
        "SELECT import_file_id FROM import_files WHERE file_sha256 = ? AND status = 'committed'",
        (digest,),
    ).fetchone()
    if existing:
        connection.execute("BEGIN IMMEDIATE")
        try:
            no_op_id = insert_import(
                connection, digest=digest, status="no_op", duplicate_of=existing[0]
            )
            connection.execute(
                """INSERT INTO audit_events
                   (audit_event_id, import_file_id, target_type, target_id, action, actor)
                   VALUES (?, ?, 'import', ?, 'import.no_op', 'verify_schema')""",
                (new_id(), no_op_id, existing[0]),
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        count = connection.execute("SELECT count(*) FROM transactions").fetchone()[0]
        return count, True

    connection.execute("BEGIN IMMEDIATE")
    try:
        import_id = insert_import(connection, digest=digest)
        insert_section(connection, import_id)
        occurrences: dict[Identity, list[int]] = defaultdict(list)
        for logical_row_idx, identity in enumerate(rows, start=1):
            occurrences[identity].append(logical_row_idx)

        for identity in sorted(occurrences):
            known = {
                rank: transaction_id
                for rank, transaction_id in connection.execute(
                    """SELECT occurrence_rank, transaction_id FROM transactions
                       WHERE account_id = ? AND booked_on = ? AND amount_cents = ? AND identity_label = ?""",
                    identity,
                )
            }
            n_existing = max(known, default=0)
            n_new = len(occurrences[identity])
            for rank in range(1, max(n_existing, n_new) + 1):
                transaction_id = known.get(rank)
                if transaction_id is None:
                    transaction_id = insert_transaction(
                        connection, identity=identity, occurrence_rank=rank
                    )
                # Une source par occurrence dans ce contenu de fichier, même
                # lorsqu'elle se rattache à une transaction plus ancienne.
                if rank <= n_new:
                    logical_row_idx = occurrences[identity][rank - 1]
                    row_digest = hashlib.sha256(
                        f"{file_label}:{logical_row_idx}".encode("utf-8")
                    ).hexdigest()
                    connection.execute(
                        """INSERT INTO source_rows
                           (source_row_id, import_file_id, section_index, logical_row_idx,
                            row_sha256, transaction_id)
                           VALUES (?, ?, 1, ?, ?, ?)""",
                        (new_id(), import_id, logical_row_idx, row_digest, transaction_id),
                    )
        connection.execute(
            """INSERT INTO audit_events
               (audit_event_id, import_file_id, target_type, target_id, action, actor)
               VALUES (?, ?, 'import', ?, 'import.commit', 'verify_schema')""",
            (new_id(), import_id, import_id),
        )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    count = connection.execute("SELECT count(*) FROM transactions").fetchone()[0]
    return count, False


def transaction_signature(connection: sqlite3.Connection) -> list[tuple[str, str, int, str, int]]:
    return connection.execute(
        """SELECT account_id, booked_on, amount_cents, identity_label, occurrence_rank
           FROM transactions ORDER BY account_id, booked_on, amount_cents, identity_label, occurrence_rank"""
    ).fetchall()


def make_demo_connection(schema_path: Path) -> sqlite3.Connection:
    connection = connect(schema_path)
    insert_account(connection)
    return connection


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

    forward = make_demo_connection(schema_path)
    observed: list[int] = []
    seen_contents: set[bytes] = set()
    for label, content, rows in (
        ("A", a_bytes, a_rows),
        ("B", b_bytes, b_rows),
        ("A", a_bytes, a_rows),
        ("B", b_bytes, b_rows),
        ("A", a_bytes, a_rows),
    ):
        count, was_no_op = run_export(forward, content=content, rows=rows, file_label=label)
        observed.append(count)
        expected = 3 if label == "A" and not observed[:-1] else 6
        if count != expected:
            raise VerificationError(f"IMP-01..05 cardinalité après {label} : {count}, attendu {expected}")
        expected_no_op = content in seen_contents
        if was_no_op != expected_no_op:
            raise VerificationError(f"IMP-01 réimport {label} non reconnu comme no-op")
        seen_contents.add(content)
    forward_signature = transaction_signature(forward)
    if observed != [3, 6, 6, 6, 6]:
        raise VerificationError(f"IMP-01..05 séquence A/B/A/B/A inattendue : {observed}")

    reverse = make_demo_connection(schema_path)
    reverse_rows = list(reversed(b_rows))
    run_export(reverse, content=b_bytes, rows=reverse_rows, file_label="B-reversed")
    run_export(reverse, content=a_bytes, rows=list(reversed(a_rows)), file_label="A-reversed")
    reverse_signature = transaction_signature(reverse)
    if forward_signature != reverse_signature:
        raise VerificationError("IMP-05 A puis B et B puis A divergent")

    expected_signature = [
        (ACCOUNT_ID, "2026-01-10", -75, "Opération ancienne nouvellement exportée", 1),
        (ACCOUNT_ID, "2026-03-12", -1123, "Paiement répété", 1),
        (ACCOUNT_ID, "2026-03-12", -1123, "Paiement répété", 2),
        (ACCOUNT_ID, "2026-03-12", -1123, "Paiement répété", 3),
        (ACCOUNT_ID, "2026-03-12", 1123, "Crédit similaire", 1),
        (ACCOUNT_ID, "2026-03-13", -50, "Paiement distinct", 1),
    ]
    if forward_signature != expected_signature:
        raise VerificationError("IMP-03/04 cardinalités de clé, signe ou date inattendues")
    if forward.execute("SELECT count(*) FROM source_rows").fetchone()[0] != 9:
        raise VerificationError("IMP-02 nombre de source_rows (occurrences A+B) inattendu")
    report("IMP-01..05 A/B/A/B/A => transactions [3, 6, 6, 6, 6], ranks 1..3")
    report("IMP-05 B puis A et ordre de lignes inversé => même signature de transactions")
    report("IMP-09 une opération plus ancienne dans B est créée malgré la date max déjà importée")
    report("INV-01 chaque occurrence de A+B garde sa source_rows (9 sources → 6 transactions)")


def verify_schema(schema_path: Path) -> None:
    connection = connect(schema_path)
    try:
        verify_foreign_key(connection)
        verify_occurrence_uniqueness(connection)
        verify_immutable_source(connection)
        verify_integer_cents(connection)
        verify_archive_metadata(connection)
        verify_committed_hash_unique(connection)
        verify_rule_audit_foreign_key(connection)
    finally:
        connection.close()
    verify_IMP_01_to_IMP_05_and_IMP_09_order_independence(schema_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA,
        help="copie alternative de schema.sql, utile pour vérifier qu'une régression échoue",
    )
    args = parser.parse_args()
    try:
        verify_schema(args.schema)
    except (OSError, sqlite3.Error, VerificationError) as error:
        print(f"ÉCHEC verify_schema: {error}", file=sys.stderr)
        return 1
    print("PASS verify_schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
