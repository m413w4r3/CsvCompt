"""Endpoints temporaires pour l'aperçu privé des imports."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, File, Header, Query, Request, UploadFile
from fastapi.responses import Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.api.v1.health import database_status
from app.core.errors import AppError
from app.db.errors import DatabaseBusyError
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
from app.services.app_secrets import ensure_hmac_key
from app.services.idempotency import purge_expired_idempotency, sign_cursor, verify_cursor
from app.services.import_archive import purge_orphaned_archives
from app.services.import_commit import commit_preview
from app.services.import_preview_store import ImportPreviewStore
from app.services.imports import (
    build_preview,
    public_preview,
    refresh_link_candidates,
    set_coverage,
    set_mapping,
    set_multiplicity,
    validate_link_existing_preview,
)
from app.services.transactions import sanitize_api_cents


@asynccontextmanager
async def _imports_lifespan(application: Any) -> AsyncIterator[None]:
    storage_status, detail_status, _revision = database_status(
        application.state.settings.database_url
    )
    if storage_status == "ok" and detail_status == "ok":
        with suppress(DatabaseBusyError, OperationalError):
            purge_expired_idempotency(application.state.db_engine)
            purge_orphaned_archives(
                application.state.db_engine, application.state.settings.data_dir
            )
    # Absent, corrupt, or busy storage is left to the ordinary readiness path;
    # expiration is retried on the next successful startup.
    yield


class _UploadLimitRoute(APIRoute):
    """Reject oversized multipart requests before Starlette spools their bodies."""

    def get_route_handler(self) -> Any:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            if request.url.path == "/api/v1/imports/preview":
                content_length = request.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        raise AppError(
                            "VALIDATION_ERROR", 400, "La taille déclarée est invalide."
                        ) from None
                    # Multipart framing and the required single boundary add a small
                    # constant overhead; the exact file length is checked in-route.
                    max_request = request.app.state.settings.max_upload_bytes + 64 * 1024
                    if declared_length > max_request:
                        raise AppError(
                            "FILE_TOO_LARGE",
                            413,
                            "Le fichier dépasse la taille maximale autorisée.",
                        )
                _database_ready(request)
                try:
                    form = await request.form(max_files=1, max_fields=1)
                except Exception as exc:
                    raise AppError(
                        "VALIDATION_ERROR", 422, "Le formulaire doit contenir un seul fichier."
                    ) from exc
                fields = list(form.multi_items())
                if (
                    len(fields) != 1
                    or fields[0][0] != "file"
                    or not isinstance(fields[0][1], StarletteUploadFile)
                ):
                    raise AppError(
                        "VALIDATION_ERROR", 422, "Le formulaire doit contenir un seul champ file."
                    )
            return await original(request)

        return handler


router = APIRouter(
    prefix="/imports", tags=["imports"], lifespan=_imports_lifespan, route_class=_UploadLimitRoute
)
_CONTENT_TYPES = {"text/csv", "application/vnd.ms-excel", "application/octet-stream", "text/plain"}


class AccountMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["known", "unknown", "ambiguous"]
    account_id: str | None = None
    alias: str | None = None
    new_account: NewAccountMapping | None = None


class SectionPreview(BaseModel):
    model_config = ConfigDict(extra="allow")
    section_index: int
    account_mapping: AccountMapping
    account_resolution: Literal["known", "unknown", "ambiguous"]
    account_id: str | None
    account_alias: str | None
    period_from: date | None
    period_to: date | None
    coverage_from: date | None
    coverage_through: date | None
    complete_through: date | None
    operation_count: int
    debit_total_cents: int | None
    credit_total_cents: int | None
    net_cents: int | None
    total_out_of_range: bool = False
    balance_as_of: date | None
    header_balance_cents: int | None
    min_date: date | None
    max_date: date | None
    provisional_days: list[date]
    anomaly_counts: dict[str, int]


class PreviewResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    preview_id: UUID
    version: int
    status: Literal["previewed"]
    encoding: str
    file_sha256: str
    generated_on: date
    generation_date_source: str
    operation_count: int
    anomaly_counts: dict[str, int]
    anomaly_summary: dict[str, int]
    already_imported: bool
    import_file_id: UUID | None
    sections: list[SectionPreview]
    warnings: list[dict[str, Any]]
    rejections: list[dict[str, Any]]
    out_of_period_candidates: list[dict[str, Any]] = Field(default_factory=list)
    can_commit: bool
    expires_at_utc: str
    reconciliation: None = None


class NewAccountMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field(min_length=1, max_length=80)
    account_role: Literal["current", "savings", "pea", "personal", "cash", "other"]


class MappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    account_id: UUID | None = None
    new_account: NewAccountMapping | None = None

    @model_validator(mode="after")
    def exactly_one_mapping(self) -> MappingRequest:
        if (self.account_id is None) == (self.new_account is None):
            raise ValueError("one_mapping_required")
        return self


class CoverageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    coverage_from: date
    coverage_through: date


class MultiplicityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    action: Literal["link_existing", "create_occurrence"]
    section_index: int | None = Field(default=None, ge=1)
    transaction_id: UUID | None = None

    @model_validator(mode="after")
    def validate_target(self) -> MultiplicityRequest:
        if self.action == "create_occurrence" and self.transaction_id is not None:
            raise ValueError("create_occurrence_has_no_transaction_id")
        if self.action == "link_existing" and self.transaction_id is None:
            raise ValueError("link_existing_requires_transaction_id")
        return self


class CommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class CommittedSection(BaseModel):
    section_index: int
    account_id: UUID
    coverage_from: date
    coverage_through: date
    complete_through: date | None
    balance_as_of: date | None
    header_balance_cents: int | None
    total_out_of_range: bool = False


class CommitResponse(BaseModel):
    import_file_id: UUID
    status: Literal["committed", "no_op"]
    no_op: bool
    duplicate_of_import_file_id: UUID | None
    file_sha256: str
    created_transaction_count: int
    new_source_row_count: int
    linked_source_row_count: int
    archive_retained: bool
    generation_date_source: Literal["header_download", "received_on_fallback"]
    sections: list[CommittedSection]


class PageResponse(BaseModel):
    limit: int
    next_cursor: str | None
    snapshot_at: str | None = None


class ImportSummary(BaseModel):
    import_file_id: UUID
    file_sha256: str
    status: Literal["previewed", "committed", "rejected", "no_op"]
    created_at_utc: str
    generated_on: date
    generation_date_source: Literal["header_download", "received_on_fallback"]
    sections: int
    operation_count: int
    created_transaction_count: int
    source_row_count: int


class ImportSummaryPage(BaseModel):
    items: list[ImportSummary]
    page: PageResponse


class ImportSectionDetail(BaseModel):
    section_index: int
    account_id: UUID | None
    account_alias: str | None
    coverage_from: date | None
    coverage_through: date | None
    complete_through: date | None
    balance_as_of: date | None
    header_balance_cents: int | None
    total_out_of_range: bool = False
    operation_count: int | None


class ImportDetail(BaseModel):
    import_file_id: UUID
    file_sha256: str
    status: Literal["previewed", "committed", "rejected", "no_op"]
    duplicate_of_import_file_id: UUID | None
    created_at_utc: str
    finished_at_utc: str | None
    generated_on: date
    generation_date_source: Literal["header_download", "received_on_fallback"]
    file_size_bytes: int
    operation_count: int
    created_transaction_count: int
    source_row_count: int
    archive_retained: bool
    sections: list[ImportSectionDetail]
    candidates: list[dict[str, str]]
    diagnostics: list[dict[str, Any]]


class RejectionItem(BaseModel):
    logical_row_idx: int | None
    code: str
    field: str | None
    message: str
    resolution_status: Literal["pending", "resolved", "not_required"]


class RejectionPage(BaseModel):
    items: list[RejectionItem]
    page: PageResponse


def _store(request: Request) -> ImportPreviewStore:
    return cast(ImportPreviewStore, request.app.state.import_preview_store)


def _database_ready(request: Request) -> Engine:
    settings = request.app.state.settings
    storage, _detail, _revision = database_status(settings.database_url)
    if storage != "ok":
        raise AppError(
            "DATABASE_NOT_READY",
            503,
            "La base de données n'est pas prête.",
            {"storage_status": storage},
        )
    return cast(Engine, request.app.state.db_engine)


def _reject_binary(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    controls = sum(byte < 9 or 13 < byte < 32 for byte in data)
    return bool(data) and controls * 20 > len(data)


@router.post("/preview", response_model=PreviewResponse)
def create_preview(request: Request, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    engine = _database_ready(request)
    content_type = (
        (file.content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    )
    if content_type not in _CONTENT_TYPES:
        raise AppError(
            "UNSUPPORTED_MEDIA_TYPE", 415, "Le type de fichier n'est pas pris en charge."
        )
    limit = request.app.state.settings.max_upload_bytes
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise AppError("FILE_TOO_LARGE", 413, "Le fichier dépasse la taille maximale autorisée.")
    if _reject_binary(data):
        raise AppError(
            "UNSUPPORTED_MEDIA_TYPE", 415, "Le contenu du fichier n'est pas un CSV texte."
        )
    now = date.today()
    result = build_preview(
        raw_bytes=data,
        engine=engine,
        store=_store(request),
        max_upload_bytes=limit,
        received_on=now,
        max_anomalies=request.app.state.settings.max_preview_anomalies,
    )
    return public_preview(result)


@router.get("/preview/{preview_id}", response_model=PreviewResponse)
def get_preview(request: Request, preview_id: UUID) -> dict[str, Any]:
    return public_preview(_store(request).get(str(preview_id)))


@router.delete("/preview/{preview_id}", status_code=204)
def delete_preview(request: Request, preview_id: UUID) -> Response:
    _store(request).delete(str(preview_id))
    return Response(status_code=204)


@router.patch(
    "/{preview_id}/sections/{section_index}/account-mapping", response_model=PreviewResponse
)
def resolve_account_mapping(
    request: Request, preview_id: UUID, section_index: int, body: MappingRequest
) -> dict[str, Any]:
    engine = _database_ready(request)
    preview = _store(request).get(str(preview_id))
    if body.expected_version != preview["version"]:
        raise AppError(
            "VERSION_CONFLICT", 409, "Cet aperçu a changé.", {"current_version": preview["version"]}
        )
    resolution: dict[str, Any]
    if body.account_id is not None:
        resolution = {"account_id": str(body.account_id)}
    else:
        resolution = {"new_account": body.new_account.model_dump() if body.new_account else None}
    updated = set_mapping(preview, section_index, resolution, engine)
    refresh_link_candidates(updated, _store(request).raw_bytes(str(preview_id)), engine)
    return public_preview(_store(request).update(str(preview_id), updated))


@router.patch("/{preview_id}/sections/{section_index}/coverage", response_model=PreviewResponse)
def resolve_coverage(
    request: Request, preview_id: UUID, section_index: int, body: CoverageRequest
) -> dict[str, Any]:
    preview = _store(request).get(str(preview_id))
    if body.expected_version != preview["version"]:
        raise AppError(
            "VERSION_CONFLICT", 409, "Cet aperçu a changé.", {"current_version": preview["version"]}
        )
    updated = set_coverage(
        preview,
        section_index,
        body.coverage_from,
        body.coverage_through,
        date.fromisoformat(preview["generated_on"])
        if preview["generation_date_source"] == "header_download"
        else None,
    )
    return public_preview(_store(request).update(str(preview_id), updated))


@router.patch("/{preview_id}/rows/{logical_row_idx}/multiplicity", response_model=PreviewResponse)
def resolve_multiplicity(
    request: Request, preview_id: UUID, logical_row_idx: int, body: MultiplicityRequest
) -> dict[str, Any]:
    preview = _store(request).get(str(preview_id))
    if body.expected_version != preview["version"]:
        raise AppError(
            "VERSION_CONFLICT", 409, "Cet aperçu a changé.", {"current_version": preview["version"]}
        )
    if body.action == "link_existing":
        candidates = [
            item["section"]
            for item in preview.get("_out_of_period_rows", [])
            if item["row"] == logical_row_idx
            and (body.section_index is None or item["section"] == body.section_index)
        ]
        if len(candidates) != 1:
            if len(candidates) > 1:
                raise AppError(
                    "MULTIPLICITY_AMBIGUOUS",
                    409,
                    "L'index logique apparaît dans plusieurs sections.",
                )
            raise AppError(
                "VALIDATION_ERROR", 422, "Cette ligne ne requiert pas de décision de multiplicité."
            )
        validate_link_existing_preview(
            preview,
            _store(request).raw_bytes(str(preview_id)),
            _database_ready(request),
            section_index=candidates[0],
            row_idx=logical_row_idx,
            transaction_id=str(body.transaction_id),
        )
    updated = set_multiplicity(
        preview,
        logical_row_idx,
        body.action,
        section_index=body.section_index,
        transaction_id=str(body.transaction_id) if body.transaction_id else None,
        actor="local_user",
    )
    return public_preview(_store(request).update(str(preview_id), updated))


def _count_import(session: Session, import_file_id: str) -> tuple[int, int]:
    source_count = (
        session.scalar(
            select(func.count(SourceRow.source_row_id)).where(
                SourceRow.import_file_id == import_file_id
            )
        )
        or 0
    )
    event = session.scalar(
        select(AuditEvent)
        .where(AuditEvent.import_file_id == import_file_id, AuditEvent.action == "import.commit")
        .order_by(AuditEvent.created_at_utc.desc())
        .limit(1)
    )
    created_count = 0
    if event is not None and event.after_json:
        try:
            after = json.loads(event.after_json)
            value = after.get("created_transaction_count", 0)
            created_count = value if type(value) is int and value >= 0 else 0
        except (json.JSONDecodeError, AttributeError):
            created_count = 0
    return created_count, source_count


def _summary(session: Session, record: ImportFile) -> dict[str, Any]:
    section_count = (
        session.scalar(
            select(func.count(ImportAccountSection.section_index)).where(
                ImportAccountSection.import_file_id == record.import_file_id
            )
        )
        or 0
    )
    created_count, source_count = _count_import(session, record.import_file_id)
    return {
        "import_file_id": record.import_file_id,
        "file_sha256": record.file_sha256,
        "status": record.status,
        "created_at_utc": record.started_at_utc.isoformat().replace("+00:00", "Z"),
        "generated_on": record.generation_date,
        "generation_date_source": record.generation_date_source,
        "sections": section_count,
        "operation_count": record.parsed_operation_count,
        "created_transaction_count": created_count,
        "source_row_count": source_count,
    }


@router.post("/{preview_id}/commit", response_model=CommitResponse)
def commit_import(
    request: Request,
    preview_id: UUID,
    body: CommitRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> dict[str, Any]:
    engine = _database_ready(request)
    return sanitize_api_cents(
        commit_preview(
            str(preview_id),
            body.expected_version,
            idempotency_key,
            "local_user",
            engine=engine,
            store=_store(request),
            data_dir=request.app.state.settings.data_dir,
        )
    )


@router.get("", response_model=ImportSummaryPage)
def list_imports(
    request: Request,
    status: Literal["previewed", "committed", "rejected", "no_op"] | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    engine = _database_ready(request)
    if period_start and period_end and period_end < period_start:
        raise AppError("VALIDATION_ERROR", 422, "La période de filtre est invalide.")
    key = ensure_hmac_key(engine)
    filters = {
        "status": status,
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
    }
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    snapshot_at: str = now
    last_started: str | None = None
    last_id: str | None = None
    if cursor:
        decoded = verify_cursor(cursor, key, filters)
        cursor_snapshot = decoded.get("snapshot_at")
        cursor_started = decoded.get("last_started")
        cursor_id = decoded.get("last_id")
        if (
            not isinstance(cursor_snapshot, str)
            or not isinstance(cursor_started, str)
            or not isinstance(cursor_id, str)
        ):
            raise AppError("CURSOR_INVALID", 422, "Le curseur est invalide.")
        snapshot_at = cursor_snapshot
        last_started = cursor_started
        last_id = cursor_id

    snapshot_value = datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
    statement = select(ImportFile).where(ImportFile.started_at_utc <= snapshot_value)
    if status is not None:
        statement = statement.where(ImportFile.status == status)
    if period_start is not None:
        statement = statement.where(
            select(ImportAccountSection.import_file_id)
            .where(
                ImportAccountSection.import_file_id == ImportFile.import_file_id,
                ImportAccountSection.coverage_through >= period_start,
            )
            .exists()
        )
    if period_end is not None:
        statement = statement.where(
            select(ImportAccountSection.import_file_id)
            .where(
                ImportAccountSection.import_file_id == ImportFile.import_file_id,
                ImportAccountSection.coverage_from <= period_end,
            )
            .exists()
        )
    if last_started is not None and last_id is not None:
        statement = statement.where(
            or_(
                ImportFile.started_at_utc
                < datetime.fromisoformat(last_started.replace("Z", "+00:00")),
                and_(
                    ImportFile.started_at_utc
                    == datetime.fromisoformat(last_started.replace("Z", "+00:00")),
                    ImportFile.import_file_id < last_id,
                ),
            )
        )
    statement = statement.order_by(
        ImportFile.started_at_utc.desc(), ImportFile.import_file_id.desc()
    )
    with request.app.state.db_engine.connect() as connection:
        from sqlalchemy.orm import Session as SyncSession

        with SyncSession(connection) as session:
            records = list(session.scalars(statement.limit(limit + 1)))
            more = len(records) > limit
            records = records[:limit]
            items = [_summary(session, record) for record in records]
    next_cursor = None
    if more and records:
        last = records[-1]
        next_cursor = sign_cursor(
            {
                "filters": filters,
                "snapshot_at": snapshot_at,
                "last_started": last.started_at_utc.isoformat().replace("+00:00", "Z"),
                "last_id": last.import_file_id,
            },
            key,
        )
    return {
        "items": items,
        "page": {"limit": limit, "next_cursor": next_cursor, "snapshot_at": snapshot_at},
    }


@router.get("/{import_file_id}/rejections", response_model=RejectionPage)
def get_import_rejections(
    request: Request,
    import_file_id: UUID,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    _database_ready(request)
    file_id = str(import_file_id)
    key = ensure_hmac_key(request.app.state.db_engine)
    filters = {"import_file_id": file_id}
    last_row: int | None = None
    last_id: str | None = None
    if cursor:
        decoded = verify_cursor(cursor, key, filters)
        last_row = decoded.get("last_row")
        last_id = decoded.get("last_id")
        if type(last_row) is not int or not isinstance(last_id, str):
            raise AppError("CURSOR_INVALID", 422, "Le curseur est invalide.")
    with request.app.state.db_engine.connect() as connection, Session(connection) as session:
        if session.get(ImportFile, file_id) is None:
            raise AppError("NOT_FOUND", 404, "Import introuvable.")
        statement = select(ImportRejection).where(ImportRejection.import_file_id == file_id)
        if last_row is not None and last_id is not None:
            row_order = func.coalesce(ImportRejection.logical_row_idx, 0)
            statement = statement.where(
                or_(
                    row_order > last_row,
                    and_(row_order == last_row, ImportRejection.import_rejection_id > last_id),
                )
            )
        records = list(
            session.scalars(
                statement.order_by(
                    func.coalesce(ImportRejection.logical_row_idx, 0),
                    ImportRejection.import_rejection_id,
                ).limit(limit + 1)
            )
        )
        more = len(records) > limit
        records = records[:limit]
        items = [
            {
                "logical_row_idx": row.logical_row_idx,
                "code": row.code,
                "field": row.field_name,
                "message": row.message,
                "resolution_status": row.resolution_status,
            }
            for row in records
        ]
    next_cursor = None
    if more and records:
        last = records[-1]
        next_cursor = sign_cursor(
            {
                "filters": filters,
                "last_row": last.logical_row_idx or 0,
                "last_id": last.import_rejection_id,
            },
            key,
        )
    return {"items": items, "page": {"limit": limit, "next_cursor": next_cursor}}


@router.get("/{import_file_id}", response_model=ImportDetail)
def get_import_detail(request: Request, import_file_id: UUID) -> dict[str, Any]:
    _database_ready(request)
    file_id = str(import_file_id)
    with request.app.state.db_engine.connect() as connection, Session(connection) as session:
        record = session.get(ImportFile, file_id)
        if record is None:
            raise AppError("NOT_FOUND", 404, "Import introuvable.")
        created_count, source_count = _count_import(session, file_id)
        sections = list(
            session.execute(
                select(ImportAccountSection, Account.alias)
                .outerjoin(Account, Account.account_id == ImportAccountSection.account_id)
                .where(ImportAccountSection.import_file_id == file_id)
                .order_by(ImportAccountSection.section_index)
            )
        )
        section_items = [
            {
                "section_index": section.section_index,
                "account_id": section.account_id,
                "account_alias": alias,
                "coverage_from": section.coverage_from,
                "coverage_through": section.coverage_through,
                "complete_through": section.complete_through,
                "balance_as_of": section.balance_as_of,
                "header_balance_cents": section.header_balance_cents,
                "operation_count": section.section_operation_count,
            }
            for section, alias in sections
        ]
        candidates = list(
            session.execute(
                select(
                    SourceRow.source_row_id,
                    Transaction.transaction_id,
                    ImportReconciliationCandidate.reason_code,
                )
                .join(
                    ImportReconciliationCandidate,
                    ImportReconciliationCandidate.source_row_id == SourceRow.source_row_id,
                )
                .join(
                    Transaction,
                    Transaction.transaction_id
                    == ImportReconciliationCandidate.candidate_transaction_id,
                )
                .where(SourceRow.import_file_id == file_id)
                .order_by(SourceRow.logical_row_idx, Transaction.transaction_id)
            )
        )
        diagnostics = list(
            session.execute(
                select(ImportRejection.code, func.count(ImportRejection.import_rejection_id))
                .where(ImportRejection.import_file_id == file_id)
                .group_by(ImportRejection.code)
                .order_by(ImportRejection.code)
            )
        )
        return sanitize_api_cents({
            "import_file_id": record.import_file_id,
            "file_sha256": record.file_sha256,
            "status": record.status,
            "duplicate_of_import_file_id": record.duplicate_of_import_file_id,
            "created_at_utc": record.started_at_utc.isoformat().replace("+00:00", "Z"),
            "finished_at_utc": record.finished_at_utc.isoformat().replace("+00:00", "Z")
            if record.finished_at_utc
            else None,
            "generated_on": record.generation_date,
            "generation_date_source": record.generation_date_source,
            "file_size_bytes": record.file_size_bytes,
            "operation_count": record.parsed_operation_count,
            "created_transaction_count": created_count,
            "source_row_count": source_count,
            "archive_retained": record.archive_relpath is not None,
            "sections": section_items,
            "candidates": [
                {
                    "source_row_id": source_id,
                    "candidate_transaction_id": transaction_id,
                    "reason_code": reason,
                }
                for source_id, transaction_id, reason in candidates
            ],
            "diagnostics": [{"code": code, "count": count} for code, count in diagnostics],
        })
