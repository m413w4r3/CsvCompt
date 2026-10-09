"""Fixtures backend synthétiques et locales."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def temp_sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'test.sqlite'}"


@pytest.fixture
def test_settings(temp_sqlite_url: str) -> Settings:
    return Settings(database_url=temp_sqlite_url, data_dir=Path("/tmp/comptes-familiaux-test"))


@pytest.fixture
def client(test_settings: Settings) -> Iterator[TestClient]:
    application = create_app(test_settings)
    with TestClient(application) as test_client:
        yield test_client
