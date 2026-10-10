"""Canonical JSON hashing for immutable review snapshots."""

from __future__ import annotations

import hashlib
import json


def canonical_json(value: object) -> str:
    """Serialize JSON deterministically so formatting cannot change its digest."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def snapshot_sha256(raw: str | None) -> str:
    """Hash parsed JSON canonically, treating a SQL NULL snapshot as JSON null."""
    if raw is None:
        return canonical_sha256(None)
    value = json.loads(raw)
    return canonical_sha256(value)
