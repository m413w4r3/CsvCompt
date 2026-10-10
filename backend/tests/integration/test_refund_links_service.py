from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import AppError
from app.db.migrate import upgrade_to_head
from app.db.models import Account, Allocation, AuditEvent, Transaction, TransactionLink
from app.db.session import create_sqlite_engine, session_scope, write_transaction
from app.domain.taxonomy import load_taxonomy
from app.services.allocations import AllocationPart, allocate
from app.services.categories import sync_taxonomy
from app.services.refund_links import (
    confirm_refund_link,
    create_refund_link,
    net_effect_by_category,
    reject_refund_link,
    replace_refund_link,
)

ROOT = Path(__file__).resolve().parents[3]
METRICS_EXAMPLES = ROOT / "docs" / "examples" / "metrics_examples.yaml"


def _in_period(item: dict[str, Any], period_start: date, period_end: date) -> bool:
    booked_on = date.fromisoformat(item["booked_on"])
    return period_start <= booked_on <= period_end


def _engine(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'refund-links.sqlite'}",
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
