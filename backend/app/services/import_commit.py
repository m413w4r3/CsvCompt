"""Atomic, idempotent commit of a resolved import preview."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import (
    Account,
    AuditEvent,
    ImportAccountSection,
    ImportFile,
    ImportReconciliationCandidate,
    ImportRejection,
    SourceRow,
    Transaction,
)
from app.db.session import session_scope, write_transaction
from app.importers.account_identity import bank_fingerprint
from app.importers.credit_agricole import parse_export
from app.importers.normalize import OperationIdentity, near_duplicate_candidates, normalize_label
from app.importers.overlap import (
    ExistingOccurrence,
    IdentityKey,
    IncomingOccurrence,
    identity_key_sha256,
    plan_reconciliation,
)
from app.importers.types import Anomaly, ParsedRow, ParsedSection
from app.services.app_secrets import read_hmac_key
from app.services.idempotency import find_replay, request_fingerprint, save_response
from app.services.import_archive import ArchiveLifecycleLock, ImportArchive
from app.services.import_preview_store import ImportPreviewStore
from app.services.imports import MAX_IMPORT_OPERATION_COUNT, _rejection_code

_HTTP_METHOD = "POST"


@dataclass(frozen=True, slots=True)
class _PreparedRow:
    section: ParsedSection
    row: ParsedRow
    identity_key: IdentityKey
    row_sha256: str
    out_of_period: bool


def _now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _line_bytes(raw_bytes: bytes, row: ParsedRow) -> bytes:
    physical_lines = raw_bytes.splitlines(keepends=True)
    if row.line_start < 1 or row.line_end < row.line_start or row.line_end > len(physical_lines):
        raise AppError("PREVIEW_INVALID", 409, "Les octets de l'aperçu ne correspondent plus.")
    return b"".join(physical_lines[row.line_start - 1 : row.line_end])


def _prepare_rows(
    raw_bytes: bytes, sections: list[ParsedSection], parsed: Any
) -> list[_PreparedRow]:
    out_of_period = {
        (anomaly.section_ordinal, anomaly.logical_row_idx)
        for anomaly in parsed.anomalies
        if anomaly.code.value == "OUT_OF_PERIOD"
    }
    prepared: list[_PreparedRow] = []
    for section in sections:
        for row in section.rows:
            amount = row.amount_cents
            if type(amount) is not int or amount == 0:
                raise AppError("VALIDATION_ERROR", 422, "Le montant de l'aperçu est invalide.")
            label = normalize_label(row.raw_label)
            if "LABEL_TRUNCATED" in label.warnings:
                raise AppError(
                    "VALIDATION_ERROR", 422, "Le libellé dépasse la limite de normalisation."
                )
            key: IdentityKey = (
                "",  # Filled with the resolved account id inside the write transaction.
                row.booked_on.isoformat(),
                amount,
                label.identity_label.value,
            )
            prepared.append(
                _PreparedRow(
                    section=section,
                    row=row,
                    identity_key=key,
                    row_sha256=hashlib.sha256(_line_bytes(raw_bytes, row)).hexdigest(),
                    out_of_period=(section.ordinal, row.logical_row_idx) in out_of_period,
                )
            )
    return prepared


def _json(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _add_audit(
    session: Session,
    *,
    import_file_id: str,
    target_type: str,
    target_id: str,
    action: str,
    actor: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    created_at_utc: datetime | None = None,
) -> None:
    timestamp = {"created_at_utc": created_at_utc} if created_at_utc is not None else {}
    session.add(
        AuditEvent(
            import_file_id=import_file_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            actor=actor,
            before_json=_json(before),
            after_json=_json(after),
            idempotency_key=idempotency_key,
            **timestamp,
        )
    )


def _existing_for_keys(
    session: Session, keys: set[IdentityKey]
) -> dict[IdentityKey, list[Transaction]]:
    """Load existing strict identities in bounded batches, then index them in memory."""

    identities = list(keys)
    result: dict[IdentityKey, list[Transaction]] = {key: [] for key in identities}
    for offset in range(0, len(identities), 200):
        chunk = identities[offset : offset + 200]
        statement = select(Transaction).where(
            or_(
                *(
                    and_(
                        Transaction.account_id == account_id,
                        Transaction.booked_on == date.fromisoformat(booked_on),
                        Transaction.amount_cents == amount_cents,
                        Transaction.identity_label == identity_label,
                    )
                    for account_id, booked_on, amount_cents, identity_label in chunk
                )
            )
        )
        for transaction in session.scalars(statement):
            key: IdentityKey = (
                transaction.account_id,
                transaction.booked_on.isoformat(),
                transaction.amount_cents,
                transaction.identity_label,
            )
            result[key].append(transaction)
    for transactions in result.values():
        transactions.sort(key=lambda item: item.occurrence_rank)
    return result


def _mark_mapping_conflict(preview: dict[str, Any], code: str, section_index: int) -> None:
    message = {
        "ACCOUNT_ALIAS_CONFLICT": "Cet alias est déjà utilisé par un autre compte.",
        "ACCOUNT_FINGERPRINT_CONFLICT": (
            "Cette identité de compte est déjà associée à un autre compte."
        ),
    }[code]
    preview["rejections"] = [
        item
        for item in preview.get("rejections", [])
        if item.get("code") not in {"ACCOUNT_ALIAS_CONFLICT", "ACCOUNT_FINGERPRINT_CONFLICT"}
    ]
    preview["rejections"].append(
        {
            "code": code,
            "severity": "blocking",
            "section": section_index,
            "logical_row_idx": None,
            "field": "account_mapping",
            "message": message,
        }
    )
    preview["can_commit"] = False


def _new_transaction(
    session: Session,
    *,
    key: IdentityKey,
    rank: int,
    raw_label: str,
) -> Transaction:
    account_id, booked_on, amount_cents, identity_label = key
    if type(amount_cents) is not int:
        raise AppError("VALIDATION_ERROR", 422, "Le montant de l'aperçu est invalide.")
    transaction = Transaction(
        account_id=account_id,
        booked_on=date.fromisoformat(booked_on),
        amount_cents=amount_cents,
        raw_label=raw_label,
        identity_label=identity_label,
        identity_key_sha256=identity_key_sha256(key),
        occurrence_rank=rank,
    )
    session.add(transaction)
    session.flush()
    return transaction


def _insert_rejections(
    session: Session,
    *,
    import_file_id: str,
    anomalies: list[Anomaly],
    resolutions: dict[str, Any],
    actor: str,
    resolved_at: str,
) -> None:
    multiplicity = resolutions.get("multiplicity", {})
    coverage = resolutions.get("coverage", {})
    for anomaly in anomalies:
        action: str | None = None
        linked_transaction_id: str | None = None
        if (
            anomaly.code.value == "OUT_OF_PERIOD"
            and anomaly.section_ordinal
            and anomaly.logical_row_idx
        ):
            resolution = multiplicity.get(
                f"{anomaly.section_ordinal}:{anomaly.logical_row_idx}", {}
            )
            action = resolution.get("action")
            linked_transaction_id = resolution.get("transaction_id")
        elif anomaly.code.value == "MISSING_PERIOD" and anomaly.section_ordinal:
            if str(anomaly.section_ordinal) in coverage:
                action = "set_coverage"

        resolved = action is not None
        is_warning = anomaly.severity == "warning" or resolved
        code = _rejection_code(anomaly.code.value)
        session.add(
            ImportRejection(
                import_file_id=import_file_id,
                section_index=anomaly.section_ordinal,
                logical_row_idx=anomaly.logical_row_idx,
                field_name=anomaly.field,
                code=code,
                message=anomaly.message,
                severity="warning" if is_warning else "error",
                requires_resolution=int(action is not None),
                resolution_status="resolved" if resolved else "not_required",
                resolution_action=action,
                resolved_transaction_id=linked_transaction_id,
                resolved_by=actor if resolved else None,
                resolved_at_utc=datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
                if resolved
                else None,
            )
        )


def _audit_preview_decisions(
    session: Session,
    *,
    import_file_id: str,
    history: list[dict[str, Any]],
    default_actor: str,
) -> None:
    """Persist each human preview decision, including replaced prior values."""

    action_by_kind = {
        "account_mapping": "import.preview.account_mapping_resolution",
        "coverage": "import.preview.coverage_resolution",
        "multiplicity": "import.preview.multiplicity_resolution",
    }
    for decision in history:
        kind = decision.get("kind")
        if not isinstance(kind, str):
            continue
        action = action_by_kind.get(kind)
        target = decision.get("target")
        before = decision.get("before")
        after_value = decision.get("after")
        if action is None or not isinstance(target, str) or not isinstance(after_value, dict):
            continue
        try:
            at_utc = datetime.fromisoformat(str(decision["at_utc"]).replace("Z", "+00:00"))
            if kind == "multiplicity":
                section_key, logical_row_idx = target.split(":", 1)
                target_type = "import_row"
                target_id = f"{import_file_id}:{section_key}:{logical_row_idx}"
                before_json = {"resolution": before}
                after_json = {
                    "section_index": int(section_key),
                    "logical_row_idx": int(logical_row_idx),
                    "resolution": after_value,
                    "method": after_value.get("action"),
                }
            else:
                target_type = "import_section"
                target_id = f"{import_file_id}:{target}"
                before_json = {
                    "account_mapping" if kind == "account_mapping" else "coverage": before
                }
                after_json = {
                    "section_index": int(target),
                    "decision": after_value,
                    "method": "set_mapping" if kind == "account_mapping" else "set_coverage",
                }
        except (KeyError, TypeError, ValueError):
            continue
        _add_audit(
            session,
            import_file_id=import_file_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            actor=str(decision.get("actor") or default_actor),
            before=before_json,
            after=after_json,
            created_at_utc=at_utc,
        )


def _validate_preview(preview: dict[str, Any], expected_version: int) -> None:
    if expected_version != preview.get("version"):
        raise AppError("VERSION_CONFLICT", 409, "Cet aperçu a changé.")
    blocking = any(item.get("severity") == "blocking" for item in preview.get("rejections", []))
    if not preview.get("can_commit") or blocking:
        raise AppError(
            "IMPORT_NOT_COMMITTABLE",
            409,
            "L'aperçu doit être entièrement résolu avant le commit.",
        )


def _validate_link_targets_before_archive(
    engine: Engine, preview: dict[str, Any], prepared_rows: list[_PreparedRow]
) -> None:
    """Reject incomplete or stale manual links before creating an archive file."""

    resolutions = preview.get("_resolutions", {})
    multiplicity = resolutions.get("multiplicity", {})
    account_mappings = resolutions.get("account_mapping", {})
    section_views = {int(item["section_index"]): item for item in preview["sections"]}
    pending: list[tuple[str, IdentityKey]] = []
    targets: set[str] = set()
    for item in prepared_rows:
        if not item.out_of_period:
            continue
        choice = multiplicity.get(f"{item.section.ordinal}:{item.row.logical_row_idx}", {})
        if choice.get("action") != "link_existing":
            continue
        transaction_id = choice.get("transaction_id")
        view = section_views.get(item.section.ordinal, {})
        mapping = account_mappings.get(str(item.section.ordinal), view.get("account_mapping", {}))
        account_id = mapping.get("account_id") if isinstance(mapping, dict) else None
        if not isinstance(transaction_id, str) or not isinstance(account_id, str):
            raise AppError(
                "MULTIPLICITY_RESOLUTION_INVALID",
                422,
                "La résolution link_existing doit cibler une transaction existante.",
            )
        if transaction_id in targets:
            raise AppError(
                "MULTIPLICITY_RESOLUTION_INVALID",
                422,
                "Deux lignes hors période ne peuvent pas viser la même transaction.",
            )
        targets.add(transaction_id)
        pending.append(
            (
                transaction_id,
                (account_id, *item.identity_key[1:]),
            )
        )
    if not pending:
        return
    with session_scope(engine) as session:
        for transaction_id, expected_identity in pending:
            transaction = session.get(Transaction, transaction_id)
            if (
                transaction is None
                or (
                    transaction.account_id,
                    transaction.booked_on.isoformat(),
                    transaction.amount_cents,
                    transaction.identity_label,
                )
                != expected_identity
            ):
                raise AppError(
                    "MULTIPLICITY_RESOLUTION_INVALID",
                    422,
                    "La transaction cible n'a pas la même identité.",
                )


def _resolve_accounts(
    session: Session,
    *,
    preview: dict[str, Any],
    parsed_sections: list[ParsedSection],
    actor: str,
    import_file_id: str,
) -> tuple[dict[int, str], dict[int, str | None]]:
    key = read_hmac_key(session)
    if key is None:
        raise AppError(
            "ACCOUNT_IDENTITY_UNAVAILABLE", 409, "L'identité locale des comptes doit être résolue."
        )
    preview_sections = {int(item["section_index"]): item for item in preview["sections"]}
    resolutions = preview.get("_resolutions", {}).get("account_mapping", {})
    account_ids: dict[int, str] = {}
    fingerprints: dict[int, str | None] = {}
    for parsed_section in parsed_sections:
        section_index = parsed_section.ordinal
        section_preview = preview_sections.get(section_index)
        if section_preview is None:
            raise AppError(
                "IMPORT_NOT_COMMITTABLE", 409, "Une section de compte doit être résolue."
            )
        fingerprint = (
            bank_fingerprint(key, parsed_section.account_number_canonical)
            if parsed_section.account_number_canonical is not None
            else None
        )
        fingerprints[section_index] = fingerprint
        mapping = section_preview.get("account_mapping", {})
        resolution = resolutions.get(str(section_index), mapping)
        account_id = resolution.get("account_id") or mapping.get("account_id")
        if account_id:
            account = session.get(Account, str(account_id))
            if account is None or not account.is_active:
                raise AppError(
                    "ACCOUNT_MAPPING_INVALID", 422, "Le compte sélectionné est indisponible."
                )
            if fingerprint is not None and account.bank_fingerprint not in (None, fingerprint):
                _mark_mapping_conflict(preview, "ACCOUNT_FINGERPRINT_CONFLICT", section_index)
                raise AppError(
                    "ACCOUNT_FINGERPRINT_CONFLICT",
                    409,
                    "Le mapping du compte est incompatible.",
                )
            if fingerprint is not None and account.bank_fingerprint is None:
                fingerprint_owner = session.scalar(
                    select(Account.account_id)
                    .where(
                        Account.bank_fingerprint == fingerprint,
                        Account.account_id != account.account_id,
                    )
                    .limit(1)
                )
                if fingerprint_owner is not None:
                    _mark_mapping_conflict(preview, "ACCOUNT_FINGERPRINT_CONFLICT", section_index)
                    raise AppError(
                        "ACCOUNT_FINGERPRINT_CONFLICT",
                        409,
                        "Cette identité de compte est déjà associée à un autre compte.",
                    )
                account.bank_fingerprint = fingerprint
                account.fingerprint_key_version = 1
                account.version += 1
            account_ids[section_index] = account.account_id
            continue

        new_account = resolution.get("new_account") or mapping.get("new_account")
        if not isinstance(new_account, dict):
            raise AppError(
                "IMPORT_NOT_COMMITTABLE", 409, "Chaque section doit être associée à un compte."
            )
        alias = new_account.get("alias")
        role = new_account.get("account_role")
        if not isinstance(alias, str) or not alias.strip() or len(alias) > 80:
            raise AppError("ACCOUNT_MAPPING_INVALID", 422, "L'alias du compte est invalide.")
        normalized_alias = alias.strip().casefold()
        existing_aliases = session.scalars(select(Account.alias))
        if any(value.strip().casefold() == normalized_alias for value in existing_aliases):
            _mark_mapping_conflict(preview, "ACCOUNT_ALIAS_CONFLICT", section_index)
            raise AppError(
                "ACCOUNT_ALIAS_CONFLICT", 409, "Cet alias est déjà utilisé par un autre compte."
            )
        if fingerprint is not None and session.scalar(
            select(Account.account_id).where(Account.bank_fingerprint == fingerprint)
        ):
            _mark_mapping_conflict(preview, "ACCOUNT_FINGERPRINT_CONFLICT", section_index)
            raise AppError(
                "ACCOUNT_FINGERPRINT_CONFLICT",
                409,
                "Cette identité de compte est déjà associée à un autre compte.",
            )
        account = Account(
            alias=alias.strip(),
            account_role=role,
            bank_fingerprint=fingerprint,
            fingerprint_key_version=1 if fingerprint is not None else None,
        )
        session.add(account)
        session.flush()
        account_ids[section_index] = account.account_id
        _add_audit(
            session,
            import_file_id=import_file_id,
            target_type="account",
            target_id=account.account_id,
            action="import.account_created",
            actor=actor,
            after={
                "section_index": section_index,
                "account_id": account.account_id,
                "alias": alias.strip(),
                "account_role": role,
            },
        )
    if len(set(account_ids.values())) != len(account_ids):
        raise AppError("DUPLICATE_ACCOUNT_SECTION", 422, "Deux sections visent le même compte.")
    return account_ids, fingerprints


def _commit_in_transaction(
    session: Session,
    *,
    preview: dict[str, Any],
    parsed: Any,
    prepared_rows: list[_PreparedRow],
    import_file_id: str,
    archive_relpath: str,
    file_sha256: str,
    idempotency_key: str,
    request_sha256: str,
    canonical_path: str,
    actor: str,
) -> dict[str, Any]:
    received_on = date.fromisoformat(preview["_received_on"])
    generated_on = date.fromisoformat(preview["generated_on"])
    now = _now_text()
    original_id = session.scalar(
        select(ImportFile.import_file_id).where(
            ImportFile.file_sha256 == file_sha256,
            ImportFile.status == "committed",
        )
    )
    if original_id is not None:
        attempted = ImportFile(
            import_file_id=import_file_id,
            file_sha256=file_sha256,
            archive_relpath=None,
            archive_storage_mode=None,
            archive_permissions_octal=None,
            file_size_bytes=len(preview["_raw_bytes"]),
            received_on=received_on,
            generation_date=generated_on,
            generation_date_source=preview["generation_date_source"],
            parsed_operation_count=parsed.operation_count,
            detected_encoding=parsed.encoding,
            status="no_op",
            duplicate_of_import_file_id=original_id,
            finished_at_utc=datetime.fromisoformat(now.replace("Z", "+00:00")),
        )
        session.add(attempted)
        session.flush()
        response: dict[str, Any] = {
            "import_file_id": import_file_id,
            "status": "no_op",
            "no_op": True,
            "duplicate_of_import_file_id": original_id,
            "file_sha256": file_sha256,
            "created_transaction_count": 0,
            "new_source_row_count": 0,
            "linked_source_row_count": 0,
            "archive_retained": False,
            "generation_date_source": preview["generation_date_source"],
            "sections": [],
        }
        _add_audit(
            session,
            import_file_id=import_file_id,
            target_type="import",
            target_id=import_file_id,
            action="import.no_op",
            actor=actor,
            before={"file_sha256": file_sha256},
            after={"status": "no_op", "duplicate_of_import_file_id": original_id},
            idempotency_key=idempotency_key,
        )
        save_response(
            session,
            idempotency_key=idempotency_key,
            http_method=_HTTP_METHOD,
            canonical_path=canonical_path,
            request_sha256=request_sha256,
            response_status=200,
            response=response,
        )
        return response

    file_record = ImportFile(
        import_file_id=import_file_id,
        file_sha256=file_sha256,
        archive_relpath=archive_relpath,
        archive_storage_mode="raw",
        archive_permissions_octal="0600",
        file_size_bytes=len(preview["_raw_bytes"]),
        received_on=received_on,
        generation_date=generated_on,
        generation_date_source=preview["generation_date_source"],
        parsed_operation_count=parsed.operation_count,
        detected_encoding=parsed.encoding,
        status="previewed",
    )
    session.add(file_record)
    session.flush()
    account_ids, fingerprints = _resolve_accounts(
        session,
        preview=preview,
        parsed_sections=parsed.sections,
        actor=actor,
        import_file_id=import_file_id,
    )

    preview_sections = {int(item["section_index"]): item for item in preview["sections"]}
    response_sections: list[dict[str, Any]] = []
    for parsed_section in parsed.sections:
        view = preview_sections[parsed_section.ordinal]
        account_id = account_ids[parsed_section.ordinal]
        coverage_from = view.get("coverage_from")
        coverage_through = view.get("coverage_through")
        complete_through = view.get("complete_through")
        if coverage_from is None or coverage_through is None:
            raise AppError(
                "IMPORT_NOT_COMMITTABLE", 409, "La période de chaque section doit être résolue."
            )
        session.add(
            ImportAccountSection(
                import_file_id=import_file_id,
                section_index=parsed_section.ordinal,
                account_id=account_id,
                account_fingerprint=fingerprints[parsed_section.ordinal],
                coverage_from=date.fromisoformat(coverage_from),
                coverage_through=date.fromisoformat(coverage_through),
                complete_through=date.fromisoformat(complete_through) if complete_through else None,
                balance_as_of=parsed_section.balance_as_of,
                header_balance_cents=parsed_section.header_balance_cents,
                section_operation_count=len(parsed_section.rows),
            )
        )
        response_sections.append(
            {
                "section_index": parsed_section.ordinal,
                "account_id": account_id,
                "coverage_from": coverage_from,
                "coverage_through": coverage_through,
                "complete_through": complete_through,
                "balance_as_of": parsed_section.balance_as_of.isoformat()
                if parsed_section.balance_as_of
                else None,
                "header_balance_cents": parsed_section.header_balance_cents,
            }
        )
    session.flush()

    prepared_with_accounts = [
        _PreparedRow(
            section=item.section,
            row=item.row,
            identity_key=(
                account_ids[item.section.ordinal],
                item.identity_key[1],
                item.identity_key[2],
                item.identity_key[3],
            ),
            row_sha256=item.row_sha256,
            out_of_period=item.out_of_period,
        )
        for item in prepared_rows
    ]
    relevant_keys = {item.identity_key for item in prepared_with_accounts}
    existing_transactions = _existing_for_keys(session, relevant_keys)
    existing_occurrences = [
        ExistingOccurrence(key, transaction.occurrence_rank, transaction.transaction_id)
        for key, transactions in existing_transactions.items()
        for transaction in transactions
    ]
    normal_rows = [item for item in prepared_with_accounts if not item.out_of_period]
    plan = plan_reconciliation(
        existing_occurrences,
        [IncomingOccurrence(item.identity_key, item.row.logical_row_idx) for item in normal_rows],
    )
    if not plan.can_commit:
        raise AppError(
            "RECONCILIATION_INCONSISTENT",
            409,
            "Les rangs d'une identité existante sont incohérents; aucun import n'a été appliqué.",
        )

    rows_by_identity_and_index = {
        (item.identity_key, item.row.logical_row_idx): item for item in normal_rows
    }
    transaction_ids: dict[tuple[IdentityKey, int], str] = {}
    rank_counts: dict[IdentityKey, tuple[int, int]] = {}
    created_transaction_count = 0
    for group in plan.identities:
        rank_counts[group.identity_key] = (group.n_existing, group.n_new)
        for transaction in existing_transactions.get(group.identity_key, []):
            transaction_ids[(group.identity_key, transaction.occurrence_rank)] = (
                transaction.transaction_id
            )
        rows_by_rank = {
            assignment.occurrence_rank: rows_by_identity_and_index[
                (group.identity_key, assignment.logical_row_idx)
            ]
            for assignment in group.assignments
        }
        for rank in group.create_ranks:
            row_source = rows_by_rank.get(rank)
            if row_source is None:
                raise AppError(
                    "RECONCILIATION_INCONSISTENT", 409, "Le plan de rang est incohérent."
                )
            transaction = _new_transaction(
                session,
                key=group.identity_key,
                rank=rank,
                raw_label=row_source.row.raw_label,
            )
            transaction_ids[(group.identity_key, rank)] = transaction.transaction_id
            created_transaction_count += 1
        for assignment in group.assignments:
            existing_id = transaction_ids.get((group.identity_key, assignment.occurrence_rank))
            if existing_id is None:
                raise AppError(
                    "RECONCILIATION_INCONSISTENT", 409, "Une occurrence ne peut pas être liée."
                )

    resolutions = preview.get("_resolutions", {})
    multiplicity = resolutions.get("multiplicity", {})
    manual_create_offsets: Counter[IdentityKey] = Counter()
    link_rows: dict[tuple[int, int], str] = {}
    for item in prepared_with_accounts:
        if not item.out_of_period:
            continue
        resolution = multiplicity.get(f"{item.section.ordinal}:{item.row.logical_row_idx}", {})
        action = resolution.get("action")
        if action == "link_existing":
            linked_id = resolution.get("transaction_id")
            linked_transaction = session.get(Transaction, linked_id) if linked_id else None
            if (
                linked_transaction is None
                or (
                    linked_transaction.account_id,
                    linked_transaction.booked_on.isoformat(),
                    linked_transaction.amount_cents,
                    linked_transaction.identity_label,
                )
                != item.identity_key
            ):
                raise AppError(
                    "MULTIPLICITY_RESOLUTION_INVALID",
                    409,
                    "La résolution de cette ligne ne correspond pas à son identité.",
                )
            link_rows[(item.section.ordinal, item.row.logical_row_idx)] = (
                linked_transaction.transaction_id
            )
        elif action == "create_occurrence":
            old = existing_transactions.get(item.identity_key, [])
            existing_max = max((transaction.occurrence_rank for transaction in old), default=0)
            normal_count = rank_counts.get(item.identity_key, (existing_max, 0))[1]
            offset = manual_create_offsets[item.identity_key] + 1
            rank = max(existing_max, normal_count) + offset
            transaction = _new_transaction(
                session,
                key=item.identity_key,
                rank=rank,
                raw_label=item.row.raw_label,
            )
            manual_create_offsets[item.identity_key] += 1
            transaction_ids[(item.identity_key, rank)] = transaction.transaction_id
            link_rows[(item.section.ordinal, item.row.logical_row_idx)] = transaction.transaction_id
            created_transaction_count += 1
        else:
            raise AppError(
                "IMPORT_NOT_COMMITTABLE", 409, "Une ligne hors période doit être résolue."
            )

    row_targets: dict[tuple[int, int], str] = {}
    for assignment in plan.assignments:
        row_source = rows_by_identity_and_index[
            (assignment.identity_key, assignment.logical_row_idx)
        ]
        row_targets[(row_source.section.ordinal, assignment.logical_row_idx)] = transaction_ids[
            (assignment.identity_key, assignment.occurrence_rank)
        ]
    row_targets.update(link_rows)

    source_ids: dict[tuple[int, int], str] = {}
    source_key_by_id: dict[str, tuple[int, int]] = {}
    seen_targets: set[str] = set()
    for item in sorted(
        prepared_with_accounts, key=lambda value: (value.section.ordinal, value.row.logical_row_idx)
    ):
        source_key = (item.section.ordinal, item.row.logical_row_idx)
        target_id = row_targets.get(source_key)
        if target_id is None:
            raise AppError(
                "RECONCILIATION_INCONSISTENT", 409, "Une source n'a pas de transaction cible."
            )
        if target_id in seen_targets:
            raise AppError(
                "MULTIPLICITY_RESOLUTION_INVALID",
                409,
                "Deux occurrences du fichier ne peuvent pas viser la même transaction.",
            )
        seen_targets.add(target_id)
        source_id = str(uuid.uuid4())
        source_ids[source_key] = source_id
        source_key_by_id[source_id] = source_key
        session.add(
            SourceRow(
                source_row_id=source_id,
                import_file_id=import_file_id,
                section_index=item.section.ordinal,
                logical_row_idx=item.row.logical_row_idx,
                physical_line_start=item.row.line_start,
                physical_line_end=item.row.line_end,
                row_sha256=item.row_sha256,
                identity_key_sha256=identity_key_sha256(item.identity_key),
                transaction_id=target_id,
            )
        )
    session.flush()

    _insert_rejections(
        session,
        import_file_id=import_file_id,
        anomalies=parsed.anomalies,
        resolutions=resolutions,
        actor=actor,
        resolved_at=now,
    )
    _audit_preview_decisions(
        session,
        import_file_id=import_file_id,
        history=preview.get("_decision_history", []),
        default_actor=actor,
    )

    # Fuzzy pairs are audit hints only. Both strict identities and source facts stay unchanged.
    operation_objects: list[OperationIdentity] = []
    object_metadata: dict[int, tuple[str | None, str]] = {}
    for item in prepared_with_accounts:
        source_key = (item.section.ordinal, item.row.logical_row_idx)
        operation = OperationIdentity(
            item.identity_key[0],
            date.fromisoformat(item.identity_key[1]),
            item.identity_key[2],
            item.identity_key[3],
        )
        operation_objects.append(operation)
        object_metadata[id(operation)] = (source_ids[source_key], row_targets[source_key])
    candidate_groups = {
        (account_id, date.fromisoformat(booked_on), amount_cents)
        for account_id, booked_on, amount_cents, _identity_label in relevant_keys
    }
    existing_candidate_ids: set[str] = set()
    candidate_group_list = list(candidate_groups)
    # Candidate lookups are grouped and bounded to avoid one SELECT per CSV row.
    for offset in range(0, len(candidate_group_list), 300):
        chunk = candidate_group_list[offset : offset + 300]
        statement = select(Transaction).where(
            or_(
                *(
                    and_(
                        Transaction.account_id == account_id,
                        Transaction.booked_on == booked_on,
                        Transaction.amount_cents == amount_cents,
                    )
                    for account_id, booked_on, amount_cents in chunk
                )
            )
        )
        for transaction in session.scalars(statement):
            if transaction.transaction_id in existing_candidate_ids:
                continue
            existing_candidate_ids.add(transaction.transaction_id)
            operation = OperationIdentity(
                transaction.account_id,
                transaction.booked_on,
                transaction.amount_cents,
                transaction.identity_label,
            )
            operation_objects.append(operation)
            object_metadata[id(operation)] = (None, transaction.transaction_id)
    inserted_candidates: set[tuple[str, str]] = set()
    for pair in near_duplicate_candidates(operation_objects):
        left_source, left_transaction = object_metadata[id(pair.left)]
        right_source, right_transaction = object_metadata[id(pair.right)]
        if left_source is not None and right_transaction is not None:
            inserted_candidates.add((left_source, right_transaction))
        if right_source is not None and left_transaction is not None:
            inserted_candidates.add((right_source, left_transaction))
    for source_id, candidate_id in sorted(inserted_candidates):
        source_key = source_key_by_id[source_id]
        if row_targets[source_key] == candidate_id:
            continue
        session.add(
            ImportReconciliationCandidate(
                source_row_id=source_id,
                candidate_transaction_id=candidate_id,
                reason_code="near_label",
            )
        )

    expected_by_identity = Counter(item.identity_key for item in prepared_with_accounts)
    observed_by_identity = Counter(
        (
            row.transaction.account_id,
            row.transaction.booked_on.isoformat(),
            row.transaction.amount_cents,
            row.transaction.identity_label,
        )
        for row in session.scalars(
            select(SourceRow).where(SourceRow.import_file_id == import_file_id)
        )
    )
    if observed_by_identity != expected_by_identity:
        raise AppError(
            "COMMIT_INTEGRITY_FAILED",
            409,
            "La provenance ne correspond pas aux occurrences du fichier.",
        )
    source_count = (
        session.scalar(
            select(func.count(SourceRow.source_row_id)).where(
                SourceRow.import_file_id == import_file_id
            )
        )
        or 0
    )
    if source_count != parsed.operation_count:
        raise AppError("COMMIT_INTEGRITY_FAILED", 409, "Le nombre de sources est incohérent.")
    orphan_id = session.scalar(
        select(Transaction.transaction_id).where(~Transaction.source_rows.any()).limit(1)
    )
    if orphan_id is not None:
        raise AppError("COMMIT_INTEGRITY_FAILED", 409, "Une transaction n'a pas de source.")
    final_transactions = _existing_for_keys(session, relevant_keys)
    for key in relevant_keys:
        ranks = sorted(
            transaction.occurrence_rank for transaction in final_transactions.get(key, [])
        )
        if ranks and ranks != list(range(1, max(ranks) + 1)):
            raise AppError(
                "COMMIT_INTEGRITY_FAILED", 409, "Les rangs d'une identité ne sont pas contigus."
            )

    session.flush()
    file_record.status = "committed"
    file_record.finished_at_utc = datetime.fromisoformat(now.replace("Z", "+00:00"))
    file_record.version += 1
    session.flush()
    linked_source_row_count = sum(
        1
        for item in prepared_with_accounts
        if item.out_of_period
        and multiplicity.get(f"{item.section.ordinal}:{item.row.logical_row_idx}", {}).get("action")
        == "link_existing"
    )
    response = {
        "import_file_id": import_file_id,
        "status": "committed",
        "no_op": False,
        "duplicate_of_import_file_id": None,
        "file_sha256": file_sha256,
        "created_transaction_count": created_transaction_count,
        "new_source_row_count": parsed.operation_count,
        "linked_source_row_count": linked_source_row_count,
        "archive_retained": True,
        "generation_date_source": preview["generation_date_source"],
        "sections": response_sections,
    }
    _add_audit(
        session,
        import_file_id=import_file_id,
        target_type="import",
        target_id=import_file_id,
        action="import.commit",
        actor=actor,
        before={"status": "previewed"},
        after={
            "status": "committed",
            "created_transaction_count": created_transaction_count,
            "source_row_count": parsed.operation_count,
        },
        idempotency_key=idempotency_key,
    )
    save_response(
        session,
        idempotency_key=idempotency_key,
        http_method=_HTTP_METHOD,
        canonical_path=canonical_path,
        request_sha256=request_sha256,
        response_status=200,
        response=response,
    )
    return response


def commit_preview(
    preview_id: str,
    expected_version: int,
    idempotency_key: str,
    actor: str,
    *,
    engine: Engine,
    store: ImportPreviewStore,
    data_dir: Path,
) -> dict[str, Any]:
    """Commit a fully resolved preview as one SQLite transaction.

    Raw bytes are staged before ``BEGIN IMMEDIATE`` and removed after any
    uncommitted failure unless a committed import references the archive.
    """

    if (
        not idempotency_key
        or len(idempotency_key) > 128
        or idempotency_key.strip() != idempotency_key
    ):
        raise AppError("VALIDATION_ERROR", 422, "La clé d'idempotence est invalide.")
    canonical_path = f"/api/v1/imports/{preview_id}/commit"
    request_sha256 = request_fingerprint(
        _HTTP_METHOD, canonical_path, {"expected_version": expected_version}
    )

    # Fast replay lookup allows a retried request to succeed after its preview
    # file has been removed. The authoritative lookup still runs under IMMEDIATE.
    def lookup_replay() -> tuple[int, dict[str, Any]] | None:
        with session_scope(engine) as lookup_session:
            return find_replay(
                lookup_session,
                idempotency_key=idempotency_key,
                http_method=_HTTP_METHOD,
                canonical_path=canonical_path,
                request_sha256=request_sha256,
            )

    replay = lookup_replay()
    if replay is not None:
        return replay[1]
    commit_marked = False
    archive = ImportArchive(data_dir)
    archive_path: Path | None = None
    archive_reservation: ArchiveLifecycleLock | None = None
    commit_confirmed = False
    try:
        try:
            preview = store.begin_commit(preview_id, expected_version)
            commit_marked = True
        except AppError as error:
            if error.code == "IMPORT_PREVIEW_EXPIRED":
                replay = lookup_replay()
                if replay is not None:
                    return replay[1]
            raise

        _validate_preview(preview, expected_version)
        raw_bytes = store.raw_bytes(preview_id)
        digest = hashlib.sha256(raw_bytes).hexdigest()
        if digest != preview.get("file_sha256"):
            raise AppError("PREVIEW_INVALID", 409, "Les octets de l'aperçu ne correspondent plus.")
        parsed = parse_export(raw_bytes)
        unresolved_blocking = any(
            anomaly.severity == "blocking"
            and not (
                anomaly.code.value == "OUT_OF_PERIOD"
                and anomaly.section_ordinal is not None
                and anomaly.logical_row_idx is not None
                and f"{anomaly.section_ordinal}:{anomaly.logical_row_idx}"
                in preview.get("_resolutions", {}).get("multiplicity", {})
            )
            and not (
                anomaly.code.value == "MISSING_PERIOD"
                and anomaly.section_ordinal is not None
                and str(anomaly.section_ordinal)
                in preview.get("_resolutions", {}).get("coverage", {})
            )
            for anomaly in parsed.anomalies
        )
        if unresolved_blocking or parsed.operation_count != preview.get("operation_count"):
            raise AppError("IMPORT_NOT_COMMITTABLE", 422, "Le fichier ne peut pas être commité.")
        if parsed.operation_count > MAX_IMPORT_OPERATION_COUNT:
            raise AppError(
                "TOO_MANY_OPERATIONS",
                413,
                (
                    "Le fichier dépasse le plafond de "
                    f"{MAX_IMPORT_OPERATION_COUNT} opérations par import."
                ),
            )
        prepared_rows = _prepare_rows(raw_bytes, parsed.sections, parsed)
        if len(prepared_rows) != parsed.operation_count:
            raise AppError(
                "IMPORT_NOT_COMMITTABLE", 422, "Le nombre d'opérations de l'aperçu est incohérent."
            )
        _validate_link_targets_before_archive(engine, preview, prepared_rows)

        import_file_id = str(uuid.uuid4())
        archive_path = archive.directory / f"{import_file_id}.bin"
        # This shared flock starts before the archive appears and remains held
        # until SQLite confirms commit or the unreferenced archive is removed.
        archive_reservation = archive.acquire_commit_reservation()
        try:
            archive_path = archive.write(import_file_id, raw_bytes)
        except BaseException:
            _remove_unreferenced_archive(engine, archive, archive_path, import_file_id)
            raise

        preview_for_transaction = dict(preview)
        # The transient upload bytes never enter SQLite or an error response.
        preview_for_transaction["_raw_bytes"] = raw_bytes
        actor_value = actor.strip()[:80] if isinstance(actor, str) else "local_user"
        if not actor_value:
            actor_value = "local_user"

        try:
            with write_transaction(engine) as session:
                replay = find_replay(
                    session,
                    idempotency_key=idempotency_key,
                    http_method=_HTTP_METHOD,
                    canonical_path=canonical_path,
                    request_sha256=request_sha256,
                    remove_expired=True,
                )
                if replay is not None:
                    response = replay[1]
                    outcome = "replay"
                else:
                    response = _commit_in_transaction(
                        session,
                        preview=preview_for_transaction,
                        parsed=parsed,
                        prepared_rows=prepared_rows,
                        import_file_id=import_file_id,
                        archive_relpath=archive.relative_path(import_file_id),
                        file_sha256=digest,
                        idempotency_key=idempotency_key,
                        request_sha256=request_sha256,
                        canonical_path=canonical_path,
                        actor=actor_value,
                    )
                    outcome = "no_op" if response["status"] == "no_op" else "committed"
            commit_confirmed = True
            if outcome in {"replay", "no_op"}:
                _remove_unreferenced_archive(engine, archive, archive_path, import_file_id)
            store.delete(preview_id, during_commit=True)
            return response
        except AppError as error:
            if not commit_confirmed:
                _remove_unreferenced_archive(engine, archive, archive_path, import_file_id)
            if error.code in {"ACCOUNT_ALIAS_CONFLICT", "ACCOUNT_FINGERPRINT_CONFLICT"}:
                preview["rejections"] = preview_for_transaction["rejections"]
                preview["can_commit"] = False
                store.update_commit_failure(preview_id, preview)
            raise
        except IntegrityError as error:
            if not commit_confirmed:
                _remove_unreferenced_archive(engine, archive, archive_path, import_file_id)
            raise AppError(
                "IMPORT_CONFLICT",
                409,
                "Le commit entre en conflit avec une contrainte d'intégrité.",
            ) from error
        except BaseException:
            if not commit_confirmed:
                _remove_unreferenced_archive(engine, archive, archive_path, import_file_id)
            raise
    finally:
        if archive_reservation is not None:
            archive.release_commit_reservation(archive_reservation)
        if commit_marked:
            store.finish_commit(preview_id)


def _remove_unreferenced_archive(
    engine: Engine, archive: ImportArchive, archive_path: Path, import_file_id: str
) -> None:
    """Delete a staged archive unless a committed import row owns it."""

    try:
        with session_scope(engine) as session:
            referenced = session.scalar(
                select(ImportFile.import_file_id).where(
                    ImportFile.import_file_id == import_file_id,
                    ImportFile.status == "committed",
                    ImportFile.archive_relpath == archive.relative_path(import_file_id),
                )
            )
        if referenced is None:
            archive.remove(archive_path)
    except Exception:
        # A later startup sweep removes any aged file once SQLite is available.
        return
