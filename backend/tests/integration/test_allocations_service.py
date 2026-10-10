from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import AppError
from app.db.errors import VersionConflictError
from app.db.migrate import upgrade_to_head
from app.db.models import (
    Account,
    Allocation,
    AuditEvent,
    ClassificationDecision,
    Transaction,
)
from app.db.session import create_sqlite_engine, session_scope, write_transaction
from app.domain.taxonomy import load_taxonomy
from app.services.allocations import (
    AllocationPart,
    allocate,
    clear_allocations,
    get_allocation_state,
)
from app.services.categories import sync_taxonomy


def _engine(tmp_path: Path, name: str = "allocations.sqlite"):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / name}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    sync_taxonomy(engine, load_taxonomy())
    return engine


def _transaction(engine, amount_cents: int, *, account_role: str = "current") -> tuple[str, str]:
    with write_transaction(engine) as session:
        account = Account(alias="Compte synthétique", account_role=account_role)
        session.add(account)
        session.flush()
        transaction = Transaction(
            account_id=account.account_id,
            booked_on=date(2026, 2, 3),
            amount_cents=amount_cents,
            raw_label="Opération synthétique",
            identity_label="OPERATION SYNTHETIQUE",
            identity_key_sha256="a" * 64,
            occurrence_rank=1,
        )
        session.add(transaction)
        session.flush()
        return account.account_id, transaction.transaction_id


def _part(category_id: str, amount_cents: int) -> AllocationPart:
    return AllocationPart(
        flow_kind="expense",
        category_id=category_id,
        amount_cents=amount_cents,
    )


@pytest.mark.integration
def test_MONEY_01_exact_complete_allocation_and_idempotence(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        _account_id, transaction_id = _transaction(engine, -10_000)
        first = allocate(
            engine,
            transaction_id,
            [_part("groceries", -7_000), _part("restaurants", -3_000)],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        assert first.state == "complete"
        assert first.residual_cents == 0
        assert {item.category_id: item.amount_cents for item in first.allocations} == {
            "groceries": -7_000,
            "restaurants": -3_000,
        }
        assert first.version == 2

        second = allocate(
            engine,
            transaction_id,
            [_part("restaurants", -3_000), _part("groceries", -7_000)],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=2,
        )
        assert second.version == 2
        with session_scope(engine) as session:
            assert session.scalar(select(func.count()).select_from(Allocation)) == 2
            assert session.scalar(select(func.count()).select_from(ClassificationDecision)) == 1
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_02_bad_total_and_partial_residual_are_atomic_and_visible(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        _account_id, transaction_id = _transaction(engine, -10_000)
        with pytest.raises(AppError) as error:
            allocate(
                engine,
                transaction_id,
                [_part("groceries", -7_000), _part("restaurants", -2_999)],
                mode="complete",
                origin="manual",
                actor="reviewer",
                expected_version=1,
            )
        assert error.value.code == "ALLOCATION_TOTAL_MISMATCH"
        with session_scope(engine) as session:
            assert session.scalar(select(func.count()).select_from(Allocation)) == 0
            assert session.scalar(select(func.count()).select_from(ClassificationDecision)) == 0

        state = allocate(
            engine,
            transaction_id,
            [_part("groceries", -7_000)],
            mode="partial",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        assert state.state == "partial"
        assert state.residual_cents == -3_000
        assert state.derived_flow_kinds == ("expense", "unknown")

        with pytest.raises(AppError) as over_allocated:
            allocate(
                engine,
                transaction_id,
                [_part("groceries", -10_001)],
                mode="partial",
                origin="manual",
                actor="reviewer",
                expected_version=2,
            )
        assert over_allocated.value.code == "ALLOCATION_TOTAL_EXCEEDED"
        assert get_allocation_state(engine, transaction_id).version == 2
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_07_personal_allowance_and_MONEY_08_cash_withdrawal_stay_separate(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        _account_id, allowance_id = _transaction(engine, -25_000)
        allowance = allocate(
            engine,
            allowance_id,
            [AllocationPart("allocation_personal", "flow_personal_a", -25_000)],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        assert allowance.allocations[0].flow_kind == "allocation_personal"
        assert allowance.allocations[0].amount_cents == -25_000

        _account_id, cash_id = _transaction(engine, -10_000)
        cash = allocate(
            engine,
            cash_id,
            [AllocationPart("cash_withdrawal_pending", "flow_cash_unallocated", -10_000)],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        assert cash.allocations[0].category_id == "flow_cash_unallocated"
        assert cash.allocations[0].flow_kind == "cash_withdrawal_pending"
        assert "expense" not in {item.flow_kind for item in cash.allocations}
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("amount_cents", "part", "expected_code"),
    [
        (10_000, AllocationPart("expense", "groceries", 10_000), "ALLOCATION_SIGN_INVALID"),
        (
            -10_000,
            AllocationPart("expense", "income_refund", -10_000),
            "ALLOCATION_CATEGORY_FLOW_INVALID",
        ),
        (
            10_000,
            AllocationPart("transfer_savings", "flow_savings_out", 10_000),
            "ALLOCATION_SIGN_INVALID",
        ),
        (
            -10_000,
            AllocationPart("transfer_savings", "flow_savings_out", 10_000),
            "ALLOCATION_SIGN_INVALID",
        ),
    ],
)
def test_MONEY_07_MONEY_08_flow_category_and_direction_are_rejected(
    tmp_path: Path, amount_cents: int, part: AllocationPart, expected_code: str
) -> None:
    engine = _engine(tmp_path, f"invalid-{uuid4()}.sqlite")
    try:
        _account_id, transaction_id = _transaction(engine, amount_cents)
        with pytest.raises(AppError) as error:
            allocate(
                engine,
                transaction_id,
                [part],
                mode="complete",
                origin="manual",
                actor="reviewer",
                expected_version=1,
            )
        assert error.value.code == expected_code
        with session_scope(engine) as session:
            assert session.scalar(select(func.count()).select_from(Allocation)) == 0
    finally:
        engine.dispose()


@pytest.mark.integration
def test_INV_08_nonmanual_sources_cannot_replace_manual_and_manual_supersedes_audited(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        account_id, transaction_id = _transaction(engine, -10_000)
        original = allocate(
            engine,
            transaction_id,
            [_part("groceries", -10_000)],
            mode="complete",
            origin="manual",
            actor="reviewer-a",
            expected_version=1,
        )
        original_id = original.allocations[0].allocation_id

        for source in ("rule", "history", "llm"):
            with pytest.raises(AppError) as error:
                allocate(
                    engine,
                    transaction_id,
                    [_part("restaurants", -10_000)],
                    mode="complete",
                    origin=source,
                    actor="automatic-source",
                    expected_version=2,
                )
            assert error.value.code == "HUMAN_DECISION_PROTECTED"

        corrected = allocate(
            engine,
            transaction_id,
            [_part("restaurants", -10_000)],
            mode="complete",
            origin="manual",
            actor="reviewer-b",
            expected_version=2,
        )
        assert corrected.version == 3
        assert corrected.allocations[0].category_id == "restaurants"
        with session_scope(engine) as session:
            old = session.get(Allocation, original_id)
            transaction = session.get(Transaction, transaction_id)
            decisions = list(
                session.scalars(
                    select(ClassificationDecision).where(
                        ClassificationDecision.transaction_id == transaction_id
                    )
                )
            )
            decision = next(item for item in decisions if item.action == "edit")
            event = session.scalar(
                select(AuditEvent).where(
                    AuditEvent.target_type == "transaction",
                    AuditEvent.target_id == transaction_id,
                    AuditEvent.action == "allocation.superseded",
                )
            )
            assert (
                old is not None
                and old.superseded_by_allocation_id == corrected.allocations[0].allocation_id
            )
            assert old.superseded_by_audit_event_id is not None
            assert transaction is not None
            assert (transaction.account_id, transaction.amount_cents, transaction.raw_label) == (
                account_id,
                -10_000,
                "Opération synthétique",
            )
            assert transaction.version == 3
            assert decision.action == "edit" and decision.actor == "reviewer-b"
            assert event is not None
            assert json.loads(event.before_json or "null")[0]["category_id"] == "groceries"
            assert json.loads(event.after_json or "null")[0]["category_id"] == "restaurants"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_approved_rule_only_applies_to_a_virgin_transaction_and_stale_version_conflicts(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        _account_id, transaction_id = _transaction(engine, -10_000)
        classified = allocate(
            engine,
            transaction_id,
            [_part("groceries", -10_000)],
            mode="complete",
            origin="rule",
            actor="approved-rule",
            expected_version=1,
        )
        assert classified.allocations[0].origin == "rule"
        with pytest.raises(AppError) as error:
            allocate(
                engine,
                transaction_id,
                [_part("restaurants", -10_000)],
                mode="complete",
                origin="rule",
                actor="approved-rule",
                expected_version=2,
            )
        assert error.value.code == "RULE_REQUIRES_UNCLASSIFIED_TRANSACTION"
        with pytest.raises(VersionConflictError):
            allocate(
                engine,
                transaction_id,
                [_part("restaurants", -10_000)],
                mode="complete",
                origin="manual",
                actor="reviewer",
                expected_version=1,
            )
        assert get_allocation_state(engine, transaction_id).version == 2
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_12_clear_retracts_audited_and_can_restore_the_prior_allocation(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        account_id, transaction_id = _transaction(engine, -10_000)
        state = allocate(
            engine,
            transaction_id,
            [_part("groceries", -10_000)],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        old = state.allocations[0]
        cleared = clear_allocations(
            engine,
            transaction_id,
            actor="reviewer-clear",
            expected_version=state.version,
        )
        assert cleared.state == "unclassified"
        assert cleared.allocations == ()
        assert cleared.residual_cents == -10_000
        assert cleared.version == state.version + 1

        with session_scope(engine) as session:
            retracted = session.get(Allocation, old.allocation_id)
            decision = session.scalar(
                select(ClassificationDecision).where(
                    ClassificationDecision.transaction_id == transaction_id,
                    ClassificationDecision.action == "clear",
                )
            )
            audit = session.scalar(
                select(AuditEvent).where(
                    AuditEvent.target_type == "transaction",
                    AuditEvent.target_id == transaction_id,
                    AuditEvent.action == "allocation.retracted",
                )
            )
            transaction = session.get(Transaction, transaction_id)
            assert retracted is not None and retracted.retracted_at_utc is not None
            assert (
                decision is not None and retracted.retracted_by_decision_id == decision.decision_id
            )
            assert audit is not None and audit.before_json == decision.before_json
            assert audit.after_json == "[]" and decision.after_json == "[]"
            snapshot = json.loads(decision.before_json or "null")
            assert snapshot[0]["allocation_id"] == old.allocation_id
            assert transaction is not None
            assert (transaction.account_id, transaction.amount_cents, transaction.raw_label) == (
                account_id,
                -10_000,
                "Opération synthétique",
            )

        repeated = clear_allocations(
            engine,
            transaction_id,
            actor="reviewer-clear",
            expected_version=cleared.version,
        )
        assert repeated.version == cleared.version
        with session_scope(engine) as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ClassificationDecision)
                    .where(ClassificationDecision.transaction_id == transaction_id)
                )
                == 2
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.action == "allocation.retracted")
                )
                == 1
            )
        with pytest.raises(VersionConflictError):
            clear_allocations(
                engine,
                transaction_id,
                actor="reviewer-clear",
                expected_version=state.version,
            )

        restored = allocate(
            engine,
            transaction_id,
            [
                AllocationPart(
                    snapshot[0]["flow_kind"],
                    snapshot[0]["category_id"],
                    snapshot[0]["amount_cents"],
                    snapshot[0]["beneficiary"],
                    snapshot[0]["cadence"],
                    snapshot[0]["flexibility"],
                    snapshot[0]["acquisition"],
                )
            ],
            mode="complete",
            origin="manual",
            actor="reviewer-undo",
            expected_version=cleared.version,
        )
        assert restored.state == "complete"
        assert restored.allocations[0].category_id == old.category_id
        assert restored.allocations[0].amount_cents == old.amount_cents
        assert restored.version == cleared.version + 1
    finally:
        engine.dispose()


@pytest.mark.integration
def test_INV_08_concurrent_sessions_produce_one_stale_version_conflict(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        _account_id, transaction_id = _transaction(engine, -1_000)
        barrier = Barrier(2)

        def attempt(category_id: str) -> str:
            barrier.wait(timeout=5)
            try:
                allocate(
                    engine,
                    transaction_id,
                    [_part(category_id, -1_000)],
                    mode="complete",
                    origin="manual",
                    actor=f"reviewer-{category_id}",
                    expected_version=1,
                )
            except VersionConflictError:
                return "conflict"
            return "committed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("groceries", "restaurants")))
        assert sorted(results) == ["committed", "conflict"]
        assert get_allocation_state(engine, transaction_id).version == 2
    finally:
        engine.dispose()


@pytest.mark.integration
def test_INV_14_mid_decision_failure_rolls_back_allocations_decision_audit_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.allocations as allocation_service

    engine = _engine(tmp_path)
    try:
        _account_id, transaction_id = _transaction(engine, -10_000)
        original = allocation_service._write_decision_and_audit

        def fail_after_audit(session, **kwargs) -> None:
            original(session, **kwargs)
            session.flush()
            raise RuntimeError("synthetic failure after audit flush")

        monkeypatch.setattr(allocation_service, "_write_decision_and_audit", fail_after_audit)
        with pytest.raises(RuntimeError, match="synthetic failure"):
            allocate(
                engine,
                transaction_id,
                [_part("groceries", -10_000)],
                mode="complete",
                origin="manual",
                actor="reviewer",
                expected_version=1,
            )
        with session_scope(engine) as session:
            assert session.scalar(select(func.count()).select_from(Allocation)) == 0
            assert session.scalar(select(func.count()).select_from(ClassificationDecision)) == 0
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.target_type == "transaction")
                )
                == 0
            )
            transaction = session.get(Transaction, transaction_id)
            assert transaction is not None and transaction.version == 1
    finally:
        engine.dispose()


@pytest.mark.integration
@hypothesis_settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    operations=st.lists(
        st.tuples(
            st.sampled_from(("manual", "rule", "history", "llm")),
            st.integers(min_value=1, max_value=10_000),
            st.sampled_from(("groceries", "restaurants", "children")),
        ),
        min_size=1,
        max_size=8,
    )
)
def test_INV_06_INV_08_random_allocation_sequences_keep_bounds_sign_and_manual_decisions(
    tmp_path: Path, operations: list[tuple[str, int, str]]
) -> None:
    engine = _engine(tmp_path, f"sequence-{uuid4()}.sqlite")
    try:
        _account_id, transaction_id = _transaction(engine, -10_000)
        for origin, allocated_magnitude, category_id in operations:
            before = get_allocation_state(engine, transaction_id)
            mode = "complete" if origin == "rule" or allocated_magnitude == 10_000 else "partial"
            amount = -10_000 if mode == "complete" else -allocated_magnitude
            try:
                state = allocate(
                    engine,
                    transaction_id,
                    [_part(category_id, amount)],
                    mode=mode,
                    origin=origin,
                    actor="sequence-reviewer",
                    expected_version=before.version,
                )
            except AppError as error:
                assert error.code in {
                    "HUMAN_DECISION_PROTECTED",
                    "RULE_REQUIRES_UNCLASSIFIED_TRANSACTION",
                    "SUGGESTION_REQUIRES_REVIEW",
                }
                state = get_allocation_state(engine, transaction_id)
            assert abs(sum(item.amount_cents for item in state.allocations)) <= 10_000
            assert state.residual_cents == -10_000 - sum(
                item.amount_cents for item in state.allocations
            )
            assert all(item.amount_cents < 0 for item in state.allocations)
            if any(item.origin == "manual" for item in before.allocations):
                assert any(item.origin == "manual" for item in state.allocations)
    finally:
        engine.dispose()
