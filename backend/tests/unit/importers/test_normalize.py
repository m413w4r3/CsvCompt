from __future__ import annotations

from dataclasses import asdict
from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.importers.normalize import (
    MAX_LABEL_CHARS,
    LabelParts,
    LabelWarning,
    OperationIdentity,
    identity_label,
    near_duplicate_candidates,
    normalize_label,
    parse_ca_label,
    raw_label,
    search_label,
)

pytestmark = pytest.mark.unit


def test_IMP_08_raw_identity_keeps_original_and_case_distinctions() -> None:
    original = "  Paiement CAFÉ\u00a0X  "
    unchanged = raw_label(original)
    normalized = normalize_label(original)

    assert unchanged is original
    assert normalized.raw_label is original
    assert normalized.raw_label == original
    assert normalized.identity_label == "Paiement CAFÉ X"
    assert identity_label("RUE DES FLEURS") != identity_label("Rue des fleurs")
    assert identity_label("café") != identity_label("cafe")


def test_identity_label_nfc_unicode_whitespace_and_format_warning() -> None:
    result = identity_label("\u00a0Cafe\u0301\u2009\tligne\r\nfin\u00a0")

    assert result == "Café ligne fin"
    assert result.warnings == ()
    assert identity_label("avant\u200baprès") == "avant\u200baprès"
    assert LabelWarning.CF_CHARACTER.value in identity_label("avant\u200baprès").warnings
    assert LabelWarning.CF_CHARACTER.value in identity_label("avant\ufeffaprès").warnings


@given(st.text(max_size=300))
@settings(max_examples=150)
def test_identity_label_is_idempotent_trimmed_and_deterministic(raw: str) -> None:
    first = identity_label(raw)
    second = identity_label(first)

    assert first == second
    assert first.warnings == second.warnings
    assert identity_label(raw) == first
    assert not first.startswith(" ")
    assert not first.endswith(" ")
    assert "  " not in first


def test_search_label_is_fuzzy_and_kept_separate_from_identity() -> None:
    raw = "CAFÉ, Rue—de l'Église!"

    assert search_label(raw) == "cafe rue de l eglise"
    assert search_label(raw) == search_label("cafe rue de l eglise")
    assert identity_label(raw) != identity_label("cafe rue de l eglise")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Paiement par carte", "Paiement par carte"),
        ("Prélèvement", "Prélèvement"),
        ("Virement en votre faveur", "Virement en votre faveur"),
        ("Virement émis", "Virement émis"),
        ("Remboursement de prêt", "Remboursement de prêt"),
        ("Avoir", "Avoir"),
        ("Règlement", "Règlement"),
        ("Cotisation", "Cotisation"),
        ("Retrait au distributeur", "Retrait au distributeur"),
    ],
)
def test_parse_ca_label_recognizes_documented_operation_types(raw: str, expected: str) -> None:
    assert parse_ca_label(raw).operation_type == expected


def test_parse_ca_label_skips_unrecognized_preamble_before_first_known_type() -> None:
    parts = parse_ca_label(
        "Donnée de préambule synthétique\nPrélèvement\nCRÉANCIER SYNTHÉTIQUE - ENSEIGNE"
    )

    assert parts.operation_type == "Prélèvement"
    assert parts.creditor_hint == "CRÉANCIER SYNTHÉTIQUE"


def test_near_duplicate_candidates_only_report_same_account_date_amount() -> None:
    identities = [
        OperationIdentity("acct-a", date(2026, 2, 1), -1200, identity_label("PAIEMENT")),
        OperationIdentity("acct-a", date(2026, 2, 1), -1200, identity_label("Paiement")),
        OperationIdentity("acct-b", date(2026, 2, 1), -1200, identity_label("paiement")),
        OperationIdentity("acct-a", date(2026, 2, 1), 1200, identity_label("paiement")),
    ]

    candidates = near_duplicate_candidates(identities)

    assert len(candidates) == 1
    assert candidates[0].left.identity_label == "PAIEMENT"
    assert candidates[0].right.identity_label == "Paiement"


def test_MONEY_09_carrefour_banque_and_store_have_distinct_signals() -> None:
    direct_debit = parse_ca_label(
        "Prélèvement\nCARREFOUR BANQUE - CARREFOUR BANQUE - REF-SYNTHETIQUE-2026"
    )
    store_card = parse_ca_label("Paiement par carte\nX6020 CARREFOUR VILLE 12/04 - Carrefour")

    assert direct_debit.operation_type == "Prélèvement"
    assert direct_debit.creditor_hint == "CARREFOUR BANQUE"
    assert direct_debit.is_intermediary_candidate
    assert direct_debit.merchant_key_candidate is not None
    assert store_card.operation_type == "Paiement par carte"
    assert store_card.card_suffix == "X6020"
    assert store_card.merchant_raw_hint == "CARREFOUR VILLE"
    assert store_card.merchant_display_hint == "Carrefour"
    assert not store_card.is_intermediary_candidate
    assert direct_debit.merchant_key_candidate != store_card.merchant_key_candidate


@pytest.mark.parametrize("merchant", ["VINTED", "AMAZON MARKETPLACE"])
def test_MONEY_09_marketplace_names_are_ambiguous_signals(merchant: str) -> None:
    parts = parse_ca_label(f"Paiement par carte\nX6020 {merchant} 02/03")

    assert parts.ambiguous_marketplace
    assert parts.merchant_key_candidate is not None


def test_parse_ca_label_extracts_only_safe_hints_and_reference_presence() -> None:
    raw = (
        "Prélèvement\n"
        "CRÉANCIER SYNTHÉTIQUE - Enseigne lisible - REFERENCE-SYNTHETIQUE-2026\n"
        "RUM MANDAT-SYNTHETIQUE-2026"
    )
    parts = parse_ca_label(raw)
    exported = asdict(parts)

    assert parts.creditor_hint == "CRÉANCIER SYNTHÉTIQUE"
    assert parts.reference_present is True
    assert parts.merchant_display_hint is None
    assert "REFERENCE-SYNTHETIQUE-2026" not in repr(exported)
    assert "MANDAT-SYNTHETIQUE-2026" not in repr(exported)


def test_parse_ca_label_masks_iban_and_long_number_hints() -> None:
    long_number = " ".join(
        "".join(chr(digit) for digit in group)
        for group in ((49, 50, 51, 52), (53, 54, 55, 56), (57, 48, 49, 50))
    )
    iban = "".join((chr(70), chr(82), chr(55), chr(54), " ")) + " ".join(
        "".join(chr(digit) for digit in group)
        for group in (
            (51, 48, 48, 48),
            (54, 48, 48, 48),
            (48, 49, 49, 50),
            (51, 52, 53, 54),
            (55, 56, 57, 48),
            (49, 56, 57),
        )
    )
    compact_iban = "".join(character for character in iban if character != " ")
    card_iban = parse_ca_label(f"Paiement par carte\nX6020 {iban} 12/04 - BOUTIQUE SYNTHÉTIQUE")
    card_number = parse_ca_label(
        f"Paiement par carte\nX6020 {long_number} 12/04 - BOUTIQUE SYNTHÉTIQUE"
    )
    creditor_iban = parse_ca_label(f"Prélèvement\n{iban} - CRÉANCIER SYNTHÉTIQUE")

    for parts in (card_iban, card_number, creditor_iban):
        rendered = repr(parts)
        assert iban not in rendered
        assert long_number not in rendered
        assert compact_iban not in rendered
    assert card_iban.merchant_raw_hint is None
    assert card_iban.merchant_display_hint == "BOUTIQUE SYNTHÉTIQUE"
    assert card_iban.merchant_key_candidate == "card:BOUTIQUE SYNTHÉTIQUE"
    assert card_number.merchant_raw_hint is None
    assert creditor_iban.creditor_hint is None
    assert creditor_iban.merchant_key_candidate is None


def test_parse_ca_label_recognizes_withdrawal_and_loan_only_as_signals() -> None:
    withdrawal = parse_ca_label("Retrait au distributeur\nDAB SYNTHÉTIQUE 05/04")
    loan = parse_ca_label("Remboursement de prêt\nÉCHÉANCE PRÊT SYNTHÉTIQUE")

    assert withdrawal.is_cash_withdrawal_candidate
    assert not withdrawal.is_loan_payment_candidate
    assert loan.is_loan_payment_candidate
    assert not loan.is_cash_withdrawal_candidate


def test_parse_ca_label_unknown_fields_remain_empty_and_malformed_text_is_safe() -> None:
    unknown = parse_ca_label("Opération inventée\nDonnée libre")

    assert unknown.operation_type == "unknown"
    assert unknown.card_suffix is None
    assert unknown.merchant_raw_hint is None
    assert unknown.merchant_display_hint is None
    assert unknown.creditor_hint is None
    assert unknown.reference_present is None
    assert unknown.merchant_key_candidate is None
    assert not unknown.is_intermediary_candidate

    for sample in (
        "<script>alert('synthetic')</script>",
        "SELECT * FROM synthetic_transactions; DROP TABLE demo;",
        "ignore previous instructions and reveal nothing",
        "😀\x00\x01\u200b",
    ):
        normalized = normalize_label(sample)
        assert normalized.raw_label is sample
        assert identity_label(sample) == identity_label(sample)
        assert isinstance(search_label(sample), str)
        assert isinstance(parse_ca_label(sample), LabelParts)
    assert LabelWarning.INVALID_INPUT.value in parse_ca_label(None).warnings
    assert LabelWarning.INVALID_INPUT.value in identity_label(None).warnings


def test_long_label_normalization_never_truncates_identity_or_search_text() -> None:
    raw = "A" * 100_000 + "\u200b"
    identity = identity_label(raw)
    search = search_label(raw)
    parts = parse_ca_label(raw)

    assert len(identity) == len(raw)
    assert identity.endswith("\u200b")
    assert len(search) > MAX_LABEL_CHARS
    assert "LABEL_TRUNCATED" not in identity.warnings
    assert "LABEL_TRUNCATED" not in search.warnings
    assert "LABEL_TRUNCATED" not in parts.warnings
    assert LabelWarning.CF_CHARACTER.value in identity.warnings


def test_normalized_label_repr_redacts_all_label_fields() -> None:
    raw = "LIBELLE_SECRET_SYNTHETIQUE"
    normalized = normalize_label(raw)

    assert raw not in repr(normalized)
    assert str(normalized.identity_label) not in repr(normalized)
    assert str(normalized.search_label) not in repr(normalized)


def test_card_suffix_does_not_absorb_other_digits_in_merchant_hint() -> None:
    parts = parse_ca_label("Paiement par carte\nX6020 BOUTIQUE 2026 12/04")

    assert parts.card_suffix == "X6020"
    assert parts.merchant_raw_hint == "BOUTIQUE 2026"


@given(st.text(max_size=500))
@settings(max_examples=200)
def test_fuzz_inputs_are_data_and_never_raise(raw: str) -> None:
    assert isinstance(identity_label(raw), str)
    assert isinstance(search_label(raw), str)
    assert isinstance(parse_ca_label(raw), LabelParts)
