from __future__ import annotations

from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.classification.memory import history_candidates, persist_suggestions
from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.db.models import (
    Account,
    Allocation,
    Category,
    CategoryFlowKind,
    ClassificationDecision,
    ClassificationSuggestion,
    Transaction,
)
from app.db.session import create_sqlite_engine, session_scope, write_transaction
from app.importers.normalize import identity_label
from app.services.payees import create_payee_for_transaction


def _database(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.sqlite'}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    return create_sqlite_engine(settings.database_url)


def _category(session, category_id: str, flow_kind: str = "expense") -> None:
    if session.get(Category, category_id) is None:
        session.add(Category(category_id=category_id, label=category_id, taxonomy_version=1))
        session.flush()
    if session.get(CategoryFlowKind, (category_id, flow_kind)) is None:
        session.add(CategoryFlowKind(category_id=category_id, flow_kind=flow_kind))
        session.flush()


def _transaction(session, account_id: str, label: str, amount_cents: int = -1000) -> Transaction:
    normalized = identity_label(label).value
    transaction = Transaction(
        account_id=account_id,
        booked_on=date(2026, 4, 1),
        amount_cents=amount_cents,
        raw_label=label,
        identity_label=normalized,
        identity_key_sha256=sha256(normalized.encode()).hexdigest(),
        occurrence_rank=1,
    )
    session.add(transaction)
    session.flush()
    assert create_payee_for_transaction(session, transaction, actor="test") is not None
    return transaction


def _allocation(
    session,
    transaction: Transaction,
    category_id: str,
    origin: str,
    flow_kind: str = "expense",
    allocation_amount_cents: int | None = None,
) -> None:
    _category(session, category_id, flow_kind)
    session.add(
        Allocation(
            transaction_id=transaction.transaction_id,
            category_id=category_id,
            flow_kind=flow_kind,
            amount_cents=(
                transaction.amount_cents
                if allocation_amount_cents is None
                else allocation_amount_cents
            ),
            origin=origin,
            decision="confirmed",
            beneficiary="household",
            cadence="occasional",
            flexibility="variable_essential",
            acquisition="unknown",
        )
    )


def _card(label: str) -> str:
    return f"Paiement par carte\nX1010 {label.upper()} 01/04 - {label}"


@pytest.mark.integration
def test_RULE_02_one_amazon_decision_does_not_suggest_for_amazon_or_vinted(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    with write_transaction(engine) as session:
        account = Account(alias="Compte synthétique", account_role="current")
        session.add(account)
        session.flush()
        amazon_source = _transaction(session, account.account_id, _card("Amazon"))
        _allocation(session, amazon_source, "groceries_supermarket", "manual")
        amazon_target = _transaction(session, account.account_id, _card("Amazon"), -2400)
        vinted_target = _transaction(session, account.account_id, _card("Vinted"), -3000)

    with session_scope(engine) as session:
        amazon_result = history_candidates(session, amazon_target)
        vinted_result = history_candidates(session, vinted_target)
        assert amazon_result.needs_review is True
        assert amazon_result.reason == "ambiguous_marketplace"
        assert amazon_result.candidates == ()
        assert vinted_result.needs_review is True
        assert vinted_result.candidates == ()
    engine.dispose()


@pytest.mark.integration
def test_same_exact_payee_three_manual_validations_suggest_exact_with_source_proof(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    with write_transaction(engine) as session:
        account = Account(alias="Compte synthétique", account_role="current")
        session.add(account)
        session.flush()
        sources = [
            _transaction(session, account.account_id, _card("Epicerie Lune"), amount)
            for amount in (-1000, -1250, -1500)
        ]
        for source in sources:
            _allocation(session, source, "groceries_supermarket", "manual")
        target = _transaction(session, account.account_id, _card("Epicerie Lune"), -2000)

    with session_scope(engine) as session:
        result = history_candidates(session, target)
        assert result.needs_review is False
        assert len(result.candidates) == 1
        candidate = result.candidates[0]
        assert candidate.category_id == "groceries_supermarket"
        assert candidate.method == "history"
        assert candidate.scope == "exact_payee"
        assert candidate.confidence == "exact"
        assert candidate.source_count == 3
        assert set(candidate.source_transaction_ids) == {row.transaction_id for row in sources}
        assert candidate.category_distribution == {"groceries_supermarket": 3}
        assert candidate.last_validated_at is not None
    engine.dispose()


@pytest.mark.integration
def test_two_manual_categories_for_same_payee_are_ambiguous_and_no_category_is_proposed(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    with write_transaction(engine) as session:
        account = Account(alias="Compte synthétique", account_role="current")
        session.add(account)
        session.flush()
        first = _transaction(session, account.account_id, _card("Bazar Sol"), -1000)
        second = _transaction(session, account.account_id, _card("Bazar Sol"), -1500)
        _allocation(session, first, "groceries_supermarket", "manual")
        _allocation(session, second, "home_supplies", "manual")
        target = _transaction(session, account.account_id, _card("Bazar Sol"), -2500)

    with session_scope(engine) as session:
        result = history_candidates(session, target)
        assert result.needs_review is True
        assert result.ambiguous_history is True
        assert len(result.candidates) == 1
        candidate = result.candidates[0]
        assert candidate.category_id is None
        assert candidate.confidence == "ambigu"
        assert candidate.category_distribution == {
            "groceries_supermarket": 1,
            "home_supplies": 1,
        }
    engine.dispose()


@pytest.mark.integration
def test_rule_and_llm_allocations_are_not_memory_evidence_and_persist_is_idempotent(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    with write_transaction(engine) as session:
        account = Account(alias="Compte synthétique", account_role="current")
        session.add(account)
        session.flush()
        rule_source = _transaction(session, account.account_id, _card("Boulangerie Lune"), -1000)
        llm_source = _transaction(session, account.account_id, _card("Boulangerie Lune"), -1100)
        _allocation(session, rule_source, "groceries_supermarket", "rule")
        _allocation(session, llm_source, "groceries_supermarket", "llm")
        target = _transaction(session, account.account_id, _card("Boulangerie Lune"), -1200)

    with session_scope(engine) as session:
        result = history_candidates(session, target)
        assert result.candidates == ()
        assert result.reason == "insufficient_manual_history"

    # Two consistent human examples yield a review suggestion; explicit persistence is safe.
    with write_transaction(engine) as session:
        manual_one = _transaction(session, account.account_id, _card("Boulangerie Lune"), -1300)
        manual_two = _transaction(session, account.account_id, _card("Boulangerie Lune"), -1400)
        _allocation(session, manual_one, "groceries_supermarket", "manual")
        _allocation(session, manual_two, "groceries_supermarket", "manual")
        result = history_candidates(session, target)
        created = persist_suggestions(session, target, result)
        created_again = persist_suggestions(session, target, result)
        assert len(created) == 1
        assert len(created_again) == 1
        assert created[0].suggestion_id == created_again[0].suggestion_id
        assert created[0].method == "history"
        assert created[0].status == "review"
        assert session.scalar(select(func.count()).select_from(ClassificationDecision)) == 0
        assert session.scalar(select(func.count()).select_from(Allocation)) == 4
        assert session.scalar(select(func.count()).select_from(ClassificationSuggestion)) == 1
    engine.dispose()


@pytest.mark.integration
def test_payee_plus_context_is_scoped_to_same_operation_and_sign(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    with write_transaction(engine) as session:
        account = Account(alias="Compte synthétique", account_role="current")
        session.add(account)
        session.flush()
        sources = [
            _transaction(session, account.account_id, _card("Magasin Lune"), -900),
            _transaction(session, account.account_id, _card("Magasin Lune"), -1100),
        ]
        for source in sources:
            _allocation(session, source, "groceries_supermarket", "manual")
        # A confirmed credit on the same merchant is excluded from the debit context.
        refund = _transaction(session, account.account_id, _card("Magasin Lune"), 500)
        _allocation(session, refund, "income_refund", "manual", flow_kind="refund")
        target = _transaction(session, account.account_id, _card("Magasin Lune"), -1000)

    with session_scope(engine) as session:
        result = history_candidates(session, target, scope="payee_plus_context")
        assert result.needs_review is True
        assert result.ambiguous_history is True
        assert len(result.candidates) == 1
        assert result.candidates[0].scope == "payee_plus_context"
        assert result.candidates[0].category_id is None
        assert result.candidates[0].source_count == 2
        assert set(result.candidates[0].source_transaction_ids) == {
            row.transaction_id for row in sources
        }
    engine.dispose()


@pytest.mark.integration
def test_historical_splits_are_never_returned_as_applicable_splits(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    with write_transaction(engine) as session:
        account = Account(alias="Compte synthétique", account_role="current")
        session.add(account)
        session.flush()
        sources = [
            _transaction(session, account.account_id, _card("Achat Partagé"), -1000),
            _transaction(session, account.account_id, _card("Achat Partagé"), -1200),
        ]
        for source in sources:
            _allocation(
                session,
                source,
                "groceries_supermarket",
                "manual",
                allocation_amount_cents=source.amount_cents * 3 // 5,
            )
            _allocation(
                session,
                source,
                "home_supplies",
                "manual",
                allocation_amount_cents=source.amount_cents * 2 // 5,
            )
        target = _transaction(session, account.account_id, _card("Achat Partagé"), -1400)

    with session_scope(engine) as session:
        result = history_candidates(session, target)
        assert result.needs_review is True
        assert result.ambiguous_history is True
        assert len(result.candidates) == 1
        candidate = result.candidates[0]
        assert candidate.category_id is None
        assert candidate.has_historical_split is True
        assert candidate.confidence == "ambigu"
    engine.dispose()
