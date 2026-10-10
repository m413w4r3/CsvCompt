"""Préparation des aperçus d'import Crédit Agricole, sans écriture métier."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import Account, ImportFile, Transaction
from app.db.session import session_scope
from app.importers.account_identity import bank_fingerprint
from app.importers.credit_agricole import parse_export
from app.importers.normalize import normalize_label
from app.importers.types import ParsedSection
from app.services.app_secrets import ensure_hmac_key
from app.services.import_preview_store import ImportPreviewStore

_ACCOUNT_ROLES = {"current", "savings", "pea", "personal", "cash", "other"}
_LONG_NUMBER = re.compile(r"\d{8,}")
_SCRIPT_TAG = re.compile(r"<\s*/?\s*script\b", re.IGNORECASE)
_INSTRUCTION_PATTERN = re.compile(r"\b(ignore|disregard|follow)\b", re.IGNORECASE)


def _d(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _record_decision(
    preview: dict[str, Any], *, kind: str, target: str, before: Any, after: Any
) -> None:
    timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    preview.setdefault("_decision_history", []).append(
        {
            "kind": kind,
            "target": target,
            "before": before,
            "after": after,
            "actor": "local_user",
            "at_utc": timestamp,
        }
    )


def _section_response(
    section: ParsedSection, fingerprint: str | None, session: Session
) -> dict[str, Any]:
    matching: list[Account] = []
    if fingerprint is not None:
        matching = list(
            session.scalars(select(Account).where(Account.bank_fingerprint == fingerprint))
        )
    resolution = "unknown" if not matching else "known" if len(matching) == 1 else "ambiguous"
    account = matching[0] if len(matching) == 1 else None
    rows = section.rows
    debits = sum(row.amount_cents for row in rows if row.amount_cents < 0)
    credits = sum(row.amount_cents for row in rows if row.amount_cents > 0)
    return {
        "section_index": section.ordinal,
        "account_mapping": {
            "status": resolution,
            "account_id": account.account_id if account else None,
            "alias": account.alias if account else None,
        },
        "account_resolution": resolution,
        "account_id": account.account_id if account else None,
        "account_alias": account.alias if account else None,
        "period_from": _d(section.coverage_from),
        "period_to": _d(section.coverage_through),
        "coverage_from": _d(section.coverage_from),
        "coverage_through": _d(section.coverage_through),
        "complete_through": _d(section.complete_through),
        "operation_count": len(rows),
        "debit_total_cents": debits,
        "credit_total_cents": credits,
        "net_cents": debits + credits,
        "balance_as_of": _d(section.balance_as_of),
        "header_balance_cents": section.header_balance_cents,
        "min_date": _d(min((row.booked_on for row in rows), default=None)),
        "max_date": _d(max((row.booked_on for row in rows), default=None)),
        "provisional_days": [],
        "anomaly_counts": {},
    }


def _rejection_code(code: str) -> str:
    if code == "MISSING_PERIOD":
        return "SECTION_RANGE_REQUIRED"
    if code in {"BOTH_AMOUNTS", "NEGATIVE_AMOUNT", "ZERO_AMOUNT", "NON_NUMERIC_AMOUNT"}:
        return "VALIDATION_ERROR"
    if code in {
        "INVALID_DATE",
        "BAD_FIELD_COUNT",
        "UNCLOSED_QUOTE",
        "INVALID_ENCODING",
        "EMPTY_FILE",
        "MISSING_HEADER",
        "PARSE_ERROR",
        "FILE_TOO_LARGE",
        "FIELD_TOO_LONG",
    }:
        return "CSV_INVALID"
    return code


def build_preview(
    *,
    raw_bytes: bytes,
    engine: Engine,
    store: ImportPreviewStore,
    max_upload_bytes: int,
    received_on: date,
) -> dict[str, Any]:
    """Parse, aggregate, and privately save one upload; only HMAC settings may be written."""
    if len(raw_bytes) > max_upload_bytes:
        raise AppError("FILE_TOO_LARGE", 413, "Le fichier dépasse la taille maximale autorisée.")
    key = ensure_hmac_key(engine)
    parsed = parse_export(raw_bytes, max_bytes=max_upload_bytes)
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    with session_scope(engine) as session:
        if (
            session.scalar(
                select(ImportFile.import_file_id).where(
                    ImportFile.file_sha256 == file_hash, ImportFile.status == "committed"
                )
            )
            is not None
        ):
            imported_id = session.scalar(
                select(ImportFile.import_file_id).where(
                    ImportFile.file_sha256 == file_hash, ImportFile.status == "committed"
                )
            )
        else:
            imported_id = None
        sections: list[dict[str, Any]] = []
        fingerprints: dict[int, str | None] = {}
        for section in parsed.sections:
            canonical = section.account_number_canonical
            fingerprint = bank_fingerprint(key, canonical) if canonical is not None else None
            fingerprints[section.ordinal] = fingerprint
            sections.append(_section_response(section, fingerprint, session))

    section_by_ordinal = {section.ordinal: section for section in parsed.sections}
    warnings: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for anomaly in parsed.anomalies:
        item = {
            "code": _rejection_code(anomaly.code.value),
            "detail_code": anomaly.code.value
            if _rejection_code(anomaly.code.value) != anomaly.code.value
            else None,
            "severity": anomaly.severity,
            "section": anomaly.section_ordinal,
            "logical_row_idx": anomaly.logical_row_idx,
            "field": anomaly.field,
            "message": anomaly.message,
        }
        if anomaly.severity == "warning":
            warnings.append(item)
        else:
            rejections.append(item)
        section_view = next(
            (value for value in sections if value["section_index"] == anomaly.section_ordinal), None
        )
        if section_view is not None:
            counts = section_view["anomaly_counts"]
            counts[anomaly.code.value] = counts.get(anomaly.code.value, 0) + 1
            if (
                anomaly.code.value == "PROVISIONAL_DAY"
                and anomaly.logical_row_idx is not None
                and anomaly.section_ordinal is not None
            ):
                parsed_section = section_by_ordinal.get(anomaly.section_ordinal)
                row = (
                    next(
                        (
                            row
                            for row in parsed_section.rows
                            if row.logical_row_idx == anomaly.logical_row_idx
                        ),
                        None,
                    )
                    if parsed_section is not None
                    else None
                )
                if row is not None:
                    section_view["provisional_days"].append(row.booked_on.isoformat())
    for parsed_section in parsed.sections:
        view = next(item for item in sections if item["section_index"] == parsed_section.ordinal)
        for row in parsed_section.rows:
            label_warnings = list(normalize_label(row.raw_label).warnings)
            if _SCRIPT_TAG.search(row.raw_label):
                label_warnings.append("SCRIPT_LABEL")
            label_folded = row.raw_label.casefold()
            if _INSTRUCTION_PATTERN.search(row.raw_label) and any(
                token in label_folded for token in ("instruction", "previous", "system", "prompt")
            ):
                label_warnings.append("PROMPT_INJECTION")
            if any(unicodedata.category(char) == "So" for char in row.raw_label):
                label_warnings.append("UNICODE_EXOTIC")
            for warning_code in dict.fromkeys(label_warnings):
                warning = {
                    "code": warning_code,
                    "severity": "warning",
                    "section": parsed_section.ordinal,
                    "logical_row_idx": row.logical_row_idx,
                    "field": "label",
                    "message": "Le libellé contient une caractéristique nécessitant une revue.",
                }
                if warning not in warnings:
                    warnings.append(warning)
                view["anomaly_counts"][warning_code] = (
                    view["anomaly_counts"].get(warning_code, 0) + 1
                )
    if any(a.code.value == "DUPLICATE_ACCOUNT_SECTION" for a in parsed.anomalies):
        rejections.append(
            {
                "code": "DUPLICATE_ACCOUNT_SECTION",
                "severity": "blocking",
                "section": None,
                "logical_row_idx": None,
                "field": None,
                "message": "Deux sections du fichier correspondent au même compte.",
            }
        )
    period_missing_sections = {
        a.section_ordinal for a in parsed.anomalies if a.code.value == "MISSING_PERIOD"
    }
    out_of_period_rows = [
        {"section": a.section_ordinal, "row": a.logical_row_idx}
        for a in parsed.anomalies
        if a.code.value == "OUT_OF_PERIOD"
        and a.section_ordinal is not None
        and a.logical_row_idx is not None
    ]
    payload: dict[str, Any] = {
        "status": "previewed",
        "encoding": parsed.encoding,
        "file_sha256": file_hash,
        "file_size_bytes": len(raw_bytes),
        "generated_on": _d(parsed.generated_on or received_on),
        "generation_date_source": "header_download"
        if parsed.generated_on
        else "received_on_fallback",
        "operation_count": parsed.operation_count,
        "sections": sections,
        "warnings": warnings,
        "rejections": rejections,
        "can_commit": False,
        "already_imported": imported_id is not None,
        "import_file_id": imported_id,
        "reconciliation": None,
        "_fingerprints": fingerprints,
        "_received_on": received_on.isoformat(),
        "_preview_protocol_version": 2,
        "_resolutions": {"account_mapping": {}, "coverage": {}, "multiplicity": {}},
        "_period_missing_sections": list(period_missing_sections),
        "_out_of_period_rows": list(out_of_period_rows),
        "_structural_rejection": any(
            a.code.value == "DUPLICATE_ACCOUNT_SECTION" for a in parsed.anomalies
        ),
    }
    _recalculate(payload)
    return store.create(payload, raw_bytes)


def _recalculate(preview: dict[str, Any]) -> None:
    resolved_multiplicity = preview["_resolutions"]["multiplicity"]
    pending_mapping = any(
        section["account_mapping"]["status"] != "known"
        and str(section["section_index"]) not in preview["_resolutions"]["account_mapping"]
        for section in preview["sections"]
    )
    pending_range = any(
        section_index not in preview["_resolutions"]["coverage"]
        for section_index in preview["_period_missing_sections"]
    )
    pending_out_of_period = any(
        f"{item['section']}:{item['row']}" not in resolved_multiplicity
        for item in preview["_out_of_period_rows"]
    )
    pending_blocking = any(item["severity"] == "blocking" for item in preview["rejections"])
    duplicate_mapped_accounts = False
    account_ids = [
        section["account_mapping"].get("account_id")
        or preview["_resolutions"]["account_mapping"]
        .get(str(section["section_index"]), {})
        .get("account_id")
        for section in preview["sections"]
    ]
    non_null = [account_id for account_id in account_ids if account_id]
    duplicate_mapped_accounts = len(non_null) != len(set(non_null))
    preview["rejections"] = [
        rejection
        for rejection in preview["rejections"]
        if not (
            rejection["code"] == "DUPLICATE_ACCOUNT_SECTION" and rejection.get("section") is None
        )
    ]
    if duplicate_mapped_accounts and not preview["_structural_rejection"]:
        preview["rejections"].append(
            {
                "code": "DUPLICATE_ACCOUNT_SECTION",
                "detail_code": None,
                "severity": "blocking",
                "section": None,
                "logical_row_idx": None,
                "field": None,
                "message": "Deux sections du fichier correspondent au même compte.",
            }
        )
    preview["can_commit"] = not any(
        (
            pending_mapping,
            pending_range,
            pending_out_of_period,
            pending_blocking,
            duplicate_mapped_accounts,
            preview["_structural_rejection"],
        )
    )


def set_mapping(
    preview: dict[str, Any], section_index: int, resolution: dict[str, Any], engine: Engine
) -> dict[str, Any]:
    section = next(
        (item for item in preview["sections"] if item["section_index"] == section_index), None
    )
    if section is None:
        raise AppError("NOT_FOUND", 404, "Section introuvable.")
    mapping: dict[str, Any]
    if "account_id" in resolution:
        with session_scope(engine) as session:
            account = session.get(Account, resolution["account_id"])
            if account is None or not account.is_active:
                raise AppError("VALIDATION_ERROR", 422, "Le compte sélectionné est indisponible.")
            mapping = {"status": "known", "account_id": account.account_id, "alias": account.alias}
    else:
        value = resolution["new_account"]
        alias = value["alias"].strip()
        if not alias or len(alias) > 80 or _LONG_NUMBER.search(alias):
            raise AppError("VALIDATION_ERROR", 422, "L'alias est invalide.")
        if value["account_role"] not in _ACCOUNT_ROLES:
            raise AppError("VALIDATION_ERROR", 422, "Le rôle de compte est invalide.")
        mapping = {
            "status": "unknown",
            "account_id": None,
            "alias": alias,
            "new_account": {"alias": alias, "account_role": value["account_role"]},
        }
    preview["_resolutions"]["account_mapping"][str(section_index)] = mapping
    _record_decision(
        preview,
        kind="account_mapping",
        target=str(section_index),
        before=section.get("account_mapping"),
        after=mapping,
    )
    section["account_mapping"] = mapping
    section["account_resolution"] = mapping["status"]
    section["account_id"] = mapping["account_id"]
    section["account_alias"] = mapping["alias"]
    _recalculate(preview)
    return preview


def set_coverage(
    preview: dict[str, Any],
    section_index: int,
    coverage_from: date,
    coverage_through: date,
    generated_on: date | None,
) -> dict[str, Any]:
    if coverage_through < coverage_from:
        raise AppError("VALIDATION_ERROR", 422, "La période de couverture est invalide.")
    section = next(
        (item for item in preview["sections"] if item["section_index"] == section_index), None
    )
    if section is None:
        raise AppError("NOT_FOUND", 404, "Section introuvable.")
    if section_index not in preview["_period_missing_sections"]:
        raise AppError(
            "VALIDATION_ERROR", 422, "Cette section n'a pas de plage de période à résoudre."
        )
    minimum = date.fromisoformat(section["min_date"]) if section.get("min_date") else None
    maximum = date.fromisoformat(section["max_date"]) if section.get("max_date") else None
    if (minimum is not None and coverage_from > minimum) or (
        maximum is not None and coverage_through < maximum
    ):
        raise AppError(
            "VALIDATION_ERROR", 422, "La plage doit contenir toutes les opérations de la section."
        )
    complete_through = None
    provisional_day = generated_on or (
        date.fromisoformat(section["max_date"])
        if section["operation_count"] > 0 and section["max_date"]
        else None
    )
    if provisional_day is not None:
        complete_through = min(coverage_through, provisional_day - timedelta(days=1))
    previous_coverage = preview["_resolutions"]["coverage"].get(str(section_index))
    if previous_coverage is None:
        previous_coverage = {
            "coverage_from": section.get("coverage_from"),
            "coverage_through": section.get("coverage_through"),
        }
    next_coverage = {
        "coverage_from": coverage_from.isoformat(),
        "coverage_through": coverage_through.isoformat(),
    }
    section.update(
        coverage_from=coverage_from.isoformat(),
        coverage_through=coverage_through.isoformat(),
        complete_through=complete_through.isoformat() if complete_through else None,
    )
    preview["_resolutions"]["coverage"][str(section_index)] = next_coverage
    _record_decision(
        preview,
        kind="coverage",
        target=str(section_index),
        before=previous_coverage,
        after=next_coverage,
    )
    preview["_period_missing_sections"] = [
        i for i in preview["_period_missing_sections"] if i != section_index
    ]
    preview["rejections"] = [
        r
        for r in preview["rejections"]
        if not (r["code"] == "SECTION_RANGE_REQUIRED" and r["section"] == section_index)
    ]
    _recalculate(preview)
    return preview


def set_multiplicity(
    preview: dict[str, Any],
    row_idx: int,
    action: str,
    *,
    section_index: int | None = None,
    transaction_id: str | None = None,
) -> dict[str, Any]:
    matches = [
        item
        for item in preview["_out_of_period_rows"]
        if item["row"] == row_idx and (section_index is None or item["section"] == section_index)
    ]
    if not matches:
        raise AppError(
            "VALIDATION_ERROR", 422, "Cette ligne ne requiert pas de décision de multiplicité."
        )
    if section_index is None and len(matches) > 1:
        raise AppError(
            "MULTIPLICITY_AMBIGUOUS",
            409,
            "L'index logique apparaît dans plusieurs sections.",
            {"sections": [item["section"] for item in matches]},
        )
    selected = matches[0]
    resolution_key = f"{selected['section']}:{selected['row']}"
    resolution = {"action": action}
    if transaction_id is not None:
        resolution["transaction_id"] = transaction_id
    previous_resolution = preview["_resolutions"]["multiplicity"].get(resolution_key)
    preview["_resolutions"]["multiplicity"][resolution_key] = resolution
    _record_decision(
        preview,
        kind="multiplicity",
        target=resolution_key,
        before=previous_resolution,
        after=resolution,
    )
    preview["rejections"] = [
        r
        for r in preview["rejections"]
        if not (
            r["code"] == "OUT_OF_PERIOD"
            and r["logical_row_idx"] == row_idx
            and r["section"] == selected["section"]
        )
    ]
    _recalculate(preview)
    return preview


def validate_link_existing_preview(
    preview: dict[str, Any],
    raw_bytes: bytes,
    engine: Engine,
    *,
    section_index: int,
    row_idx: int,
    transaction_id: str,
) -> None:
    """Check a human link target at preview time against the strict row identity."""

    parsed = parse_export(raw_bytes)
    parsed_section = next((item for item in parsed.sections if item.ordinal == section_index), None)
    row = (
        next((item for item in parsed_section.rows if item.logical_row_idx == row_idx), None)
        if parsed_section is not None
        else None
    )
    section_preview = next(
        (item for item in preview["sections"] if item["section_index"] == section_index), None
    )
    if row is None or section_preview is None:
        raise AppError("VALIDATION_ERROR", 422, "La ligne à lier est introuvable.")
    mapping = (
        preview.get("_resolutions", {})
        .get("account_mapping", {})
        .get(str(section_index), section_preview.get("account_mapping", {}))
    )
    account_id = mapping.get("account_id") if isinstance(mapping, dict) else None
    if not account_id:
        raise AppError(
            "VALIDATION_ERROR", 422, "Un compte connu est requis pour lier une occurrence."
        )
    identity_label = normalize_label(row.raw_label).identity_label.value
    with session_scope(engine) as session:
        transaction = session.get(Transaction, transaction_id)
        if transaction is None or (
            transaction.account_id,
            transaction.booked_on,
            transaction.amount_cents,
            transaction.identity_label,
        ) != (account_id, row.booked_on, row.amount_cents, identity_label):
            raise AppError(
                "VALIDATION_ERROR", 422, "La transaction cible n'a pas la même identité."
            )


def public_preview(preview: dict[str, Any]) -> dict[str, Any]:
    """Strip private resolution and identity metadata before producing the API response."""
    return {
        key: value
        for key, value in preview.items()
        if not key.startswith("_") and key not in {"file_size_bytes", "created_at_utc"}
    }
