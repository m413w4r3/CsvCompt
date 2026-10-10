import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import DatabaseBusyError, VersionConflictError
from app.db.errors import VersionConflictError as DatabaseDomainVersionConflictError
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
def test_API_01_errors_are_normalized_without_input_echo(
    client: TestClient, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    not_found = client.get("/no-such-route?token=secret-value")
    method = client.post("/api/v1/health", json={"password": "private"})
    validation_app = create_app(
        Settings(database_url=f"sqlite:///{tmp_path / 'validation.sqlite'}", _env_file=None)
    )

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
    assert "/no-such-route?token=secret-value" not in caplog.text


@pytest.mark.contract
def test_foreign_host_is_rejected_with_normalized_error(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"host": "attacker.invalid"})
    assert response.status_code in (400, 403)
    assert response.json() == {
        "error": {
            "code": "INVALID_HOST",
            "message": "En-tête Host non autorisé.",
            "details": {},
        }
    }
    assert "attacker.invalid" not in response.text


@pytest.mark.contract
def test_database_and_version_conflicts_have_official_error_codes(tmp_path) -> None:
    app = create_app(
        Settings(database_url=f"sqlite:///{tmp_path / 'errors.sqlite'}", _env_file=None)
    )

    @app.get("/api/v1/database-busy")
    def database_busy() -> None:
        raise DatabaseBusyError()

    @app.get("/api/v1/version-conflict")
    def version_conflict() -> None:
        raise VersionConflictError(current_version=7)

    @app.get("/api/v1/database-version-conflict")
    def database_version_conflict() -> None:
        raise DatabaseDomainVersionConflictError()

    with TestClient(app) as client:
        busy = client.get("/api/v1/database-busy")
        conflict = client.get("/api/v1/version-conflict")
        database_conflict = client.get("/api/v1/database-version-conflict")
    assert busy.status_code == 503
    assert busy.json()["error"]["code"] == "DATABASE_BUSY"
    assert busy.json()["error"]["details"] == {"retryable": True}
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"
    assert conflict.json()["error"]["details"] == {"current_version": 7}
    assert database_conflict.status_code == 409
    assert database_conflict.json()["error"]["code"] == "VERSION_CONFLICT"


def _documented_json_example(example_name: str) -> dict[str, object]:
    api_doc = Path(__file__).parents[3] / "docs" / "API.md"
    document = api_doc.read_text(encoding="utf-8")
    match = re.search(
        rf"<!-- contract-example:{re.escape(example_name)} -->\s*```json\s*(.*?)\s*```",
        document,
        flags=re.DOTALL,
    )
    assert match is not None, f"Exemple JSON {example_name} absent de docs/API.md"
    parsed = json.loads(match.group(1))
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.contract
def test_documented_error_and_health_examples_match_real_response_shapes(
    tmp_path,
) -> None:
    documented_error = _documented_json_example("error")
    documented_health = _documented_json_example("health")
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'docs.sqlite'}", _env_file=None))

    @app.get("/api/v1/documented-conflict")
    def documented_conflict() -> None:
        raise VersionConflictError(current_version=3)

    with TestClient(app) as client:
        actual_error = client.get("/api/v1/documented-conflict").json()
        actual_health = client.get("/api/v1/health").json()

    assert documented_error == actual_error
    assert set(documented_health) == set(actual_health)
    assert set(documented_health["database"]) == set(actual_health["database"])
    assert isinstance(actual_health["time_utc"], str)
    assert isinstance(actual_health["llm_enabled"], bool)
    assert actual_health["storage_status"] in {"ok", "degraded", "unavailable"}
    assert isinstance(actual_health["llm_enabled"], bool)
    assert actual_health["database"]["revision"] is None or isinstance(
        actual_health["database"]["revision"], str
    )
    assert {"status", "api_version", "storage_status", "llm_enabled"} <= set(documented_health)
    assert documented_health["api_version"] == "1"
    assert documented_health["storage_status"] == "ok"


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
