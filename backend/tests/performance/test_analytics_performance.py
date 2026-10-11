from __future__ import annotations

import hashlib
import os
import time
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.db.models import Account, Allocation, Transaction
from app.db.session import write_transaction
from app.main import create_app
from tests.helpers.analytics_db import commit_section, make_engine, make_settings

pytestmark = pytest.mark.skipif(
    os.environ.get("COMPTES_RUN_ANALYTICS_PERF") != "1",
    reason="activer explicitement avec COMPTES_RUN_ANALYTICS_PERF=1",
)
CATEGORIES = (
    "groceries_supermarket",
    "groceries_bakery",
    "restaurants_dining",
    "housing_energy",
    "transport_fuel",
)


def _seed(tmp_path: Path, size: int) -> tuple[object, str, int, int]:
    """Synthetic March ledger: expenses, salaries, partial splits and unallocated rows."""
    settings = make_settings(tmp_path, f"perf-{size}")
    engine = make_engine(settings)
    account_id = str(uuid4())
    with write_transaction(engine) as session:
        session.add(
            Account(account_id=account_id, alias="Benchmark synthétique", account_role="current")
        )
    commit_section(engine, account_id, date(2026, 3, 1), date(2026, 3, 31))
    bank_total = 0
    consumption = 0
    with write_transaction(engine) as session:
        connection = session.connection()
        transactions: list[dict[str, object]] = []
        allocations: list[dict[str, object]] = []
        for index in range(size):
            transaction_id = str(uuid4())
            identity = f"synthetic-analytics-{size}-{index}"
            kind = index % 10
            income = kind == 7
            amount = (1_000 + index % 4_000) * (1 if income else -1)
            bank_total += amount
            transactions.append(
                {
                    "transaction_id": transaction_id,
                    "account_id": account_id,
                    "booked_on": date(2026, 3, 1 + index % 31),
                    "amount_cents": amount,
                    "raw_label": "TRANSACTION SYNTHETIQUE PERFORMANCE",
                    "identity_label": identity,
                    "identity_key_sha256": hashlib.sha256(identity.encode()).hexdigest(),
                    "occurrence_rank": 1,
                    "version": 1,
                }
            )
            if kind == 9:
                continue  # unallocated: stays a visible residual
            allocated = amount if kind != 8 else amount // 2
            if kind != 7:
                consumption += -allocated
            allocations.append(
                {
                    "allocation_id": str(uuid4()),
                    "transaction_id": transaction_id,
                    "category_id": "income_salary_a" if income else CATEGORIES[index % 5],
                    "flow_kind": "income_recurring" if income else "expense",
                    "amount_cents": allocated,
                    "origin": "manual",
                    "decision": "confirmed",
                }
            )
            if len(transactions) == 1_000:
                connection.execute(insert(Transaction.__table__), transactions)
                connection.execute(insert(Allocation.__table__), allocations)
                transactions.clear()
                allocations.clear()
        if transactions:
            connection.execute(insert(Transaction.__table__), transactions)
        if allocations:
            connection.execute(insert(Allocation.__table__), allocations)
    engine.dispose()
    return settings, account_id, bank_total, consumption


def _timed(client: TestClient, path: str, params: dict[str, object]) -> tuple[float, dict]:
    started = time.perf_counter()
    response = client.get(f"/api/v1/analytics/{path}", params=params)
    elapsed = time.perf_counter() - started
    assert response.status_code == 200, response.text
    return elapsed, response.json()


@pytest.mark.integration
def test_PERF_01_analytics_reads_on_10000_and_100000_synthetic_transactions(
    tmp_path: Path,
) -> None:
    report: list[str] = []
    for size in (10_000, 100_000):
        settings, account_id, bank_total, consumption = _seed(tmp_path, size)
        client = TestClient(create_app(settings))  # type: ignore[arg-type]
        with client:
            params: dict[str, object] = {
                "period_start": "2026-03-01",
                "period_end": "2026-03-31",
                "account_ids": account_id,
                "mode": "family_life",
            }
            timings: dict[str, float] = {}
            timings["overview"], overview = _timed(client, "overview", params)
            assert overview["coverage"]["operation_count"] == size
            assert overview["period"]["completeness"] == "complete"
            assert overview["raw_cents"] == bank_total
            consumption_metric = next(
                item
                for item in overview["metrics"]
                if item["metric_definition_id"] == "consumption_net.v1"
            )
            assert consumption_metric["raw_cents"] == consumption
            timings["spending-hierarchy"], hierarchy = _timed(client, "spending-hierarchy", params)
            assert hierarchy["item_sum_cents"] == consumption
            timings["family"], family = _timed(client, "family", params)
            assert family["item_sum_cents"] == consumption
            timings["series-day"], series = _timed(
                client,
                "series",
                {**params, "metric_definition_id": "bank_net_flow.v1", "bucket": "day"},
            )
            assert sum(point["raw_cents"] for point in series["series"]) == bank_total
            timings["drilldown-page"], drill = _timed(
                client,
                "drilldown",
                {**params, "metric_definition_id": "consumption_net.v1", "limit": 50},
            )
            assert drill["detail_summary"]["reconciles"] is True
            assert len(drill["items"]) == 50
        report.append(
            f"{size}: " + " ".join(f"{name}={seconds:.3f}s" for name, seconds in timings.items())
        )
        ceiling = 30.0 if size == 100_000 else 6.0
        assert max(timings.values()) < ceiling, report
    print("analytics_read_seconds " + " | ".join(report))
