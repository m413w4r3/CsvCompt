import logging

import pytest

import app.__main__ as backend_main
from app.core.config import Settings
from app.core.logging import RedactionFilter, redact_text


@pytest.mark.unit
def test_filter_redacts_secrets_iban_and_long_numbers() -> None:
    iban = "FR" + "76" + "A1" * 10
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "llm_api_key=%s authorization=%s iban=%s ref=%s",
        ("sk-test-secret-value", "Bearer secret-token", iban, "12345678"),
        None,
    )
    assert RedactionFilter().filter(record)
    rendered = record.getMessage()
    assert "test-secret-value" not in rendered
    assert "secret-token" not in rendered
    assert iban not in rendered
    assert "12345678" not in rendered


@pytest.mark.unit
def test_redaction_masks_api_key_fields_and_bearer_tokens() -> None:
    value = redact_text("api_key=private authorization:Bearer abcdefghijklmnop")
    assert "private" not in value
    assert "abcdefghijklmnop" not in value


@pytest.mark.unit
def test_httpx_access_message_does_not_keep_request_url() -> None:
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        'HTTP Request: GET http://127.0.0.1/private/path?q=secret "HTTP/1.1 200 OK"',
        (),
        None,
    )
    RedactionFilter().filter(record)
    assert "/private/path" not in record.getMessage()
    assert "q=secret" not in record.getMessage()


@pytest.mark.unit
def test_uvicorn_access_logs_are_disabled_and_log_config_redacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(backend_main, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(
        backend_main.uvicorn,
        "run",
        lambda *args, **kwargs: captured.update({"args": args, **kwargs}),
    )

    backend_main.main()

    assert captured["access_log"] is False
    log_config = captured["log_config"]
    assert isinstance(log_config, dict)
    assert log_config["handlers"]["access"]["filters"] == ["redaction"]
    assert log_config["filters"]["redaction"]["()"] is RedactionFilter
    assert captured["args"] == ("app.main:create_app",)
    assert captured["factory"] is True
