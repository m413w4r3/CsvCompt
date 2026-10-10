from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.db.models import Account, AuditEvent, Payee, PayeeAlias, Transaction
from app.db.session import create_sqlite_engine, session_scope, write_transaction
from app.importers.normalize import identity_label
from app.services.payees import (
    add_payee_alias,
    create_payee_for_transaction,
    ensure_payee_for_transaction,
    find_ambiguous_aliases,
    merge_payees,
    preview_payee_merge,
    remove_payee_alias,
    unmerge_payees,
)


def _database(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'payees.sqlite'}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    return create_sqlite_engine(settings.database_url)


def _transaction(
    session, account_id: str, raw_label: str, amount_cents: int = -1250
) -> Transaction:
    normalized = identity_label(raw_label).value
    transaction = Transaction(
        account_id=account_id,
        booked_on=date(2026, 3, 10),
        amount_cents=amount_cents,
        raw_label=raw_label,
        identity_label=normalized,
        identity_key_sha256=sha256(normalized.encode()).hexdigest(),
        occurrence_rank=1,
    )
    session.add(transaction)
    session.flush()
    return transaction


def _business_snapshot(session) -> tuple[tuple[object, ...], ...]:
    payees = tuple(
        (row.payee_id, row.display_name, row.normalized_name, row.is_active)
        for row in session.scalars(select(Payee).order_by(Payee.payee_id))
    )
    aliases = tuple(
        (
            row.payee_alias_id,
            row.payee_id,
            row.alias_text,
            row.normalized_alias,
            row.match_kind,
            row.approved_by,
        )
        for row in session.scalars(select(PayeeAlias).order_by(PayeeAlias.payee_alias_id))
    )
    transactions = tuple(
        (row.transaction_id, row.payee_id)
        for row in session.scalars(select(Transaction).order_by(Transaction.transaction_id))
    )
    return payees, aliases, transactions


@pytest.mark.integration
def test_MONEY_09_exact_payee_assignment_keeps_carrefour_bank_and_store_distinct(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    with write_transaction(engine) as session:
        account = Account(alias="Compte synthétique", account_role="current")
        session.add(account)
        session.flush()
        bank_tx = _transaction(
            session,
            account.account_id,
            "Prélèvement\nCARREFOUR BANQUE - CARREFOUR BANQUE - REF-SYNTHETIQUE",
        )
        store_tx = _transaction(
            session,
            account.account_id,
            "Paiement par carte\nX6020 CARREFOUR VILLE 12/04 - Carrefour",
        )
        bank_payee = create_payee_for_transaction(session, bank_tx, actor="test")
        store_payee = create_payee_for_transaction(session, store_tx, actor="test")

    assert bank_payee is not None and store_payee is not None
    assert bank_payee.payee_id != store_payee.payee_id
    assert bank_tx.payee_id == bank_payee.payee_id
    assert store_tx.payee_id == store_payee.payee_id
    with session_scope(engine) as session:
        assert (
            session.get(Payee, bank_payee.payee_id).normalized_name
            != session.get(Payee, store_payee.payee_id).normalized_name
        )
    engine.dispose()


@pytest.mark.integration
def test_neighboring_key_stays_unresolved_and_alias_add_remove_is_audited(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    with write_transaction(engine) as session:
        account = Account(alias="Compte synthétique", account_role="current")
        session.add(account)
        session.flush()
        source = _transaction(
            session, account.account_id, "Paiement par carte\nX1111 LUNE 01/03 - Lune"
        )
        payee = create_payee_for_transaction(session, source, actor="test")
        assert payee is not None

        near = _transaction(
            session, account.account_id, "Paiement par carte\nX2222 LUNE PLUS 02/03 - Lune Plus"
        )
        assert ensure_payee_for_transaction(session, near) is None
        assert near.payee_id is None

        alias = add_payee_alias(
            session,
            payee.payee_id,
            "Lune Plus",
            scope="exact",
            evidence_transaction_ids=[source.transaction_id],
            actor="reviewer-synthetic",
        )
        assert ensure_payee_for_transaction(session, near) is not None
        alias_id = alias.payee_alias_id
        attachment_event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.target_type == "transaction",
                AuditEvent.target_id == near.transaction_id,
                AuditEvent.action == "payee_attached_exact",
            )
        )
        assert attachment_event is not None
        assert attachment_event.actor == "system:exact-key"
        assert json.loads(attachment_event.after_json)["alias_id"] == alias_id

        remove_payee_alias(session, alias_id, actor="reviewer-synthetic")
        later = _transaction(
            session,
            account.account_id,
            "Paiement par carte\nX3333 LUNE PLUS 03/03 - Lune Plus",
            -1300,
        )
        assert ensure_payee_for_transaction(session, later) is None
        assert later.payee_id is None
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.target_type == "payee_alias")
        ).all()
        assert {event.action for event in events} >= {"payee_alias_added", "payee_alias_removed"}
        assert all(event.actor == "reviewer-synthetic" for event in events)
        assert all(event.created_at_utc is not None for event in events)
    engine.dispose()


@pytest.mark.integration
def test_merge_preview_confirmation_and_unmerge_restore_business_state(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    with write_transaction(engine) as session:
        account = Account(alias="Compte synthétique", account_role="current")
        session.add(account)
        session.flush()
        bank_tx = _transaction(
            session,
            account.account_id,
            "Prélèvement\nCARREFOUR BANQUE - CARREFOUR BANQUE - REF-SYNTHETIQUE",
        )
        store_tx = _transaction(
            session,
            account.account_id,
            "Paiement par carte\nX6020 CARREFOUR VILLE 12/04 - Carrefour",
        )
        bank = create_payee_for_transaction(session, bank_tx, actor="test")
        store = create_payee_for_transaction(session, store_tx, actor="test")
        assert bank is not None and store is not None
        preview = preview_payee_merge(session, bank.payee_id, store.payee_id)
        assert preview.transaction_count == 1
        assert preview.transaction_ids == (bank_tx.transaction_id,)
        source_alias = add_payee_alias(
            session,
            bank.payee_id,
            "Banque Exemple Alias",
            scope="exact",
            evidence_transaction_ids=[bank_tx.transaction_id],
            actor="reviewer-synthetic",
        )
        preview = preview_payee_merge(session, bank.payee_id, store.payee_id)
        assert preview.alias_ids == (source_alias.payee_alias_id,)
        before = _business_snapshot(session)

    tampered_preview = replace(
        preview,
        transaction_ids=(),
        transaction_count=0,
    )
    with (
        write_transaction(engine) as session,
        pytest.raises(ValueError, match="aperçu de fusion est obsolète"),
    ):
        merge_payees(
            session,
            bank.payee_id,
            store.payee_id,
            preview=tampered_preview,
            actor="reviewer-synthetic",
            confirm=True,
            confirm_intermediary_store=True,
        )

    with (
        write_transaction(engine) as session,
        pytest.raises(ValueError, match="confirmation explicite"),
    ):
        merge_payees(
            session,
            bank.payee_id,
            store.payee_id,
            preview=preview,
            actor="reviewer-synthetic",
            confirm=False,
        )

    with write_transaction(engine) as session:
        event = merge_payees(
            session,
            bank.payee_id,
            store.payee_id,
            preview=preview,
            actor="reviewer-synthetic",
            confirm=True,
            confirm_intermediary_store=True,
        )
        assert event.action == "payee_merged"
        moved_alias = session.get(PayeeAlias, source_alias.payee_alias_id)
        assert moved_alias is not None and moved_alias.payee_id == store.payee_id

    with write_transaction(engine) as session:
        unmerge_payees(session, event.audit_event_id, actor="reviewer-synthetic")

    with session_scope(engine) as session:
        # Version and updated_at remain monotone; every business link and field is restored.
        assert _business_snapshot(session) == before
        restored_alias = session.get(PayeeAlias, source_alias.payee_alias_id)
        assert restored_alias is not None and restored_alias.payee_id == bank.payee_id
        assert session.get(Transaction, bank_tx.transaction_id).payee_id == bank.payee_id
        assert session.get(Transaction, store_tx.transaction_id).payee_id == store.payee_id
        actions = session.scalars(
            select(AuditEvent.action).where(AuditEvent.target_type == "payee_merge")
        ).all()
        assert actions == ["payee_merged", "payee_unmerged"]
    engine.dispose()


@pytest.mark.integration
def test_alias_validation_rejects_card_suffix_mandate_and_long_identifiers(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    with write_transaction(engine) as session:
        account = Account(alias="Compte synthétique", account_role="current")
        session.add(account)
        session.flush()
        tx = _transaction(
            session, account.account_id, "Paiement par carte\nX1010 MARCHAND LUNE 01/03 - Lune"
        )
        payee = create_payee_for_transaction(session, tx, actor="test")
        assert payee is not None
        synthetic_iban = "".join(("FR", "76", "12345678901234567890123"))
        sensitive_values = (
            "Carte X6020",
            "Mandat RUM-SYNTHETIQUE-123456789012",
            synthetic_iban,
        )
        for value in sensitive_values:
            with pytest.raises(ValueError, match="alias contient une référence ou un numéro"):
                add_payee_alias(
                    session,
                    payee.payee_id,
                    value,
                    scope="exact",
                    evidence_transaction_ids=[tx.transaction_id],
                    actor="reviewer-synthetic",
                )
    engine.dispose()


@pytest.mark.integration
def test_unmerge_refuses_to_overwrite_a_link_changed_after_merge(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    with write_transaction(engine) as session:
        account = Account(alias="Compte synthétique", account_role="current")
        session.add(account)
        session.flush()
        source_tx = _transaction(
            session, account.account_id, "Paiement par carte\nX1111 LUNE 01/03 - Lune"
        )
        target_tx = _transaction(
            session, account.account_id, "Paiement par carte\nX2222 SOLEIL 02/03 - Soleil"
        )
        third_tx = _transaction(
            session, account.account_id, "Paiement par carte\nX3333 ETOILE 03/03 - Etoile"
        )
        source = create_payee_for_transaction(session, source_tx, actor="test")
        target = create_payee_for_transaction(session, target_tx, actor="test")
        third = create_payee_for_transaction(session, third_tx, actor="test")
        assert source is not None and target is not None and third is not None
        preview = preview_payee_merge(session, source.payee_id, target.payee_id)

    with write_transaction(engine) as session:
        merge = merge_payees(
            session,
            source.payee_id,
            target.payee_id,
            preview=preview,
            actor="reviewer-synthetic",
            confirm=True,
        )

    with write_transaction(engine) as session:
        changed = session.get(Transaction, source_tx.transaction_id)
        assert changed is not None
        changed.payee_id = third.payee_id

    with (
        write_transaction(engine) as session,
        pytest.raises(ValueError, match="transaction a changé depuis la fusion"),
    ):
        unmerge_payees(session, merge.audit_event_id, actor="reviewer-synthetic")

    with session_scope(engine) as session:
        assert session.get(Transaction, source_tx.transaction_id).payee_id == third.payee_id
        assert session.get(Payee, source.payee_id).is_active == 0
    engine.dispose()


@pytest.mark.integration
def test_with_context_alias_requires_operation_and_sign_and_search_collision_is_audit_only(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    with write_transaction(engine) as session:
        account = Account(alias="Compte synthétique", account_role="current")
        session.add(account)
        session.flush()
        proof = _transaction(
            session, account.account_id, "Paiement par carte\nX1111 LUNE 01/03 - Etoile"
        )
        first = create_payee_for_transaction(session, proof, actor="test")
        other_proof = _transaction(
            session,
            account.account_id,
            "Paiement par carte\nX2222 SOLEIL 01/03 - Soleil",
            -1500,
        )
        second = create_payee_for_transaction(session, other_proof, actor="test")
        assert first is not None and second is not None

        with pytest.raises(ValueError, match="correspond déjà à un autre payee"):
            add_payee_alias(
                session,
                first.payee_id,
                "Soleil",
                scope="exact",
                evidence_transaction_ids=[proof.transaction_id],
                actor="reviewer-synthetic",
            )
        legacy_payee = Payee(display_name="Echo Legacy", normalized_name="legacy:opaque")
        session.add(legacy_payee)
        session.flush()
        with pytest.raises(ValueError, match="correspond déjà à un autre payee"):
            add_payee_alias(
                session,
                first.payee_id,
                "Echo Legacy",
                scope="exact",
                evidence_transaction_ids=[proof.transaction_id],
                actor="reviewer-synthetic",
            )

        add_payee_alias(
            session,
            first.payee_id,
            "Etoile Nouvelle",
            scope="with_context",
            context={"operation_type": "Paiement par carte", "sign": -1},
            evidence_transaction_ids=[proof.transaction_id],
            actor="reviewer-synthetic",
        )
        add_payee_alias(
            session,
            first.payee_id,
            "Cafe Lune",
            scope="exact",
            evidence_transaction_ids=[proof.transaction_id],
            actor="reviewer-synthetic",
        )
        add_payee_alias(
            session,
            second.payee_id,
            "Café Lune",
            scope="exact",
            evidence_transaction_ids=[other_proof.transaction_id],
            actor="reviewer-synthetic",
        )

        debit = _transaction(
            session,
            account.account_id,
            "Paiement par carte\nX3333 ETOILE NOUVELLE 02/03 - Etoile Nouvelle",
            -1600,
        )
        credit = _transaction(
            session,
            account.account_id,
            "Paiement par carte\nX4444 ETOILE NOUVELLE 03/03 - Etoile Nouvelle",
            400,
        )
        assert ensure_payee_for_transaction(session, debit).payee_id == first.payee_id
        assert ensure_payee_for_transaction(session, credit) is None

        collisions = find_ambiguous_aliases(session)
        collision = next(row for row in collisions if row.search_key == "cafe lune")
        assert collision.payee_ids == tuple(sorted((first.payee_id, second.payee_id)))
        assert len(collision.alias_ids) == 2
    engine.dispose()
