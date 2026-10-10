from __future__ import annotations

import json
import random
import time
from datetime import date
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.classification import engine as rules_engine
from app.classification import rule_store
from app.classification.engine import (
    SimulationScope,
    apply_rule,
    evaluate_transaction,
    simulate_rule,
)
from app.classification.rule_store import (
    RatioPart,
    RuleAction,
    RuleCondition,
    RuleDraft,
    RuleStoreError,
    approve_rule,
    create_rule,
    disable_rule,
    exclude_transaction_from_rule,
    get_rule,
    label_pattern_matches,
    update_rule,
)
from app.core.config import Settings
from app.core.errors import AppError
from app.db.errors import VersionConflictError
from app.db.migrate import upgrade_to_head
from app.db.models import (
    Account,
    AuditEvent,
    ClassificationSuggestion,
    ImportAccountSection,
    ImportFile,
    Payee,
    Rule,
    RuleExecution,
    SourceRow,
    Transaction,
)
from app.db.session import create_sqlite_engine, session_scope, write_transaction
from app.domain.taxonomy import load_taxonomy
from app.importers.normalize import identity_label
from app.services.allocations import (
    AllocationPart,
    allocate,
    clear_allocations,
    get_allocation_state,
)
from app.services.categories import sync_taxonomy
from app.services.payees import create_payee_for_transaction


def _database(tmp_path: Path, name: str = "rules.sqlite"):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / name}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    sync_taxonomy(engine, load_taxonomy())
    return engine


def _insert_transactions(
    engine,
    amounts: list[int],
    *,
    label: str = "Operation SYNTHETIC",
    payee: str | None = None,
    first_day: int = 1,
) -> tuple[str, list[str]]:
    with write_transaction(engine) as session:
        account = Account(alias="Compte de test synthétique", account_role="current")
        session.add(account)
        session.flush()
        payee_id = None
        if payee:
            metadata = json.dumps(
                {
                    "version": 1,
                    "entity_kind": "marketplace"
                    if payee.casefold() in {"vinted", "amazon", "paypal"}
                    else "merchant",
                    "merchant_name": payee,
                    "operation_type": "Paiement par carte",
                    "source_kind": "card",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            row = Payee(display_name=payee, normalized_name=metadata)
            session.add(row)
            session.flush()
            payee_id = row.payee_id
        transactions = []
        for index, amount in enumerate(amounts):
            label_value = label if len(amounts) == 1 else f"{label} {index:05d}"
            normalized = identity_label(label_value).value
            transaction = Transaction(
                account_id=account.account_id,
                booked_on=date(2026, 2, min(first_day + index, 28)),
                amount_cents=amount,
                raw_label=label_value,
                identity_label=normalized,
                identity_key_sha256=sha256(normalized.encode()).hexdigest(),
                occurrence_rank=1,
                payee_id=payee_id,
            )
            session.add(transaction)
            transactions.append(transaction)
        session.flush()
        return account.account_id, [row.transaction_id for row in transactions]


def _draft(
    *,
    scope: str = "selection",
    category: str = "groceries",
    priority: int = 100,
    pattern: str = "*SYNTHETIC*",
    payee_id: str | None = None,
    condition: RuleCondition | None = None,
    ratios: tuple[RatioPart, ...] = (),
) -> RuleDraft:
    return RuleDraft(
        name="Règle synthétique",
        condition=condition
        or RuleCondition(payee_id=payee_id, label_pattern=pattern if pattern else None),
        action=RuleAction(
            flow_kind="expense",
            category_id=None if ratios else category,
            ratios=ratios,
        ),
        priority=priority,
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


def _append_synthetic_source(engine, transaction_id: str, occurrence: int) -> None:
    with write_transaction(engine) as session:
        transaction = session.get(Transaction, transaction_id)
        assert transaction is not None
        import_id = str(uuid4())
        session.add(
            ImportFile(
                import_file_id=import_id,
                file_sha256=sha256(f"synthetic-import-{occurrence}".encode()).hexdigest(),
                archive_relpath=f"data/originals/{import_id}.bin",
                archive_storage_mode="raw",
                archive_permissions_octal="0600",
                file_size_bytes=0,
                received_on=date(2026, 2, 28),
                generation_date=date(2026, 2, 27),
                generation_date_source="header_download",
                parsed_operation_count=1,
                status="previewed",
            )
        )
        session.flush()
        session.add(
            ImportAccountSection(
                import_file_id=import_id,
                section_index=1,
                account_id=transaction.account_id,
                coverage_from=transaction.booked_on,
                coverage_through=transaction.booked_on,
                complete_through=transaction.booked_on,
                section_operation_count=1,
            )
        )
        session.flush()
        session.add(
            SourceRow(
                source_row_id=str(uuid4()),
                import_file_id=import_id,
                section_index=1,
                logical_row_idx=1,
                row_sha256=sha256(f"synthetic-row-{occurrence}".encode()).hexdigest(),
                identity_key_sha256=transaction.identity_key_sha256,
                transaction_id=transaction_id,
            )
        )


@pytest.mark.integration
def test_RULE_10_rules_are_versioned_and_ambiguous_payees_are_narrowed(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    try:
        _account_id, ids = _insert_transactions(engine, [-1200], payee="Vinted")
        with session_scope(engine) as session:
            payee = session.get(Transaction, ids[0])
            assert payee is not None and payee.payee_id is not None
            payee_id = payee.payee_id

        allocate(
            engine,
            ids[0],
            [AllocationPart(flow_kind="expense", category_id="clothing", amount_cents=-1200)],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        with session_scope(engine) as session:
            assert session.scalar(select(Rule.rule_id)) is None

        too_broad = _draft(scope="existing_and_future", payee_id=payee_id, pattern="")
        with pytest.raises(RuleStoreError) as failure:
            create_rule(engine, too_broad, actor="reviewer")
        assert failure.value.code == "AMBIGUOUS_MARKETPLACE_GLOBAL_SCOPE"

        constrained = RuleDraft(
            name="Vinted débiteur borné",
            condition=RuleCondition(
                payee_id=payee_id,
                label_pattern="*SYNTHETIC*",
                direction="debit",
                min_amount_cents=100,
            ),
            action=RuleAction(flow_kind="expense", category_id="clothing"),
            priority=12,
            application_scope="existing_and_future",
            ambiguous_reason="Validation ciblée des débits de ce compte.",
        )
        created = create_rule(engine, constrained, actor="reviewer")
        approved = approve_rule(
            engine, created.rule_id, expected_version=created.version, actor="reviewer"
        )
        assert created.status == "draft"
        assert approved.status == "approved"
        assert approved.version == created.version + 1
        assert approved.created_by == "reviewer"
        assert approved.approved_by == "reviewer"

        with pytest.raises(VersionConflictError):
            update_rule(
                engine,
                approved.rule_id,
                constrained,
                expected_version=created.version,
                actor="reviewer",
            )
        edited = update_rule(
            engine,
            approved.rule_id,
            RuleDraft(
                name="Vinted débit limité",
                condition=RuleCondition(
                    payee_id=payee_id,
                    label_pattern="*SYNTHETIC*",
                    direction="debit",
                    max_amount_cents=5000,
                ),
                action=constrained.action,
                priority=12,
                application_scope="existing_and_future",
                ambiguous_reason="Validation ciblée des débits de ce compte.",
            ),
            expected_version=approved.version,
            actor="reviewer",
        )
        assert edited.status == "draft"
        disabled = disable_rule(
            engine, edited.rule_id, expected_version=edited.version, actor="reviewer"
        )
        assert disabled.status == "disabled"
        assert get_rule(engine, disabled.rule_id).version == disabled.version
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_01_human_decision_precedes_a_later_approved_rule(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    try:
        _account_id, ids = _insert_transactions(engine, [-2400])
        manual = allocate(
            engine,
            ids[0],
            [AllocationPart(flow_kind="expense", category_id="clothing", amount_cents=-2400)],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        rule = _approved_rule(engine, _draft(scope="existing_and_future", category="groceries"))
        before = _snapshot_all_tables(engine)
        result = apply_rule(
            engine,
            rule.rule_id,
            scope="selection",
            selection=ids,
            expected_version=rule.version,
            actor="reviewer",
        )
        after = _snapshot_all_tables(engine)
        assert result.applied_ids == ()
        assert result.skipped_manual_ids == tuple(ids)
        assert get_allocation_state(engine, ids[0]).allocations == manual.allocations
        # The only first-call changes are the required skipped-execution and audit rows.
        assert before["allocations"] == after["allocations"]
        assert before["classification_decisions"] == after["classification_decisions"]
        assert get_allocation_state(engine, ids[0]).version == manual.version
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_01_human_clear_of_rule_allocation_stays_protected(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    try:
        _account_id, ids = _insert_transactions(engine, [-2_400])
        first_rule = _approved_rule(
            engine, _draft(scope="existing_and_future", category="groceries")
        )
        first_application = apply_rule(
            engine,
            first_rule.rule_id,
            scope="selection",
            selection=ids,
            expected_version=first_rule.version,
            actor="reviewer",
        )
        assert first_application.applied_ids == tuple(ids)

        cleared = clear_allocations(
            engine,
            ids[0],
            actor="reviewer",
            expected_version=get_allocation_state(engine, ids[0]).version,
        )
        assert cleared.state == "unclassified"
        disabled = disable_rule(
            engine,
            first_rule.rule_id,
            expected_version=first_rule.version,
            actor="reviewer",
        )
        next_rule = _approved_rule(
            engine, _draft(scope="existing_and_future", category="restaurants")
        )

        evaluation = evaluate_transaction(engine, ids[0])
        assert evaluation.human_decision_protected
        assert evaluation.winner is not None and evaluation.winner.source == "human"

        result = apply_rule(
            engine,
            next_rule.rule_id,
            scope="selection",
            selection=ids,
            expected_version=next_rule.version,
            actor="reviewer",
        )
        assert result.applied_ids == ()
        assert result.skipped_manual_ids == tuple(ids)
        assert get_allocation_state(engine, ids[0]).state == "unclassified"
        assert disabled.status == "disabled"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_02_unapproved_future_rule_has_no_propagation(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    try:
        _account_id, ids = _insert_transactions(engine, [-2100], payee="Amazon")
        with session_scope(engine) as session:
            transaction = session.get(Transaction, ids[0])
            assert transaction is not None and transaction.payee_id is not None
            payee_id = transaction.payee_id
        draft = RuleDraft(
            name="Marketplace future à revoir",
            condition=RuleCondition(
                payee_id=payee_id, label_pattern="*SYNTHETIC*", direction="debit"
            ),
            action=RuleAction(flow_kind="expense", category_id="groceries"),
            application_scope="future_only",
            ambiguous_reason="Motif de test, avec sens explicite.",
        )
        rule = create_rule(engine, draft, actor="reviewer")
        assert rule.status == "draft"
        before = _snapshot_all_tables(engine)
        evaluation = evaluate_transaction(engine, ids[0], context="future")
        assert evaluation.winner is None
        with pytest.raises(AppError) as failure:
            apply_rule(
                engine,
                rule.rule_id,
                scope="selection",
                selection=ids,
                expected_version=rule.version,
                actor="reviewer",
            )
        assert failure.value.code == "RULE_NOT_APPROVED"
        assert before == _snapshot_all_tables(engine)
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_03_dry_run_previews_140_rows_without_database_writes(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    try:
        _account_id, ids = _insert_transactions(engine, [-100 - index for index in range(140)])
        draft = _draft(scope="selection")
        before = _snapshot_all_tables(engine)
        simulation = simulate_rule(
            engine,
            draft,
            SimulationScope(transaction_ids=tuple(ids), page_size=100, max_transactions=200),
        )
        assert simulation.matched_count == 140
        assert len(simulation.rows) == 100
        assert simulation.next_offset == 100
        assert len({row.transaction_id for row in simulation.rows}) == 100
        assert simulation.totals_by_category["groceries"].transaction_count == 140
        assert simulation.totals_by_category["groceries"].amount_cents == sum(
            -100 - i for i in range(140)
        )
        assert before == _snapshot_all_tables(engine)
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_11_priority_conflicts_human_and_suggestion_order(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    try:
        _account_id, ids = _insert_transactions(engine, [-2500, -3100, -900])
        low_a = _approved_rule(
            engine, _draft(scope="existing_and_future", category="groceries", priority=50)
        )
        low_b = _approved_rule(
            engine, _draft(scope="existing_and_future", category="restaurants", priority=50)
        )
        conflict = evaluate_transaction(engine, ids[0])
        assert conflict.conflict_rule_ids == tuple(sorted((low_a.rule_id, low_b.rule_id)))
        assert conflict.winner is None

        manual = allocate(
            engine,
            ids[0],
            [AllocationPart(flow_kind="expense", category_id="clothing", amount_cents=-2500)],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        protected = evaluate_transaction(engine, ids[0])
        assert protected.human_decision_protected
        assert protected.winner is not None and protected.winner.source == "human"
        assert protected.winner.category_id == "clothing"
        assert manual.state == "complete"

        high = _approved_rule(
            engine, _draft(scope="existing_and_future", category="groceries", priority=5)
        )
        middle = _approved_rule(
            engine, _draft(scope="existing_and_future", category="restaurants", priority=10)
        )
        ordered = evaluate_transaction(engine, ids[1])
        assert ordered.winner is not None
        assert ordered.winner.rule_id == high.rule_id
        assert ordered.winner.rule_priority == 5
        assert middle.rule_id != high.rule_id

        equal_a = _approved_rule(
            engine,
            _draft(scope="existing_and_future", category="clothing", priority=0),
        )
        equal_b = _approved_rule(
            engine,
            _draft(scope="existing_and_future", category="clothing", priority=0),
        )
        equivalent = evaluate_transaction(engine, ids[1])
        assert equivalent.conflict_rule_ids == ()
        assert equivalent.winner is not None
        assert equivalent.winner.rule_id == min(equal_a.rule_id, equal_b.rule_id)

        with write_transaction(engine) as session:
            target = session.get(Transaction, ids[2])
            assert target is not None
            session.add_all(
                [
                    ClassificationSuggestion(
                        transaction_id=ids[2],
                        proposed_flow_kind="expense",
                        category_id="groceries",
                        suggested_amount_cents=-900,
                        method="heuristic",
                        confidence_label="doute",
                        reason="Fixture synthétique",
                    ),
                    ClassificationSuggestion(
                        transaction_id=ids[2],
                        proposed_flow_kind="expense",
                        category_id="restaurants",
                        suggested_amount_cents=-900,
                        method="llm",
                        confidence_label="doute",
                        reason="Fixture synthétique",
                    ),
                ]
            )
        suggestion_order = evaluate_transaction(engine, ids[2])
        sources = [candidate.source for candidate in suggestion_order.candidates]
        assert sources.index("heuristic") < sources.index("llm")
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_18_exact_memory_precedes_heuristic_and_llm(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    try:
        source_ids: list[str] = []
        with write_transaction(engine) as session:
            account = Account(alias="Compte mémoire synthétique", account_role="current")
            session.add(account)
            session.flush()
            for day in range(1, 5):
                label = f"Paiement par carte\nX1010 MARCHAND LUNE {day:02d}/04 - Marchand Lune"
                normalized = identity_label(label).value
                transaction = Transaction(
                    account_id=account.account_id,
                    booked_on=date(2026, 4, day),
                    amount_cents=-1_000,
                    raw_label=label,
                    identity_label=normalized,
                    identity_key_sha256=sha256(normalized.encode()).hexdigest(),
                    occurrence_rank=1,
                )
                session.add(transaction)
                session.flush()
                assert (
                    create_payee_for_transaction(session, transaction, actor="reviewer") is not None
                )
                source_ids.append(transaction.transaction_id)

        for transaction_id in source_ids[:3]:
            allocate(
                engine,
                transaction_id,
                [AllocationPart(flow_kind="expense", category_id="groceries", amount_cents=-1_000)],
                mode="complete",
                origin="manual",
                actor="reviewer",
                expected_version=get_allocation_state(engine, transaction_id).version,
            )
        with write_transaction(engine) as session:
            session.add_all(
                [
                    ClassificationSuggestion(
                        transaction_id=source_ids[3],
                        proposed_flow_kind="expense",
                        category_id="restaurants",
                        suggested_amount_cents=-1_000,
                        method="heuristic",
                        confidence_label="doute",
                        reason="Fixture synthétique",
                    ),
                    ClassificationSuggestion(
                        transaction_id=source_ids[3],
                        proposed_flow_kind="expense",
                        category_id="clothing",
                        suggested_amount_cents=-1_000,
                        method="llm",
                        confidence_label="doute",
                        reason="Fixture synthétique",
                    ),
                ]
            )
        result = evaluate_transaction(engine, source_ids[3])
        assert [candidate.source for candidate in result.candidates] == [
            "memory",
            "heuristic",
            "llm",
        ]
        assert result.winner is not None and result.winner.source == "memory"
        assert result.winner.category_id == "groceries"
        assert result.winner.source_transaction_ids == tuple(sorted(source_ids[:3]))
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_19_explicit_account_direction_amount_date_and_flow_conditions(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    try:
        account_id, ids = _insert_transactions(engine, [-1_250, 1_500])
        with write_transaction(engine) as session:
            session.add(
                ClassificationSuggestion(
                    transaction_id=ids[0],
                    proposed_flow_kind="expense",
                    category_id="groceries",
                    suggested_amount_cents=-1_250,
                    method="heuristic",
                    confidence_label="doute",
                    reason="Fixture synthétique",
                )
            )
        draft = RuleDraft(
            name="Débit synthétique précis",
            condition=RuleCondition(
                label_pattern="*SYNTHETIC*",
                account_ids=(account_id,),
                direction="debit",
                min_amount_cents=1_000,
                max_amount_cents=1_300,
                booked_from=date(2026, 2, 1),
                booked_through=date(2026, 2, 1),
                flow_kind="expense",
            ),
            action=RuleAction(flow_kind="expense", category_id="groceries"),
            application_scope="selection",
        )
        simulation = simulate_rule(engine, draft, SimulationScope(transaction_ids=tuple(ids)))
        assert tuple(row.transaction_id for row in simulation.rows) == (ids[0],)
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_20_label_only_marketplace_appears_ambiguous_in_simulation(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    try:
        _account_id, ids = _insert_transactions(engine, [-1_250], label="Order AMAZON synthetic")
        draft = RuleDraft(
            name="Marketplace par motif explicite",
            condition=RuleCondition(label_pattern="*AMAZON*", direction="debit"),
            action=RuleAction(flow_kind="expense", category_id="groceries"),
            application_scope="existing_and_future",
            ambiguous_reason="Motif marketplace limité au libellé synthétique.",
        )
        rule = _approved_rule(engine, draft)
        simulation = simulate_rule(engine, rule, SimulationScope(page_size=10))
        assert tuple(row.transaction_id for row in simulation.rows) == (ids[0],)
        assert simulation.ambiguous_transaction_ids == (ids[0],)
        assert simulation.rows[0].ambiguous
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_21_exception_count_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(tmp_path)
    try:
        monkeypatch.setattr(rule_store, "MAX_RULE_EXCEPTIONS", 1)
        _account_id, ids = _insert_transactions(engine, [-1_250, -1_500])
        rule = _approved_rule(engine, _draft(scope="existing_and_future"))
        excluded = exclude_transaction_from_rule(
            engine,
            rule.rule_id,
            ids[0],
            expected_version=rule.version,
            actor="reviewer",
            reason="Première exception.",
        )
        with pytest.raises(RuleStoreError) as failure:
            exclude_transaction_from_rule(
                engine,
                rule.rule_id,
                ids[1],
                expected_version=excluded.version,
                actor="reviewer",
                reason="Dépassement du plafond.",
            )
        assert failure.value.code == "RULE_EXCEPTION_LIMIT"
        unchanged = get_rule(engine, rule.rule_id)
        assert unchanged.version == excluded.version
        assert unchanged.exceptions == excluded.exceptions
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_12_simulation_is_read_only_with_totals_paging_and_ratio_rounding(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    try:
        _account_id, ids = _insert_transactions(engine, [-10_001, -101, -99])
        draft = _draft(
            scope="selection",
            ratios=(RatioPart("groceries", 7000), RatioPart("restaurants", 3000)),
        )
        before = _snapshot_all_tables(engine)
        first = simulate_rule(
            engine,
            draft,
            SimulationScope(transaction_ids=tuple(ids), page_size=2, max_transactions=10),
        )
        assert first.matched_count == 3
        assert len(first.rows) == 2
        assert first.next_offset == 2
        assert first.totals_by_category["groceries"].amount_cents == -7141
        assert first.totals_by_category["groceries"].amount_euros == "-71,41 €"
        assert first.totals_by_category["restaurants"].amount_cents == -3060
        assert first.totals_by_category["restaurants"].amount_euros == "-30,60 €"
        assert first.rows[0].amount_cents == -10_001
        assert first.rows[0].amount_euros == "-100,01 €"
        assert sum(part["amount_cents"] for part in first.rows[0].after) == -10_001
        second = simulate_rule(
            engine,
            draft,
            SimulationScope(transaction_ids=tuple(ids), offset=2, page_size=2, max_transactions=10),
        )
        assert len(second.rows) == 1
        after = _snapshot_all_tables(engine)
        assert before == after
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_13_apply_is_idempotent_and_preserves_manual_decisions_and_import_sources(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    try:
        _account_id, ids = _insert_transactions(engine, [-1700, -2200])
        rule = _approved_rule(engine, _draft(scope="selection"))
        manual = allocate(
            engine,
            ids[0],
            [AllocationPart(flow_kind="expense", category_id="clothing", amount_cents=-1700)],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        _append_synthetic_source(engine, ids[0], 1)
        result = apply_rule(
            engine,
            rule.rule_id,
            scope="selection",
            selection=ids,
            expected_version=rule.version,
            actor="reviewer",
        )
        assert result.applied_ids == (ids[1],)
        assert result.skipped_manual_ids == (ids[0],)
        assert get_allocation_state(engine, ids[0]).allocations == manual.allocations
        second_before = _snapshot_all_tables(engine)
        replay = apply_rule(
            engine,
            rule.rule_id,
            scope="selection",
            selection=ids,
            expected_version=rule.version,
            actor="reviewer",
        )
        assert replay.applied_ids == ()
        assert replay.skipped_manual_ids == (ids[0],)
        assert second_before == _snapshot_all_tables(engine)
        with session_scope(engine) as session:
            executions = list(
                session.scalars(select(RuleExecution).where(RuleExecution.rule_id == rule.rule_id))
            )
            assert {(item.transaction_id, item.outcome) for item in executions} == {
                (ids[0], "skipped_manual"),
                (ids[1], "applied"),
            }
            assert all(item.audit_event_id for item in executions)
            assert (
                session.scalar(
                    select(AuditEvent.audit_event_id).where(
                        AuditEvent.target_type == "transaction",
                        AuditEvent.target_id == ids[1],
                        AuditEvent.action == "rule.applied",
                    )
                )
                is not None
            )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_13_allocation_rolls_back_when_execution_trace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(tmp_path)
    try:
        _account_id, ids = _insert_transactions(engine, [-1_700])
        rule = _approved_rule(engine, _draft(scope="selection"))
        before = _snapshot_all_tables(engine)

        def fail_trace(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic rule trace failure")

        monkeypatch.setattr(rules_engine, "_trace_execution", fail_trace)
        with pytest.raises(RuntimeError, match="synthetic rule trace failure"):
            apply_rule(
                engine,
                rule.rule_id,
                scope="selection",
                selection=ids,
                expected_version=rule.version,
                actor="reviewer",
            )

        assert before == _snapshot_all_tables(engine)
        assert get_allocation_state(engine, ids[0]).state == "unclassified"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_13_stale_rule_does_not_write_manual_skip_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(tmp_path)
    try:
        _account_id, ids = _insert_transactions(engine, [-1_700])
        allocate(
            engine,
            ids[0],
            [AllocationPart(flow_kind="expense", category_id="clothing", amount_cents=-1_700)],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        rule = _approved_rule(engine, _draft(scope="existing_and_future"))
        simulate_all = rules_engine._simulate_all_pages

        def simulate_then_disable(db_engine, selected_rule, scope):
            result = simulate_all(db_engine, selected_rule, scope)
            disable_rule(
                db_engine,
                selected_rule.rule_id,
                expected_version=selected_rule.version,
                actor="reviewer",
            )
            return result

        monkeypatch.setattr(rules_engine, "_simulate_all_pages", simulate_then_disable)
        with pytest.raises(VersionConflictError):
            apply_rule(
                engine,
                rule.rule_id,
                scope="existing_and_future",
                expected_version=rule.version,
                actor="reviewer",
            )

        with session_scope(engine) as session:
            assert (
                session.scalar(
                    select(RuleExecution.rule_execution_id).where(
                        RuleExecution.rule_id == rule.rule_id,
                        RuleExecution.transaction_id == ids[0],
                    )
                )
                is None
            )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_14_exceptions_are_durable_and_never_apply(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    try:
        _account_id, ids = _insert_transactions(engine, [-1500, -1600])
        rule = _approved_rule(engine, _draft(scope="existing_and_future"))
        excluded = exclude_transaction_from_rule(
            engine,
            rule.rule_id,
            ids[0],
            expected_version=rule.version,
            actor="reviewer",
            reason="Exclure cette occurrence choisie.",
        )
        assert excluded.status == "approved"
        assert excluded.version == rule.version + 1
        assert excluded.exceptions[0].transaction_id == ids[0]
        assert excluded.exceptions[0].reason == "Exclure cette occurrence choisie."
        assert excluded.exceptions[0].actor == "reviewer"
        assert get_rule(engine, rule.rule_id).exceptions == excluded.exceptions
        changed = exclude_transaction_from_rule(
            engine,
            rule.rule_id,
            ids[0],
            expected_version=excluded.version,
            actor="second-reviewer",
            reason="Conserver le motif corrigé.",
        )
        assert changed.exceptions[0].actor == "second-reviewer"
        assert changed.exceptions[0].reason == "Conserver le motif corrigé."
        with session_scope(engine) as session:
            audit_rows = list(
                session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.action == "rule.transaction_excluded")
                    .order_by(AuditEvent.created_at_utc)
                )
            )
            assert len(audit_rows) == 2
            assert '"actor":"reviewer"' in (audit_rows[0].after_json or "")
            reason = "Exclure cette occurrence choisie."
            assert f'"reason_length":{len(reason)}' in (audit_rows[0].after_json or "")
            assert f'"reason_sha256":"{sha256(reason.encode()).hexdigest()}"' in (
                audit_rows[0].after_json or ""
            )
            assert reason not in (audit_rows[0].after_json or "")
            assert '"actor":"second-reviewer"' in (audit_rows[1].after_json or "")
            assert "Conserver le motif corrigé." not in (audit_rows[1].after_json or "")
        simulation = simulate_rule(engine, changed, SimulationScope(page_size=10))
        assert tuple(row.transaction_id for row in simulation.rows) == (ids[1],)
        result = apply_rule(
            engine,
            rule.rule_id,
            scope="existing_and_future",
            expected_version=changed.version,
            actor="reviewer",
        )
        assert result.applied_ids == (ids[1],)
        assert get_allocation_state(engine, ids[0]).state == "unclassified"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_15_patterns_are_literal_bounded_and_safe(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    try:
        assert label_pattern_matches("*(a+)+*", "Libellé (a+)+ synthétique")
        long_label = "x" * 20_000 + " FIN"
        started = time.perf_counter()
        assert label_pattern_matches("*x*x*x*FIN", long_label)
        assert time.perf_counter() - started < 1.0
        with pytest.raises(RuleStoreError) as failure:
            create_rule(engine, _draft(pattern="*a*b*c*d*e*"), actor="reviewer")
        assert failure.value.code == "RULE_PATTERN_TOO_COMPLEX"
        with pytest.raises(RuleStoreError) as failure:
            create_rule(
                engine,
                RuleDraft(
                    name="Règle avec float interdit",
                    condition=RuleCondition(label_pattern="SYNTHETIC", min_amount_cents=10.0),  # type: ignore[arg-type]
                    action=RuleAction(flow_kind="expense", category_id="groceries"),
                ),
                actor="reviewer",
            )
        assert failure.value.code == "RULE_INVALID"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_16_seeded_thousand_step_rule_decision_import_property(tmp_path: Path) -> None:
    engine = _database(tmp_path)
    try:
        _account_id, ids = _insert_transactions(engine, [-2000 - index for index in range(12)])
        rule = _approved_rule(engine, _draft(scope="selection"))
        rng = random.Random(19016)
        protected: dict[str, tuple[tuple[str, str, int], ...]] = {}
        import_sequence = 0

        for _step in range(1_000):
            transaction_id = rng.choice(ids)
            operation = rng.choice(("rule", "decision", "import"))
            current = get_allocation_state(engine, transaction_id)
            if operation == "decision":
                category_id = rng.choice(("restaurants", "clothing"))
                manual = allocate(
                    engine,
                    transaction_id,
                    [
                        AllocationPart(
                            flow_kind="expense",
                            category_id=category_id,
                            amount_cents=current.transaction_amount_cents,
                        )
                    ],
                    mode="complete",
                    origin="manual",
                    actor="reviewer",
                    expected_version=current.version,
                )
                protected[transaction_id] = tuple(
                    sorted(
                        (item.category_id, item.flow_kind, item.amount_cents)
                        for item in manual.allocations
                    )
                )
            else:
                if operation == "import":
                    import_sequence += 1
                    _append_synthetic_source(engine, transaction_id, import_sequence)
                apply_rule(
                    engine,
                    rule.rule_id,
                    scope="selection",
                    selection=(transaction_id,),
                    expected_version=rule.version,
                    actor="reviewer",
                )
                expected = protected.get(transaction_id)
                if expected is not None:
                    current_after = get_allocation_state(engine, transaction_id)
                    actual = tuple(
                        sorted(
                            (item.category_id, item.flow_kind, item.amount_cents)
                            for item in current_after.allocations
                        )
                    )
                    assert actual == expected
                    assert all(item.origin == "manual" for item in current_after.allocations)

            state = get_allocation_state(engine, transaction_id)
            assert (
                state.residual_cents + sum(item.amount_cents for item in state.allocations)
                == state.transaction_amount_cents
            )
        assert import_sequence > 0
    finally:
        engine.dispose()


@pytest.mark.integration
def test_RULE_17_ten_thousand_transaction_simulation_is_bounded_and_measured(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    try:
        _account_id, ids = _insert_transactions(
            engine,
            [-100] * MAX_SIMULATION_COUNT,
            label="Operation SYNTHETIC",
        )
        draft = _draft(scope="selection")
        started = time.perf_counter()
        simulation = simulate_rule(
            engine,
            draft,
            SimulationScope(
                transaction_ids=tuple(ids),
                page_size=1_000,
                max_transactions=MAX_SIMULATION_COUNT,
            ),
        )
        elapsed = time.perf_counter() - started
        assert simulation.scanned_count == MAX_SIMULATION_COUNT
        assert simulation.matched_count == MAX_SIMULATION_COUNT
        assert len(simulation.rows) == 1_000
        assert simulation.next_offset == 1_000
        assert simulation.totals_by_category["groceries"].transaction_count == MAX_SIMULATION_COUNT
        assert simulation.totals_by_category["groceries"].amount_cents == -1_000_000
        assert not simulation.truncated
        assert 0 < simulation.elapsed_ns < 30_000_000_000
        assert elapsed < 30
    finally:
        engine.dispose()


MAX_SIMULATION_COUNT = 10_000
