from __future__ import annotations

import errno
import logging
import socket
from ipaddress import ip_address

import pytest
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.main import create_app
from app.services.app_secrets import ensure_hmac_key


@pytest.mark.integration
def test_SEC_01_loopback_host_is_accepted_and_nonlocal_host_is_rejected(client) -> None:
    local = client.get("/api/v1/health", headers={"host": "127.0.0.1:9513"})
    hostile = client.get("/api/v1/health", headers={"host": "bank.example.invalid"})

    assert local.status_code == 200
    assert hostile.status_code == 400
    assert hostile.json() == {
        "error": {
            "code": "INVALID_HOST",
            "message": "En-tête Host non autorisé.",
            "details": {},
        }
    }


@pytest.mark.integration
@pytest.mark.parametrize("method", ["post", "patch"])
def test_SEC_02_hostile_origin_is_rejected_for_mutations(client, method: str) -> None:
    if method == "post":
        response = client.post(
            "/api/v1/imports/preview",
            headers={"Origin": "https://attacker.invalid"},
            files={"file": ("synthetic.csv", b"synthetic", "text/csv")},
        )
    else:
        response = client.patch(
            "/api/v1/imports/550e8400-e29b-41d4-a716-446655440010/sections/1/coverage",
            headers={"Origin": "https://attacker.invalid"},
            json={
                "expected_version": 1,
                "coverage_from": "2026-03-01",
                "coverage_through": "2026-03-31",
            },
        )

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "ORIGIN_NOT_ALLOWED",
            "message": "Origine de mutation non autorisée.",
            "details": {},
        }
    }


@pytest.mark.integration
def test_SEC_02_local_referer_is_used_when_origin_is_absent(client) -> None:
    response = client.post(
        "/api/v1/imports/preview",
        headers={"Referer": "http://localhost:5173/transactions"},
        files={"file": ("synthetic.csv", b"invalid synthetic content", "text/csv")},
    )
    assert response.status_code != 403


@pytest.mark.integration
def test_SEC_02_cors_preflight_is_explicit_and_never_wildcard(client) -> None:
    allowed = client.options(
        "/api/v1/imports/preview",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, idempotency-key",
        },
    )
    rejected = client.options(
        "/api/v1/imports/preview",
        headers={
            "Origin": "https://attacker.invalid",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert allowed.status_code == 204
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert allowed.headers["access-control-allow-origin"] != "*"
    assert "POST" in allowed.headers["access-control-allow-methods"]
    assert rejected.status_code == 403
    assert "access-control-allow-origin" not in rejected.headers


@pytest.mark.integration
def test_SEC_02_api_security_headers_and_html_csp(test_settings: Settings) -> None:
    app = create_app(test_settings)

    def html_page(_request: Request) -> HTMLResponse:
        return HTMLResponse("<h1>local</h1>")

    app.add_route("/security-test.html", html_page)
    with TestClient(app) as client:
        api_response = client.get("/api/v1/health")
        html_response = client.get("/security-test.html")

    assert api_response.headers["x-content-type-options"] == "nosniff"
    assert api_response.headers["referrer-policy"] == "no-referrer"
    assert api_response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in html_response.headers["content-security-policy"]
    assert "connect-src 'self'" in html_response.headers["content-security-policy"]


@pytest.mark.integration
def test_SEC_04_health_openapi_and_internal_error_do_not_disclose_secrets_or_paths(
    test_settings: Settings, tmp_path, caplog
) -> None:
    fake_secret = "sk-test-only-secret-value"
    fake_iban = "FR" + "76" + "A1" * 10
    private_path = str(tmp_path / "private-test-data" / "accounts.sqlite")
    settings = test_settings.model_copy(
        update={
            "database_url": f"sqlite:///{private_path}",
            "llm_api_key": SecretStr(fake_secret),
        }
    )
    assert fake_secret not in repr(settings)
    assert private_path not in repr(settings)
    app = create_app(settings)

    def raise_synthetic_error(_request: Request):
        raise RuntimeError(f"{fake_secret} {fake_iban} {private_path}")

    app.add_route("/api/v1/security-test-error", raise_synthetic_error)
    caplog.set_level(logging.ERROR)
    with TestClient(app) as client:
        responses = [
            client.get("/api/v1/health"),
            client.get("/openapi.json"),
            client.get("/api/v1/security-test-error"),
        ]
        injected_input = client.post(
            "/api/v1/imports/preview",
            files={"file": ("synthetic.csv", f"{fake_secret},{fake_iban}".encode(), "text/csv")},
        )

    output = "\n".join(response.text for response in [*responses, injected_input]) + caplog.text
    assert all(response.status_code in {200, 500} for response in responses)
    assert injected_input.status_code != 403
    assert fake_secret not in output
    assert fake_iban not in output
    assert private_path not in output
    assert responses[-1].json()["error"]["code"] == "INTERNAL_ERROR"


@pytest.mark.integration
def test_SEC_05_llm_disabled_import_and_transaction_reads_make_no_external_calls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.app.state.settings.llm_enabled is False
    upgrade_to_head(client.app.state.settings)
    ensure_hmac_key(client.app.state.db_engine)
    attempted_external_hosts: list[str] = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def is_loopback_address(address) -> tuple[str, bool]:
        host = str(address[0] if isinstance(address, tuple) else address)
        try:
            is_loopback = ip_address(host).is_loopback
        except ValueError:
            is_loopback = host.lower() == "localhost"
        return host, is_loopback

    def block_external_connect(sock, address):
        host, is_loopback = is_loopback_address(address)
        if is_loopback:
            return original_connect(sock, address)
        attempted_external_hosts.append(host)
        raise OSError("external network disabled in SEC-05 test")

    def block_external_connect_ex(sock, address):
        host, is_loopback = is_loopback_address(address)
        if is_loopback:
            return original_connect_ex(sock, address)
        attempted_external_hosts.append(host)
        return errno.EACCES

    monkeypatch.setattr(socket.socket, "connect", block_external_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", block_external_connect)
    transactions = client.get("/api/v1/transactions")
    preview = client.post(
        "/api/v1/imports/preview",
        files={"file": ("synthetic.csv", b"not a bank statement", "text/csv")},
    )

    assert transactions.status_code < 500, (transactions.text, attempted_external_hosts)
    assert preview.status_code < 500
    assert attempted_external_hosts == []
