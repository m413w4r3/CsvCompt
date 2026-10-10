"""Persistent regression coverage for the T19 rule engine review (M-01)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.classification import engine as rules_engine
from app.classification.engine import (
    SimulationScope,
    apply_rule,
    evaluate_transaction,
    simulate_rule,
)
from app.classification.rule_store import (
    RuleAction,
    RuleCondition,
    RuleDraft,
    approve_rule,
    create_rule,
)
from app.core.config import Settings
from app.db.errors import VersionConflictError
from app.db.migrate import upgrade_to_head
from app.db.models import (
    Account,
    Allocation,
    AuditEvent,
    ClassificationDecision,
    RuleExecution,
    Transaction,
)
from app.db.session import create_sqlite_engine, session_scope, write_transaction
from app.domain.taxonomy import load_taxonomy
from app.importers.normalize import identity_label
from app.services.allocations import AllocationPart, allocate, get_allocation_state
from app.services.categories import sync_taxonomy


def _database(tmp_path: Path, filename: str = "rules-hardening.sqlite"):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / filename}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    sync_taxonomy(engine, load_taxonomy())
    return engine


def _insert_transactions(
    engine, amounts: list[int], *, labels: list[str] | None = None
) -> list[str]:
    if labels is None:
        labels = [f"Opération SYNTHETIC {index}" for index in range(len(amounts))]
    assert len(labels) == len(amounts)
    with write_transaction(engine) as session:
        account = Account(alias="Compte de test synthétique", account_role="current")
        session.add(account)
        session.flush()
        transactions = []
        for index, (amount, label) in enumerate(zip(amounts, labels, strict=True)):
            normalized = identity_label(label).value
            row = Transaction(
                account_id=account.account_id,
                booked_on=date(2026, 2, index + 1),
                amount_cents=amount,
                raw_label=label,
                identity_label=normalized,
                identity_key_sha256=sha256(normalized.encode()).hexdigest(),
                occurrence_rank=1,
            )
            session.add(row)
            transactions.append(row)
        session.flush()
        return [row.transaction_id for row in transactions]


def _draft(
    category: str = "groceries",
    *,
    priority: int = 100,
    pattern: str = "*SYNTHETIC*",
    scope: str = "selection",
) -> RuleDraft:
    return RuleDraft(
        name="Règle synthétique",
        condition=RuleCondition(label_pattern=pattern),
        action=RuleAction(flow_kind="expense", category_id=category),
        priority=priority,  # type: ignore[arg-type]
        application_scope=scope,  # type: ignore[arg-type]
    )


def _approved_rule(engine, draft: RuleDraft):
    created = create_rule(engine, draft, actor="reviewer")
    return approve_rule(engine, created.rule_id, expected_version=created.version, actor="reviewer")


def _snapshot_all_tables(engine) -> dict[str, tuple[tuple[object, ...], ...]]:
    with session_scope(engine) as session:
        names = (
            session.connection()
            .exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            .scalars()
            .all()
        )
        return {
            name: tuple(
                tuple(row)
                for row in session.connection().exec_driver_sql(
                    f'SELECT * FROM "{name}" ORDER BY rowid'
                )
            )
            for name in names
        }


def _apply(
    engine, rule, transaction_ids: list[str] | tuple[str, ...], *, batch_id: str | None = None
):
    return apply_rule(
        engine,
        rule.rule_id,
        scope="selection",
        selection=transaction_ids,
        expected_version=rule.version,
        actor="reviewer",
        batch_id=batch_id,
    )


@pytest.mark.integration
def test_RULE_01_human_reject_without_allocation_remains_locked_against_rule(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    try:
        (transaction_id,) = _insert_transactions(engine, [-2400])
        with write_transaction(engine) as session:
            session.add(
                ClassificationDecision(
                    decision_id=str(uuid4()),
                    transaction_id=transaction_id,
                    action="reject",
                    actor="reviewer",
                    transaction_version=1,
                )
            )

        rule = _approved_rule(engine, _draft(scope="existing_and_future"))
        result = _apply(engine, rule, [transaction_id])

        assert result.applied_ids == ()
        assert result.skipped_manual_ids == (transaction_id,)
        assert get_allocation_state(engine, transaction_id).allocations == ()
        evaluation = evaluate_transaction(engine, transaction_id)
        assert evaluation.human_decision_protected
        assert evaluation.winner is not None and evaluation.winner.source == "human"
        with session_scope(engine) as session:
            assert (
                session.scalar(
                    select(Allocation.allocation_id).where(
                        Allocation.transaction_id == transaction_id,
                        Allocation.superseded_by_allocation_id.is_(None),
                        Allocation.retracted_at_utc.is_(None),
                    )
                )
                is None
            )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_01_human_split_keeps_parts_total_and_version_after_rule_application(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    try:
        (transaction_id,) = _insert_transactions(engine, [-2400])
        manual = allocate(
            engine,
            transaction_id,
            [
                AllocationPart(flow_kind="expense", category_id="groceries", amount_cents=-900),
                AllocationPart(flow_kind="expense", category_id="clothing", amount_cents=-1500),
            ],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        rule = _approved_rule(engine, _draft(category="restaurants", scope="existing_and_future"))

        result = _apply(engine, rule, [transaction_id])
        after = get_allocation_state(engine, transaction_id)

        assert result.skipped_manual_ids == (transaction_id,)
        assert after.version == manual.version
        assert sorted(
            (part.category_id, part.amount_cents, part.origin) for part in after.allocations
        ) == [
            ("clothing", -1500, "manual"),
            ("groceries", -900, "manual"),
        ]
        assert sum(part.amount_cents for part in after.allocations) == -2400
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_11_rule_insertion_and_generated_id_permutations_keep_outcomes_stable(
    tmp_path: Path,
) -> None:
    outcomes = []
    for run, order in enumerate((("groceries", "restaurants"), ("restaurants", "groceries"))):
        engine = _database(tmp_path, f"rules-order-{run}.sqlite")
        try:
            ids = _insert_transactions(
                engine,
                [-1000, -1200],
                labels=["Opération CONFLICT-SYNTHETIC", "Opération WINNER-SYNTHETIC"],
            )
            conflict_rules = [
                _approved_rule(
                    engine,
                    _draft(
                        category=category,
                        pattern="*CONFLICT-SYNTHETIC*",
                        priority=10,
                        scope="existing_and_future",
                    ),
                )
                for category in order
            ]
            winner_rules = [
                _approved_rule(
                    engine,
                    _draft(
                        category="groceries",
                        pattern="*WINNER-SYNTHETIC*",
                        priority=20,
                        scope="existing_and_future",
                    ),
                )
                for _category in order
            ]

            conflict = evaluate_transaction(engine, ids[0])
            winner = evaluate_transaction(engine, ids[1])

            assert conflict.winner is None
            assert conflict.conflict_rule_ids == tuple(
                sorted(rule.rule_id for rule in conflict_rules)
            )
            assert winner.conflict_rule_ids == ()
            assert winner.winner is not None
            assert winner.winner.category_id == "groceries"
            assert winner.winner.rule_id == min(rule.rule_id for rule in winner_rules)
            outcomes.append(
                (
                    conflict.winner,
                    len(conflict.conflict_rule_ids),
                    winner.winner.category_id,
                )
            )
        finally:
            engine.dispose()

    assert [item[1:] for item in outcomes] == [(2, "groceries"), (2, "groceries")]


@pytest.mark.integration
def test_RULE_13_interrupted_batch_replay_traces_each_operation_once_without_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(tmp_path)
    try:
        ids = _insert_transactions(engine, [-1000, -1200])
        rule = _approved_rule(engine, _draft(scope="existing_and_future"))
        batch_id = str(uuid4())
        original_trace = rules_engine._trace_execution
        calls = 0

        def fail_on_second_trace(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic trace failure on second operation")
            return original_trace(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(rules_engine, "_trace_execution", fail_on_second_trace)
            with pytest.raises(RuntimeError, match="synthetic trace failure"):
                _apply(engine, rule, ids, batch_id=batch_id)

        assert get_allocation_state(engine, ids[0]).state == "complete"
        assert get_allocation_state(engine, ids[1]).state == "unclassified"
        with session_scope(engine) as session:
            initial = list(
                session.scalars(select(RuleExecution).where(RuleExecution.batch_id == batch_id))
            )
            assert [(row.transaction_id, row.outcome) for row in initial] == [(ids[0], "applied")]

        replay = _apply(engine, rule, ids, batch_id=batch_id)
        assert replay.applied_ids == (ids[0], ids[1])
        with session_scope(engine) as session:
            executions = list(
                session.scalars(select(RuleExecution).where(RuleExecution.batch_id == batch_id))
            )
            assert len(executions) == 2
            assert {row.transaction_id for row in executions} == set(ids)
            assert all(row.outcome == "applied" for row in executions)
            linked_decisions = {row.classification_decision_id for row in executions}
            assert None not in linked_decisions
            for execution in executions:
                assert (
                    session.scalar(
                        select(Allocation.allocation_id).where(
                            Allocation.transaction_id == execution.transaction_id,
                            Allocation.origin == "rule",
                            Allocation.superseded_by_allocation_id.is_(None),
                            Allocation.retracted_at_utc.is_(None),
                        )
                    )
                    is not None
                )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_03_simulation_after_partial_batch_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(tmp_path)
    try:
        ids = _insert_transactions(engine, [-1000, -1200])
        rule = _approved_rule(engine, _draft(scope="existing_and_future"))
        original_trace = rules_engine._trace_execution
        calls = 0

        def fail_on_second_trace(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic trace failure on second operation")
            return original_trace(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(rules_engine, "_trace_execution", fail_on_second_trace)
            with pytest.raises(RuntimeError, match="synthetic trace failure"):
                _apply(engine, rule, ids, batch_id=str(uuid4()))

        before = _snapshot_all_tables(engine)
        simulation = simulate_rule(
            engine,
            rule,
            SimulationScope(transaction_ids=tuple(ids), page_size=10, max_transactions=10),
        )
        after = _snapshot_all_tables(engine)

        assert before == after
        assert simulation.already_classified_ids == (ids[0],)
        assert tuple(row.transaction_id for row in simulation.rows) == (ids[1],)
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_13_two_session_rule_race_rejects_stale_human_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(tmp_path)
    rule_entered_trace = threading.Event()
    human_started = threading.Event()
    release_rule_trace = threading.Event()
    try:
        ids = _insert_transactions(engine, [-1000, -1200])
        rule_target, preserved_human_target = ids
        human_before = allocate(
            engine,
            preserved_human_target,
            [AllocationPart(flow_kind="expense", category_id="clothing", amount_cents=-1200)],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        rule = _approved_rule(engine, _draft(scope="existing_and_future"))
        original_trace = rules_engine._trace_execution

        def pause_rule_inside_its_write(*args, **kwargs):
            result = original_trace(*args, **kwargs)
            rule_entered_trace.set()
            if not release_rule_trace.wait(timeout=10):
                raise TimeoutError("rule trace synchronization timed out")
            return result

        monkeypatch.setattr(rules_engine, "_trace_execution", pause_rule_inside_its_write)

        def stale_human_write():
            human_started.set()
            return allocate(
                engine,
                rule_target,
                [AllocationPart(flow_kind="expense", category_id="clothing", amount_cents=-1000)],
                mode="complete",
                origin="manual",
                actor="reviewer",
                expected_version=1,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            rule_future = pool.submit(_apply, engine, rule, [rule_target, preserved_human_target])
            assert rule_entered_trace.wait(timeout=10)
            human_future = pool.submit(stale_human_write)
            assert human_started.wait(timeout=10)
            release_rule_trace.set()
            applied = rule_future.result(timeout=10)
            with pytest.raises(VersionConflictError):
                human_future.result(timeout=10)

        assert applied.applied_ids == (rule_target,)
        current_rule_target = get_allocation_state(engine, rule_target)
        assert current_rule_target.version == 2
        assert len(current_rule_target.allocations) == 1
        assert current_rule_target.allocations[0].origin == "rule"
        preserved = get_allocation_state(engine, preserved_human_target)
        assert preserved.version == human_before.version
        assert preserved.allocations == human_before.allocations
        assert all(part.origin == "manual" for part in preserved.allocations)
        with session_scope(engine) as session:
            decisions = list(
                session.scalars(
                    select(ClassificationDecision).where(
                        ClassificationDecision.transaction_id == rule_target
                    )
                )
            )
            assert len(decisions) == 1
            assert decisions[0].action == "confirm"
            assert '"origin":"rule"' in (decisions[0].after_json or "")
            assert (
                session.scalar(
                    select(AuditEvent.audit_event_id).where(
                        AuditEvent.target_type == "transaction",
                        AuditEvent.target_id == preserved_human_target,
                        AuditEvent.action == "allocation.superseded",
                    )
                )
                is None
            )
    finally:
        release_rule_trace.set()
        engine.dispose()
