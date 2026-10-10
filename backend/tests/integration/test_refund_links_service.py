from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
import yaml
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.errors import AppError
from app.db.errors import VersionConflictError
from app.db.migrate import upgrade_to_head
from app.db.models import (
    Account,
    Allocation,
    AuditEvent,
    RefundLinkAllocation,
    Transaction,
    TransactionLink,
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
from app.services.refund_links import (
    confirm_refund_link,
    create_refund_link,
    get_refund_link_state,
    net_effect_by_category,
    reject_refund_link,
    replace_refund_link,
)

ROOT = Path(__file__).resolve().parents[3]
METRICS_EXAMPLES = ROOT / "docs" / "examples" / "metrics_examples.yaml"


def _in_period(item: dict[str, Any], period_start: date, period_end: date) -> bool:
    booked_on = date.fromisoformat(item["booked_on"])
    return period_start <= booked_on <= period_end


def _engine(tmp_path: Path, name: str = "refund-links.sqlite"):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / name}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    sync_taxonomy(engine, load_taxonomy())
    return engine


def _account(engine, alias: str, role: str = "current") -> str:
    with write_transaction(engine) as session:
        row = Account(alias=alias, account_role=role)
        session.add(row)
        session.flush()
        return row.account_id


def _transaction(
    engine,
    account_id: str,
    amount_cents: int,
    label: str,
    *,
    booked_on: date = date(2026, 2, 3),
) -> str:
    with write_transaction(engine) as session:
        row = Transaction(
            account_id=account_id,
            booked_on=booked_on,
            amount_cents=amount_cents,
            raw_label="Opération synthétique",
            identity_label=label,
            identity_key_sha256="c" * 64,
            occurrence_rank=1,
        )
        session.add(row)
        session.flush()
        return row.transaction_id


def _allocate(
    engine,
    transaction_id: str,
    amount_cents: int,
    flow_kind: str,
    category_id: str,
    *,
    expected_version: int = 1,
) -> dict[str, str]:
    state = allocate(
        engine,
        transaction_id,
        [AllocationPart(flow_kind, category_id, amount_cents)],
        mode="complete",
        origin="manual",
        actor="reviewer",
        expected_version=expected_version,
    )
    return {item.category_id: item.allocation_id for item in state.allocations}


@pytest.mark.integration
def test_S01_S12_yaml_oracle_is_replayed_through_allocation_and_refund_services(
    tmp_path: Path,
) -> None:
    document = yaml.safe_load(METRICS_EXAMPLES.read_text(encoding="utf-8"))
    example = next(item for item in document["examples"] if item["id"] == "S01_S12_golden")
    engine = _engine(tmp_path)
    try:
        account_id = _account(engine, "Oracle S01-S12 synthétique")
        transaction_ids: dict[str, str] = {}
        for transaction in example["transactions"]:
            transaction_id = _transaction(
                engine,
                account_id,
                transaction["amount_cents"],
                f"SYNTHETIC {transaction['id']}",
                booked_on=date.fromisoformat(transaction["booked_on"]),
            )
            transaction_ids[transaction["id"]] = transaction_id
            parts = [
                AllocationPart(
                    allocation["flow_kind"],
                    allocation["category_id"],
                    allocation["amount_cents"],
                )
                for allocation in transaction["allocations"]
            ]
            allocate(
                engine,
                transaction_id,
                parts,
                mode="complete",
                origin="manual",
                actor="yaml-oracle-replay",
                expected_version=1,
            )

        for link in example["transaction_links"]:
            proposal = create_refund_link(
                engine,
                transaction_ids[link["from_transaction"]],
                transaction_ids[link["to_transaction"]],
                link["amount_cents"],
                actor="yaml-oracle-replay",
            )
            if link["status"] == "confirmed":
                confirm_refund_link(
                    engine,
                    proposal.transaction_link_id,
                    actor="yaml-oracle-replay",
                    expected_version=proposal.version,
                )

        query = example["queries"][0]
        expected = query["expected"]
        effects = {
            transaction["id"]: net_effect_by_category(
                engine, transaction_ids[transaction["id"]], account_ids=[account_id]
            )
            for transaction in example["transactions"]
            if any(item["flow_kind"] == "expense" for item in transaction["allocations"])
        }
        actual_consumption = sum(
            sum(effect.by_category_cents.values()) for effect in effects.values()
        )
        assert actual_consumption == expected["consumption_net_cents"]
        assert (
            effects["S03"].by_category_cents[query["category_id"]] == expected["category_net_cents"]
        )
        assert (
            sum(effect.matched_refunds_cents for effect in effects.values())
            == expected["matched_refunds_cents"]
        )
        assert (
            sum(item["amount_cents"] for item in example["transactions"])
            == expected["bank_net_flow_cents"]
        )
        assert (
            sum(
                next(item for item in example["transactions"] if item["id"] == identifier)[
                    "amount_cents"
                ]
                for identifier in query["pair_transaction_ids"]
            )
            == expected["pair_bank_flow_cents"]
        )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_03_proposed_refund_does_not_count_and_confirmed_link_nets_exactly(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        account_id = _account(engine, "Compte synthétique")
        purchase_id = _transaction(engine, account_id, -10_000, "ACHAT SYNTHESE")
        refund_id = _transaction(engine, account_id, 2_000, "AVOIR SYNTHESE")
        _allocate(engine, purchase_id, -10_000, "expense", "groceries_supermarket")
        refund_allocations = _allocate(engine, refund_id, 2_000, "refund", "income_refund")

        proposed = create_refund_link(
            engine,
            refund_id,
            purchase_id,
            2_000,
            actor="reviewer",
            source_allocation_id=refund_allocations["income_refund"],
        )
        assert proposed.status == "proposed"
        before_confirmation = net_effect_by_category(engine, purchase_id)
        assert before_confirmation.by_category_cents == {"groceries_supermarket": 10_000}
        assert before_confirmation.matched_refunds_cents == 0

        confirmed = confirm_refund_link(
            engine,
            proposed.transaction_link_id,
            actor="reviewer",
            expected_version=proposed.version,
        )
        assert confirmed.status == "confirmed"
        effect = net_effect_by_category(engine, purchase_id)
        assert effect.by_category_cents == {"groceries_supermarket": 8_000}
        assert effect.matched_refunds_cents == 2_000
        with session_scope(engine) as session:
            bank_pair_flow = session.scalar(
                select(func.sum(Transaction.amount_cents)).where(
                    Transaction.transaction_id.in_([purchase_id, refund_id])
                )
            )
            transactions = list(
                session.scalars(
                    select(Transaction).where(
                        Transaction.transaction_id.in_([purchase_id, refund_id])
                    )
                )
            )
            assert bank_pair_flow == -8_000
            assert sorted(item.amount_cents for item in transactions) == [-10_000, 2_000]
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.target_type == "transaction_link")
                )
                == 2
            )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_11_prorated_and_targeted_refunds_share_allocation_capacity(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        account_id = _account(engine, "Compte synthétique")
        purchase_id = _transaction(engine, account_id, -20_000, "ACHAT MIXTE PRORATA")
        purchase = allocate(
            engine,
            purchase_id,
            [
                AllocationPart("expense", "groceries", -10_000),
                AllocationPart("expense", "restaurants", -10_000),
            ],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        allocation_by_category = {
            item.category_id: item.allocation_id for item in purchase.allocations
        }

        prorated_credit_id = _transaction(engine, account_id, 10_000, "AVOIR PRORATA")
        prorated_source = _allocate(engine, prorated_credit_id, 10_000, "refund", "income_refund")
        prorated = create_refund_link(
            engine,
            prorated_credit_id,
            purchase_id,
            10_000,
            actor="reviewer",
            source_allocation_id=prorated_source["income_refund"],
        )
        confirm_refund_link(
            engine,
            prorated.transaction_link_id,
            actor="reviewer",
            expected_version=prorated.version,
        )
        effect = net_effect_by_category(engine, purchase_id)
        assert effect.by_category_cents == {"groceries": 5_000, "restaurants": 5_000}
        assert effect.matched_refunds_cents == 10_000
        with session_scope(engine) as session:
            parts = list(
                session.scalars(
                    select(RefundLinkAllocation).where(
                        RefundLinkAllocation.transaction_link_id == prorated.transaction_link_id
                    )
                )
            )
            assert sum(item.amount_cents for item in parts) == 10_000
            assert {
                session.get(Allocation, item.allocation_id).category_id: item.amount_cents
                for item in parts
            } == {"groceries": 5_000, "restaurants": 5_000}

        targeted_credit_id = _transaction(engine, account_id, 8_000, "AVOIR CIBLE")
        targeted_source = _allocate(engine, targeted_credit_id, 8_000, "refund", "income_refund")
        with pytest.raises(AppError) as over_target:
            create_refund_link(
                engine,
                targeted_credit_id,
                purchase_id,
                8_000,
                actor="reviewer",
                source_allocation_id=targeted_source["income_refund"],
                target_allocation_id=allocation_by_category["groceries"],
            )
        assert over_target.value.status_code == 409
        assert over_target.value.code == "REFUND_TARGET_ALLOCATION_CAP_EXCEEDED"
        after_rejection = net_effect_by_category(engine, purchase_id)
        assert after_rejection.by_category_cents == {"groceries": 5_000, "restaurants": 5_000}
        assert all(value >= 0 for value in after_rejection.by_category_cents.values())
        assert sum(after_rejection.by_category_cents.values()) == 10_000
    finally:
        engine.dispose()


@pytest.mark.integration
@hypothesis_settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@given(
    capacities=st.lists(st.integers(min_value=1, max_value=12), min_size=2, max_size=2),
    requests=st.lists(
        st.tuples(
            st.booleans(),
            st.integers(min_value=0, max_value=1),
            st.integers(min_value=1, max_value=30),
        ),
        min_size=0,
        max_size=8,
    ),
)
def test_MONEY_11_accepted_link_sequences_never_make_category_net_negative(
    tmp_path: Path,
    capacities: list[int],
    requests: list[tuple[bool, int, int]],
) -> None:
    engine = _engine(tmp_path, f"property-{uuid4()}.sqlite")
    try:
        categories = ("groceries", "restaurants")
        account_id = _account(engine, f"Compte synthétique {uuid4()}")
        total_expense = sum(capacities)
        purchase_id = _transaction(engine, account_id, -total_expense, f"ACHAT {uuid4()}")
        purchase = allocate(
            engine,
            purchase_id,
            [
                AllocationPart("expense", category_id, -capacity)
                for category_id, capacity in zip(categories, capacities, strict=True)
            ],
            mode="complete",
            origin="manual",
            actor="property-test",
            expected_version=1,
        )
        allocation_by_category = {
            item.category_id: item.allocation_id for item in purchase.allocations
        }
        accepted_cents = 0
        for index, (targeted, target_index, amount) in enumerate(requests):
            credit_id = _transaction(
                engine, account_id, amount, f"AVOIR PROPRIETE {index} {uuid4()}"
            )
            source = _allocate(engine, credit_id, amount, "refund", "income_refund")
            try:
                proposal = create_refund_link(
                    engine,
                    credit_id,
                    purchase_id,
                    amount,
                    actor="property-test",
                    source_allocation_id=source["income_refund"],
                    target_allocation_id=(
                        allocation_by_category[categories[target_index]] if targeted else None
                    ),
                )
            except AppError:
                continue
            confirm_refund_link(
                engine,
                proposal.transaction_link_id,
                actor="property-test",
                expected_version=proposal.version,
            )
            accepted_cents += amount

        effect = net_effect_by_category(engine, purchase_id)
        assert all(amount >= 0 for amount in effect.by_category_cents.values())
        assert sum(effect.by_category_cents.values()) == total_expense - accepted_cents
        assert effect.matched_refunds_cents == accepted_cents
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_10_refund_source_and_target_allocation_caps_are_enforced(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        account_id = _account(engine, "Compte synthétique")
        purchase_id = _transaction(engine, account_id, -10_000, "ACHAT MIXTE")
        credit_id = _transaction(engine, account_id, 5_000, "CREDIT MIXTE")
        _allocate(engine, purchase_id, -10_000, "expense", "groceries")
        credit_allocations = allocate(
            engine,
            credit_id,
            [
                AllocationPart("refund", "income_refund", 2_000),
                AllocationPart("income_other", "income_extraordinary", 3_000),
            ],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        source_refund_id = next(
            item.allocation_id
            for item in credit_allocations.allocations
            if item.flow_kind == "refund"
        )
        with pytest.raises(AppError) as source_error:
            create_refund_link(
                engine,
                credit_id,
                purchase_id,
                5_000,
                actor="reviewer",
                source_allocation_id=source_refund_id,
            )
        assert source_error.value.code == "REFUND_SOURCE_CAP_EXCEEDED"

        target_expense_id = get_active_allocations(engine, purchase_id)[0]
        target_credit_id = _transaction(engine, account_id, 12_000, "CREDIT CIBLE")
        target_credit_allocations = _allocate(
            engine, target_credit_id, 12_000, "refund", "income_refund"
        )
        with pytest.raises(AppError) as target_error:
            create_refund_link(
                engine,
                target_credit_id,
                purchase_id,
                10_001,
                actor="reviewer",
                source_allocation_id=target_credit_allocations["income_refund"],
                target_allocation_id=target_expense_id,
            )
        assert target_error.value.code == "REFUND_TARGET_CAP_EXCEEDED"
    finally:
        engine.dispose()


def get_active_allocations(engine, transaction_id: str) -> list[str]:
    with session_scope(engine) as session:
        return list(
            session.scalars(
                select(Allocation.allocation_id).where(
                    Allocation.transaction_id == transaction_id,
                    Allocation.superseded_by_allocation_id.is_(None),
                    Allocation.retracted_at_utc.is_(None),
                )
            )
        )


@pytest.mark.integration
def test_MONEY_10_multi_category_prorata_matches_yaml_oracle_without_duplicates(
    tmp_path: Path,
) -> None:
    document = yaml.safe_load(METRICS_EXAMPLES.read_text(encoding="utf-8"))
    example = next(
        item
        for item in document["examples"]
        if item["id"] == "ADR_024_refund_multi_category_prorata"
    )
    engine = _engine(tmp_path)
    try:
        account_id = _account(engine, "Compte synthétique")
        transaction_ids: dict[str, str] = {}
        allocation_ids: dict[str, dict[str, str]] = {}
        for transaction in example["transactions"]:
            transaction_id = _transaction(
                engine,
                account_id,
                transaction["amount_cents"],
                f"SYNTHETIC {transaction['id']}",
                booked_on=date.fromisoformat(transaction["booked_on"]),
            )
            transaction_ids[transaction["id"]] = transaction_id
            parts = [
                AllocationPart(
                    allocation["flow_kind"],
                    allocation["category_id"],
                    allocation["amount_cents"],
                )
                for allocation in transaction["allocations"]
            ]
            state = allocate(
                engine,
                transaction_id,
                parts,
                mode="complete",
                origin="manual",
                actor="yaml-oracle-replay",
                expected_version=1,
            )
            allocation_ids[transaction["id"]] = {}
            for source in transaction["allocations"]:
                if "id" in source:
                    match = next(
                        item
                        for item in state.allocations
                        if item.flow_kind == source["flow_kind"]
                        and item.category_id == source["category_id"]
                        and item.amount_cents == source["amount_cents"]
                    )
                    allocation_ids[transaction["id"]][source["id"]] = match.allocation_id

        for link in example["transaction_links"]:
            proposed = create_refund_link(
                engine,
                transaction_ids[link["from_transaction"]],
                transaction_ids[link["to_transaction"]],
                link["amount_cents"],
                actor="yaml-oracle-replay",
                source_allocation_id=allocation_ids[link["from_transaction"]].get(
                    link.get("source_allocation_id", "")
                ),
                target_allocation_id=(
                    allocation_ids[link["to_transaction"]][link["target_allocation_id"]]
                    if link.get("target_allocation_id")
                    else None
                ),
            )
            if link["status"] == "confirmed":
                confirm_refund_link(
                    engine,
                    proposed.transaction_link_id,
                    actor="yaml-oracle-replay",
                    expected_version=proposed.version,
                )

        purchase_id = transaction_ids["multi_purchase"]
        effect = net_effect_by_category(engine, purchase_id)
        query = example["queries"][0]
        expected = query["expected"]
        assert effect.by_category_cents == query["expected_category_net_by_id_cents"]
        assert sum(effect.by_category_cents.values()) == expected["consumption_net_cents"]
        assert effect.matched_refunds_cents == expected["matched_refunds_cents"]
        assert effect.by_category_cents[query["category_id"]] == expected["category_net_cents"]
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_10_same_account_scope_proposed_rejected_and_confirmed_supersession(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        current_account = _account(engine, "Compte courant synthétique")
        other_account = _account(engine, "Compte séparé synthétique")
        purchase_id = _transaction(engine, current_account, -10_000, "ACHAT COURANT")
        refund_id = _transaction(engine, current_account, 2_000, "AVOIR COURANT")
        second_refund_id = _transaction(engine, current_account, 1_500, "AUTRE AVOIR")
        cross_purchase_id = _transaction(engine, other_account, -10_000, "ACHAT AUTRE COMPTE")
        _allocate(engine, purchase_id, -10_000, "expense", "groceries")
        _allocate(engine, refund_id, 2_000, "refund", "income_refund")
        _allocate(engine, second_refund_id, 1_500, "refund", "income_refund")
        _allocate(engine, cross_purchase_id, -10_000, "expense", "restaurants")

        with pytest.raises(AppError) as scope_error:
            create_refund_link(
                engine,
                refund_id,
                cross_purchase_id,
                1_000,
                actor="reviewer",
            )
        assert scope_error.value.code == "REFUND_SCOPE_MISMATCH"

        proposed = create_refund_link(
            engine,
            refund_id,
            purchase_id,
            1_000,
            actor="reviewer",
        )
        rejected = reject_refund_link(
            engine,
            proposed.transaction_link_id,
            actor="reviewer",
            expected_version=proposed.version,
        )
        assert rejected.status == "rejected"
        assert net_effect_by_category(engine, purchase_id).matched_refunds_cents == 0

        proposed2 = create_refund_link(
            engine,
            second_refund_id,
            purchase_id,
            1_500,
            actor="reviewer",
        )
        confirmed = confirm_refund_link(
            engine,
            proposed2.transaction_link_id,
            actor="reviewer",
            expected_version=proposed2.version,
        )
        with pytest.raises(AppError) as reject_confirmed:
            reject_refund_link(
                engine,
                confirmed.transaction_link_id,
                actor="reviewer",
                expected_version=confirmed.version,
            )
        assert reject_confirmed.value.code == "REFUND_LINK_CONFIRMED_PROTECTED"

        replacement = replace_refund_link(
            engine,
            confirmed.transaction_link_id,
            target_transaction_id=purchase_id,
            amount_cents=1_000,
            actor="reviewer",
            expected_version=confirmed.version,
            target_allocation_id=get_active_allocations(engine, purchase_id)[0],
        )
        assert replacement.status == "confirmed"
        repeated_replacement = replace_refund_link(
            engine,
            confirmed.transaction_link_id,
            target_transaction_id=purchase_id,
            amount_cents=1_000,
            actor="reviewer",
            expected_version=confirmed.version,
            target_allocation_id=get_active_allocations(engine, purchase_id)[0],
        )
        assert repeated_replacement.transaction_link_id == replacement.transaction_link_id
        active_effect = net_effect_by_category(engine, purchase_id)
        assert active_effect.by_category_cents == {"groceries": 9_000}
        with session_scope(engine) as session:
            old = session.get(TransactionLink, confirmed.transaction_link_id)
            events = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.target_type == "transaction_link",
                        AuditEvent.action == "refund_link.superseded",
                    )
                )
            )
            assert old is not None
            assert old.superseded_by_transaction_link_id == replacement.transaction_link_id
            assert old.version == confirmed.version + 1
            assert len(events) == 1 and events[0].before_json and events[0].after_json
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_12_clear_refuses_allocations_referenced_by_confirmed_refunds(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        account_id = _account(engine, "Compte synthétique")
        purchase_id = _transaction(engine, account_id, -10_000, "ACHAT AVEC AVOIR")
        credit_id = _transaction(engine, account_id, 2_000, "AVOIR CONFIRME")
        _allocate(engine, purchase_id, -10_000, "expense", "groceries")
        source = _allocate(engine, credit_id, 2_000, "refund", "income_refund")
        proposal = create_refund_link(
            engine,
            credit_id,
            purchase_id,
            2_000,
            actor="reviewer",
            source_allocation_id=source["income_refund"],
        )
        confirmed = confirm_refund_link(
            engine,
            proposal.transaction_link_id,
            actor="reviewer",
            expected_version=proposal.version,
        )
        with pytest.raises(AppError) as error:
            clear_allocations(engine, purchase_id, actor="reviewer", expected_version=2)
        assert error.value.status_code == 409
        assert error.value.code == "ALLOCATION_HAS_CONFIRMED_REFUND_LINK"
        with pytest.raises(AppError) as source_error:
            clear_allocations(engine, credit_id, actor="reviewer", expected_version=2)
        assert source_error.value.status_code == 409
        assert source_error.value.code == "ALLOCATION_HAS_CONFIRMED_REFUND_LINK"
        assert net_effect_by_category(engine, purchase_id).matched_refunds_cents == 2_000
        assert confirmed.status == "confirmed"
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("leg", "mutation"),
    [
        ("source", "reallocate"),
        ("target", "reallocate"),
        ("source", "clear"),
        ("target", "clear"),
    ],
)
def test_MONEY_16_allocation_change_stales_proposal_and_never_blocks_clear(
    tmp_path: Path, leg: str, mutation: str
) -> None:
    engine = _engine(tmp_path)
    try:
        account_id = _account(engine, "Compte synthétique")
        purchase_id = _transaction(engine, account_id, -10_000, "ACHAT AVEC PROPOSITION")
        credit_id = _transaction(engine, account_id, 2_000, "AVOIR PROPOSE")
        _allocate(engine, purchase_id, -10_000, "expense", "groceries")
        source = _allocate(engine, credit_id, 2_000, "refund", "income_refund")
        proposal = create_refund_link(
            engine,
            credit_id,
            purchase_id,
            2_000,
            actor="reviewer",
            source_allocation_id=source["income_refund"],
        )
        changed_transaction_id = credit_id if leg == "source" else purchase_id
        if mutation == "reallocate" and leg == "source":
            allocate(
                engine,
                credit_id,
                [AllocationPart("refund", "income_refund", 1_500)],
                mode="partial",
                origin="manual",
                actor="reviewer",
                expected_version=2,
            )
        elif mutation == "reallocate":
            allocate(
                engine,
                purchase_id,
                [
                    AllocationPart("expense", "groceries", -4_000),
                    AllocationPart("expense", "restaurants_dining", -6_000),
                ],
                mode="complete",
                origin="manual",
                actor="reviewer",
                expected_version=2,
            )
        else:
            clear_allocations(engine, changed_transaction_id, actor="reviewer", expected_version=2)

        stale = get_refund_link_state(engine, proposal.transaction_link_id)
        assert stale.status == "rejected"
        assert stale.version == proposal.version + 1
        repeated_rejection = reject_refund_link(
            engine,
            proposal.transaction_link_id,
            actor="reviewer",
            expected_version=stale.version,
        )
        assert repeated_rejection.status == "rejected"

        for transaction_id in (purchase_id, credit_id):
            state = get_allocation_state(engine, transaction_id)
            if state.allocations:
                clear_allocations(
                    engine,
                    transaction_id,
                    actor="reviewer",
                    expected_version=state.version,
                )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_17_public_service_grid_returns_typed_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    try:
        account_id = _account(engine, "Compte grille services")
        purchase_a = _transaction(engine, account_id, -1_000, "ACHAT GRILLE A")
        purchase_b = _transaction(engine, account_id, -1_000, "ACHAT GRILLE B")
        credit_id = _transaction(engine, account_id, 2_000, "AVOIR GRILLE")
        _allocate(engine, purchase_a, -1_000, "expense", "groceries")
        _allocate(engine, purchase_b, -1_000, "expense", "restaurants_dining")
        source = _allocate(engine, credit_id, 2_000, "refund", "income_refund")
        source_allocation_id = source["income_refund"]
        first = create_refund_link(
            engine, credit_id, purchase_a, 1_000, actor="reviewer",
            source_allocation_id=source_allocation_id,
        )
        first = confirm_refund_link(
            engine, first.transaction_link_id, actor="reviewer", expected_version=first.version
        )
        second = create_refund_link(
            engine, credit_id, purchase_b, 1_000, actor="reviewer",
            source_allocation_id=source_allocation_id,
        )
        confirm_refund_link(
            engine, second.transaction_link_id, actor="reviewer", expected_version=second.version
        )
        with pytest.raises(AppError) as active_duplicate:
            replace_refund_link(
                engine,
                first.transaction_link_id,
                target_transaction_id=purchase_b,
                amount_cents=500,
                actor="reviewer",
                expected_version=first.version,
            )
        assert active_duplicate.value.status_code == 409
        assert active_duplicate.value.code == "REFUND_LINK_ALREADY_EXISTS"

        missing = str(uuid4())
        invalid_calls = (
            ("TRANSACTION_NOT_FOUND", lambda: allocate(
                engine, missing, [AllocationPart("expense", "groceries", -1)],
                mode="complete", origin="manual", actor="reviewer", expected_version=1,
            )),
            ("TRANSACTION_NOT_FOUND", lambda: clear_allocations(
                engine, missing, actor="reviewer", expected_version=1
            )),
            ("TRANSACTION_NOT_FOUND", lambda: get_allocation_state(engine, missing)),
            ("TRANSACTION_NOT_FOUND", lambda: create_refund_link(
                engine, missing, purchase_a, 1, actor="reviewer"
            )),
            ("REFUND_LINK_NOT_FOUND", lambda: confirm_refund_link(
                engine, missing, actor="reviewer", expected_version=1
            )),
            ("REFUND_LINK_NOT_FOUND", lambda: reject_refund_link(
                engine, missing, actor="reviewer", expected_version=1
            )),
            ("REFUND_LINK_NOT_FOUND", lambda: replace_refund_link(
                engine, missing, target_transaction_id=purchase_a, amount_cents=1,
                actor="reviewer", expected_version=1,
            )),
            ("REFUND_LINK_NOT_FOUND", lambda: get_refund_link_state(engine, missing)),
            ("TRANSACTION_NOT_FOUND", lambda: net_effect_by_category(engine, missing)),
        )
        for expected_code, call in invalid_calls:
            with pytest.raises(AppError) as invalid:
                call()
            assert invalid.value.status_code in (404, 409, 422)
            assert invalid.value.code == expected_code

        import app.services.allocations as allocation_service
        import app.services.refund_links as refund_service

        @contextmanager
        def integrity_failure_context():
            raise IntegrityError(
                "synthetic SQLite constraint", {}, sqlite3.IntegrityError("constraint")
            )
            yield

        def fail_context(*_args: Any, **_kwargs: Any):
            return integrity_failure_context()

        monkeypatch.setattr(allocation_service, "write_transaction", fail_context)
        monkeypatch.setattr(allocation_service, "session_scope", fail_context)
        monkeypatch.setattr(refund_service, "write_transaction", fail_context)
        monkeypatch.setattr(refund_service, "session_scope", fail_context)
        integrity_calls = (
            ("ALLOCATION_CONFLICT", lambda: allocation_service.allocate(
                engine, purchase_a, [AllocationPart("expense", "groceries", -1)],
                mode="partial", origin="manual", actor="reviewer", expected_version=1,
            )),
            ("ALLOCATION_CONFLICT", lambda: allocation_service.clear_allocations(
                engine, purchase_a, actor="reviewer", expected_version=1
            )),
            ("ALLOCATION_CONFLICT", lambda: allocation_service.get_allocation_state(
                engine, purchase_a
            )),
            ("REFUND_LINK_CONFLICT", lambda: refund_service.create_refund_link(
                engine, credit_id, purchase_a, 1, actor="reviewer"
            )),
            ("REFUND_LINK_CONFLICT", lambda: refund_service.confirm_refund_link(
                engine, first.transaction_link_id, actor="reviewer", expected_version=1
            )),
            ("REFUND_LINK_CONFLICT", lambda: refund_service.reject_refund_link(
                engine, first.transaction_link_id, actor="reviewer", expected_version=1
            )),
            ("REFUND_LINK_CONFLICT", lambda: refund_service.replace_refund_link(
                engine, first.transaction_link_id, target_transaction_id=purchase_a,
                amount_cents=1, actor="reviewer", expected_version=1,
            )),
            ("REFUND_LINK_CONFLICT", lambda: refund_service.get_refund_link_state(
                engine, first.transaction_link_id
            )),
            ("REFUND_LINK_CONFLICT", lambda: refund_service.net_effect_by_category(
                engine, purchase_a
            )),
        )
        for expected_code, call in integrity_calls:
            with pytest.raises(AppError) as translated:
                call()
            assert translated.value.status_code == 409
            assert translated.value.code == expected_code
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_20_insufficient_remaining_prorata_capacity_is_refused_atomically(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        account_id = _account(engine, "Compte prorata capacité")
        purchase_id = _transaction(engine, account_id, -1_000, "ACHAT CAPACITE RESTANTE")
        allocate(
            engine,
            purchase_id,
            [
                AllocationPart("expense", "groceries", -500),
                AllocationPart("expense", "restaurants_dining", -500),
            ],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        first_credit_id = _transaction(engine, account_id, 800, "AVOIR CAPACITE PREMIER")
        first_source = _allocate(engine, first_credit_id, 800, "refund", "income_refund")
        first = create_refund_link(
            engine,
            first_credit_id,
            purchase_id,
            800,
            actor="reviewer",
            source_allocation_id=first_source["income_refund"],
        )
        confirm_refund_link(
            engine, first.transaction_link_id, actor="reviewer", expected_version=first.version
        )

        second_credit_id = _transaction(engine, account_id, 300, "AVOIR CAPACITE SECOND")
        second_source = _allocate(engine, second_credit_id, 300, "refund", "income_refund")
        with pytest.raises(AppError) as insufficient:
            create_refund_link(
                engine,
                second_credit_id,
                purchase_id,
                300,
                actor="reviewer",
                source_allocation_id=second_source["income_refund"],
            )
        assert insufficient.value.status_code == 409
        assert insufficient.value.code == "REFUND_PRORATA_INFEASIBLE"
        with session_scope(engine) as session:
            assert session.scalar(
                select(func.count()).select_from(TransactionLink).where(
                    TransactionLink.source_transaction_id == second_credit_id
                )
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(RefundLinkAllocation).join(
                    TransactionLink,
                    TransactionLink.transaction_link_id
                    == RefundLinkAllocation.transaction_link_id,
                ).where(TransactionLink.target_transaction_id == purchase_id)
            ) == 2
        effect = net_effect_by_category(engine, purchase_id)
        assert effect.matched_refunds_cents == 800
        assert effect.by_category_cents == {"groceries": 100, "restaurants_dining": 100}
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_13_raw_sql_trigger_rejects_cross_account_refund_link(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        source_account = _account(engine, "Compte source synthétique")
        target_account = _account(engine, "Compte cible synthétique")
        credit_id = _transaction(engine, source_account, 1_000, "AVOIR AUTRE COMPTE")
        purchase_id = _transaction(engine, target_account, -1_000, "ACHAT AUTRE COMPTE")
        source = _allocate(engine, credit_id, 1_000, "refund", "income_refund")
        _allocate(engine, purchase_id, -1_000, "expense", "groceries")
        with (
            pytest.raises(IntegrityError, match="refund_link_direction_invalid"),
            write_transaction(engine) as session,
        ):
            session.execute(
                text(
                    """INSERT INTO transaction_links
                           (transaction_link_id, source_transaction_id, target_transaction_id,
                            link_kind, amount_cents, source_allocation_id, status)
                           VALUES (:link_id, :source_id, :target_id, 'refund_of', 1000,
                                   :source_allocation_id, 'proposed')"""
                ),
                {
                    "link_id": str(uuid4()),
                    "source_id": credit_id,
                    "target_id": purchase_id,
                    "source_allocation_id": source["income_refund"],
                },
            )
    finally:
        engine.dispose()


def _kpi_multicategory_refund_fixture(tmp_path: Path, name: str):
    engine = _engine(tmp_path, name)
    account_id = _account(engine, "Compte synthétique")
    purchase_id = _transaction(engine, account_id, -10_000, "ACHAT KPI MULTI")
    allocate(
        engine,
        purchase_id,
        [
            AllocationPart("expense", "groceries", -7_000),
            AllocationPart("expense", "restaurants", -3_000),
        ],
        mode="complete",
        origin="manual",
        actor="reviewer",
        expected_version=1,
    )
    credit_id = _transaction(engine, account_id, 2_000, "AVOIR KPI MULTI")
    source = _allocate(engine, credit_id, 2_000, "refund", "income_refund")
    proposal = create_refund_link(
        engine,
        credit_id,
        purchase_id,
        2_000,
        actor="reviewer",
        source_allocation_id=source["income_refund"],
    )
    confirm_refund_link(
        engine,
        proposal.transaction_link_id,
        actor="reviewer",
        expected_version=proposal.version,
    )
    return engine, purchase_id


@pytest.mark.integration
def test_KPI_03_service_category_contributions_sum_to_multicategory_total(
    tmp_path: Path,
) -> None:
    engine, purchase_id = _kpi_multicategory_refund_fixture(tmp_path, "kpi-03.sqlite")
    try:
        effect = net_effect_by_category(engine, purchase_id)
        assert effect.by_category_cents == {"groceries": 5_600, "restaurants": 2_400}
        assert sum(effect.by_category_cents.values()) == 8_000
        assert effect.matched_refunds_cents == 2_000
    finally:
        engine.dispose()


@pytest.mark.integration
def test_KPI_07_service_refund_drilldown_parts_recompose_exact_total(
    tmp_path: Path,
) -> None:
    engine, purchase_id = _kpi_multicategory_refund_fixture(tmp_path, "kpi-07.sqlite")
    try:
        effect = net_effect_by_category(engine, purchase_id)
        with session_scope(engine) as session:
            link = session.scalar(
                select(TransactionLink).where(
                    TransactionLink.target_transaction_id == purchase_id,
                    TransactionLink.status == "confirmed",
                    TransactionLink.superseded_by_transaction_link_id.is_(None),
                )
            )
            assert link is not None
            parts = list(
                session.scalars(
                    select(RefundLinkAllocation).where(
                        RefundLinkAllocation.transaction_link_id == link.transaction_link_id
                    )
                )
            )
            assert sum(item.amount_cents for item in parts) == link.amount_cents
            assert {item.allocation_id for item in parts} == {
                allocation.allocation_id
                for allocation in session.scalars(
                    select(Allocation).where(
                        Allocation.transaction_id == purchase_id,
                        Allocation.flow_kind == "expense",
                    )
                )
            }
        assert sum(effect.by_category_cents.values()) == 8_000
        assert effect.matched_refunds_cents == 2_000
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_14_link_create_confirmation_retry_and_stale_versions(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        account_id = _account(engine, "Compte synthétique")
        purchase_id = _transaction(engine, account_id, -5_000, "ACHAT RETRY")
        credit_id = _transaction(engine, account_id, 2_000, "AVOIR RETRY")
        _allocate(engine, purchase_id, -5_000, "expense", "groceries")
        source = _allocate(engine, credit_id, 2_000, "refund", "income_refund")
        first = create_refund_link(
            engine,
            credit_id,
            purchase_id,
            1_000,
            actor="reviewer",
            source_allocation_id=source["income_refund"],
        )
        repeated_create = create_refund_link(
            engine,
            credit_id,
            purchase_id,
            1_000,
            actor="reviewer",
            source_allocation_id=source["income_refund"],
        )
        assert repeated_create.transaction_link_id == first.transaction_link_id
        confirmed = confirm_refund_link(
            engine,
            first.transaction_link_id,
            actor="reviewer",
            expected_version=first.version,
        )
        repeated_confirm = confirm_refund_link(
            engine,
            first.transaction_link_id,
            actor="reviewer",
            expected_version=first.version,
        )
        assert repeated_confirm.version == confirmed.version
        with pytest.raises(VersionConflictError):
            confirm_refund_link(
                engine,
                first.transaction_link_id,
                actor="reviewer",
                expected_version=0,
            )
        with session_scope(engine) as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.action == "refund_link.proposed")
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.action == "refund_link.confirmed")
                )
                == 1
            )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_14_replacement_preserves_source_allocation_and_stale_replay_identity(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        account_id = _account(engine, "Compte synthétique")
        purchase_id = _transaction(engine, account_id, -5_000, "ACHAT SOURCE REPLACEMENT")
        credit_id = _transaction(engine, account_id, 4_000, "AVOIR SOURCE REPLACEMENT")
        _allocate(engine, purchase_id, -5_000, "expense", "groceries")
        source = allocate(
            engine,
            credit_id,
            [
                AllocationPart("refund", "income_refund", 1_000),
                AllocationPart("refund", "income_refund", 3_000, beneficiary="adult_a"),
            ],
            mode="complete",
            origin="manual",
            actor="reviewer",
            expected_version=1,
        )
        changed_source_id = next(
            item.allocation_id for item in source.allocations if item.amount_cents == 1_000
        )
        stable_source_id = next(
            item.allocation_id for item in source.allocations if item.amount_cents == 3_000
        )
        proposal = create_refund_link(
            engine,
            credit_id,
            purchase_id,
            1_000,
            actor="reviewer",
            source_allocation_id=stable_source_id,
        )
        confirmed = confirm_refund_link(
            engine,
            proposal.transaction_link_id,
            actor="reviewer",
            expected_version=proposal.version,
        )
        with pytest.raises(AppError) as changed_source_error:
            replace_refund_link(
                engine,
                confirmed.transaction_link_id,
                target_transaction_id=purchase_id,
                amount_cents=1_500,
                actor="reviewer",
                expected_version=confirmed.version,
                source_allocation_id=changed_source_id,
            )
        assert changed_source_error.value.status_code == 409
        assert changed_source_error.value.code == "REFUND_SOURCE_ALLOCATION_IMMUTABLE"

        replacement = replace_refund_link(
            engine,
            confirmed.transaction_link_id,
            target_transaction_id=purchase_id,
            amount_cents=1_500,
            actor="reviewer",
            expected_version=confirmed.version,
        )
        assert replacement.source_allocation_id == stable_source_id
        repeated_replacement = replace_refund_link(
            engine,
            confirmed.transaction_link_id,
            target_transaction_id=purchase_id,
            amount_cents=1_500,
            actor="reviewer",
            expected_version=confirmed.version,
        )
        assert repeated_replacement.transaction_link_id == replacement.transaction_link_id

        with pytest.raises(VersionConflictError):
            replace_refund_link(
                engine,
                confirmed.transaction_link_id,
                target_transaction_id=purchase_id,
                amount_cents=1_500,
                actor="reviewer",
                expected_version=confirmed.version,
                source_allocation_id=changed_source_id,
            )

        with session_scope(engine) as session:
            active = list(
                session.scalars(
                    select(TransactionLink).where(
                        TransactionLink.source_transaction_id == credit_id,
                        TransactionLink.status == "confirmed",
                        TransactionLink.superseded_by_transaction_link_id.is_(None),
                    )
                )
            )
            assert len(active) == 1
            assert active[0].transaction_link_id == replacement.transaction_link_id
            assert active[0].source_allocation_id == stable_source_id
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.action == "refund_link.superseded")
                )
                == 1
            )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_14_concurrent_confirmation_is_one_audited_transition(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        account_id = _account(engine, "Compte synthétique")
        purchase_id = _transaction(engine, account_id, -5_000, "ACHAT CONFIRMATION CONCURRENTE")
        credit_id = _transaction(engine, account_id, 2_000, "AVOIR CONFIRMATION CONCURRENTE")
        _allocate(engine, purchase_id, -5_000, "expense", "groceries")
        source = _allocate(engine, credit_id, 2_000, "refund", "income_refund")
        proposal = create_refund_link(
            engine,
            credit_id,
            purchase_id,
            1_000,
            actor="reviewer",
            source_allocation_id=source["income_refund"],
        )
        barrier = Barrier(2)

        def confirm_once() -> str:
            barrier.wait(timeout=5)
            result = confirm_refund_link(
                engine,
                proposal.transaction_link_id,
                actor="reviewer",
                expected_version=proposal.version,
            )
            return result.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: confirm_once(), range(2)))
        assert results == ["confirmed", "confirmed"]
        effect = net_effect_by_category(engine, purchase_id)
        assert effect.by_category_cents == {"groceries": 4_000}
        with session_scope(engine) as session:
            link = session.get(TransactionLink, proposal.transaction_link_id)
            assert link is not None and link.version == proposal.version + 1
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.action == "refund_link.confirmed")
                )
                == 1
            )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_14_concurrent_replacements_serialize_on_link_version(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        account_id = _account(engine, "Compte synthétique")
        purchase_id = _transaction(engine, account_id, -5_000, "ACHAT REMPLACEMENT CONCURRENT")
        credit_id = _transaction(engine, account_id, 2_000, "AVOIR REMPLACEMENT CONCURRENT")
        purchase_allocations = _allocate(engine, purchase_id, -5_000, "expense", "groceries")
        source = _allocate(engine, credit_id, 2_000, "refund", "income_refund")
        proposal = create_refund_link(
            engine,
            credit_id,
            purchase_id,
            1_000,
            actor="reviewer",
            source_allocation_id=source["income_refund"],
        )
        confirmed = confirm_refund_link(
            engine,
            proposal.transaction_link_id,
            actor="reviewer",
            expected_version=proposal.version,
        )
        barrier = Barrier(2)

        def replace_once(amount: int) -> str:
            barrier.wait(timeout=5)
            try:
                replace_refund_link(
                    engine,
                    confirmed.transaction_link_id,
                    target_transaction_id=purchase_id,
                    amount_cents=amount,
                    actor="reviewer",
                    expected_version=confirmed.version,
                    target_allocation_id=purchase_allocations["groceries"],
                )
            except VersionConflictError:
                return "conflict"
            return "committed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(replace_once, (1_200, 1_300)))
        assert sorted(results) == ["committed", "conflict"]
        effect = net_effect_by_category(engine, purchase_id)
        assert effect.by_category_cents["groceries"] in (3_700, 3_800)
        assert effect.matched_refunds_cents in (1_200, 1_300)
        with pytest.raises(VersionConflictError):
            replace_refund_link(
                engine,
                confirmed.transaction_link_id,
                target_transaction_id=purchase_id,
                amount_cents=1_400,
                actor="reviewer",
                expected_version=confirmed.version,
                target_allocation_id=purchase_allocations["groceries"],
            )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_10_explicit_account_scope_ignores_link_effect_when_one_leg_is_missing(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        account_id = _account(engine, "Compte synthétique")
        purchase_id = _transaction(engine, account_id, -10_000, "ACHAT SCOPE")
        refund_id = _transaction(engine, account_id, 2_000, "AVOIR SCOPE")
        _allocate(engine, purchase_id, -10_000, "expense", "groceries")
        _allocate(engine, refund_id, 2_000, "refund", "income_refund")
        proposal = create_refund_link(engine, refund_id, purchase_id, 2_000, actor="reviewer")
        confirm_refund_link(
            engine,
            proposal.transaction_link_id,
            actor="reviewer",
            expected_version=proposal.version,
        )
        scoped = net_effect_by_category(engine, purchase_id, account_ids=[])
        assert scoped.by_category_cents == {"groceries": 10_000}
        assert scoped.matched_refunds_cents == 0
        assert scoped.ignored_out_of_scope_refunds_cents == 2_000
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_10_all_yaml_split_vectors_match_service_results(tmp_path: Path) -> None:
    from app.domain.money_split import MoneySplitError, split_by_basis_points

    document = yaml.safe_load(METRICS_EXAMPLES.read_text(encoding="utf-8"))
    for example in document["examples"]:
        for split in example.get("splits", []):
            try:
                actual = split_by_basis_points(
                    split["amount_cents"],
                    list(zip(split["category_ids"], split["percentages_bps"], strict=True)),
                )
            except MoneySplitError:
                actual = {}
            expected_valid = split["expected_valid"]
            assert bool(actual) == expected_valid, f"{example['id']} split validity mismatch"
            expected_ids = split.get("expected_category_ids")
            if expected_ids is not None:
                assert list(actual) == expected_ids
            assert list(actual.values()) == split["expected_allocations_cents"]
            if "expected_by_category_cents" in split:
                assert actual == split["expected_by_category_cents"]
    engine = _engine(tmp_path)
    try:
        example = next(
            item
            for item in document["examples"]
            if item["id"] == "MONEY_03_refund_partial_and_unmatched"
        )
        account_id = _account(engine, "Oracle MONEY-03 synthétique")
        transaction_ids: dict[str, str] = {}
        for transaction in example["transactions"]:
            transaction_id = _transaction(
                engine,
                account_id,
                transaction["amount_cents"],
                f"SYNTHETIC {transaction['id']}",
                booked_on=date.fromisoformat(transaction["booked_on"]),
            )
            transaction_ids[transaction["id"]] = transaction_id
            allocations = transaction["allocations"]
            if not allocations:
                continue
            allocate(
                engine,
                transaction_id,
                [
                    AllocationPart(item["flow_kind"], item["category_id"], item["amount_cents"])
                    for item in allocations
                ],
                mode="complete",
                origin="manual",
                actor="yaml-oracle-replay",
                expected_version=1,
            )
        for link in example["transaction_links"]:
            proposed = create_refund_link(
                engine,
                transaction_ids[link["from_transaction"]],
                transaction_ids[link["to_transaction"]],
                link["amount_cents"],
                actor="yaml-oracle-replay",
            )
            if link["status"] == "confirmed":
                confirm_refund_link(
                    engine,
                    proposed.transaction_link_id,
                    actor="yaml-oracle-replay",
                    expected_version=proposed.version,
                )

        effect = net_effect_by_category(engine, transaction_ids["purchase"])
        expected = example["queries"][0]["expected"]
        assert effect.by_category_cents == {"groceries_supermarket": expected["category_net_cents"]}
        assert sum(effect.by_category_cents.values()) == expected["consumption_net_cents"]
        assert effect.matched_refunds_cents == expected["matched_refunds_cents"]
        assert expected["bank_net_flow_cents"] == sum(
            item["amount_cents"] for item in example["transactions"]
        )
        refund_total = sum(
            allocation["amount_cents"]
            for transaction in example["transactions"]
            for allocation in transaction["allocations"]
            if allocation["flow_kind"] == "refund"
        )
        assert refund_total - effect.matched_refunds_cents == expected["unmatched_refunds_cents"]
    finally:
        engine.dispose()


@pytest.mark.integration
def test_MONEY_10_yaml_refund_examples_replay_through_services_and_match_metric_oracles(
    tmp_path: Path,
) -> None:
    document = yaml.safe_load(METRICS_EXAMPLES.read_text(encoding="utf-8"))
    engine = _engine(tmp_path)
    try:
        for example in document["examples"]:
            if not example.get("transaction_links"):
                continue
            example_id = example["id"]
            account_ids = {
                account_key: _account(
                    engine,
                    f"{example_id} {account_key}",
                    metadata["role"],
                )
                for account_key, metadata in example["accounts"].items()
            }
            transaction_ids: dict[str, str] = {}
            allocations_by_id: dict[str, dict[str, str]] = {}
            for transaction in example["transactions"]:
                transaction_id = _transaction(
                    engine,
                    account_ids[transaction["account_id"]],
                    transaction["amount_cents"],
                    f"SYNTHETIC {example_id} {transaction['id']}",
                    booked_on=date.fromisoformat(transaction["booked_on"]),
                )
                transaction_ids[transaction["id"]] = transaction_id
                yaml_allocations = transaction.get("allocations", [])
                if not yaml_allocations:
                    allocations_by_id[transaction["id"]] = {}
                    continue
                origins = {item.get("origin", "manual") for item in yaml_allocations}
                assert len(origins) == 1
                amount_total = sum(item["amount_cents"] for item in yaml_allocations)
                state = allocate(
                    engine,
                    transaction_id,
                    [
                        AllocationPart(
                            item["flow_kind"],
                            item["category_id"],
                            item["amount_cents"],
                            item.get("beneficiary", "unknown"),
                            item.get("cadence", "unknown"),
                            item.get("flexibility", "unknown"),
                            item.get("acquisition", "unknown"),
                        )
                        for item in yaml_allocations
                    ],
                    mode="complete" if amount_total == transaction["amount_cents"] else "partial",
                    origin=next(iter(origins)),
                    actor="yaml-oracle-replay",
                    expected_version=1,
                )
                allocations_by_id[transaction["id"]] = {}
                for yaml_allocation in yaml_allocations:
                    if "id" in yaml_allocation:
                        active = next(
                            item
                            for item in state.allocations
                            if item.flow_kind == yaml_allocation["flow_kind"]
                            and item.category_id == yaml_allocation["category_id"]
                            and item.amount_cents == yaml_allocation["amount_cents"]
                        )
                        allocations_by_id[transaction["id"]][yaml_allocation["id"]] = (
                            active.allocation_id
                        )

            confirmed: list[tuple[dict[str, object], str]] = []
            refused_scope_links: list[dict[str, object]] = []
            for link in example["transaction_links"]:
                source_yaml_id = link["from_transaction"]
                target_yaml_id = link["to_transaction"]
                source_allocation_key = link.get("source_allocation_id")
                target_allocation_key = link.get("target_allocation_id")
                try:
                    proposal = create_refund_link(
                        engine,
                        transaction_ids[source_yaml_id],
                        transaction_ids[target_yaml_id],
                        link["amount_cents"],
                        actor="yaml-oracle-replay",
                        source_allocation_id=(
                            allocations_by_id[source_yaml_id].get(source_allocation_key)
                            if source_allocation_key
                            else None
                        ),
                        target_allocation_id=(
                            allocations_by_id[target_yaml_id][target_allocation_key]
                            if target_allocation_key
                            else None
                        ),
                    )
                except AppError as error:
                    assert error.code == "REFUND_SCOPE_MISMATCH"
                    refused_scope_links.append(link)
                    continue
                if link["status"] == "confirmed":
                    confirmed_link = confirm_refund_link(
                        engine,
                        proposal.transaction_link_id,
                        actor="yaml-oracle-replay",
                        expected_version=proposal.version,
                    )
                    confirmed.append((link, confirmed_link.transaction_link_id))

            for invalid_case in example.get("invalid_link_cases", []):
                invalid_link = invalid_case["link"]
                with pytest.raises(AppError):
                    create_refund_link(
                        engine,
                        transaction_ids[invalid_link["from_transaction"]],
                        transaction_ids[invalid_link["to_transaction"]],
                        invalid_link["amount_cents"],
                        actor="yaml-oracle-replay",
                        source_allocation_id=(
                            allocations_by_id[invalid_link["from_transaction"]].get(
                                invalid_link.get("source_allocation_id", "")
                            )
                            if invalid_link.get("source_allocation_id")
                            else None
                        ),
                        target_allocation_id=(
                            allocations_by_id[invalid_link["to_transaction"]][
                                invalid_link["target_allocation_id"]
                            ]
                            if invalid_link.get("target_allocation_id")
                            else None
                        ),
                    )

            by_yaml_id = {item["id"]: item for item in example["transactions"]}
            for query in example.get("queries", []):
                expected = query.get("expected", {})
                period_start = date.fromisoformat(example["period"]["start"])
                period_end = date.fromisoformat(example["period"]["end"])
                selected_yaml_accounts = set(query.get("accounts", []))
                matched_cents = sum(
                    link["amount_cents"]
                    for link, _link_id in confirmed
                    if link["from_transaction"] in by_yaml_id
                    and by_yaml_id[link["from_transaction"]]["account_id"] in selected_yaml_accounts
                    and by_yaml_id[link["to_transaction"]]["account_id"] in selected_yaml_accounts
                    and _in_period(by_yaml_id[link["from_transaction"]], period_start, period_end)
                    and (
                        query.get("personal_mode", "envelope") == "observed"
                        or example["accounts"][by_yaml_id[link["to_transaction"]]["account_id"]][
                            "role"
                        ]
                        != "personal"
                    )
                )
                gross_expense_cents = sum(
                    -allocation["amount_cents"]
                    for transaction in example["transactions"]
                    if transaction["account_id"] in selected_yaml_accounts
                    and _in_period(transaction, period_start, period_end)
                    and (
                        query.get("personal_mode", "envelope") == "observed"
                        or example["accounts"][transaction["account_id"]]["role"] != "personal"
                    )
                    for allocation in transaction.get("allocations", [])
                    if allocation["flow_kind"] == "expense"
                )
                if (
                    "consumption_net_cents" in expected
                    and expected["consumption_net_cents"] is not None
                ):
                    assert (
                        gross_expense_cents - matched_cents == expected["consumption_net_cents"]
                    ), example_id
                if "matched_refunds_cents" in expected:
                    assert matched_cents == expected["matched_refunds_cents"], example_id
                refund_total = sum(
                    allocation["amount_cents"]
                    for transaction in example["transactions"]
                    if transaction["account_id"] in selected_yaml_accounts
                    and _in_period(transaction, period_start, period_end)
                    for allocation in transaction.get("allocations", [])
                    if allocation["flow_kind"] == "refund"
                )
                if "unmatched_refunds_cents" in expected:
                    assert refund_total - matched_cents == expected["unmatched_refunds_cents"], (
                        example_id
                    )
                if "unattributed_out_of_scope_refunds_cents" in expected:
                    out_of_scope = sum(
                        link["amount_cents"]
                        for link in refused_scope_links
                        if by_yaml_id[link["from_transaction"]]["account_id"]
                        in selected_yaml_accounts
                        and _in_period(
                            by_yaml_id[link["from_transaction"]], period_start, period_end
                        )
                    )
                    assert out_of_scope == expected["unattributed_out_of_scope_refunds_cents"], (
                        example_id
                    )
                if "pair_bank_flow_cents" in expected:
                    pair_ids = query.get("pair_transaction_ids", [])
                    actual_pair_flow = sum(
                        by_yaml_id[transaction_id]["amount_cents"] for transaction_id in pair_ids
                    )
                    assert actual_pair_flow == expected["pair_bank_flow_cents"]

                if (
                    "bank_net_flow_cents" in expected
                    and expected["bank_net_flow_cents"] is not None
                ):
                    actual_bank_flow = sum(
                        transaction["amount_cents"]
                        for transaction in example["transactions"]
                        if transaction["account_id"] in selected_yaml_accounts
                        and _in_period(transaction, period_start, period_end)
                    )
                    assert actual_bank_flow == expected["bank_net_flow_cents"], example_id
    finally:
        engine.dispose()
