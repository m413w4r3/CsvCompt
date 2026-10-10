"""Idempotency records and signed keyset cursors for import endpoints."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import IdempotencyRecord
from app.db.session import write_transaction


def request_fingerprint(http_method: str, canonical_path: str, body: dict[str, Any]) -> str:
    """Return a stable digest over the method, canonical path, and request JSON."""

    canonical = json.dumps(
        {"method": http_method.upper(), "path": canonical_path, "body": body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def find_replay(
    session: Session,
    *,
    idempotency_key: str,
    http_method: str,
    canonical_path: str,
    request_sha256: str,
    remove_expired: bool = False,
) -> tuple[int, dict[str, Any]] | None:
    """Return a saved response or raise a stable conflict for a reused key."""

    record = session.get(IdempotencyRecord, idempotency_key)
    if record is None:
        return None
    expires_at: str = str(
        session.execute(
            text("SELECT expires_at_utc FROM idempotency_records WHERE idempotency_key = :key"),
            {"key": idempotency_key},
        ).scalar_one()
    )
    if datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")) <= datetime.now(UTC):
        if remove_expired:
            session.delete(record)
        return None
    if (
        record.http_method != http_method.upper()
        or record.canonical_path != canonical_path
        or record.request_sha256 != request_sha256
    ):
        raise AppError(
            "IDEMPOTENCY_CONFLICT", 409, "Cette clé a déjà été utilisée pour une autre requête."
        )
    return record.response_status, json.loads(record.response_json)


def save_response(
    session: Session,
    *,
    idempotency_key: str,
    http_method: str,
    canonical_path: str,
    request_sha256: str,
    response_status: int,
    response: dict[str, Any],
) -> None:
    """Persist the exact safe response for seven-day retries."""

    session.add(
        IdempotencyRecord(
            idempotency_key=idempotency_key,
            http_method=http_method.upper(),
            canonical_path=canonical_path,
            request_sha256=request_sha256,
            response_status=response_status,
            response_json=json.dumps(
                response, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
        )
    )


def purge_expired_idempotency(engine: Engine) -> int:
    """Purge expired seven-day records at startup or on the next available startup."""

    with write_transaction(engine) as session:
        session.execute(
            text(
                "DELETE FROM idempotency_records WHERE expires_at_utc <= "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            )
        )
        return int(session.execute(text("SELECT changes()")).scalar_one())


def sign_cursor(payload: dict[str, Any], key: bytes) -> str:
    """Encode JSON cursor contents and authenticate them with the local HMAC key."""

    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=")
    signature = hmac.new(key, encoded, hashlib.sha256).digest()
    signature_text = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{encoded.decode('ascii')}.{signature_text}"


def verify_cursor(cursor: str, key: bytes, expected_filters: dict[str, Any]) -> dict[str, Any]:
    """Validate a signed cursor and ensure it belongs to the canonical filters."""

    try:
        encoded_part, signature_part = cursor.split(".", 1)
        encoded = encoded_part.encode("ascii")
        signature = base64.urlsafe_b64decode(signature_part + "=" * (-len(signature_part) % 4))
        expected = hmac.new(key, encoded, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        decoded = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
        payload = json.loads(decoded)
        if not isinstance(payload, dict) or payload.get("filters") != expected_filters:
            raise ValueError("filters")
        return payload
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise AppError("CURSOR_INVALID", 422, "Le curseur est invalide.") from error
