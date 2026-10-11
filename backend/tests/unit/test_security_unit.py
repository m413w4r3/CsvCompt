from __future__ import annotations

import ipaddress
import logging
import re
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError
from starlette.datastructures import Headers

import app.__main__ as backend_main
from app.core.config import PROJECT_ROOT, Settings
from app.core.logging import configure_logging
from app.core.security import DEFAULT_ALLOWED_ORIGINS, LocalSecurityMiddleware, normalize_origin


def _is_loopback_host(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


@pytest.mark.unit
def test_SEC_01_default_bind_and_cors_origins_are_loopback() -> None:
    settings = Settings(_env_file=None)
    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 9513
    assert settings.llm_enabled is False
    assert settings.cors_allowed_origins == list(DEFAULT_ALLOWED_ORIGINS)
    assert all(
        _is_loopback_host(urlsplit(origin).hostname or "")
        for origin in settings.cors_allowed_origins
    )


@pytest.mark.unit
def test_SEC_01_non_loopback_bind_requires_explicit_opt_in() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings(app_host="0.0.0.0", _env_file=None)
    assert (
        Settings(app_host="0.0.0.0", allow_non_loopback=True, _env_file=None).app_host == "0.0.0.0"
    )


@pytest.mark.unit
def test_SEC_01_server_binds_to_loopback_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)
    captured: dict[str, object] = {}
    monkeypatch.setattr(backend_main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        backend_main.uvicorn, "run", lambda *args, **kwargs: captured.update(kwargs)
    )

    backend_main.main()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9513


@pytest.mark.unit
def test_SEC_01_non_loopback_opt_in_logs_a_safe_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(app_host="0.0.0.0", allow_non_loopback=True, _env_file=None)
    warnings: list[str] = []
    monkeypatch.setattr(backend_main, "get_settings", lambda: settings)
    monkeypatch.setattr(backend_main.uvicorn, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backend_main.logger, "warning", warnings.append)

    backend_main.main()

    assert len(warnings) == 1
    assert "bind externe n'est pas supporté en v1" in warnings[0]
    assert "0.0.0.0" not in warnings[0]


@pytest.mark.unit
def test_SEC_02_cors_rejects_wildcard_and_external_origins() -> None:
    for origins in ([], ["*"], ["https://example.invalid"], ["http://127.0.0.1:5173/path"]):
        with pytest.raises(ValidationError):
            Settings(cors_allowed_origins=origins, _env_file=None)


@pytest.mark.unit
def test_SEC_02_referer_origin_parser_ignores_path_but_checks_origin() -> None:
    assert normalize_origin("http://localhost:5173/transactions?q=private", allow_path=True) == (
        "http://localhost:5173"
    )
    assert normalize_origin("https://evil.invalid/path", allow_path=True) == "https://evil.invalid"
    assert normalize_origin("null") is None
    assert normalize_origin("http://localhost:5173/path") is None


@pytest.mark.unit
def test_SEC_04_injected_secret_and_iban_are_redacted_from_captured_logs(caplog) -> None:
    configure_logging()
    caplog.set_level(logging.INFO)
    fake_secret = "sk-test-only-secret-value"
    fake_iban = "FR" + "76" + "A1" * 10

    logging.getLogger("security.test").warning(
        "input llm_api_key=%s iban=%s", fake_secret, fake_iban
    )

    assert fake_secret not in caplog.text
    assert fake_iban not in caplog.text


@pytest.mark.unit
def test_SEC_05_no_external_domain_is_hardcoded_in_application_sources() -> None:
    source_roots = (PROJECT_ROOT / "backend/app", PROJECT_ROOT / "frontend/src")
    frontend_entrypoints = (
        PROJECT_ROOT / "frontend/index.html",
        PROJECT_ROOT / "frontend/vite.config.ts",
        PROJECT_ROOT / "frontend/playwright.config.ts",
        PROJECT_ROOT / "frontend/eslint.config.js",
    )
    url_pattern = re.compile(
        r"(?i)(?<![:/])(?:https?:)?//([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::\d+)?)"
        r"(?=[/?#\s\"'`<>),;]|$)"
    )
    violations: list[str] = []
    source_files = [
        path
        for root in source_roots
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".html"}
    ]
    source_files.extend(path for path in frontend_entrypoints if path.is_file())
    for path in source_files:
        source = path.read_text(encoding="utf-8")
        for match in url_pattern.finditer(source):
            authority = match.group(1)
            try:
                hostname = urlsplit(f"http://{authority}").hostname
            except ValueError:
                hostname = None
            if hostname is None or not _is_loopback_host(hostname):
                violations.append(f"{path.relative_to(PROJECT_ROOT)}: {authority}")
    assert not violations, "URL externe codée en dur : " + ", ".join(violations)


@pytest.mark.unit
def test_SEC_02_missing_mutation_source_is_not_trusted() -> None:
    middleware = LocalSecurityMiddleware(
        app=lambda _scope, _receive, _send: None,
        allowed_origins=DEFAULT_ALLOWED_ORIGINS,
        bind_host="127.0.0.1",
        bind_port=9513,
    )
    headers = Headers(raw=[(b"host", b"127.0.0.1:9513")])
    assert middleware._source_origin(headers) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("[::1]:9513", True),
        ("[::1]", True),
        ("LOCALHOST:9513", True),
        ("localhost.:9513", False),
        ("127.0.0.1.evil.com:9513", False),
        ("user@localhost:9513", False),
        ("localhost:9999", False),
    ],
)
def test_SEC_01_allowed_host_parser_normalizes_ipv6_and_rejects_host_confusion(
    host: str, allowed: bool
) -> None:
    middleware = LocalSecurityMiddleware(
        app=lambda _scope, _receive, _send: None,
        allowed_origins=DEFAULT_ALLOWED_ORIGINS,
        bind_host="127.0.0.1",
        bind_port=9513,
    )
    assert middleware._host_is_allowed(host) is allowed
