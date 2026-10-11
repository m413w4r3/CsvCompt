from __future__ import annotations

import hashlib
import json
import time
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.db.models import Account, Allocation, Payee, Rule, Transaction
from app.db.session import create_sqlite_engine, session_scope, write_transaction
from app.domain.taxonomy import load_taxonomy
from app.importers.normalize import identity_label
from app.main import create_app
from app.services.allocations import AllocationPart, allocate
from app.services.app_secrets import ensure_hmac_key, read_hmac_key
from app.services.categories import sync_taxonomy
from app.services.idempotency import verify_cursor


def _ready(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'review-api.sqlite'}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    sync_taxonomy(engine, load_taxonomy())
    ensure_hmac_key(engine)
    engine.dispose()
    return TestClient(create_app(settings))


def _seed(client: TestClient, amounts: tuple[int, ...] = (-1234, -1234)) -> tuple[str, ...]:
    with write_transaction(client.app.state.db_engine) as session:
        account = Account(alias="Compte API synthétique", account_role="current")
        payee = Payee(
            display_name="Marchand synthétique",
            normalized_name=f'{{"name":"merchant-{uuid4()}"}}',
        )
        session.add_all((account, payee))
        session.flush()
        rows: list[Transaction] = []
        for index, amount in enumerate(amounts):
            label = f"API-REVIEW-SYNTHETIC-{index}"
            normalized = identity_label(label).value
            rows.append(
                Transaction(
                    account_id=account.account_id,
                    payee_id=payee.payee_id,
                    booked_on=date(2026, 5, index + 1),
                    amount_cents=amount,
                    raw_label=label,
                    identity_label=normalized,
                    identity_key_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
                    occurrence_rank=1,
                )
            )
        session.add_all(rows)
        session.flush()
        return tuple(row.transaction_id for row in rows)


def _seed_many(client: TestClient, count: int) -> tuple[str, ...]:
    with write_transaction(client.app.state.db_engine) as session:
        account = Account(alias=f"Compte test {count}", account_role="current")
        payee = Payee(
            display_name="Marchand de charge",
            normalized_name=json.dumps({"name": f"merchant-{uuid4()}"}, separators=(",", ":")),
        )
        session.add_all((account, payee))
        session.flush()
        rows = []
        for index in range(count):
            label = f"REVIEW-PROPERTY-{index}"
            normalized = identity_label(label).value
            rows.append(
                Transaction(
                    account_id=account.account_id,
                    payee_id=payee.payee_id,
                    booked_on=date(2026, 1 + (index % 12), 1),
                    amount_cents=-(index + 1),
                    raw_label=label,
                    identity_label=normalized,
                    identity_key_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
                    occurrence_rank=1,
                )
            )
        session.add_all(rows)
        session.flush()
        return tuple(row.transaction_id for row in rows)


def _state(client: TestClient, transaction_id: str) -> tuple[int, tuple[tuple[str, int, str], ...]]:
    with session_scope(client.app.state.db_engine) as session:
        transaction = session.get(Transaction, transaction_id)
        assert transaction is not None
        rows = session.scalars(
            select(Allocation).where(
                Allocation.transaction_id == transaction_id,
                Allocation.superseded_by_allocation_id.is_(None),
                Allocation.retracted_at_utc.is_(None),
            )
        ).all()
        return transaction.version, tuple(
            sorted((row.category_id, row.amount_cents, row.origin) for row in rows)
        )


@pytest.mark.integration
def test_REVIEW_01_queue_groups_filters_and_no_store(tmp_path: Path) -> None:
    client = _ready(tmp_path)
    try:
        _seed(client)
        response = client.get("/api/v1/review/queue", params={"direction": "debit", "limit": 10})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        groups = response.json()["items"]
        assert len(groups) == 1
        assert groups[0]["transaction_count"] == 2
        assert groups[0]["amount_cents"] == -2468
        assert groups[0]["impact_cents"] == 2468
        assert "transaction_ids" not in groups[0]
        assert (
            client.get("/api/v1/review/queue", params={"direction": "credit"}).json()["items"] == []
        )
        with session_scope(client.app.state.db_engine) as session:
            account_id = session.scalar(select(Account.account_id))
        filtered = client.get(
            "/api/v1/review/queue",
            params={
                "account_id": account_id,
                "period_start": "2026-05-02",
                "period_end": "2026-05-02",
                "status": "unclassified",
            },
        )
        assert filtered.status_code == 200
        assert len(filtered.json()["items"]) == 1
        assert filtered.json()["items"][0]["transaction_count"] == 1
    finally:
        client.close()


@pytest.mark.integration
def test_REVIEW_02_cursor_paginates_stably_and_is_bound_to_filters(tmp_path: Path) -> None:
    client = _ready(tmp_path)
    try:
        # Each seed call creates an independent counterparty group.
        _seed(client, (-100,))
        _seed(client, (-100,))
        _seed(client, (-100,))
        first = client.get("/api/v1/review/queue", params={"limit": 1})
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert first.json()["page"]["next_cursor"]
        collected = [first.json()["items"][0]["review_group_id"]]
        cursor = first.json()["page"]["next_cursor"]
        while cursor:
            page = client.get("/api/v1/review/queue", params={"limit": 1, "cursor": cursor})
            assert page.status_code == 200, page.text
            collected.extend(item["review_group_id"] for item in page.json()["items"])
            cursor = page.json()["page"]["next_cursor"]
        assert len(collected) == len(set(collected)) == 3

        changed_filters = client.get(
            "/api/v1/review/queue",
            params={"limit": 1, "status": "human", "cursor": first.json()["page"]["next_cursor"]},
        )
        assert changed_filters.status_code == 422
        assert changed_filters.json()["error"]["code"] == "CURSOR_INVALID"

        # Expire the pinned snapshot explicitly and verify the stable stale code.
        with session_scope(client.app.state.db_engine) as session:
            key = read_hmac_key(session)
        assert key is not None
        decoded = verify_cursor(
            first.json()["page"]["next_cursor"],
            key,
            {
                "period_start": None,
                "period_end": None,
                "account_id": None,
                "direction": None,
                "status": None,
                "limit": 1,
            },
        )
        store = client.app.state.transaction_snapshot_store
        store.discard(decoded["snapshot_id"])
        stale = client.get(
            "/api/v1/review/queue",
            params={"limit": 1, "cursor": first.json()["page"]["next_cursor"]},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "CURSOR_STALE"
    finally:
        client.close()


@pytest.mark.integration
def test_REVIEW_02_cursor_retains_exact_sort_key_for_unsafe_impact(tmp_path: Path) -> None:
    client = _ready(tmp_path)
    try:
        _seed(client, (9_007_199_254_740_992,))
        _seed(client, (-100,))
        first = client.get("/api/v1/review/queue", params={"limit": 1})
        assert first.status_code == 200
        assert first.json()["items"][0]["impact_cents"] is None
        assert first.json()["items"][0]["total_out_of_range"] is True
        cursor = first.json()["page"]["next_cursor"]
        second = client.get("/api/v1/review/queue", params={"limit": 1, "cursor": cursor})
        assert second.status_code == 200, second.text
        assert second.json()["items"][0]["impact_cents"] == 100
        assert second.json()["page"]["next_cursor"] is None
    finally:
        client.close()


@pytest.mark.integration
def test_REVIEW_04_and_REVIEW_08_single_transaction_confirmation_is_scoped_and_idempotent(
    tmp_path: Path,
) -> None:
    client = _ready(tmp_path)
    try:
        first, second = _seed(client)
        before_second = _state(client, second)
        key = str(uuid4())
        body = {
            "transaction_id": first,
            "expected_version": 1,
            "actor": "synthetic-reviewer",
            "action": "classify",
            "scope": "this_transaction",
            "allocations": [
                {"flow_kind": "expense", "category_id": "groceries", "amount_cents": -1234}
            ],
        }
        response = client.post(
            "/api/v1/review/decisions", headers={"Idempotency-Key": key}, json=body
        )
        assert response.status_code == 200, response.text
        replay = client.post(
            "/api/v1/review/decisions", headers={"Idempotency-Key": key}, json=body
        )
        assert replay.status_code == 200
        assert replay.json() == response.json()
        assert _state(client, first)[1] == (("groceries", -1234, "manual"),)
        assert _state(client, second) == before_second
        changed_body = {**body, "actor": "other-actor"}
        assert (
            client.post(
                "/api/v1/review/decisions", headers={"Idempotency-Key": key}, json=changed_body
            ).json()["error"]["code"]
            == "IDEMPOTENCY_CONFLICT"
        )
    finally:
        client.close()


@pytest.mark.integration
def test_REVIEW_05_selection_conflict_is_atomic_and_lists_conflicts(tmp_path: Path) -> None:
    client = _ready(tmp_path)
    try:
        first, second = _seed(client)
        body = {
            "transaction_id": first,
            "expected_version": 1,
            "actor": "synthetic-reviewer",
            "action": "classify",
            "scope": "selection",
            "selection": [{"transaction_id": second, "expected_version": 99}],
            "allocations": [
                {"flow_kind": "expense", "category_id": "groceries", "amount_cents": -1234}
            ],
        }
        response = client.post(
            "/api/v1/review/decisions", headers={"Idempotency-Key": str(uuid4())}, json=body
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "REVIEW_SELECTION_CONFLICT"
        assert response.json()["error"]["details"]["conflicts"][0]["transaction_id"] == second
        assert _state(client, first)[1] == ()
        assert _state(client, second)[1] == ()
    finally:
        client.close()


@pytest.mark.integration
def test_REVIEW_07_split_exact_and_invalid_amounts_are_atomic(tmp_path: Path) -> None:
    client = _ready(tmp_path)
    try:
        (transaction_id,) = _seed(client, (-10001,))
        body = {
            "transaction_id": transaction_id,
            "expected_version": 1,
            "actor": "synthetic-reviewer",
            "action": "classify",
            "allocations": [
                {"flow_kind": "expense", "category_id": "groceries", "amount_cents": -5001},
                {"flow_kind": "expense", "category_id": "restaurants", "amount_cents": -5000},
            ],
        }
        ok = client.post(
            "/api/v1/review/decisions", headers={"Idempotency-Key": str(uuid4())}, json=body
        )
        assert ok.status_code == 200, ok.text
        assert sum(item[1] for item in _state(client, transaction_id)[1]) == -10001
        second = _seed(client, (-10001,))[0]
        invalid = client.post(
            "/api/v1/review/decisions",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "transaction_id": second,
                "expected_version": 1,
                "actor": "synthetic-reviewer",
                "action": "classify",
                "allocations": [
                    {"flow_kind": "expense", "category_id": "groceries", "amount_cents": -5000},
                    {"flow_kind": "expense", "category_id": "restaurants", "amount_cents": -5000},
                ],
            },
        )
        assert invalid.status_code == 422
        assert _state(client, second) == (1, ())
        non_integer = client.post(
            "/api/v1/review/decisions",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "transaction_id": second,
                "expected_version": 1,
                "actor": "synthetic-reviewer",
                "action": "classify",
                "allocations": [
                    {"flow_kind": "expense", "category_id": "groceries", "amount_cents": -10001.0}
                ],
            },
        )
        assert non_integer.status_code == 422
        assert _state(client, second) == (1, ())
    finally:
        client.close()


@pytest.mark.integration
def test_REVIEW_10_undo_restores_prior_allocations_without_changing_bank_source(
    tmp_path: Path,
) -> None:
    client = _ready(tmp_path)
    try:
        (transaction_id,) = _seed(client, (-1234,))
        response = client.post(
            "/api/v1/review/decisions",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "transaction_id": transaction_id,
                "expected_version": 1,
                "actor": "synthetic-reviewer",
                "action": "classify",
                "allocations": [
                    {"flow_kind": "expense", "category_id": "groceries", "amount_cents": -1234}
                ],
            },
        )
        assert response.status_code == 200, response.text
        decision_id = response.json()["items"][0]["decision_id"]
        undo = client.post(
            f"/api/v1/review/decisions/{decision_id}/undo",
            headers={"Idempotency-Key": str(uuid4())},
            json={"expected_version": 2, "actor": "synthetic-reviewer"},
        )
        assert undo.status_code == 200, undo.text
        assert undo.json()["status"] == "unclassified"
        assert _state(client, transaction_id)[1] == ()
        with session_scope(client.app.state.db_engine) as session:
            transaction = session.get(Transaction, transaction_id)
            assert transaction is not None and transaction.amount_cents == -1234
    finally:
        client.close()


@pytest.mark.integration
def test_REVIEW_06_future_rule_returns_simulation_and_only_creates_draft(tmp_path: Path) -> None:
    client = _ready(tmp_path)
    try:
        (transaction_id,) = _seed(client, (-1234,))
        key = str(uuid4())
        body = {
            "transaction_id": transaction_id,
            "expected_version": 1,
            "actor": " synthetic-reviewer ",
            "action": "classify",
            "scope": "future_rule",
            "rule": {
                "name": "Règle future synthétique",
                "condition": {"label_pattern": "*API-REVIEW-SYNTHETIC*", "direction": "debit"},
                "action": {"flow_kind": "expense", "category_id": "groceries"},
            },
        }
        response = client.post(
            "/api/v1/review/decisions", headers={"Idempotency-Key": key}, json=body
        )
        assert response.status_code == 200, response.text
        assert response.json()["applied"] is False
        assert response.json()["simulation"]["database_changed"] is False
        assert response.json()["rule"]["status"] == "draft"
        assert _state(client, transaction_id) == (1, ())
        replay = client.post(
            "/api/v1/review/decisions", headers={"Idempotency-Key": key}, json=body
        )
        assert replay.json() == response.json()
        malformed = {
            **body,
            "rule": {
                **body["rule"],
                "action": {"flow_kind": "expense", "ratios": {"groceries": 10000}},
            },
        }
        invalid = client.post(
            "/api/v1/review/decisions",
            headers={"Idempotency-Key": str(uuid4())},
            json=malformed,
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "REVIEW_INVALID"
    finally:
        client.close()


@pytest.mark.integration
def test_REVIEW_06_future_rule_recovers_after_response_persistence_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _ready(tmp_path)
    try:
        (transaction_id,) = _seed(client, (-1234,))
        body = {
            "transaction_id": transaction_id,
            "expected_version": 1,
            "actor": " synthetic-reviewer ",
            "action": "classify",
            "scope": "future_rule",
            "rule": {
                "name": " Règle de reprise synthétique ",
                "condition": {"label_pattern": "*API-REVIEW-SYNTHETIC*", "direction": "debit"},
                "action": {"flow_kind": "expense", "category_id": "groceries"},
            },
        }

        def interrupted(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic persistence interruption")

        from app.api.v1 import review as review_api

        original = review_api._update_idempotency_response
        monkeypatch.setattr(review_api, "_update_idempotency_response", interrupted)
        headers = {"Idempotency-Key": str(uuid4())}
        failed = client.post("/api/v1/review/decisions", headers=headers, json=body)
        assert failed.status_code == 500
        monkeypatch.setattr(review_api, "_update_idempotency_response", original)
        allocate(
            client.app.state.db_engine,
            transaction_id,
            [AllocationPart("expense", "groceries", -1234)],
            mode="complete",
            origin="manual",
            actor="synthetic-reviewer",
            expected_version=1,
        )
        recovered = client.post("/api/v1/review/decisions", headers=headers, json=body)
        assert recovered.status_code == 200, recovered.text
        with session_scope(client.app.state.db_engine) as session:
            assert len(session.scalars(select(Rule)).all()) == 1
    finally:
        client.close()


@pytest.mark.integration
def test_REVIEW_03_group_detail_exposes_ordered_suggestions_without_applying(
    tmp_path: Path,
) -> None:
    client = _ready(tmp_path)
    try:
        _seed(client)
        queue = client.get("/api/v1/review/queue").json()
        group_id = queue["items"][0]["review_group_id"]
        detail = client.get(f"/api/v1/review/queue/{group_id}")
        assert detail.status_code == 200, detail.text
        assert len(detail.json()["transactions"]) == 2
        assert all(
            "evaluation" in row and "history" in row for row in detail.json()["transactions"]
        )
        assert all(row["allocations"] == [] for row in detail.json()["transactions"])
    finally:
        client.close()


@pytest.mark.integration
def test_REVIEW_11_human_classification_survives_reload_and_is_protected(tmp_path: Path) -> None:
    client = _ready(tmp_path)
    try:
        (transaction_id,) = _seed(client, (-1234,))
        classified = client.post(
            "/api/v1/review/decisions",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "transaction_id": transaction_id,
                "expected_version": 1,
                "actor": "synthetic-reviewer",
                "action": "classify",
                "allocations": [
                    {"flow_kind": "expense", "category_id": "groceries", "amount_cents": -1234}
                ],
            },
        )
        assert classified.status_code == 200, classified.text
        assert _state(client, transaction_id) == (2, (("groceries", -1234, "manual"),))
        queue = client.get("/api/v1/review/queue", params={"status": "human"})
        assert queue.status_code == 200
        group_id = queue.json()["items"][0]["review_group_id"]
        detail = client.get(f"/api/v1/review/queue/{group_id}")
        row = detail.json()["transactions"][0]
        assert row["transaction_id"] == transaction_id
        assert row["allocations"][0]["category_id"] == "groceries"
        assert row["evaluation"]["human_decision_protected"] is True
    finally:
        client.close()


@pytest.mark.integration
def test_REVIEW_11_selection_seeded_500_transactions_preserves_exact_totals(tmp_path: Path) -> None:
    client = _ready(tmp_path)
    try:
        ids = _seed_many(client, 500)
        body = {
            "transaction_id": ids[0],
            "expected_version": 1,
            "actor": "synthetic-reviewer",
            "action": "classify",
            "scope": "selection",
            "allocations": [
                {"flow_kind": "expense", "category_id": "groceries", "amount_cents": -1}
            ],
            "selection": [
                {
                    "transaction_id": transaction_id,
                    "expected_version": 1,
                    "allocations": [
                        {
                            "flow_kind": "expense",
                            "category_id": "groceries",
                            "amount_cents": -(index + 2),
                        }
                    ],
                }
                for index, transaction_id in enumerate(ids[1:])
            ],
        }
        response = client.post(
            "/api/v1/review/decisions", headers={"Idempotency-Key": str(uuid4())}, json=body
        )
        assert response.status_code == 200, response.text
        with session_scope(client.app.state.db_engine) as session:
            rows = session.scalars(
                select(Allocation).where(
                    Allocation.transaction_id.in_(ids),
                    Allocation.superseded_by_allocation_id.is_(None),
                    Allocation.retracted_at_utc.is_(None),
                )
            ).all()
            sums = {transaction_id: 0 for transaction_id in ids}
            assert all(row.origin == "manual" for row in rows)
            for row in rows:
                sums[row.transaction_id] += row.amount_cents
            transactions = session.scalars(
                select(Transaction).where(Transaction.transaction_id.in_(ids))
            ).all()
            amounts = {row.transaction_id: row.amount_cents for row in transactions}
            assert sums == amounts
    finally:
        client.close()


@pytest.mark.integration
def test_REVIEW_12_queue_of_10000_transactions_under_two_seconds(tmp_path: Path) -> None:
    client = _ready(tmp_path)
    try:
        _seed_many(client, 10_000)
        started = time.perf_counter()
        response = client.get("/api/v1/review/queue", params={"limit": 10})
        elapsed = time.perf_counter() - started
        assert response.status_code == 200, response.text
        assert elapsed < 2.0
        assert response.json()["items"][0]["transaction_count"] == 10_000
    finally:
        client.close()


@pytest.mark.integration
def test_REVIEW_09_refund_link_actions_use_versioned_service_and_refuse_unsafe_unlink(
    tmp_path: Path,
) -> None:
    client = _ready(tmp_path)
    try:
        target_id, source_id = _seed(client, (-1000, 500))
        allocate(
            client.app.state.db_engine,
            target_id,
            [AllocationPart("expense", "groceries", -1000)],
            mode="complete",
            origin="manual",
            actor="synthetic-reviewer",
            expected_version=1,
        )
        allocate(
            client.app.state.db_engine,
            source_id,
            [AllocationPart("refund", "income_refund", 500)],
            mode="complete",
            origin="manual",
            actor="synthetic-reviewer",
            expected_version=1,
        )
        key = str(uuid4())
        proposal = client.post(
            "/api/v1/review/refund-links",
            headers={"Idempotency-Key": key},
            json={
                "source_transaction_id": source_id,
                "target_transaction_id": target_id,
                "amount_cents": 500,
                "actor": "synthetic-reviewer",
                "expected_source_version": 2,
                "expected_target_version": 2,
            },
        )
        assert proposal.status_code == 200, proposal.text
        assert proposal.json()["status"] == "proposed"
        replay = client.post(
            "/api/v1/review/refund-links",
            headers={"Idempotency-Key": key},
            json={
                "source_transaction_id": source_id,
                "target_transaction_id": target_id,
                "amount_cents": 500,
                "actor": "synthetic-reviewer",
                "expected_source_version": 2,
                "expected_target_version": 2,
            },
        )
        assert replay.json() == proposal.json()
        link_id = proposal.json()["transaction_link_id"]
        confirm = client.post(
            f"/api/v1/review/refund-links/{link_id}/confirm",
            headers={"Idempotency-Key": str(uuid4())},
            json={"expected_version": 1, "actor": "synthetic-reviewer"},
        )
        assert confirm.status_code == 200, confirm.text
        assert confirm.json()["status"] == "confirmed"
        unlink = client.request(
            "DELETE",
            f"/api/v1/review/refund-links/{link_id}",
            headers={"Idempotency-Key": str(uuid4())},
            json={"expected_version": 2, "actor": "synthetic-reviewer"},
        )
        assert unlink.status_code == 409
        assert unlink.json()["error"]["code"] == "REFUND_LINK_CONFIRMED_PROTECTED"
    finally:
        client.close()
