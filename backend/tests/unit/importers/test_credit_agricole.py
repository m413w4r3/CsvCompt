from __future__ import annotations

import json
import random
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.importers.account_identity import bank_fingerprint, canonicalize_account_number
from app.importers.amounts import parse_debit_credit, parse_french_amount
from app.importers.credit_agricole import parse_export
from app.importers.types import AnomalyCode

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"
MANIFEST_PATH = FIXTURES_DIR / "manifest.yaml"
pytestmark = pytest.mark.unit


def _codes(result: Any) -> set[str]:
    return {anomaly.code.value for anomaly in result.anomalies}


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _cents_to_fr(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    euros, minor = divmod(abs(cents), 100)
    grouped = f"{euros:,}".replace(",", "\u202f")
    return f"{sign}{grouped},{minor:02d}"


def test_CSV_01_decodes_cp1252_utf8_and_bom_variants() -> None:
    cp1252 = parse_export(_fixture_bytes("ca_2026_simple.csv"))
    utf8 = parse_export(_fixture_bytes("ca_2026_utf8_crlf.csv"))
    bom = parse_export(_fixture_bytes("ca_2026_utf8_crlf_bom.csv"))
    nbsp = parse_export(_fixture_bytes("ca_2026_utf8_crlf_nbsp.csv"))

    assert cp1252.encoding == "cp1252"
    assert utf8.encoding == "utf-8"
    assert bom.encoding == "utf-8-sig"
    assert nbsp.encoding == "utf-8"
    assert [result.operation_count for result in (cp1252, utf8, bom, nbsp)] == [40] * 4
    assert not any(result.has_blocking_anomalies for result in (cp1252, utf8, bom, nbsp))
    assert all(
        type(row.amount_cents) is int for result in (cp1252, utf8, bom, nbsp) for row in result.rows
    )
    assert any(any(ord(character) > 127 for character in row.raw_label) for row in cp1252.rows)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_CSV_01_accepts_all_physical_line_endings(newline: str) -> None:
    text = newline.join(
        [
            "Téléchargement du 10/01/2026;",
            "Compte de Dépôt carte n° 123;",
            "Solde au 09/01/2026 10,00 €;",
            "Liste des opérations du compte entre le 01/01/2026 et le 31/01/2026;",
            "Date;Libellé;Débit euros;Crédit euros;",
            "08/01/2026;Opération synthétique;1,00;;",
        ]
    )
    result = parse_export(text.encode())
    assert result.operation_count == 1
    assert result.rows[0].amount_cents == -100
    assert not result.has_blocking_anomalies


def test_CSV_02_preserves_quoted_multiline_label_and_fake_section_headers() -> None:
    label = (
        'ligne 1; avec une citation "interne"\r\n'
        "Compte espèce PEA\r"
        "Date;Libellé;Débit euros;Crédit euros;\n\n"
    )
    escaped_label = '"' + label.replace('"', '""') + '"'
    text = (
        "Téléchargement du 10/01/2026;\r\n"
        "\r\n"
        "Compte de Dépôt carte n° 123\u00a0456;\r\n"
        "Solde au 09/01/2026 1 234,56 €;\r\n"
        "Liste des opérations du compte entre le 01/01/2026 et le 31/01/2026;\r\n"
        "Date;Libellé;Débit euros;Crédit euros;\r\n"
        f"08/01/2026;{escaped_label};10,00;;\r\n"
    )

    result = parse_export(text.encode("utf-8"))

    assert len(result.sections) == 1
    assert result.operation_count == 1
    assert result.rows[0].raw_label == label
    assert result.rows[0].line_start == 7
    assert result.rows[0].line_end == 11
    assert result.rows[0].amount_cents == -1000
    assert result.sections[0].account_number_canonical is not None
    assert result.sections[0].account_number_canonical.value == "123456"
    assert "DUPLICATE_HEADER" not in _codes(result)
    assert "DUPLICATE_ACCOUNT_SECTION" not in _codes(result)


def test_CSV_03_parses_multiple_sections_without_counting_metadata() -> None:
    result = parse_export(_fixture_bytes("ca_2026_multi_sections.csv"))

    assert len(result.sections) == 2
    assert [len(section.rows) for section in result.sections] == [5, 3]
    assert [section.ordinal for section in result.sections] == [1, 2]
    assert [section.header_balance_cents for section in result.sections] == [182345, 58760]
    assert [section.section_index for section in result.sections] == [1, 2]
    assert [section.coverage_from for section in result.sections] == [date(2026, 1, 1)] * 2
    assert [section.coverage_through for section in result.sections] == [date(2026, 4, 30)] * 2
    assert [section.section_operation_count for section in result.sections] == [5, 3]
    assert all(section.balance_as_of is not None for section in result.sections)
    assert result.operation_count == 8
    assert not result.has_blocking_anomalies


def test_CSV_04_manifest_recounts_all_34_synthetic_fixtures() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert len(manifest["fixtures"]) == 34

    for name, expected in manifest["fixtures"].items():
        raw = _fixture_bytes(name)
        result = parse_export(raw)
        expected_encoding = expected["encoding"]
        encoding_map = {"utf-8-bom": "utf-8-sig", "cp1252-invalid": "unknown"}
        if expected_encoding == "cp1252" and raw.isascii():
            expected_encoding = "utf-8"
        assert result.encoding == encoding_map.get(expected_encoding, expected_encoding), name
        actual_codes = _codes(result)

        for expected_anomaly in expected.get("anomalies", []):
            if expected_anomaly.get("expected_behavior") == "accepté":
                continue
            code = expected_anomaly["code"]
            assert code in actual_codes, name
            if code != "INVALID_ENCODING" and "line_number" in expected_anomaly:
                assert any(
                    item.code.value == code and item.line_no == expected_anomaly["line_number"]
                    for item in result.anomalies
                ), name

        if expected_encoding == "cp1252-invalid":
            # Strict decoding rejects the whole byte stream before any partial row is published.
            assert result.sections == []
            assert result.operation_count == 0
            assert result.has_blocking_anomalies
            continue

        assert len(result.sections) == len(expected["sections"]), name
        assert actual_codes == {
            item["code"]
            for item in expected.get("anomalies", [])
            if item.get("expected_behavior") != "accepté"
        }, name
        for section, section_expected in zip(result.sections, expected["sections"], strict=True):
            rows = section.rows
            assert len(rows) == section_expected["operation_count"], name
            assert (
                sum(max(-row.amount_cents, 0) for row in rows) == section_expected["debit_cents"]
            ), name
            assert (
                sum(max(row.amount_cents, 0) for row in rows) == section_expected["credit_cents"]
            ), name
            assert section.header_balance_cents == section_expected["header_balance_cents"], name
            actual_dates = [row.booked_on.isoformat() for row in rows]
            assert (min(actual_dates) if actual_dates else None) == section_expected["min_date"], (
                name
            )
            assert (max(actual_dates) if actual_dates else None) == section_expected["max_date"], (
                name
            )
            if section_expected["period_declared"]:
                assert section.period_from == date.fromisoformat(
                    section_expected["declared_period_start"]
                ), name
                assert section.period_to == date.fromisoformat(
                    section_expected["declared_period_end"]
                ), name
            else:
                assert section.period_from is None and section.period_to is None, name


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5,00", 500),
        ("2 811,82", 281182),
        ("2\u00a0811,82", 281182),
        ("2\u202f811,82", 281182),
        ("2\u2009811,82", 281182),
        ("+5,00", 500),
        ("-5,00", -500),
        ("5", 500),
        ("12.345", AnomalyCode.NON_NUMERIC_AMOUNT),
        ("1,2,3", AnomalyCode.NON_NUMERIC_AMOUNT),
        ("1,234", AnomalyCode.NON_NUMERIC_AMOUNT),
        ("1e3", AnomalyCode.NON_NUMERIC_AMOUNT),
        ("", AnomalyCode.NON_NUMERIC_AMOUNT),
    ],
)
def test_CSV_04_french_amount_grammar_is_strict(text: str, expected: int | AnomalyCode) -> None:
    assert parse_french_amount(text) == expected


@pytest.mark.parametrize(
    ("debit", "credit", "expected"),
    [
        ("10,00", "", -1000),
        ("", "10,00", 1000),
        ("1,00", "2,00", AnomalyCode.BOTH_AMOUNTS),
        ("", "", AnomalyCode.ZERO_AMOUNT),
        ("0,00", "", AnomalyCode.ZERO_AMOUNT),
        ("", "0,00", AnomalyCode.ZERO_AMOUNT),
        ("-1,00", "", AnomalyCode.NEGATIVE_AMOUNT),
        ("", "-1,00", AnomalyCode.NEGATIVE_AMOUNT),
    ],
)
def test_CSV_04_debit_credit_signs_and_rejections(
    debit: str, credit: str, expected: int | AnomalyCode
) -> None:
    assert parse_debit_credit(debit, credit) == expected


@settings(max_examples=2500, derandomize=True, deadline=None)
@example(1)
@example(-1)
@example(99)
@example(-99)
@example(100)
@example(-100)
@example(10**12)
@example(-(10**12))
@given(st.integers(min_value=-(10**12), max_value=10**12))
def test_CSV_04_french_amount_round_trips_thousands_of_cent_values(cents: int) -> None:
    result = parse_french_amount(_cents_to_fr(cents))
    assert type(result) is int
    assert result == cents


def test_CSV_04_strict_dates_period_errors_and_provisional_completeness() -> None:
    invalid = parse_export(_fixture_bytes("adversarial/invalid_date.csv"))
    without_period = parse_export(_fixture_bytes("adversarial/section_without_period.csv"))
    provisional = parse_export(_fixture_bytes("provisional_day_A.csv"))

    assert "INVALID_DATE" in _codes(invalid)
    assert invalid.operation_count == 0
    assert "MISSING_PERIOD" in _codes(without_period)
    assert without_period.has_blocking_anomalies
    assert provisional.sections[0].complete_through == date(2026, 10, 8)
    assert "PROVISIONAL_DAY" in _codes(provisional)
    assert not provisional.has_blocking_anomalies


def test_CSV_04_duplicate_sections_and_out_of_period_are_blocking() -> None:
    duplicate = parse_export(_fixture_bytes("adversarial/same_account_two_sections.csv"))
    out_of_period = parse_export(_fixture_bytes("adversarial/out_of_period_line.csv"))

    assert "DUPLICATE_ACCOUNT_SECTION" in _codes(duplicate)
    assert duplicate.has_blocking_anomalies
    assert len(duplicate.sections) == 2
    assert "OUT_OF_PERIOD" in _codes(out_of_period)
    assert out_of_period.has_blocking_anomalies
    assert out_of_period.operation_count == 1


def test_CSV_04_unknown_rows_after_header_are_never_silently_ignored() -> None:
    prefix = (
        "Téléchargement du 10/01/2026;\n"
        "Compte de Dépôt carte n° 123;\n"
        "Solde au 09/01/2026 10,00 €;\n"
        "Liste des opérations du compte entre le 01/01/2026 et le 31/01/2026;\n"
        "Date;Libellé;Débit euros;Crédit euros;\n"
    )
    malformed_count = parse_export((prefix + "date inconnue;libellé;10,00;\n").encode())
    invalid_date = parse_export((prefix + "date inconnue;libellé;10,00;;\n").encode())

    assert "BAD_FIELD_COUNT" in _codes(malformed_count)
    assert malformed_count.operation_count == 0
    assert malformed_count.has_blocking_anomalies
    assert "INVALID_DATE" in _codes(invalid_date)
    assert invalid_date.operation_count == 0
    assert invalid_date.has_blocking_anomalies


def test_CSV_04_limits_and_empty_inputs_return_anomalies() -> None:
    sample = _fixture_bytes("ca_2026_simple.csv")
    too_large = parse_export(sample, max_bytes=len(sample) - 1)
    too_long = parse_export(sample, max_field_chars=20)
    long_label = parse_export(
        _fixture_bytes("adversarial/very_long_label_5100.csv"), max_field_chars=5000
    )
    empty = parse_export(b"")
    preamble = parse_export("Téléchargement du 10/01/2026;\n".encode())

    assert "FILE_TOO_LARGE" in _codes(too_large)
    assert "FIELD_TOO_LONG" in _codes(too_long)
    assert "FIELD_TOO_LONG" in _codes(long_label)
    assert long_label.operation_count == 0
    assert "EMPTY_FILE" in _codes(empty)
    assert empty.has_blocking_anomalies
    assert "MISSING_HEADER" in _codes(preamble)
    assert preamble.has_blocking_anomalies


def test_CSV_04_account_number_canonicalization_and_hmac_vector() -> None:
    canonical = canonicalize_account_number("\u00a0001-٢٣\u202f\u00a0")
    assert not isinstance(canonical, AnomalyCode)
    assert canonical.value == "00123"
    assert str(canonical) == "<redacted>"
    assert format(canonical, "s") == "<redacted>"
    assert "00123" not in repr(canonical)
    assert bank_fingerprint(b"key", canonical) == (
        "8ed8eec81fb3833627aab61432bfc7bc46191c5d55398507513a43a242b1738e"
    )


@pytest.mark.parametrize(
    "raw", ["", " \t\u00a0", "-123", "123-", "1 - 2", "1/2", "12A", "1\u200b2"]
)
def test_CSV_04_account_number_rejects_ambiguous_punctuation(raw: str) -> None:
    assert canonicalize_account_number(raw) == AnomalyCode.INVALID_ACCOUNT_NUMBER


def test_CSV_04_errors_never_include_source_labels_or_accounts() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for fixture_name in manifest["fixtures"]:
        result = parse_export(_fixture_bytes(fixture_name))
        messages = [anomaly.message for anomaly in result.anomalies]
        for row in result.rows:
            if len(row.raw_label) >= 3:
                assert all(row.raw_label not in message for message in messages)
        for section in result.sections:
            canonical = section.account_number_canonical
            if canonical is not None and len(canonical.value) >= 4:
                assert all(canonical.value not in message for message in messages)


def test_CSV_04_arbitrary_and_mutated_bytes_never_raise_or_print(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rng = random.Random(1907)
    samples = [rng.randbytes(size) for size in range(128)]
    original = _fixture_bytes("ca_2026_simple.csv")
    for _ in range(100):
        mutated = bytearray(original)
        for _ in range(rng.randint(1, 5)):
            position = rng.randrange(len(mutated))
            mutated[position] = rng.randrange(256)
        samples.append(bytes(mutated))

    results = [parse_export(sample) for sample in samples]
    assert all(result is not None for result in results)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_CSV_04_parse_is_deterministic_and_repr_redacts_accounts() -> None:
    source = _fixture_bytes("ca_2026_multi_sections.csv")
    first = parse_export(source)
    second = parse_export(source)
    assert first == second
    assert "<redacted>" in repr(first)
    assert all(
        section.account_number_canonical is None
        or section.account_number_canonical.value not in repr(first)
        for section in first.sections
    )


def test_CSV_04_line_numbers_include_multiline_records_and_blank_rows() -> None:
    result = parse_export(_fixture_bytes("adversarial/interleaved_blank_lines.csv"))
    assert "INTERLEAVED_BLANK_LINES" in _codes(result)
    assert result.rows[0].line_start == 13
    assert result.rows[0].line_end == 17
