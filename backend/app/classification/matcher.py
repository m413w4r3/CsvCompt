"""Exact payee identity helpers; fuzzy label equality is audit-only."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache

from app.importers.normalize import (
    INTERMEDIARY_SIGNAL_TOKENS,
    LabelParts,
    identity_label,
    search_label,
)

# Reviewed configuration: these names identify marketplaces where one merchant
# can represent unrelated baskets. They are never category rules.
AMBIGUOUS_MARKETPLACES = (
    "Amazon",
    "Vinted",
    "AliExpress",
    "Temu",
    "PayPal",
    "eBay",
    "Klarna",
    "Alma",
)

_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", re.IGNORECASE)
_LONG_DIGITS_RE = re.compile(r"(?<!\d)(?:\d[\s-]?){7,}\d(?!\d)")
_INTERMEDIARY_SEARCH_TOKENS = tuple(
    str(search_label(token)) for token in INTERMEDIARY_SIGNAL_TOKENS
)
_MARKETPLACE_SEARCH_TOKENS = tuple(str(search_label(token)) for token in AMBIGUOUS_MARKETPLACES)


@dataclass(frozen=True, slots=True)
class PayeeKey:
    """Stable exact key and safe display hints derived from a parsed CA label."""

    normalized_name: str
    merchant_name: str
    source_kind: str
    operation_type: str
    entity_kind: str
    is_intermediary: bool
    ambiguous: bool


@lru_cache(maxsize=8192)
def derive_payee_key(label_parts: LabelParts) -> PayeeKey | None:
    """Build a key only from exact, safe CA merchant/creditor signals.

    The strict identity normalizer preserves case, accents, punctuation and
    digits. Search normalization is used only to compare against reviewed
    signal lists; it never becomes part of the key.
    """

    if label_parts.operation_type == "Paiement par carte":
        source_kind = "card"
        merchant_name = label_parts.merchant_display_hint or label_parts.merchant_raw_hint
        expected_prefix = "card:"
    elif label_parts.operation_type == "Prélèvement":
        source_kind = "creditor"
        merchant_name = label_parts.creditor_hint
        expected_prefix = "creditor:"
    else:
        # Withdrawals, private transfers, refunds, loan payments and unknown
        # operations have no reliable merchant identity in the current parser.
        return None

    if not merchant_name and label_parts.merchant_key_candidate:
        candidate = label_parts.merchant_key_candidate
        if candidate.startswith(expected_prefix):
            merchant_name = candidate[len(expected_prefix) :]
    if not isinstance(merchant_name, str):
        return None

    exact_name = identity_label(merchant_name).value
    if not exact_name or _contains_sensitive_identifier(exact_name):
        return None

    searchable_name = str(search_label(exact_name))
    is_intermediary = label_parts.is_intermediary_candidate or _matches_any(
        searchable_name, _INTERMEDIARY_SEARCH_TOKENS
    )
    ambiguous = not is_intermediary and (
        label_parts.ambiguous_marketplace
        or _matches_any(searchable_name, _MARKETPLACE_SEARCH_TOKENS)
    )
    entity_kind = "intermediary" if is_intermediary else "marketplace" if ambiguous else "merchant"

    # Canonical JSON avoids delimiter collisions while keeping the exact name
    # inspectable for key migrations. No card suffix or reference is included.
    normalized_name = json.dumps(
        {
            "entity_kind": entity_kind,
            "merchant_name": exact_name,
            "operation_type": label_parts.operation_type,
            "source_kind": source_kind,
            "version": 1,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return PayeeKey(
        normalized_name=normalized_name,
        merchant_name=exact_name,
        source_kind=source_kind,
        operation_type=label_parts.operation_type,
        entity_kind=entity_kind,
        is_intermediary=is_intermediary,
        ambiguous=ambiguous,
    )


def key_metadata(normalized_name: str) -> dict[str, object] | None:
    """Decode only keys written by this version; legacy text stays opaque."""

    try:
        payload = json.loads(normalized_name)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    if not all(
        isinstance(payload.get(field), str)
        for field in ("entity_kind", "merchant_name", "operation_type", "source_kind")
    ):
        return None
    return payload


def _matches_any(searchable_name: str, values: tuple[str, ...]) -> bool:
    padded_name = f" {searchable_name} "
    return any(f" {value} " in padded_name for value in values)


def _contains_sensitive_identifier(value: str) -> bool:
    compact = "".join(
        character for character in value if not character.isspace() and character != "-"
    )
    return bool(_IBAN_RE.search(compact) or _LONG_DIGITS_RE.search(value))
