"""Pure and privacy-safe parser for Crédit Agricole semicolon exports."""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta

from app.importers.account_identity import (
    CanonicalAccountNumber,
    canonicalize_account_number,
)
from app.importers.amounts import parse_debit_credit, parse_french_amount
from app.importers.encoding import (
    DEFAULT_MAX_BYTES,
    DecodedExport,
    decode_export_bytes,
    normalize_line_endings,
)
from app.importers.types import (
    Anomaly,
    AnomalyCode,
    ParsedExport,
    ParsedRow,
    ParsedSection,
    Severity,
)

DEFAULT_MAX_FIELD_CHARS = 10_000
_ABSOLUTE_MAX_FIELD_CHARS = 100_000
_LONG_LABEL_WARNING_CHARS = 5_000
_DATE_RE = re.compile(r"^([0-9]{2})/([0-9]{2})/([0-9]{4})$")
_DATE_LIKE_RE = re.compile(r"^[0-9]{1,4}[/.-]")
_DOWNLOAD_RE = re.compile(r"^Téléchargement du\s+(.+?)\s*$", re.IGNORECASE)
_ACCOUNT_SECTION_RE = re.compile(r"^Compte\b", re.IGNORECASE)
_ACCOUNT_NUMBER_RE = re.compile(r"^Compte\b.*?\bn°\s*(.*?)\s*$", re.IGNORECASE)
_BALANCE_RE = re.compile(r"^Solde au\s+([^\s]+)\s+(.+?)\s*$", re.IGNORECASE)
_PERIOD_RE = re.compile(
    r"^Liste des opérations du compte entre le\s+(.+?)\s+et\s+(?:le\s+)?(.+?)\s*$",
    re.IGNORECASE,
)
_HEADER = ("date", "libellé", "débit euros", "crédit euros")

_MESSAGES: dict[AnomalyCode, str] = {
    AnomalyCode.BAD_FIELD_COUNT: "Une ligne ne contient pas le nombre de champs attendu.",
    AnomalyCode.BOTH_AMOUNTS: "Les colonnes débit et crédit sont renseignées simultanément.",
    AnomalyCode.DUPLICATE_ACCOUNT_SECTION: (
        "Le même compte apparaît dans plusieurs sections du fichier."
    ),
    AnomalyCode.DUPLICATE_HEADER: "Un en-tête de colonnes est répété dans la section.",
    AnomalyCode.EMPTY_FILE: "Le fichier ne contient aucune donnée exploitable.",
    AnomalyCode.FIELD_TOO_LONG: "Un champ dépasse la longueur maximale autorisée.",
    AnomalyCode.FILE_TOO_LARGE: "Le fichier dépasse la taille maximale autorisée.",
    AnomalyCode.INTERLEAVED_BLANK_LINES: "Des lignes vides sont intercalées entre des opérations.",
    AnomalyCode.INVALID_ACCOUNT_NUMBER: "Le numéro de compte ne respecte pas le format attendu.",
    AnomalyCode.INVALID_DATE: "Une date ne respecte pas le calendrier ou le format attendu.",
    AnomalyCode.INVALID_ENCODING: "L’encodage du fichier n’est pas valide.",
    AnomalyCode.LONG_LABEL: "Un libellé dépasse la longueur recommandée.",
    AnomalyCode.MISSING_HEADER: "L’en-tête des opérations est absent.",
    AnomalyCode.MISSING_PERIOD: "La période déclarée de la section est absente.",
    AnomalyCode.NEGATIVE_AMOUNT: "Un montant de colonne ne peut pas être négatif.",
    AnomalyCode.NON_NUMERIC_AMOUNT: "Un montant ne respecte pas le format numérique attendu.",
    AnomalyCode.OUT_OF_PERIOD: "La date de l’opération est hors de la période déclarée.",
    AnomalyCode.PARSE_ERROR: "Le fichier ne peut pas être interprété de manière sûre.",
    AnomalyCode.PROVISIONAL_DAY: "La journée de génération est provisoire.",
    AnomalyCode.UNCLOSED_QUOTE: "Un champ cité n’est pas fermé avant la fin du fichier.",
    AnomalyCode.ZERO_AMOUNT: "Un montant nul est refusé pour le format Crédit Agricole.",
}


@dataclass(frozen=True, slots=True)
class _CsvRecord:
    raw: str
    line_start: int
    line_end: int
    unclosed_quote: bool = False


def _is_newline(character: str) -> bool:
    return character == "\r" or character == "\n"


def _iter_csv_records(text: str) -> Iterator[_CsvRecord]:
    """Split physical records outside quotes, preserving quoted line breaks."""

    chars: list[str] = []
    line = 1
    record_start = 1
    in_quotes = False
    at_field_start = True
    index = 0
    while index < len(text):
        character = text[index]
        if in_quotes:
            chars.append(character)
            if character == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    chars.append('"')
                    index += 2
                    continue
                in_quotes = False
            elif _is_newline(character):
                if character == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                    chars.append("\n")
                    index += 1
                line += 1
            index += 1
            continue

        if at_field_start and character == '"':
            chars.append(character)
            in_quotes = True
            at_field_start = False
            index += 1
            continue
        if character == ";":
            chars.append(character)
            at_field_start = True
            index += 1
            continue
        if _is_newline(character):
            record_end = line
            yield _CsvRecord("".join(chars), record_start, record_end)
            chars.clear()
            if character == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                index += 1
            line += 1
            record_start = line
            at_field_start = True
            index += 1
            continue
        chars.append(character)
        at_field_start = False
        index += 1

    if chars or in_quotes:
        yield _CsvRecord("".join(chars), record_start, line, unclosed_quote=in_quotes)


def _parse_record(record: _CsvRecord) -> list[str] | AnomalyCode:
    if record.unclosed_quote:
        return AnomalyCode.UNCLOSED_QUOTE
    try:
        reader = csv.reader(
            io.StringIO(record.raw, newline=""),
            delimiter=";",
            quotechar='"',
            strict=True,
        )
        return next(reader, [])
    except csv.Error as error:
        if "field larger than field limit" in str(error):
            return AnomalyCode.FIELD_TOO_LONG
        return AnomalyCode.PARSE_ERROR
    except Exception:
        return AnomalyCode.PARSE_ERROR


def _parse_date(value: str) -> date | None:
    match = _DATE_RE.fullmatch(value.strip())
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _header_matches(row: list[str]) -> bool:
    if len(row) not in (4, 5):
        return False
    normalized = [
        unicodedata.normalize("NFC", cell.strip().lstrip("\ufeff")).casefold() for cell in row
    ]
    return tuple(normalized[:4]) == _HEADER and (len(row) == 4 or normalized[4] == "")


def _looks_like_data(row: list[str]) -> bool:
    if not row:
        return False
    first = row[0].strip()
    return bool(_DATE_LIKE_RE.match(first)) or len(row) == 5


def _is_metadata_line(value: str) -> bool:
    lowered = value.strip().casefold()
    return (
        lowered.startswith("madame")
        or lowered.startswith("monsieur")
        or lowered.startswith("mademoiselle")
        or lowered.startswith("solde initial")
        or lowered.startswith("solde de début")
        or lowered.startswith("solde précédent")
    )


def _anomaly(
    code: AnomalyCode,
    severity: Severity,
    *,
    section: int | None = None,
    row: int | None = None,
    line: int | None = None,
    field: str | None = None,
) -> Anomaly:
    return Anomaly(
        code=code,
        severity=severity,
        section_ordinal=section,
        logical_row_idx=row,
        line_no=line,
        field=field,
        message=_MESSAGES[code],
    )


def _parse_export_impl(
    data: bytes,
    *,
    max_bytes: int,
    max_field_chars: int,
) -> ParsedExport:
    decoded = decode_export_bytes(data, max_bytes=max_bytes)
    if isinstance(decoded, AnomalyCode):
        return ParsedExport(
            encoding="unknown",
            generated_on=None,
            generation_date_source="missing",
            anomalies=[_anomaly(decoded, "blocking", field="file")],
        )
    assert isinstance(decoded, DecodedExport)
    result = ParsedExport(
        encoding=decoded.encoding,
        generated_on=None,
        generation_date_source="missing",
    )
    if not data or not decoded.text:
        result.anomalies.append(_anomaly(AnomalyCode.EMPTY_FILE, "blocking", line=0, field="file"))
        return result

    current: ParsedSection | None = None
    section_start_line: dict[int, int] = {}
    expected_header_line: dict[int, int] = {}
    expected_period_line: dict[int, int] = {}
    period_phrase_seen: dict[int, bool] = {}
    header_seen: dict[int, bool] = {}
    missing_header_reported: set[int] = set()
    candidate_idx: dict[int, int] = {}
    sections_by_number: dict[str, int] = {}
    pending_blanks: dict[int, list[int]] = {}

    def start_section(raw_number: str | None, line_no: int) -> ParsedSection:
        ordinal = len(result.sections) + 1
        canonical_number: CanonicalAccountNumber | None = None
        if raw_number is not None:
            canonical = canonicalize_account_number(raw_number)
            if isinstance(canonical, AnomalyCode):
                result.anomalies.append(
                    _anomaly(
                        canonical,
                        "blocking",
                        section=ordinal,
                        line=line_no,
                        field="account_number",
                    )
                )
            else:
                canonical_number = canonical
                previous = sections_by_number.get(canonical.value)
                if previous is not None:
                    result.anomalies.append(
                        _anomaly(
                            AnomalyCode.DUPLICATE_ACCOUNT_SECTION,
                            "blocking",
                            section=ordinal,
                            line=line_no,
                            field="account_number",
                        )
                    )
                else:
                    sections_by_number[canonical.value] = ordinal
        section = ParsedSection(ordinal=ordinal, account_number_canonical=canonical_number)
        result.sections.append(section)
        section_start_line[ordinal] = line_no
        expected_header_line[ordinal] = line_no + 5
        expected_period_line[ordinal] = line_no + 3
        period_phrase_seen[ordinal] = False
        header_seen[ordinal] = False
        candidate_idx[ordinal] = 0
        pending_blanks[ordinal] = []
        return section

    def close_header_check(section: ParsedSection | None) -> None:
        if (
            section is not None
            and not header_seen.get(section.ordinal, False)
            and section.ordinal not in missing_header_reported
        ):
            result.anomalies.append(
                _anomaly(
                    AnomalyCode.MISSING_HEADER,
                    "blocking",
                    section=section.ordinal,
                    line=expected_header_line.get(
                        section.ordinal, section_start_line.get(section.ordinal)
                    ),
                    field="header",
                )
            )
            missing_header_reported.add(section.ordinal)

    for record in _iter_csv_records(decoded.text):
        parsed = _parse_record(record)
        if parsed == AnomalyCode.UNCLOSED_QUOTE:
            result.anomalies.append(
                _anomaly(
                    AnomalyCode.UNCLOSED_QUOTE,
                    "blocking",
                    section=current.ordinal if current else None,
                    row=candidate_idx.get(current.ordinal, 0) + 1 if current else None,
                    line=record.line_start,
                    field="row",
                )
            )
            break
        if parsed == AnomalyCode.FIELD_TOO_LONG:
            result.anomalies.append(
                _anomaly(
                    AnomalyCode.FIELD_TOO_LONG,
                    "blocking",
                    section=current.ordinal if current else None,
                    line=record.line_start,
                    field="row",
                )
            )
            continue
        if isinstance(parsed, AnomalyCode):
            result.anomalies.append(
                _anomaly(
                    parsed,
                    "blocking",
                    section=current.ordinal if current else None,
                    line=record.line_start,
                    field="row",
                )
            )
            continue
        row = parsed
        if not row or all(not cell.strip() for cell in row):
            if current is not None and header_seen[current.ordinal]:
                pending_blanks[current.ordinal].append(record.line_start)
            continue
        if any(len(cell) > max_field_chars for cell in row):
            result.anomalies.append(
                _anomaly(
                    AnomalyCode.FIELD_TOO_LONG,
                    "blocking",
                    section=current.ordinal if current else None,
                    line=record.line_start,
                    field="row",
                )
            )
            continue

        first = normalize_line_endings(row[0]).strip().lstrip("\ufeff") if row else ""
        download_match = _DOWNLOAD_RE.fullmatch(first)
        if download_match:
            generated = _parse_date(download_match.group(1))
            if generated is None:
                result.anomalies.append(
                    _anomaly(
                        AnomalyCode.INVALID_DATE,
                        "blocking",
                        section=current.ordinal if current else None,
                        line=record.line_start,
                        field="generated_on",
                    )
                )
                result.generated_on = None
                result.generation_date_source = "missing"
            elif result.generated_on is None:
                result.generated_on = generated
                result.generation_date_source = "preamble"
            continue

        account_number_match = _ACCOUNT_NUMBER_RE.fullmatch(first)
        if _ACCOUNT_SECTION_RE.match(first):
            close_header_check(current)
            raw_number = account_number_match.group(1) if account_number_match else None
            current = start_section(raw_number, record.line_start)
            continue

        balance_match = _BALANCE_RE.fullmatch(first)
        if balance_match and current is not None:
            expected_period_line[current.ordinal] = record.line_start + 2
            balance_date = _parse_date(balance_match.group(1))
            if balance_date is None:
                result.anomalies.append(
                    _anomaly(
                        AnomalyCode.INVALID_DATE,
                        "blocking",
                        section=current.ordinal,
                        line=record.line_start,
                        field="balance_as_of",
                    )
                )
            else:
                current.balance_as_of = balance_date
            balance_text = balance_match.group(2).strip()
            if balance_text.endswith("€"):
                balance_text = balance_text[:-1].strip()
            balance = parse_french_amount(balance_text)
            if isinstance(balance, AnomalyCode):
                result.anomalies.append(
                    _anomaly(
                        balance,
                        "blocking",
                        section=current.ordinal,
                        line=record.line_start,
                        field="header_balance_cents",
                    )
                )
            else:
                current.header_balance_cents = balance
            continue

        period_match = _PERIOD_RE.fullmatch(first)
        if period_match and current is not None:
            period_phrase_seen[current.ordinal] = True
            expected_header_line[current.ordinal] = record.line_start + 2
            period_from = _parse_date(period_match.group(1))
            period_to = _parse_date(period_match.group(2))
            if period_from is None or period_to is None or period_from > period_to:
                result.anomalies.append(
                    _anomaly(
                        AnomalyCode.INVALID_DATE,
                        "blocking",
                        section=current.ordinal,
                        line=record.line_start,
                        field="period",
                    )
                )
            else:
                current.period_from = period_from
                current.period_to = period_to
            continue

        if _header_matches(row):
            if current is None:
                current = ParsedSection(ordinal=1, account_number_canonical=None)
                result.sections.append(current)
                section_start_line[current.ordinal] = record.line_start
                expected_header_line[current.ordinal] = record.line_start + 1
                expected_period_line[current.ordinal] = record.line_start + 1
                period_phrase_seen[current.ordinal] = False
                header_seen[current.ordinal] = False
                candidate_idx[current.ordinal] = 0
                pending_blanks[current.ordinal] = []
            if header_seen[current.ordinal]:
                result.anomalies.append(
                    _anomaly(
                        AnomalyCode.DUPLICATE_HEADER,
                        "warning",
                        section=current.ordinal,
                        line=record.line_start,
                        field="header",
                    )
                )
            else:
                header_seen[current.ordinal] = True
            continue

        if _is_metadata_line(first):
            continue

        if current is None:
            if _looks_like_data(row):
                result.anomalies.append(
                    _anomaly(
                        AnomalyCode.MISSING_HEADER,
                        "blocking",
                        line=record.line_start,
                        field="header",
                    )
                )
            continue
        if not header_seen[current.ordinal]:
            if _looks_like_data(row) and current.ordinal not in missing_header_reported:
                result.anomalies.append(
                    _anomaly(
                        AnomalyCode.MISSING_HEADER,
                        "blocking",
                        section=current.ordinal,
                        line=record.line_start,
                        field="header",
                    )
                )
                missing_header_reported.add(current.ordinal)
            continue

        candidate_idx[current.ordinal] += 1
        logical_row_idx = candidate_idx[current.ordinal]
        if len(row) != 5 or row[4].strip():
            result.anomalies.append(
                _anomaly(
                    AnomalyCode.BAD_FIELD_COUNT,
                    "blocking",
                    section=current.ordinal,
                    row=logical_row_idx,
                    line=record.line_start,
                    field="row",
                )
            )
            continue

        booked_on = _parse_date(row[0])
        if booked_on is None:
            result.anomalies.append(
                _anomaly(
                    AnomalyCode.INVALID_DATE,
                    "blocking",
                    section=current.ordinal,
                    row=logical_row_idx,
                    line=record.line_start,
                    field="booked_on",
                )
            )
            continue

        amount = parse_debit_credit(row[2], row[3])
        if isinstance(amount, AnomalyCode):
            result.anomalies.append(
                _anomaly(
                    amount,
                    "blocking",
                    section=current.ordinal,
                    row=logical_row_idx,
                    line=record.line_start,
                    field="amount_cents",
                )
            )
            continue

        raw_label = row[1]
        if len(raw_label) > _LONG_LABEL_WARNING_CHARS:
            result.anomalies.append(
                _anomaly(
                    AnomalyCode.LONG_LABEL,
                    "warning",
                    section=current.ordinal,
                    row=logical_row_idx,
                    line=record.line_start,
                    field="raw_label",
                )
            )
        if pending_blanks[current.ordinal]:
            result.anomalies.append(
                _anomaly(
                    AnomalyCode.INTERLEAVED_BLANK_LINES,
                    "warning",
                    section=current.ordinal,
                    row=logical_row_idx,
                    line=pending_blanks[current.ordinal][0],
                    field="row",
                )
            )
            pending_blanks[current.ordinal].clear()

        parsed_row = ParsedRow(
            logical_row_idx=logical_row_idx,
            booked_on=booked_on,
            amount_cents=amount,
            raw_label=raw_label,
            line_start=record.line_start,
            line_end=record.line_end,
            section_ordinal=current.ordinal,
        )
        current.rows.append(parsed_row)

        if (
            current.period_from is not None
            and current.period_to is not None
            and not (current.period_from <= booked_on <= current.period_to)
        ):
            result.anomalies.append(
                _anomaly(
                    AnomalyCode.OUT_OF_PERIOD,
                    "blocking",
                    section=current.ordinal,
                    row=logical_row_idx,
                    line=record.line_start,
                    field="booked_on",
                )
            )
        if result.generated_on is not None and booked_on >= result.generated_on:
            result.anomalies.append(
                _anomaly(
                    AnomalyCode.PROVISIONAL_DAY,
                    "warning",
                    section=current.ordinal,
                    row=logical_row_idx,
                    line=record.line_start,
                    field="booked_on",
                )
            )

    close_header_check(current)
    if not result.sections:
        result.anomalies.append(
            _anomaly(AnomalyCode.MISSING_HEADER, "blocking", line=1, field="header")
        )

    for section in result.sections:
        if not period_phrase_seen.get(section.ordinal, False):
            result.anomalies.append(
                _anomaly(
                    AnomalyCode.MISSING_PERIOD,
                    "blocking",
                    section=section.ordinal,
                    line=expected_period_line.get(
                        section.ordinal, section_start_line.get(section.ordinal)
                    ),
                    field="period",
                )
            )
        if section.period_to is None:
            section.complete_through = None
        elif result.generated_on is not None:
            previous = _previous_day(result.generated_on)
            section.complete_through = min(section.period_to, previous) if previous else None
        elif section.rows:
            latest = max(row.booked_on for row in section.rows)
            previous = _previous_day(latest)
            section.complete_through = min(section.period_to, previous) if previous else None
            for parsed_row in section.rows:
                if parsed_row.booked_on == latest:
                    result.anomalies.append(
                        _anomaly(
                            AnomalyCode.PROVISIONAL_DAY,
                            "warning",
                            section=section.ordinal,
                            row=parsed_row.logical_row_idx,
                            line=parsed_row.line_start,
                            field="booked_on",
                        )
                    )
        else:
            section.complete_through = None
    return result


def _previous_day(value: date) -> date | None:
    return None if value == date.min else value - timedelta(days=1)


def parse_export(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
) -> ParsedExport:
    """Parse CSV bytes without IO, persistence, logging, or partial exceptions."""

    try:
        if type(max_field_chars) is not int or max_field_chars < 0:
            return ParsedExport(
                encoding="unknown",
                generated_on=None,
                generation_date_source="missing",
                anomalies=[_anomaly(AnomalyCode.PARSE_ERROR, "blocking", field="file")],
            )
        field_limit = min(max_field_chars, _ABSOLUTE_MAX_FIELD_CHARS)
        return _parse_export_impl(data, max_bytes=max_bytes, max_field_chars=field_limit)
    except Exception:
        return ParsedExport(
            encoding="unknown",
            generated_on=None,
            generation_date_source="missing",
            anomalies=[_anomaly(AnomalyCode.PARSE_ERROR, "blocking", field="file")],
        )
