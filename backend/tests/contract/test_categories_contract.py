from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.migrate import upgrade_to_head
from app.db.session import create_sqlite_engine
from app.domain.taxonomy import load_taxonomy
from app.services.categories import sync_taxonomy


@pytest.mark.contract
def test_API_02_categories_read_contract_returns_tree_dimensions_and_fingerprint(
    client: TestClient,
) -> None:
    settings = client.app.state.settings
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    sync_taxonomy(engine, load_taxonomy())
    response = client.get("/api/v1/categories")
    assert response.status_code == 200
    body = response.json()
    assert body["taxonomy_version"] == 1
    assert len(body["taxonomy_sha256"]) == 64
    assert set(body["dimensions"]) == {
        "beneficiary",
        "cadence",
        "flexibility",
        "acquisition",
        "decision",
        "origin",
    }
    roots = {item["category_id"]: item for item in body["items"]}
    assert "groceries" in roots
    assert any(
        child["category_id"] == "groceries_organic" for child in roots["groceries"]["children"]
    )
    engine.dispose()


@pytest.mark.contract
def test_API_02_categories_read_requires_migrated_database(client: TestClient) -> None:
    response = client.get("/api/v1/categories")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_NOT_READY"
