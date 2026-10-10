from app.classification.matcher import derive_payee_key
from app.importers.normalize import LabelParts, parse_ca_label


def test_MONEY_09_exact_keys_keep_carrefour_banque_and_store_separate() -> None:
    bank = parse_ca_label("Prélèvement\nCARREFOUR BANQUE - CARREFOUR BANQUE - REF-SYNTHETIQUE")
    store = parse_ca_label("Paiement par carte\nX6020 CARREFOUR VILLE 12/04 - Carrefour")

    bank_key = derive_payee_key(bank)
    store_key = derive_payee_key(store)

    assert bank_key is not None and store_key is not None
    assert bank_key.normalized_name != store_key.normalized_name
    assert bank_key.is_intermediary is True
    assert store_key.is_intermediary is False
    assert bank_key.ambiguous is False


def test_derive_payee_key_uses_only_significant_exact_signals() -> None:
    first = LabelParts(
        operation_type="Paiement par carte",
        card_suffix="X6020",
        merchant_raw_hint="MARCHAND LUNE",
        merchant_display_hint="Marchand Lune",
        reference_present=False,
    )
    irrelevant_fields_changed = LabelParts(
        operation_type="Paiement par carte",
        card_suffix="X9184",
        merchant_raw_hint="MARCHAND LUNE",
        merchant_display_hint="Marchand Lune",
        reference_present=True,
        warnings=("CF_CHARACTER",),
    )
    different_store = LabelParts(
        operation_type="Paiement par carte",
        merchant_raw_hint="MARCHAND LUNE",
        merchant_display_hint="Marchand Lune",
        is_intermediary_candidate=True,
    )

    first_key = derive_payee_key(first)
    second_key = derive_payee_key(irrelevant_fields_changed)
    distinct_key = derive_payee_key(different_store)

    assert first_key is not None and second_key is not None and distinct_key is not None
    assert first_key.normalized_name == second_key.normalized_name
    assert first_key.normalized_name != distinct_key.normalized_name


def test_derive_payee_key_flags_marketplaces_and_refuses_non_merchants() -> None:
    amazon = parse_ca_label("Paiement par carte\nX1010 ACHAT EN LIGNE 01/03 - Amazon")
    vinted = parse_ca_label("Paiement par carte\nX2020 VINTED 02/03 - Vinted")
    withdrawal = parse_ca_label("Retrait au distributeur\nDAB SYNTHETIQUE")
    transfer = parse_ca_label("Virement émis\nPARTICULIER EXEMPLE")

    amazon_key = derive_payee_key(amazon)
    vinted_key = derive_payee_key(vinted)

    assert amazon_key is not None and amazon_key.ambiguous is True
    assert vinted_key is not None and vinted_key.ambiguous is True
    assert amazon_key.normalized_name != vinted_key.normalized_name
    assert derive_payee_key(withdrawal) is None
    assert derive_payee_key(transfer) is None


def test_derive_payee_key_rejects_sensitive_candidate_text() -> None:
    synthetic_iban = "".join(("FR", "76", "12345678901234567890123"))
    sensitive = LabelParts(
        operation_type="Prélèvement",
        creditor_hint=f"CREANCIER TEST {synthetic_iban}",
        merchant_key_candidate=f"creditor:CREANCIER TEST {synthetic_iban}",
    )

    assert derive_payee_key(sensitive) is None
