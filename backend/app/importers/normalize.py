"""Pure, conservative label representations and Crédit Agricole hints.

This module never changes the source label, classifies an operation, or merges
merchant identities. The fuzzy representation is for human-facing search only.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

MAX_LABEL_CHARS = 20_000
"""Maximum NFC characters accepted by the T09 parser for an operation label."""


class LabelWarning(StrEnum):
    """Fixed warning codes; warning messages never include source text."""

    CF_CHARACTER = "CF_CHARACTER"
    INVALID_INPUT = "INVALID_INPUT"


class _WarnedText(str):
    warnings: tuple[str, ...]

    def __new__(cls, value: str, warnings: Iterable[str] = ()) -> _WarnedText:
        instance = super().__new__(cls, value)
        instance.warnings = tuple(dict.fromkeys(warnings))
        return instance

    @property
    def value(self) -> str:
        """Return the string value for callers preferring an explicit field."""

        return str.__str__(self)


class IdentityLabel(_WarnedText):
    """A `str` identity value that also carries non-destructive warnings."""


class SearchLabel(_WarnedText):
    """A fuzzy search value, explicitly distinct from an identity label."""


@dataclass(frozen=True, slots=True)
class NormalizedLabel:
    """Keep the exact source object beside the two derived representations."""

    raw_label: str = field(repr=False)
    identity_label: IdentityLabel = field(repr=False)
    search_label: SearchLabel = field(repr=False)
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LabelParts:
    """Conservative structured hints from a multi-line CA operation label."""

    operation_type: str
    card_suffix: str | None = None
    merchant_raw_hint: str | None = None
    merchant_display_hint: str | None = None
    creditor_hint: str | None = None
    reference_present: bool | None = None
    is_intermediary_candidate: bool = False
    is_cash_withdrawal_candidate: bool = False
    is_loan_payment_candidate: bool = False
    ambiguous_marketplace: bool = False
    merchant_key_candidate: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OperationIdentity:
    """The strict identity dimensions needed to audit near-duplicate labels."""

    account_id: str
    booked_on: date | str
    amount_cents: int
    identity_label: str


@dataclass(frozen=True, slots=True)
class NearDuplicateCandidate:
    """A pair for human audit only; no transaction is merged or removed."""

    left: OperationIdentity
    right: OperationIdentity


_WHITE_SPACE_SINGLETONS = frozenset(
    {
        0x0020,
        0x0085,
        0x00A0,
        0x1680,
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
    }
)
_WHITE_SPACE_RANGES = ((0x0009, 0x000D), (0x2000, 0x200A))

_OPERATION_TYPES = {
    "paiement par carte": "Paiement par carte",
    "prelevement": "Prélèvement",
    "virement en votre faveur": "Virement en votre faveur",
    "virement emis": "Virement émis",
    "remboursement de pret": "Remboursement de prêt",
    "avoir": "Avoir",
    "reglement": "Règlement",
    "cotisation": "Cotisation",
    "retrait au distributeur": "Retrait au distributeur",
}

_CARD_PREFIX_RE = re.compile(r"^\s*X(\d{4})(?=\s|$)", re.IGNORECASE)
_CARD_DATE_RE = re.compile(r"(?<!\d)(?:0[1-9]|[12]\d|3[01])/(?:0[1-9]|1[0-2])(?!\d)")
_DISPLAY_SEPARATOR_RE = re.compile(r"\s+-\s+")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", re.IGNORECASE)
_LONG_NUMBER_RE = re.compile(r"(?<!\d)\d{8,}(?!\d)")
_REFERENCE_MARKER_RE = re.compile(r"\b(?:SEPA|MANDAT|ICS|RUM|IBAN|REFERENCE|REF)\b", re.IGNORECASE)

# Deliberately configurable by changing this tuple as reviewed signals evolve.
# These are review flags, not merchant aliases or automatic matching rules.
INTERMEDIARY_SIGNAL_TOKENS = (
    "CARREFOUR BANQUE",
    "PRLV COMPTANT",
    "COFIDIS",
    "CETELEM",
    "SOFINCO",
    "ONEY",
    "FLOA BANK",
    "ORGANISME DE CREDIT",
    "CREDIT RENOUVELABLE",
)

# Ambiguous platforms/intermediaries need a human decision before any broad rule.
AMBIGUOUS_MARKETPLACE_TOKENS = (
    "AMAZON",
    "VINTED",
    "PAYPAL",
    "CARREFOUR BANQUE",
    "KLARNA",
    "ALMA",
    "ONEY",
)


def raw_label(raw: str) -> str:
    """Return the original decoded label unchanged, including the same object."""

    return raw


def identity_label(raw: str) -> IdentityLabel:
    """Apply the strict import identity rule: NFC, Unicode White_Space, trim.

    The T09 parser enforces raw and NFC bounds before this function is used for
    persisted identities. This function never clips an identity. Unicode
    category ``Cf`` is preserved and reported as ``CF_CHARACTER``.
    """

    source, warnings = _source_and_warnings(raw)
    normalized = unicodedata.normalize("NFC", source)
    chars: list[str] = []
    previous_was_space = False
    for character in normalized:
        if _is_white_space(character):
            if chars and not previous_was_space:
                chars.append(" ")
            previous_was_space = True
        else:
            chars.append(character)
            previous_was_space = False
    value = "".join(chars).strip(" ")
    return IdentityLabel(value, warnings)


def search_label(raw: str) -> SearchLabel:
    """Return a lossy, human-search representation; never use as identity."""

    source, warnings = _source_and_warnings(raw)
    decomposed = unicodedata.normalize("NFKD", source.casefold())
    chars: list[str] = []
    previous_was_space = False
    for character in decomposed:
        category = unicodedata.category(character)
        if category.startswith("M"):
            continue
        if _is_white_space(character) or category.startswith(("P", "C")):
            if chars and not previous_was_space:
                chars.append(" ")
            previous_was_space = True
        else:
            chars.append(character)
            previous_was_space = False
    value = "".join(chars).strip(" ")
    return SearchLabel(value, warnings)


def normalize_label(raw: str) -> NormalizedLabel:
    """Return raw, strict identity, and fuzzy search values as separate fields."""

    identity = identity_label(raw)
    search = search_label(raw)
    warnings = tuple(dict.fromkeys((*identity.warnings, *search.warnings)))
    return NormalizedLabel(raw, identity, search, warnings)


def parse_ca_label(raw: str) -> LabelParts:
    """Extract reversible CA hints without assigning category, flow, or merchant.

    Only the four digit card suffix pattern is captured. References are reduced
    to a boolean. For arbitrary non-string inputs, the parser fails closed and
    returns fixed warning codes without echoing source values.
    """

    source, warnings = _source_and_warnings(raw)
    lines = source.splitlines()
    type_index: int | None = None
    operation_type = "unknown"
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        recognized_type = _OPERATION_TYPES.get(search_label(line))
        if recognized_type is not None:
            type_index = index
            operation_type = recognized_type
            break

    card_suffix: str | None = None
    merchant_raw_hint: str | None = None
    merchant_display_hint: str | None = None
    creditor_hint: str | None = None
    reference_present: bool | None = None
    merchant_key_candidate: str | None = None

    following_lines = lines[type_index + 1 :] if type_index is not None else []
    if operation_type == "Paiement par carte":
        card_line = next((line for line in following_lines if line.strip()), None)
        if card_line is not None:
            card_match = _CARD_PREFIX_RE.match(card_line)
            if card_match is not None:
                card_suffix = f"X{card_match.group(1)}"
                after_card = card_line[card_match.end() :]
                date_match = _CARD_DATE_RE.search(after_card)
                if date_match is not None:
                    merchant_raw_hint = after_card[: date_match.start()].strip()
                    after_date = after_card[date_match.end() :]
                    display_separator = _DISPLAY_SEPARATOR_RE.search(after_date)
                    if display_separator is not None:
                        display = after_date[display_separator.end() :].strip()
                        if display:
                            merchant_display_hint = display

    if operation_type == "Prélèvement":
        creditor_line_index = next(
            (
                index
                for index, line in enumerate(lines)
                if index > (type_index if type_index is not None else -1) and line.strip()
            ),
            None,
        )
        reference_present = False
        if creditor_line_index is not None:
            creditor_line = lines[creditor_line_index]
            creditor_piece = creditor_line.split(" - ", maxsplit=1)[0].strip()
            if creditor_piece:
                creditor_hint = creditor_piece
                creditor_segments = creditor_line.split(" - ")
                if len(creditor_segments) >= 3 and creditor_segments[-1].strip():
                    reference_present = True
            if any(_looks_like_reference(line) for line in lines[creditor_line_index + 1 :]):
                reference_present = True
    merchant_raw_hint = _safe_hint(merchant_raw_hint)
    merchant_display_hint = _safe_hint(merchant_display_hint)
    creditor_hint = _safe_hint(creditor_hint)
    if operation_type == "Paiement par carte":
        merchant_key_source = merchant_raw_hint or merchant_display_hint
        if merchant_key_source:
            merchant_key_candidate = f"card:{identity_label(merchant_key_source)}"
    elif operation_type == "Prélèvement" and creditor_hint:
        merchant_key_candidate = f"creditor:{identity_label(creditor_hint)}"

    full_search = str(search_label(source))
    intermediary_candidate = _contains_configured_phrase(full_search, INTERMEDIARY_SIGNAL_TOKENS)
    ambiguous_marketplace = _contains_configured_phrase(full_search, AMBIGUOUS_MARKETPLACE_TOKENS)
    loan_candidate = operation_type == "Remboursement de prêt" or _contains_configured_phrase(
        full_search,
        ("REMBOURSEMENT DE PRET", "ECHEANCE PRET", "MENSUALITE PRET"),
    )
    cash_withdrawal_candidate = (
        operation_type == "Retrait au distributeur"
        or _contains_configured_phrase(
            full_search, ("RETRAIT DAB", "RETRAIT AU DISTRIBUTEUR", "RETRAIT DISTRIBUTEUR")
        )
    )

    return LabelParts(
        operation_type=operation_type,
        card_suffix=card_suffix,
        merchant_raw_hint=merchant_raw_hint,
        merchant_display_hint=merchant_display_hint,
        creditor_hint=creditor_hint,
        reference_present=reference_present,
        is_intermediary_candidate=intermediary_candidate,
        is_cash_withdrawal_candidate=cash_withdrawal_candidate,
        is_loan_payment_candidate=loan_candidate,
        ambiguous_marketplace=ambiguous_marketplace,
        merchant_key_candidate=merchant_key_candidate,
        warnings=warnings,
    )


def near_duplicate_candidates(
    identities: Iterable[OperationIdentity],
) -> list[NearDuplicateCandidate]:
    """Report fuzzy-equal but strict-different labels in the same operation group."""

    groups: dict[tuple[str, date | str, int], list[OperationIdentity]] = {}
    for identity in identities:
        key = (identity.account_id, identity.booked_on, identity.amount_cents)
        groups.setdefault(key, []).append(identity)

    candidates: list[NearDuplicateCandidate] = []
    for group in groups.values():
        for left_index, left in enumerate(group):
            for right in group[left_index + 1 :]:
                if left.identity_label == right.identity_label:
                    continue
                if search_label(left.identity_label) == search_label(right.identity_label):
                    candidates.append(NearDuplicateCandidate(left=left, right=right))
    return candidates


def _source_and_warnings(raw: object) -> tuple[str, tuple[str, ...]]:
    """Safely unwrap text and collect fixed warnings without exposing its value."""

    if not isinstance(raw, str):
        return "", (LabelWarning.INVALID_INPUT.value,)
    source = str.__str__(raw)
    inherited = getattr(raw, "warnings", ())
    warnings = list(inherited if isinstance(inherited, tuple) else ())
    if any(unicodedata.category(character) == "Cf" for character in source):
        warnings.append(LabelWarning.CF_CHARACTER.value)
    return source, tuple(dict.fromkeys(warnings))


def _safe_hint(value: str | None) -> str | None:
    """Hide hints containing IBAN-like values or long digit sequences."""

    if value is None:
        return None
    compact = "".join(
        character for character in value if not _is_white_space(character) and character not in "-"
    )
    if _LONG_NUMBER_RE.search(compact) or _IBAN_RE.search(compact):
        return None
    return value


def _is_white_space(character: str) -> bool:
    codepoint = ord(character)
    return codepoint in _WHITE_SPACE_SINGLETONS or any(
        start <= codepoint <= end for start, end in _WHITE_SPACE_RANGES
    )


def _contains_configured_phrase(text: str, phrases: Iterable[str]) -> bool:
    padded_text = f" {text} "
    return any(f" {search_label(phrase)} " in padded_text for phrase in phrases)


def _looks_like_reference(text: str) -> bool:
    return bool(
        _IBAN_RE.search(text) or _LONG_NUMBER_RE.search(text) or _REFERENCE_MARKER_RE.search(text)
    )
