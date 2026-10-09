"""In-memory canonical account identity and keyed fingerprint helpers."""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
from dataclasses import dataclass

from app.importers.types import AnomalyCode

_ALLOWED_WHITESPACE = " \t\u00a0\u202f\u2009"


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalAccountNumber:
    """Canonical account number whose string representations are always redacted."""

    _value: str

    @property
    def value(self) -> str:
        """Return the canonical value for in-memory matching and HMAC only."""

        return self._value

    def __repr__(self) -> str:
        return "CanonicalAccountNumber(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"

    def __format__(self, format_spec: str) -> str:
        del format_spec
        return "<redacted>"


def _digit(character: str) -> str | None:
    if unicodedata.category(character) != "Nd":
        return None
    try:
        return str(unicodedata.decimal(character))
    except (TypeError, ValueError):
        return None


def canonicalize_account_number(
    raw: str,
) -> CanonicalAccountNumber | AnomalyCode:
    """Apply the narrow account-number canonicalization from IMPORT_CONTRACT."""

    if type(raw) is not str:
        return AnomalyCode.INVALID_ACCOUNT_NUMBER
    token = raw.strip(_ALLOWED_WHITESPACE)
    if not token:
        return AnomalyCode.INVALID_ACCOUNT_NUMBER
    output: list[str] = []
    index = 0
    while index < len(token):
        character = token[index]
        digit = _digit(character)
        if digit is not None:
            output.append(digit)
            index += 1
            continue
        if character in _ALLOWED_WHITESPACE:
            start = index
            while index < len(token) and token[index] in _ALLOWED_WHITESPACE:
                index += 1
            if (
                start == 0
                or index == len(token)
                or _digit(token[start - 1]) is None
                or _digit(token[index]) is None
            ):
                return AnomalyCode.INVALID_ACCOUNT_NUMBER
            continue
        if character == "-":
            if (
                index == 0
                or index + 1 == len(token)
                or _digit(token[index - 1]) is None
                or _digit(token[index + 1]) is None
            ):
                return AnomalyCode.INVALID_ACCOUNT_NUMBER
            index += 1
            continue
        return AnomalyCode.INVALID_ACCOUNT_NUMBER
    if not output:
        return AnomalyCode.INVALID_ACCOUNT_NUMBER
    return CanonicalAccountNumber("".join(output))


def bank_fingerprint(hmac_key: bytes, canonical: CanonicalAccountNumber) -> str:
    """Return a domain-separated HMAC-SHA-256 fingerprint as lowercase hex."""

    if type(hmac_key) is not bytes or not isinstance(canonical, CanonicalAccountNumber):
        raise TypeError("invalid fingerprint input")
    message = b"credit-agricole\0" + canonical.value.encode("utf-8")
    return hmac.new(hmac_key, message, hashlib.sha256).hexdigest()
