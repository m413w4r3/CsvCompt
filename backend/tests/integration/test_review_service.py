from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.classification.engine import apply_rule, evaluate_transaction
from app.classification.rule_store import (
    RuleAction,
    RuleCondition,
    RuleDraft,
    approve_rule,
    create_rule,
)
from app.core.config import Settings
from app.core.errors import AppError
from app.db.migrate import upgrade_to_head
from app.db.models import (
    Account,
    AuditEvent,
    ClassificationDecision,
    RuleExecution,
    Transaction,
)
from app.db.session import create_sqlite_engine, session_scope, write_transaction
from app.domain.taxonomy import load_taxonomy
from app.importers.normalize import identity_label
from app.services import review as review_service
from app.services.allocations import AllocationPart, allocate
from app.services.categories import sync_taxonomy
from app.services.review import undo_decision, undo_rule_application


def _engine(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'review.sqlite'}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    sync_taxonomy(engine, load_taxonomy())
    return engine


def _transactions(engine, amounts: tuple[int, ...]) -> tuple[str, ...]:
    with write_transaction(engine) as session:
        account = Account(alias="Compte de test revue", account_role="current")
        session.add(account)
        session.flush()
        result = []
        for index, amount in enumerate(amounts):
            label = f"CONFIDENTIAL-REVIEW-SYNTHETIC-{index}"
            normalized = identity_label(label).value
            transaction = Transaction(
                account_id=account.account_id,
                booked_on=date(2026, 5, index + 1),
                amount_cents=amount,
                raw_label=label,
                identity_label=normalized,
                identity_key_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
                occurrence_rank=1,
            )
            session.add(transaction)
            result.append(transaction)
        session.flush()
        return tuple(row.transaction_id for row in result)


def _parts(category: str, amount: int) -> tuple[AllocationPart, ...]:
    return (AllocationPart(flow_kind="expense", category_id=category, amount_cents=amount),)


def _approved_rule(engine):
    created = create_rule(
        engine,
        RuleDraft(
            name="Règle revue synthétique",
            condition=RuleCondition(label_pattern="*REVIEW-SYNTHETIC*", direction="debit"),
            action=RuleAction(flow_kind="expense", category_id="groceries"),
            application_scope="selection",
        ),
        actor="reviewer-synthetic",
    )
    return approve_rule(
        engine, created.rule_id, expected_version=created.version, actor="reviewer-synthetic"
    )


def _table_snapshot(engine) -> dict[str, tuple[tuple[object, ...], ...]]:
    names = (
        "allocations",
        "audit_events",
        "classification_decisions",
        "rule_executions",
        "transactions",
    )
    with session_scope(engine) as session:
        return {
            name: tuple(
                tuple(row)
                for row in session.connection().exec_driver_sql(
                    f'SELECT * FROM "{name}" ORDER BY rowid'
                )
            )
            for name in names
        }


@pytest.mark.integration
def test_RULE_04_manual_reventilation_undo_restores_exact_cents_and_is_idempotent(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        (transaction_id,) = _transactions(engine, (-10_000,))
        original = allocate(
            engine,
            transaction_id,
            (
                AllocationPart(flow_kind="expense", category_id="groceries", amount_cents=-7_001),
                AllocationPart(flow_kind="expense", category_id="restaurants", amount_cents=-2_999),
            ),
            mode="complete",
            origin="manual",
            actor="reviewer-synthetic",
            expected_version=1,
        )
        corrected = allocate(
            engine,
            transaction_id,
            _parts("clothing", -10_000),
            mode="complete",
            origin="manual",
            actor="reviewer-synthetic",
            expected_version=original.version,
        )
        assert original.decision_id is not None and corrected.decision_id is not None

        undone = undo_decision(
            engine,
            corrected.decision_id,
            expected_version=corrected.version,
            actor="reviewer-synthetic",
        )

        assert undone.state.version == corrected.version + 1
        assert undone.idempotent is False
        assert sorted(
            (item.category_id, item.amount_cents, item.origin) for item in undone.state.allocations
        ) == [("groceries", -7_001, "manual"), ("restaurants", -2_999, "manual")]
        with session_scope(engine) as session:
            transaction = session.get(Transaction, transaction_id)
            target = session.get(ClassificationDecision, corrected.decision_id)
            audit = session.scalar(
                select(AuditEvent)
                .where(
                    AuditEvent.target_type == "transaction",
                    AuditEvent.target_id == transaction_id,
                    AuditEvent.action == "classification.undo",
                )
                .order_by(AuditEvent.created_at_utc.desc())
            )
            assert transaction is not None and transaction.amount_cents == -10_000
            assert target is not None and target.undone_by_decision_id == undone.undo_decision_id
            assert audit is not None and audit.actor == "reviewer-synthetic"
            assert "CONFIDENTIAL-REVIEW-SYNTHETIC" not in (audit.before_json or "")
            assert "CONFIDENTIAL-REVIEW-SYNTHETIC" not in (audit.after_json or "")
            audit_count = session.scalar(
                select(AuditEvent.audit_event_id).where(
                    AuditEvent.target_type == "transaction",
                    AuditEvent.target_id == transaction_id,
                    AuditEvent.action == "classification.undo",
                )
            )
            assert audit_count is not None

        snapshot_after_undo = _table_snapshot(engine)
        replay = undo_decision(
            engine,
            corrected.decision_id,
            expected_version=1,
            actor="reviewer-synthetic",
        )
        assert replay.idempotent is True
        assert replay.undo_decision_id == undone.undo_decision_id
        assert _table_snapshot(engine) == snapshot_after_undo
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_04_undo_rejects_version_divergence_without_writing(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        (transaction_id,) = _transactions(engine, (-2_500,))
        state = allocate(
            engine,
            transaction_id,
            _parts("groceries", -2_500),
            mode="complete",
            origin="manual",
            actor="reviewer-synthetic",
            expected_version=1,
        )
        assert state.decision_id is not None
        with write_transaction(engine) as session:
            transaction = session.get(Transaction, transaction_id)
            assert transaction is not None
            transaction.version += 1
        before = _table_snapshot(engine)

        with pytest.raises(AppError) as failure:
            undo_decision(
                engine,
                state.decision_id,
                expected_version=state.version + 1,
                actor="reviewer-synthetic",
            )

        assert failure.value.code == "UNDO_STATE_DIVERGED"
        assert _table_snapshot(engine) == before
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_04_invalid_snapshot_returns_contract_error_without_writes(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        (transaction_id,) = _transactions(engine, (-4_000,))
        original = allocate(
            engine,
            transaction_id,
            _parts("groceries", -4_000),
            mode="complete",
            origin="manual",
            actor="reviewer-synthetic",
            expected_version=1,
        )
        corrected = allocate(
            engine,
            transaction_id,
            _parts("clothing", -4_000),
            mode="complete",
            origin="manual",
            actor="reviewer-synthetic",
            expected_version=original.version,
        )
        assert corrected.decision_id is not None

        with write_transaction(engine) as session:
            row = session.get(ClassificationDecision, corrected.decision_id)
            assert row is not None
            snapshot = json.loads(row.before_json)
            snapshot[0]["category_id"] = "invalid-synthetic-category"
            trigger_sql = (
                session.connection()
                .exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='trg_classification_decisions_undo_guard'"
                )
                .scalar_one()
            )
            connection = session.connection()
            connection.exec_driver_sql("DROP TRIGGER trg_classification_decisions_undo_guard")
            connection.exec_driver_sql(
                "UPDATE classification_decisions SET before_json=?, version=version+1 "
                "WHERE decision_id=?",
                (
                    json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
                    corrected.decision_id,
                ),
            )
            connection.exec_driver_sql(trigger_sql)

        before = _table_snapshot(engine)
        with pytest.raises(AppError) as failure:
            undo_decision(
                engine,
                corrected.decision_id,
                expected_version=corrected.version,
                actor="reviewer-synthetic",
            )

        assert failure.value.code == "UNDO_SNAPSHOT_INVALID"
        assert _table_snapshot(engine) == before
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_04_rule_undo_requires_confirmation_for_later_human_decision(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        (transaction_id,) = _transactions(engine, (-3_000,))
        rule = _approved_rule(engine)
        batch_id = str(uuid4())
        applied = apply_rule(
            engine,
            rule.rule_id,
            scope="selection",
            selection=(transaction_id,),
            expected_version=rule.version,
            actor="reviewer-synthetic",
            batch_id=batch_id,
        )
        assert applied.applied_ids == (transaction_id,)
        manual = allocate(
            engine,
            transaction_id,
            _parts("groceries", -3_000),
            mode="complete",
            origin="manual",
            actor="reviewer-synthetic",
            expected_version=applied.allocation_states[0].version,
        )
        assert manual.allocations[0].origin == "manual"
        assert manual.version == applied.allocation_states[0].version + 1
        before = _table_snapshot(engine)

        with pytest.raises(AppError) as failure:
            undo_rule_application(
                engine,
                batch_id,
                expected_versions={transaction_id: manual.version},
                actor="reviewer-synthetic",
            )
        assert failure.value.code == "UNDO_HUMAN_DECISION_CONFIRMATION_REQUIRED"
        assert _table_snapshot(engine) == before

        undone = undo_rule_application(
            engine,
            batch_id,
            expected_versions={transaction_id: manual.version},
            actor="reviewer-synthetic",
            confirm_human_override=True,
        )
        assert undone.human_override_ids == (transaction_id,)
        assert undone.states[0].state == "unclassified"
        assert undone.states[0].residual_cents == -3_000
        assert evaluate_transaction(engine, transaction_id).human_decision_protected
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_04_rule_batch_undo_is_atomic_when_a_later_row_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    try:
        transaction_ids = _transactions(engine, (-1_000, -2_000))
        rule = _approved_rule(engine)
        batch_id = str(uuid4())
        applied = apply_rule(
            engine,
            rule.rule_id,
            scope="selection",
            selection=transaction_ids,
            expected_version=rule.version,
            actor="reviewer-synthetic",
            batch_id=batch_id,
        )
        versions = {state.transaction_id: state.version for state in applied.allocation_states}
        before = _table_snapshot(engine)
        real_apply_one = review_service._undo_rule_execution_in_session
        calls = 0

        def fail_after_first(*args, **kwargs):
            nonlocal calls
            result = real_apply_one(*args, **kwargs)
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic rollback after second row")
            return result

        monkeypatch.setattr(review_service, "_undo_rule_execution_in_session", fail_after_first)
        with pytest.raises(RuntimeError, match="synthetic rollback"):
            undo_rule_application(
                engine,
                batch_id,
                expected_versions=versions,
                actor="reviewer-synthetic",
            )
        assert calls == 2
        assert _table_snapshot(engine) == before
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_04_concurrent_rule_batch_undo_replay_creates_one_undo_per_transaction(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        transaction_ids = _transactions(engine, (-1_000, -2_000))
        rule = _approved_rule(engine)
        batch_id = str(uuid4())
        applied = apply_rule(
            engine,
            rule.rule_id,
            scope="selection",
            selection=transaction_ids,
            expected_version=rule.version,
            actor="reviewer-synthetic",
            batch_id=batch_id,
        )
        versions = {state.transaction_id: state.version for state in applied.allocation_states}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: undo_rule_application(
                        engine,
                        batch_id,
                        expected_versions=versions,
                        actor="reviewer-synthetic",
                    ),
                    range(2),
                )
            )

        assert sorted(result.idempotent for result in results) == [False, True]
        with session_scope(engine) as session:
            undo_events = list(
                session.scalars(
                    select(ClassificationDecision).where(
                        ClassificationDecision.action == "undo",
                        ClassificationDecision.transaction_id.in_(transaction_ids),
                    )
                )
            )
            executions = list(
                session.scalars(select(RuleExecution).where(RuleExecution.batch_id == batch_id))
            )
            assert len(undo_events) == len(transaction_ids)
            assert len(executions) == len(transaction_ids)
            assert all(row.undone_by_decision_id for row in executions)
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_04_rule_apply_replay_with_same_batch_id_adds_no_duplicate_trace(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        transaction_ids = _transactions(engine, (-1_500, -2_500))
        rule = _approved_rule(engine)
        batch_id = str(uuid4())
        first = apply_rule(
            engine,
            rule.rule_id,
            scope="selection",
            selection=transaction_ids,
            expected_version=rule.version,
            actor="reviewer-synthetic",
            batch_id=batch_id,
        )
        before = _table_snapshot(engine)
        replay = apply_rule(
            engine,
            rule.rule_id,
            scope="selection",
            selection=transaction_ids,
            expected_version=rule.version,
            actor="reviewer-synthetic",
            batch_id=batch_id,
        )
        assert first.batch_id == replay.batch_id == batch_id
        assert replay.applied_ids == first.applied_ids
        assert _table_snapshot(engine) == before
    finally:
        engine.dispose()
