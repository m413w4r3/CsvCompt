import logging

import pytest

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
