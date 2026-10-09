"""Strict, deterministic decoding for Crédit Agricole exports."""

from __future__ import annotations

from dataclasses import dataclass

from app.importers.types import AnomalyCode

DEFAULT_MAX_BYTES = 20_000_000


@dataclass(frozen=True, slots=True)
class DecodedExport:
    text: str
    encoding: str


def decode_export_bytes(
    data: bytes, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> DecodedExport | AnomalyCode:
    """Decode UTF-8 (with an optional BOM) or strict CP1252.

    Newline characters remain available to the CSV scanner. That scanner treats
    CRLF, LF and CR alike as record separators while preserving line breaks
    inside quoted fields.
    """

    if type(data) is not bytes or type(max_bytes) is not int or max_bytes < 0:
        return AnomalyCode.PARSE_ERROR
    if len(data) > max_bytes:
        return AnomalyCode.FILE_TOO_LARGE
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return DecodedExport(data.decode("utf-8-sig", errors="strict"), "utf-8-sig")
        except UnicodeDecodeError:
            return AnomalyCode.INVALID_ENCODING
    try:
        return DecodedExport(data.decode("utf-8", errors="strict"), "utf-8")
    except UnicodeDecodeError:
        try:
            return DecodedExport(data.decode("cp1252", errors="strict"), "cp1252")
        except UnicodeDecodeError:
            return AnomalyCode.INVALID_ENCODING


def normalize_line_endings(text: str) -> str:
    """Normalize physical line separators for metadata-only text handling."""

    return text.replace("\r\n", "\n").replace("\r", "\n")
