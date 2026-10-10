"""Exact parsing of French formatted amounts into signed integer cents."""

from __future__ import annotations

import re

from app.importers.types import AnomalyCode

_GROUPING_SPACES = " \u00a0\u202f\u2009"
MAX_AMOUNT_CENTS = 10**13
_MAX_EUROS_TEXT = "100000000000"
_AMOUNT_RE = re.compile(
    rf"^(?P<sign>[+-]?)(?P<euros>[0-9]+|[0-9]{{1,3}}(?:[{_GROUPING_SPACES}][0-9]{{3}})+)"
    r"(?:,(?P<cents>[0-9]{2}))?$"
)


def parse_french_amount(value: str) -> int | AnomalyCode:
    """Parse an amount such as ``2 811,82`` without float or Decimal."""

    if type(value) is not str:
        return AnomalyCode.NON_NUMERIC_AMOUNT
    stripped = value.strip()
    if not stripped:
        return AnomalyCode.NON_NUMERIC_AMOUNT
    match = _AMOUNT_RE.fullmatch(stripped)
    if match is None:
        return AnomalyCode.NON_NUMERIC_AMOUNT
    euros_text = match.group("euros")
    if any(separator in euros_text for separator in _GROUPING_SPACES):
        separators = {char for char in euros_text if char in _GROUPING_SPACES}
        if len(separators) != 1:
            return AnomalyCode.NON_NUMERIC_AMOUNT
        euros_text = euros_text.translate(str.maketrans("", "", _GROUPING_SPACES))
    significant_euros = euros_text.lstrip("0") or "0"
    cents_text = match.group("cents") or "00"
    if len(significant_euros) > len(_MAX_EUROS_TEXT) or (
        len(significant_euros) == len(_MAX_EUROS_TEXT)
        and (significant_euros > _MAX_EUROS_TEXT or cents_text != "00")
    ):
        return AnomalyCode.AMOUNT_OUT_OF_RANGE
    try:
        cents = int(significant_euros) * 100 + int(cents_text)
    except (ValueError, OverflowError):
        return AnomalyCode.NON_NUMERIC_AMOUNT
    if cents > MAX_AMOUNT_CENTS:
        return AnomalyCode.AMOUNT_OUT_OF_RANGE
    return -cents if match.group("sign") == "-" else cents


def parse_debit_credit(debit: str, credit: str) -> int | AnomalyCode:
    """Convert CA debit/credit columns to a signed, nonzero cent amount."""

    if type(debit) is not str or type(credit) is not str:
        return AnomalyCode.NON_NUMERIC_AMOUNT
    debit_present = bool(debit.strip())
    credit_present = bool(credit.strip())
    if debit_present and credit_present:
        return AnomalyCode.BOTH_AMOUNTS
    if not debit_present and not credit_present:
        return AnomalyCode.ZERO_AMOUNT
    parsed = parse_french_amount(debit if debit_present else credit)
    if isinstance(parsed, AnomalyCode):
        return parsed
    if parsed < 0:
        return AnomalyCode.NEGATIVE_AMOUNT
    if parsed == 0:
        return AnomalyCode.ZERO_AMOUNT
    return -parsed if debit_present else parsed
