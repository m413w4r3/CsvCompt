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
_IBAN_LIKE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$", re.IGNORECASE)
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
    max_anomalies: int = 1000,
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
    anomaly_totals: dict[str, int] = {}
    warning_total = 0
    rejection_total = 0
    anomaly_limit = max(1, max_anomalies)
    for anomaly in parsed.anomalies:
        code = _rejection_code(anomaly.code.value)
        item = {
            "code": code,
            "detail_code": anomaly.code.value if code != anomaly.code.value else None,
            "severity": anomaly.severity,
            "section": anomaly.section_ordinal,
            "logical_row_idx": anomaly.logical_row_idx,
            "field": anomaly.field,
            "message": anomaly.message,
        }
        anomaly_totals[code] = anomaly_totals.get(code, 0) + 1
        if anomaly.severity == "warning":
            warning_total += 1
            if len(warnings) < anomaly_limit:
                warnings.append(item)
        else:
            rejection_total += 1
            if len(rejections) < anomaly_limit:
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
                    day = row.booked_on.isoformat()
                    if day not in section_view["provisional_days"]:
                        section_view["provisional_days"].append(day)
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
                    warning_total += 1
                    anomaly_totals[warning_code] = anomaly_totals.get(warning_code, 0) + 1
                    if len(warnings) < anomaly_limit:
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
    parser_missing_account_sections = {
        anomaly.section_ordinal
        for anomaly in parsed.anomalies
        if anomaly.code.value == "MISSING_ACCOUNT_NUMBER"
    }
    for section in parsed.sections:
        if (
            section.account_number_canonical is None
            and section.ordinal not in parser_missing_account_sections
        ):
            anomaly_totals["MISSING_ACCOUNT_NUMBER"] = (
                anomaly_totals.get("MISSING_ACCOUNT_NUMBER", 0) + 1
            )
            section_view = next(
                item for item in sections if item["section_index"] == section.ordinal
            )
            section_view["anomaly_counts"]["MISSING_ACCOUNT_NUMBER"] = (
                section_view["anomaly_counts"].get("MISSING_ACCOUNT_NUMBER", 0) + 1
            )
            rejection_total += 1
            if len(rejections) < anomaly_limit:
                rejections.append(
                    {
                        "code": "MISSING_ACCOUNT_NUMBER",
                        "detail_code": None,
                        "severity": "blocking",
                        "section": section.ordinal,
                        "logical_row_idx": None,
                        "field": "account_number",
                        "message": "Le numéro de compte de cette section n'a pas pu être reconnu.",
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
    all_out_of_period_rows = out_of_period_rows
    out_of_period_rows = out_of_period_rows[:anomaly_limit]
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
        "anomaly_counts": anomaly_totals,
        "anomaly_summary": {
            "warning_count": warning_total,
            "rejection_count": rejection_total,
            "stored_warning_count": len(warnings),
            "stored_rejection_count": len(rejections),
            "omitted_warning_count": max(0, warning_total - len(warnings)),
            "omitted_rejection_count": max(0, rejection_total - len(rejections)),
        },
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
        "_canonical_sections": [
            section.ordinal
            for section in parsed.sections
            if section.account_number_canonical is not None
        ],
        "_initial_known_sections": [
            section["section_index"]
            for section in sections
            if section["account_mapping"]["status"] == "known"
        ],
        "_row_dates": [
            {
                "section": section.ordinal,
                "row": row.logical_row_idx,
                "date": row.booked_on.isoformat(),
            }
            for section in parsed.sections
            for row in section.rows
        ],
        "_out_of_period_rows": list(out_of_period_rows),
        "_out_of_period_omitted": len(all_out_of_period_rows) - len(out_of_period_rows),
        "_unresolvable_blocking_count": sum(
            1
            for anomaly in parsed.anomalies
            if anomaly.severity == "blocking"
            and anomaly.code.value
            not in {"MISSING_PERIOD", "OUT_OF_PERIOD", "DUPLICATE_ACCOUNT_SECTION"}
        )
        + sum(1 for section in parsed.sections if section.account_number_canonical is None),
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
    # Rebuild resolution-dependent rejections before reading their blocking state.
    preview["rejections"] = [
        rejection
        for rejection in preview["rejections"]
        if rejection["code"]
        not in {
            "ACCOUNT_MAPPING_REQUIRED",
            "DUPLICATE_ACCOUNT_SECTION",
            "DUPLICATE_ACCOUNT_ALIAS",
        }
    ]
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
    if duplicate_mapped_accounts or preview["_structural_rejection"]:
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
    pending_aliases: set[str] = set()
    duplicate_alias = False
    for section in preview["sections"]:
        section_index = str(section["section_index"])
        mapping = preview["_resolutions"]["account_mapping"].get(
            section_index, section["account_mapping"]
        )
        if int(section_index) not in preview.get(
            "_canonical_sections", [item["section_index"] for item in preview["sections"]]
        ):
            continue
        if (
            mapping.get("status") != "known"
            and section_index not in preview["_resolutions"]["account_mapping"]
        ):
            preview["rejections"].append(
                {
                    "code": "ACCOUNT_MAPPING_REQUIRED",
                    "detail_code": None,
                    "severity": "blocking",
                    "section": section["section_index"],
                    "logical_row_idx": None,
                    "field": "account_mapping",
                    "message": "Associez cette section à un compte avant de continuer.",
                }
            )
        alias = mapping.get("alias")
        if isinstance(alias, str):
            normalized_alias = alias.strip().casefold()
            if normalized_alias in pending_aliases:
                duplicate_alias = True
            pending_aliases.add(normalized_alias)
    if duplicate_alias:
        preview["rejections"].append(
            {
                "code": "DUPLICATE_ACCOUNT_ALIAS",
                "detail_code": None,
                "severity": "blocking",
                "section": None,
                "logical_row_idx": None,
                "field": "alias",
                "message": "Deux sections utilisent le même alias de compte.",
            }
        )
    pending_blocking = (
        any(item["severity"] == "blocking" for item in preview["rejections"])
        or preview.get("_unresolvable_blocking_count", 0) > 0
        or preview.get("_out_of_period_omitted", 0) > 0
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
    if section_index not in preview.get("_canonical_sections", []):
        raise AppError(
            "ACCOUNT_MAPPING_INVALID",
            422,
            "Cette section ne peut pas être associée sans numéro reconnu.",
        )
    initial_known_sections = preview.get(
        "_initial_known_sections",
        [
            item["section_index"]
            for item in preview["sections"]
            if item["account_mapping"].get("status") == "known"
        ],
    )
    if section_index in initial_known_sections:
        raise AppError(
            "ACCOUNT_MAPPING_LOCKED", 409, "Le compte de cette section est déjà identifié."
        )
    mapping: dict[str, Any]
    if "account_id" in resolution:
        with session_scope(engine) as session:
            account = session.get(Account, resolution["account_id"])
            if account is None or not account.is_active:
                raise AppError("VALIDATION_ERROR", 422, "Le compte sélectionné est indisponible.")
            fingerprint = preview.get("_fingerprints", {}).get(
                section_index, preview.get("_fingerprints", {}).get(str(section_index))
            )
            if account.bank_fingerprint is not None and account.bank_fingerprint != fingerprint:
                raise AppError(
                    "ACCOUNT_FINGERPRINT_CONFLICT", 422, "Le compte sélectionné est incompatible."
                )
            mapping = {"status": "known", "account_id": account.account_id, "alias": account.alias}
    else:
        value = resolution["new_account"]
        alias = value["alias"].strip()
        compact_alias = "".join(char for char in alias if char.isalnum())
        digit_count = sum(unicodedata.category(char) == "Nd" for char in alias)
        if (
            not alias
            or len(alias) > 80
            or digit_count > 7
            or _IBAN_LIKE.fullmatch(compact_alias) is not None
            or "<" in alias
            or ">" in alias
        ):
            raise AppError("VALIDATION_ERROR", 422, "L'alias est invalide.")
        if value["account_role"] not in _ACCOUNT_ROLES:
            raise AppError("VALIDATION_ERROR", 422, "Le rôle de compte est invalide.")
        mapping = {
            "status": "unknown",
            "account_id": None,
            "alias": alias,
            "new_account": {"alias": alias, "account_role": value["account_role"]},
        }
    if "new_account" in resolution:
        with session_scope(engine) as session:
            aliases = list(session.scalars(select(Account.alias)))
            if any(
                existing.strip().casefold() == mapping["alias"].strip().casefold()
                for existing in aliases
            ):
                raise AppError("ACCOUNT_ALIAS_CONFLICT", 409, "Cet alias est déjà utilisé.")
            fingerprint = preview.get("_fingerprints", {}).get(
                section_index, preview.get("_fingerprints", {}).get(str(section_index))
            )
            if fingerprint is not None and session.scalar(
                select(Account.account_id).where(Account.bank_fingerprint == fingerprint)
            ):
                raise AppError(
                    "ACCOUNT_FINGERPRINT_CONFLICT",
                    409,
                    "Cette empreinte est déjà liée à un compte.",
                )
    if "new_account" in resolution:
        for other in preview["sections"]:
            if other["section_index"] == section_index:
                continue
            other_mapping = preview["_resolutions"]["account_mapping"].get(
                str(other["section_index"]), other["account_mapping"]
            )
            if (
                other_mapping.get("alias")
                and other_mapping["alias"].strip().casefold() == mapping["alias"].strip().casefold()
            ):
                raise AppError(
                    "ACCOUNT_ALIAS_CONFLICT", 409, "Cet alias est déjà utilisé dans l'aperçu."
                )
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
    # Re-evaluate every row against the accepted coverage. The required span check
    # above means a valid resolution cannot leave an operation outside that span.
    preview["_out_of_period_rows"] = [
        item for item in preview["_out_of_period_rows"] if item["section"] != section_index
    ] + [
        {"section": section_index, "row": item["row"]}
        for item in preview.get("_row_dates", [])
        if item["section"] == section_index
        and not (coverage_from <= date.fromisoformat(item["date"]) <= coverage_through)
    ]
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
    actor: str = "local_user",
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
    preview["_decision_history"][-1]["actor"] = actor
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
