from __future__ import annotations

from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.analytics import queries
from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.db.models import (
    Account,
    Allocation,
    ImportAccountSection,
    ImportFile,
    SourceRow,
    Transaction,
)
from app.db.session import create_sqlite_engine, write_transaction
from app.domain.taxonomy import load_taxonomy
from app.main import create_app
from app.services.app_secrets import ensure_hmac_key
from app.services.categories import sync_taxonomy
from tests.helpers.analytics_db import (
    add_account,
    add_transaction,
    build_family_database,
    classify,
    commit_section,
    make_engine,
    make_settings,
)


def _analytics_client(tmp_path: Path) -> tuple[TestClient, str]:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'analytics.sqlite'}",
        data_dir=tmp_path / "analytics-data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    sync_taxonomy(engine, load_taxonomy())
    ensure_hmac_key(engine)
    with write_transaction(engine) as session:
        account = Account(alias="Compte synthétique analytics", account_role="current")
        session.add(account)
        session.flush()
        import_file_id = str(uuid4())
        import_file = ImportFile(
            import_file_id=import_file_id,
            file_sha256=sha256(b"synthetic analytics fixture").hexdigest(),
            archive_relpath=f"data/originals/{import_file_id}.bin",
            archive_storage_mode="raw",
            archive_permissions_octal="0600",
            file_size_bytes=1,
            received_on=date(2026, 4, 1),
            generation_date=date(2026, 4, 1),
            generation_date_source="header_download",
            parsed_operation_count=5,
            status="previewed",
        )
        session.add(import_file)
        session.flush()
        session.add(
            ImportAccountSection(
                import_file_id=import_file.import_file_id,
                section_index=1,
                account_id=account.account_id,
                coverage_from=date(2026, 3, 1),
                coverage_through=date(2026, 3, 31),
                complete_through=date(2026, 3, 31),
                section_operation_count=5,
            )
        )
        entries = [
            (date(2026, 3, 2), 30_000, "income_recurring", "income_salary_a"),
            (date(2026, 3, 3), 2_500, "income_other", "income_benefits"),
            (date(2026, 3, 5), -6_000, "expense", "groceries"),
            (date(2026, 3, 6), -4_000, "expense", "restaurants_dining"),
            (date(2026, 3, 7), 5_000_000, "loan_disbursement", "flow_loan_disbursement"),
        ]
        for index, (booked_on, amount, flow_kind, category_id) in enumerate(entries, start=1):
            identity = f"analytics-{uuid4()}"
            transaction = Transaction(
                account_id=account.account_id,
                booked_on=booked_on,
                amount_cents=amount,
                raw_label="LIBELLE SYNTHETIQUE CONFIDENTIEL",
                identity_label=identity,
                identity_key_sha256=sha256(identity.encode()).hexdigest(),
                occurrence_rank=1,
            )
            session.add(transaction)
            session.flush()
            session.add(
                SourceRow(
                    source_row_id=str(uuid4()),
                    import_file_id=import_file.import_file_id,
                    section_index=1,
                    logical_row_idx=index,
                    row_sha256=sha256(f"row-{index}".encode()).hexdigest(),
                    identity_key_sha256=transaction.identity_key_sha256,
                    transaction_id=transaction.transaction_id,
                )
            )
            session.add(
                Allocation(
                    transaction_id=transaction.transaction_id,
                    category_id=category_id,
                    flow_kind=flow_kind,
                    amount_cents=amount,
                    origin="manual",
                    decision="confirmed",
                )
            )
        session.flush()
        import_file.status = "committed"
        import_file.version = 2
        import_file.finished_at_utc = datetime(2026, 4, 1, tzinfo=UTC)
    client = TestClient(create_app(settings))
    return client, account.account_id


def test_KPI_03_analytics_endpoints_share_filters_totals_and_disable_cache(
    tmp_path: Path,
) -> None:
    client, account_id = _analytics_client(tmp_path)
    with client:
        params = {
            "period_start": "2026-03-01",
            "period_end": "2026-03-31",
            "account_ids": account_id,
            "mode": "family_life",
        }
        overview = client.get("/api/v1/analytics/overview", params=params)
        cashflow = client.get("/api/v1/analytics/cashflow", params=params)
        hierarchy = client.get("/api/v1/analytics/spending-hierarchy", params=params)
        assert overview.status_code == cashflow.status_code == hierarchy.status_code == 200
        assert overview.headers["cache-control"] == "no-store"
        assert overview.json()["raw_cents"] == cashflow.json()["raw_cents"] == 5_022_500
        assert hierarchy.json()["raw_cents"] == 10_000
        assert hierarchy.json()["item_sum_cents"] == hierarchy.json()["raw_cents"]
        assert sum(item["raw_cents"] for item in cashflow.json()["by_flow_kind"]) == 5_022_500
        assert "LIBELLE SYNTHETIQUE CONFIDENTIEL" not in overview.text


def test_KPI_11_income_endpoint_separates_salary_from_a_loan_credit(tmp_path: Path) -> None:
    client, account_id = _analytics_client(tmp_path)
    with client:
        response = client.get(
            "/api/v1/analytics/income",
            params={
                "period_start": "2026-03-01",
                "period_end": "2026-03-31",
                "account_ids": account_id,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["metric_definition_id"] == "regular_income.v1"
        assert body["raw_cents"] == 30_000
        assert body["loan_disbursement_cents"] == 5_000_000
        assert body["other_income_cents"] == 2_500
        assert body["bank_credits_cents"] == 5_032_500
        assert response.headers["cache-control"] == "no-store"


def test_KPI_12_invalid_selected_loan_commitment_is_a_422(tmp_path: Path) -> None:
    client, account_id = _analytics_client(tmp_path)
    with client:
        response = client.get(
            "/api/v1/analytics/overview",
            params={
                "period_start": "2026-03-01",
                "period_end": "2026-03-31",
                "account_ids": account_id,
                "commitment_transaction_ids": str(uuid4()),
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_KPI_13_server_clock_marks_the_current_calendar_month_partial(tmp_path: Path) -> None:
    client, account_id = _analytics_client(tmp_path)
    client.app.state.clock = lambda: datetime(2026, 3, 15, tzinfo=UTC)
    with client:
        response = client.get(
            "/api/v1/analytics/overview",
            params={
                "period_start": "2026-03-01",
                "period_end": "2026-03-31",
                "account_ids": account_id,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["period"]["as_of"] == "2026-03-15"
        assert body["period"]["completeness"] == "partial"
        capacity = next(
            item
            for item in body["metrics"]
            if item["metric_definition_id"] == "operational_saving_capacity.v1"
        )
        assert capacity["raw_cents"] is None
        assert capacity["unavailable_reason"] == "period_not_complete"


def test_KPI_07_drilldown_cursor_recomposes_spending_and_labels_are_explicit(
    tmp_path: Path,
) -> None:
    client, account_id = _analytics_client(tmp_path)
    with client:
        params = {
            "period_start": "2026-03-01",
            "period_end": "2026-03-31",
            "account_ids": account_id,
            "mode": "family_life",
            "metric_definition_id": "consumption_net.v1",
            "limit": 1,
        }
        first = client.get("/api/v1/analytics/drilldown", params=params)
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["raw_cents"] == 10_000
        assert first_body["detail_summary"]["recalculated_raw_cents"] == 10_000
        assert first_body["detail_summary"]["reconciles"] is True
        assert first_body["page"]["next_cursor"]
        assert first_body["items"][0]["active_allocation_ids"]
        assert "raw_label" not in first_body["items"][0]

        next_params = {**params, "cursor": first_body["page"]["next_cursor"]}
        second = client.get("/api/v1/analytics/drilldown", params=next_params)
        assert second.status_code == 200
        assert second.json()["page"]["next_cursor"] is None
        contributions = [
            item["metric_contribution_cents"]
            for body in (first_body, second.json())
            for item in body["items"]
        ]
        assert sum(contributions) == first_body["raw_cents"]

        explicit = client.get(
            "/api/v1/analytics/drilldown",
            params={**params, "include_labels": "true"},
        )
        assert explicit.status_code == 200
        assert explicit.json()["items"][0]["raw_label"] == "LIBELLE SYNTHETIQUE CONFIDENTIEL"


def test_KPI_10_analytics_filters_return_normalized_422_errors(tmp_path: Path) -> None:
    client, account_id = _analytics_client(tmp_path)
    with client:
        response = client.get(
            "/api/v1/analytics/overview",
            params={
                "period_start": "2025-12-01",
                "period_end": "2025-12-31",
                "account_ids": account_id,
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        invalid_flow = client.get(
            "/api/v1/analytics/cashflow",
            params={
                "period_start": "2026-03-01",
                "period_end": "2026-03-31",
                "account_ids": account_id,
                "flow_kind": "not-a-flow",
            },
        )
        assert invalid_flow.status_code == 422
        assert invalid_flow.json()["error"]["code"] == "VALIDATION_ERROR"


# --- Rich family ledger written through the allocation and refund services -----------------


def _family_client(
    tmp_path: Path, *, classify_unallocated: bool = False
) -> tuple[TestClient, dict[str, str]]:
    settings = make_settings(tmp_path, "family")
    engine = make_engine(settings)
    try:
        ids = build_family_database(engine, classify_unallocated=classify_unallocated)
    finally:
        engine.dispose()
    client = TestClient(create_app(settings))
    client.app.state.clock = lambda: datetime(2026, 4, 15, tzinfo=UTC)
    return client, ids


def _march(ids: dict[str, str], **extra: object) -> dict[str, object]:
    return {
        "period_start": "2026-03-01",
        "period_end": "2026-03-31",
        "account_ids": [ids["cur"], ids["per"], ids["sav"]],
        "mode": "family_life",
        **extra,
    }


def _get(client: TestClient, path: str, params: dict[str, object]) -> dict:
    response = client.get(f"/api/v1/analytics/{path}", params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_KPI_21_breakdown_endpoints_sum_to_the_overview_aggregates(tmp_path: Path) -> None:
    client, ids = _family_client(tmp_path)
    with client:
        params = _march(ids)
        overview = _get(client, "overview", params)
        hierarchy = _get(client, "spending-hierarchy", params)
        family = _get(client, "family", params)
        savings = _get(client, "savings", params)
        merchants = _get(client, "merchants", params)
        income = _get(client, "income", params)
        cashflow = _get(client, "cashflow", params)
        metrics = {item["metric_definition_id"]: item for item in overview["metrics"]}
        consumption = metrics["consumption_net.v1"]["raw_cents"]
        assert consumption == 5_999
        assert hierarchy["raw_cents"] == hierarchy["item_sum_cents"] == consumption
        assert sum(node["raw_cents"] for node in hierarchy["tree"]) == consumption
        assert family["raw_cents"] == family["item_sum_cents"] == consumption
        assert (
            family["personal_allowances_sum_cents"]
            == metrics["personal_allowances.v1"]["raw_cents"]
        )
        assert savings["raw_cents"] == savings["by_category_sum_cents"] == 15_000
        assert merchants["raw_cents"] == merchants["item_sum_cents"] == consumption
        assert income["raw_cents"] == 300_000
        assert income["loan_disbursement_cents"] == 5_000_000
        assert income["other_income_cents"] == 18_000
        assert income["refunds_cents"] == 4_001
        assert cashflow["raw_cents"] == overview["raw_cents"]
        assert sum(item["raw_cents"] for item in cashflow["by_flow_kind"]) == overview["raw_cents"]
        assert cashflow["credits_cents"] - cashflow["debits_cents"] == overview["raw_cents"]
        # Aucun libellé bancaire hors drilldown explicite.
        for body in (overview, hierarchy, family, savings, merchants, income, cashflow):
            assert "LIBELLE SYNTHETIQUE CONFIDENTIEL" not in str(body)


def test_KPI_22_category_filter_follows_children_and_linked_refund_credits(
    tmp_path: Path,
) -> None:
    client, ids = _family_client(tmp_path)
    with client:
        body = _get(client, "overview", _march(ids, category_ids="groceries"))
        metrics = {item["metric_definition_id"]: item for item in body["metrics"]}
        # Achat + deux avoirs liés à l'alimentation ; l'achat du compte personnel est exclu.
        assert body["raw_cents"] == -10_000 + 3_001 + 1_000
        assert metrics["consumption_net.v1"]["raw_cents"] == 6_000 - 1_801 - 1_000
        hierarchy = _get(client, "spending-hierarchy", _march(ids, category_ids="groceries"))
        assert [item["category_id"] for item in hierarchy["items"]] == ["groceries"]
        unknown = _get(client, "overview", _march(ids, flow_kind="unknown"))
        assert unknown["raw_cents"] == -1_234
        assert unknown["coverage"]["operation_count"] == 1


def test_KPI_15_series_buckets_recompose_the_period_total(tmp_path: Path) -> None:
    client, ids = _family_client(tmp_path)
    with client:
        params = _march(ids)
        total = _get(client, "overview", params)["raw_cents"]
        for bucket, expected_points in (("day", 31), ("week", 6), ("month", 1)):
            body = _get(
                client,
                "series",
                {**params, "metric_definition_id": "bank_net_flow.v1", "bucket": bucket},
            )
            assert len(body["series"]) == expected_points
            assert sum(point["raw_cents"] for point in body["series"]) == total
            assert {point["completeness"] for point in body["series"]} == {"complete"}
        consumption = _get(
            client,
            "series",
            {**params, "metric_definition_id": "consumption_net.v1", "bucket": "week"},
        )
        assert sum(point["raw_cents"] for point in consumption["series"]) == 5_999
        capacity = _get(
            client,
            "series",
            {**params, "metric_definition_id": "operational_saving_capacity.v1", "bucket": "month"},
        )
        assert capacity["series"][0]["raw_cents"] is None
        assert capacity["series"][0]["unavailable_reason"] == "relevant_residual"


def test_KPI_16_every_metric_drilldown_reconciles_across_pages(tmp_path: Path) -> None:
    client, ids = _family_client(tmp_path, classify_unallocated=True)
    additive = {
        "bank_net_flow.v1",
        "regular_income.v1",
        "consumption_net.v1",
        "personal_allowances.v1",
        "savings_net_transferred.v1",
        "operational_saving_capacity.v1",
    }
    with client:
        params = _march(ids)
        for metric_id in queries.OVERVIEW_METRICS:
            seen: set[str] = set()
            contribution_sum = 0
            cursor = None
            while True:
                query = {**params, "metric_definition_id": metric_id, "limit": 3}
                if cursor:
                    query["cursor"] = cursor
                body = _get(client, "drilldown", query)
                for item in body["items"]:
                    assert item["transaction_id"] not in seen
                    seen.add(item["transaction_id"])
                    contribution_sum += item.get("metric_contribution_cents") or 0
                assert body["detail_summary"]["reconciles"] in {True, None}
                cursor = body["page"]["next_cursor"]
                if cursor is None:
                    break
            if metric_id in additive:
                assert contribution_sum == body["raw_cents"], metric_id
            assert body["detail_summary"]["reconciles"] is True, metric_id
        stale = client.get(
            "/api/v1/analytics/drilldown",
            params={**params, "metric_definition_id": "bank_net_flow.v1", "cursor": "x" * 20},
        )
        assert stale.status_code == 422
        assert stale.json()["error"]["code"] == "CURSOR_INVALID"
        # Le curseur est lié aux filtres : un autre mode personnel l'invalide.
        first = _get(
            client, "drilldown", {**params, "metric_definition_id": "bank_net_flow.v1", "limit": 2}
        )
        mismatch = client.get(
            "/api/v1/analytics/drilldown",
            params={
                **params,
                "personal_mode": "observed",
                "metric_definition_id": "bank_net_flow.v1",
                "limit": 2,
                "cursor": first["page"]["next_cursor"],
            },
        )
        assert mismatch.status_code == 422


def test_KPI_13_overview_exposes_monthly_deltas_and_unavailable_reasons(tmp_path: Path) -> None:
    client, ids = _family_client(tmp_path, classify_unallocated=True)
    with client:
        body = _get(client, "overview", _march(ids))
        deltas = {item["base_metric_definition_id"]: item for item in body["monthly_deltas"]}
        assert deltas["regular_income.v1"]["raw_cents"] == 20_000
        assert deltas["regular_income.v1"]["previous_raw_cents"] == 280_000
        assert deltas["bank_net_flow.v1"]["status"] == "available"
        committed = _get(
            client, "overview", _march(ids, commitment_transaction_ids=ids["loan-unsplit"])
        )
        committed_deltas = {
            item["base_metric_definition_id"]: item for item in committed["monthly_deltas"]
        }
        capacity = committed_deltas["operational_saving_capacity.v1"]
        assert capacity["raw_cents"] is None
        assert capacity["unavailable_reason"] == "commitments_not_comparable"
        assert committed_deltas["regular_income.v1"]["raw_cents"] == 20_000
        january = _get(
            client,
            "overview",
            {**_march(ids), "period_start": "2026-01-01", "period_end": "2026-01-31"},
        )
        assert {item["unavailable_reason"] for item in january["monthly_deltas"]} == {
            "previous_period_not_available"
        }
        # Une échéance sélectionnée doit être un prêt de la sélection, pas un achat.
        invalid = client.get(
            "/api/v1/analytics/overview",
            params=_march(ids, commitment_transaction_ids=ids["purchase"]),
        )
        assert invalid.status_code == 422


def test_KPI_17_unsafe_integers_are_null_and_flagged_not_rounded(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, "unsafe")
    engine = make_engine(settings)
    try:
        account_id = add_account(engine, "Compte démesuré", "current")
        commit_section(engine, account_id, date(2026, 3, 1), date(2026, 3, 31))
        huge = 2**60
        transaction_id = add_transaction(engine, account_id, date(2026, 3, 5), huge)
        classify(engine, transaction_id, ("income_recurring", "income_salary_a", huge, "adult_a"))
    finally:
        engine.dispose()
    client = TestClient(create_app(settings))
    with client:
        params = {
            "period_start": "2026-03-01",
            "period_end": "2026-03-31",
            "account_ids": account_id,
        }
        body = _get(client, "overview", params)
        assert body["raw_cents"] is None
        assert body["total_out_of_range"] is True
        income = next(
            item for item in body["metrics"] if item["metric_definition_id"] == "regular_income.v1"
        )
        assert income["raw_cents"] is None
        assert income["total_out_of_range"] is True
        # Le taux (bps) d'un revenu démesuré n'est pas non plus envoyé en entier non sûr.
        text = client.get("/api/v1/analytics/overview", params=params).text
        assert str(huge) not in text
        cashflow = _get(client, "cashflow", params)
        assert cashflow["credits_cents"] is None
        assert cashflow["total_out_of_range"] is True


def test_KPI_10_every_analytics_error_is_normalized_and_uncached(tmp_path: Path) -> None:
    client, ids = _family_client(tmp_path)
    with client:
        base = _march(ids)
        cases = [
            ("overview", {**base, "account_ids": [str(uuid4())]}),
            ("overview", {**base, "category_ids": "not_a_category"}),
            ("overview", {k: v for k, v in base.items() if k != "account_ids"}),
            ("overview", {**base, "period_start": "2026-04-01", "period_end": "2026-03-01"}),
            ("series", {**base, "metric_definition_id": "monthly_delta.v1"}),
            ("series", {**base, "metric_definition_id": "bank_net_flow.v1", "bucket": "year"}),
            ("drilldown", {**base, "metric_definition_id": "monthly_delta.v1"}),
            ("drilldown", {**base, "metric_definition_id": "nope.v1"}),
            ("drilldown", {**base, "metric_definition_id": "bank_net_flow.v1", "limit": 0}),
        ]
        for path, params in cases:
            response = client.get(f"/api/v1/analytics/{path}", params=params)
            assert response.status_code == 422, (path, params, response.text)
            assert response.json()["error"]["code"] == "VALIDATION_ERROR"
            assert response.headers["cache-control"] == "no-store"
