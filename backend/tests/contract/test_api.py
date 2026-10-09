import logging
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.mark.contract
def test_API_01_health_exact_contract_is_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'absent.sqlite'}",
        llm_api_key="private-test-key",
        _env_file=None,
    )
    app = create_app(settings, clock=lambda: datetime(2026, 1, 2, tzinfo=UTC))

    def reject_socket(*_args, **_kwargs):
        raise AssertionError("health ne doit ouvrir aucun socket")

    with TestClient(app) as client:
        monkeypatch.setattr("socket.socket", reject_socket)
        with caplog.at_level(logging.INFO):
            response = client.get("/api/v1/health")
        monkeypatch.undo()
    assert response.status_code == 200
    body = response.json()
    assert {"status", "api_version", "storage_status", "llm_enabled"} <= body.keys()
    assert body["status"] == "ok"
    assert body["api_version"] == "1"
    assert body["storage_status"] == "degraded"
    assert body["llm_enabled"] is False
    assert body["version"] == "0.1.0"
    assert body["time_utc"] == "2026-01-02T00:00:00Z"
    assert body["database"] == {"status": "not_initialized", "revision": None}
    assert body["llm"] == {"enabled": False}
    assert body["offline_ready"] is True
    assert not (tmp_path / "absent.sqlite").exists()
    assert "private-test-key" not in response.text
    assert "private-test-key" not in repr(settings)
    assert "private-test-key" not in caplog.text


@pytest.mark.contract
def test_API_01_corrupt_database_is_unavailable_without_affecting_health(tmp_path) -> None:
    database = tmp_path / "corrupt.sqlite"
    database.write_bytes(b"not a sqlite database")
    app = create_app(Settings(database_url=f"sqlite:///{database}", _env_file=None))

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["storage_status"] == "unavailable"
    assert response.json()["database"]["status"] == "unavailable"


@pytest.mark.contract
def test_API_01_errors_are_normalized_without_input_echo(client: TestClient) -> None:
    not_found = client.get("/no-such-route?token=secret-value")
    method = client.post("/api/v1/health", json={"password": "private"})
    validation_app = create_app(Settings(_env_file=None))

    @validation_app.get("/api/v1/sample")
    def sample(limit: int) -> dict[str, int]:
        return {"limit": limit}

    with TestClient(validation_app) as validation_client:
        validation = validation_client.get("/api/v1/sample", params={"limit": "secret-invalid"})
    for response, status, code in (
        (not_found, 404, "NOT_FOUND"),
        (method, 405, "METHOD_NOT_ALLOWED"),
        (validation, 422, "VALIDATION_ERROR"),
    ):
        assert response.status_code == status
        assert set(response.json()) == {"error"}
        assert set(response.json()["error"]) == {"code", "message", "details"}
        assert response.json()["error"]["code"] == code
    assert "secret-invalid" not in validation.text
    assert "secret-value" not in not_found.text


@pytest.mark.contract
def test_API_01_internal_error_has_correlation_without_traceback(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'app.sqlite'}", _env_file=None))

    @app.get("/api/v1/failure")
    def fail() -> None:
        raise RuntimeError("private request payload")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/failure")
    error = response.json()["error"]
    assert response.status_code == 500
    assert error["code"] == "INTERNAL_ERROR"
    assert "private request payload" not in response.text
    assert response.headers["x-correlation-id"] == error["details"]["correlation_id"]
    assert "private request payload" not in caplog.text
    assert "Traceback" not in caplog.text
