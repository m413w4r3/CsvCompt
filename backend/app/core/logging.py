"""Journalisation structurée avec rédaction des valeurs sensibles."""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(?:llm_api_key|api[_-]?key|authorization)\b\s*[:=]\s*"
            r"(?:Bearer\s+)?[^\s,;]+"
        ),
        "[REDACTED_FIELD]",
    ),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b"), "[REDACTED_IBAN]"),
    (re.compile(r"\d{8,}"), "[REDACTED_NUMBER]"),
)
_HTTPX_REQUEST_URL = re.compile(r'^(HTTP Request: \w+)\s+\S+(\s+"HTTP/[^\"]+")$')


def redact_text(value: str) -> str:
    """Masque les secrets, identifiants bancaires et longues suites numériques."""
    result = value
    for pattern, replacement in _REDACTIONS:
        result = pattern.sub(replacement, result)
    return result


class RedactionFilter(logging.Filter):
    """Rédige le message formaté avant qu'un handler ne l'écrive."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = "[message de log indisponible]"
        if record.name == "httpx":
            message = _HTTPX_REQUEST_URL.sub(r"\1 [URL REDACTED]\2", message)
        record.msg = redact_text(message)
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    """Émet une ligne JSON stable sans attributs de requête ou de secret."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time_utc": datetime.fromtimestamp(record.created, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))


def configure_logging() -> None:
    """Configure les loggers applicatifs sans journaliser de corps HTTP."""
    root = logging.getLogger()
    if not root.handlers:
        handler: logging.Handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    for handler in root.handlers:
        if not any(isinstance(item, RedactionFilter) for item in handler.filters):
            handler.addFilter(RedactionFilter())
        handler.setFormatter(JsonFormatter())
    root.setLevel(logging.INFO)


def uvicorn_log_config() -> dict[str, Any]:
    """Configuration Uvicorn structurée; filtre également les loggers serveur."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"redaction": {"()": RedactionFilter}},
        "formatters": {"json": {"()": JsonFormatter}},
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "filters": ["redaction"],
                "stream": "ext://sys.stderr",
            },
            "access": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "filters": ["redaction"],
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["access"],
                "level": "INFO",
                "propagate": False,
            },
        },
        "root": {"handlers": ["default"], "level": "INFO"},
    }
