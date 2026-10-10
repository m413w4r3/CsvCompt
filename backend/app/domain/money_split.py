"""Exact integer-cent splits using the rounding contract in ADR-007."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

MoneyParts = Iterable[tuple[str, int]] | Mapping[str, int]


class MoneySplitError(ValueError):
    """A split cannot be represented without losing or reversing cents."""


def _strict_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise MoneySplitError(f"{name} must be a strict integer")
    return value


def _as_pairs(parts: MoneyParts) -> list[tuple[str, int]]:
    raw_parts = list(parts.items()) if isinstance(parts, Mapping) else list(parts)
    normalized: list[tuple[str, int]] = []
    for item in raw_parts:
        try:
            category_id, value = item
        except (TypeError, ValueError) as error:
            raise MoneySplitError("each part must be a (category_id, integer) pair") from error
        if not isinstance(category_id, str) or not category_id:
            raise MoneySplitError("category_id must be a non-empty string")
        normalized.append((category_id, _strict_int(value, "part value")))
    if not normalized:
        raise MoneySplitError("at least one part is required")
    return normalized


def _round_ratio_half_away(numerator: int, denominator: int) -> int:
    """Round an exact rational to the nearest integer, ties away from zero."""
    if denominator <= 0:
        raise MoneySplitError("denominator must be positive")
    sign = -1 if numerator < 0 else 1
    magnitude = abs(numerator)
    return sign * ((2 * magnitude + denominator) // (2 * denominator))


def _merge(parts: list[tuple[str, int]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for category_id, value in parts:
        merged[category_id] = merged.get(category_id, 0) + value
    return merged


def _validate_signed_result(amount_cents: int, result: Mapping[str, int]) -> None:
    if any(value == 0 or (value > 0) != (amount_cents > 0) for value in result.values()):
        raise MoneySplitError("split contains a zero part or a part with the wrong sign")
    if sum(result.values()) != amount_cents:
        raise MoneySplitError("split does not sum exactly to the source amount")


def split_by_basis_points(amount_cents: int, parts: MoneyParts) -> dict[str, int]:
    """Split signed cents by positive basis-point weights.

    Duplicate category IDs are merged before ordering and calculation. Categories
    are processed by descending merged weight, then ascending ID. Every result
    except the last is rounded half away from zero; the last receives the exact
    signed remainder. The whole split is rejected if any part is zero or reverses
    the source sign.
    """
    amount = _strict_int(amount_cents, "amount_cents")
    if amount == 0:
        raise MoneySplitError("amount_cents must be non-zero")
    parsed = _as_pairs(parts)
    if any(weight <= 0 for _, weight in parsed):
        raise MoneySplitError("basis-point weights must be positive")
    merged_weights = _merge(parsed)
    if sum(merged_weights.values()) != 10_000:
        raise MoneySplitError("basis-point weights must total exactly 10000")

    order = sorted(
        merged_weights, key=lambda category_id: (-merged_weights[category_id], category_id)
    )
    remaining = amount
    result: dict[str, int] = {}
    for index, category_id in enumerate(order):
        if index == len(order) - 1:
            part_cents = remaining
        else:
            part_cents = _round_ratio_half_away(amount * merged_weights[category_id], 10_000)
            remaining -= part_cents
        result[category_id] = part_cents

    _validate_signed_result(amount, result)
    return result


def split_by_amounts(amount_cents: int, parts: MoneyParts) -> dict[str, int]:
    """Validate and merge an exact signed-cent split by category."""
    amount = _strict_int(amount_cents, "amount_cents")
    if amount == 0:
        raise MoneySplitError("amount_cents must be non-zero")
    parsed = _as_pairs(parts)
    if any(value == 0 or (value > 0) != (amount > 0) for _, value in parsed):
        raise MoneySplitError("each amount part must be non-zero and match the source sign")
    merged = _merge(parsed)
    if sum(merged.values()) != amount:
        raise MoneySplitError("amount parts must sum exactly to amount_cents")
    result = {category_id: merged[category_id] for category_id in sorted(merged)}
    _validate_signed_result(amount, result)
    return result


def prorate_link(amount_cents: int, allocations: MoneyParts) -> dict[str, int]:
    """Attribute a positive refund link across expense categories by exact ratio.

    ``allocations`` contains ``(category_id, negative_expense_cents)`` pairs.
    Repeated category IDs are aggregated by absolute amount. Zero attribution for
    a category is valid for very small refunds; the sum always equals the link.
    """
    amount = _strict_int(amount_cents, "amount_cents")
    if amount <= 0:
        raise MoneySplitError("refund link amount must be positive")
    parsed = _as_pairs(allocations)
    if any(value >= 0 for _, value in parsed):
        raise MoneySplitError("proration weights must be negative expense amounts")
    weights = _merge([(category_id, abs(value)) for category_id, value in parsed])
    denominator = sum(weights.values())
    if denominator <= 0:
        raise MoneySplitError("at least one non-zero expense allocation is required")

    order = sorted(weights, key=lambda category_id: (-weights[category_id], category_id))
    remaining = amount
    result: dict[str, int] = {}
    for index, category_id in enumerate(order):
        if index == len(order) - 1:
            part_cents = remaining
        else:
            part_cents = _round_ratio_half_away(amount * weights[category_id], denominator)
            remaining -= part_cents
        result[category_id] = part_cents
    if sum(result.values()) != amount:
        raise MoneySplitError("prorated refund does not sum exactly to the link amount")
    return result
