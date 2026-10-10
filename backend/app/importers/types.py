"""Typed, privacy-safe values shared by the import parsers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.importers.account_identity import CanonicalAccountNumber


class AnomalyCode(StrEnum):
    """Stable parser anomaly codes; messages never include source text."""

    AMOUNT_OUT_OF_RANGE = "AMOUNT_OUT_OF_RANGE"
    BAD_FIELD_COUNT = "BAD_FIELD_COUNT"
    BOTH_AMOUNTS = "BOTH_AMOUNTS"
    COVERAGE_TAIL_GAP = "COVERAGE_TAIL_GAP"
    DATE_IMPLAUSIBLE = "DATE_IMPLAUSIBLE"
    DUPLICATE_ACCOUNT_SECTION = "DUPLICATE_ACCOUNT_SECTION"
    DUPLICATE_HEADER = "DUPLICATE_HEADER"
    EMPTY_FILE = "EMPTY_FILE"
    FIELD_TOO_LONG = "FIELD_TOO_LONG"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FUTURE_DATED_ROW = "FUTURE_DATED_ROW"
    GENERATION_DATE_INCONSISTENT = "GENERATION_DATE_INCONSISTENT"
    INTERLEAVED_BLANK_LINES = "INTERLEAVED_BLANK_LINES"
    INVALID_ACCOUNT_NUMBER = "INVALID_ACCOUNT_NUMBER"
    INVALID_DATE = "INVALID_DATE"
    INVALID_ENCODING = "INVALID_ENCODING"
    LONG_LABEL = "LONG_LABEL"
    LABEL_TOO_LONG = "LABEL_TOO_LONG"
    MISSING_ACCOUNT_NUMBER = "MISSING_ACCOUNT_NUMBER"
    MISSING_HEADER = "MISSING_HEADER"
    MISSING_PERIOD = "MISSING_PERIOD"
    NEGATIVE_AMOUNT = "NEGATIVE_AMOUNT"
    NON_NUMERIC_AMOUNT = "NON_NUMERIC_AMOUNT"
    OUT_OF_PERIOD = "OUT_OF_PERIOD"
    PARSE_ERROR = "PARSE_ERROR"
    PROVISIONAL_DAY = "PROVISIONAL_DAY"
    UNCLOSED_QUOTE = "UNCLOSED_QUOTE"
    ZERO_AMOUNT = "ZERO_AMOUNT"


Severity = Literal["blocking", "warning"]
GenerationDateSource = Literal["preamble", "missing"]


@dataclass(frozen=True, slots=True)
class Anomaly:
    code: AnomalyCode
    severity: Severity
    section_ordinal: int | None = None
    logical_row_idx: int | None = None
    line_no: int | None = None
    field: str | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class ParsedRow:
    logical_row_idx: int
    booked_on: date
    amount_cents: int
    raw_label: str = field(repr=False)
    line_start: int
    line_end: int
    section_ordinal: int

    @property
    def physical_line_start(self) -> int:
        return self.line_start

    @property
    def physical_line_end(self) -> int:
        return self.line_end


@dataclass(slots=True)
class ParsedSection:
    ordinal: int
    account_number_canonical: CanonicalAccountNumber | None
    balance_as_of: date | None = None
    header_balance_cents: int | None = None
    period_from: date | None = None
    period_to: date | None = None
    complete_through: date | None = None
    rows: list[ParsedRow] = field(default_factory=list)

    @property
    def section_index(self) -> int:
        """Name matching ``import_account_sections.section_index``."""

        return self.ordinal

    @property
    def coverage_from(self) -> date | None:
        return self.period_from

    @property
    def coverage_through(self) -> date | None:
        return self.period_to

    @property
    def section_operation_count(self) -> int:
        return len(self.rows)


@dataclass(slots=True)
class ParsedExport:
    encoding: str
    generated_on: date | None
    generation_date_source: GenerationDateSource
    sections: list[ParsedSection] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)

    @property
    def has_blocking_anomalies(self) -> bool:
        return any(anomaly.severity == "blocking" for anomaly in self.anomalies)

    @property
    def rows(self) -> list[ParsedRow]:
        """Return rows in source section order without changing their contents."""

        return [row for section in self.sections for row in section.rows]

    @property
    def operation_count(self) -> int:
        return sum(len(section.rows) for section in self.sections)
