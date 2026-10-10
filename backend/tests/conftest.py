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
def test_settings(temp_sqlite_url: str, tmp_path: Path) -> Settings:
    return Settings(
        database_url=temp_sqlite_url,
        data_dir=tmp_path / "app-data",
        _env_file=None,
    )


@pytest.fixture(autouse=True)
def test_clients_use_loopback_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aligne le Host par défaut de TestClient sur le bind local de l'application."""
    original_init = TestClient.__init__

    def init_with_loopback(self, app, *args, **kwargs):
        if args:
            args = ("http://127.0.0.1" if args[0] == "http://testserver" else args[0], *args[1:])
        else:
            kwargs.setdefault("base_url", "http://127.0.0.1")
        original_init(self, app, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", init_with_loopback)


@pytest.fixture
def client(test_settings: Settings) -> Iterator[TestClient]:
    application = create_app(test_settings)
    with TestClient(application) as test_client:
        yield test_client
