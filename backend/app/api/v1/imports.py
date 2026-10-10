"""Endpoints temporaires pour l'aperçu privé des imports."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.engine import Engine

from app.api.v1.health import database_status
from app.core.errors import AppError
from app.services.import_preview_store import ImportPreviewStore
from app.services.imports import (
    build_preview,
    public_preview,
    set_coverage,
    set_mapping,
    set_multiplicity,
)

router = APIRouter(prefix="/imports", tags=["imports"])
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
    debit_total_cents: int
    credit_total_cents: int
    net_cents: int
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
    sections: list[SectionPreview]
    warnings: list[dict[str, Any]]
    rejections: list[dict[str, Any]]
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
async def create_preview(request: Request, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    engine = _database_ready(request)
    content_type = (
        (file.content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    )
    if content_type not in _CONTENT_TYPES:
        raise AppError(
            "UNSUPPORTED_MEDIA_TYPE", 415, "Le type de fichier n'est pas pris en charge."
        )
    limit = request.app.state.settings.max_upload_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise AppError("FILE_TOO_LARGE", 413, "Le fichier dépasse la taille maximale autorisée.")
    if _reject_binary(data):
        raise AppError(
            "UNSUPPORTED_MEDIA_TYPE", 415, "Le contenu du fichier n'est pas un CSV texte."
        )
    now = request.app.state.clock().date()
    result = build_preview(
        raw_bytes=data,
        engine=engine,
        store=_store(request),
        max_upload_bytes=limit,
        received_on=now,
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
    updated = set_multiplicity(
        preview, logical_row_idx, body.action, section_index=body.section_index
    )
    return public_preview(_store(request).update(str(preview_id), updated))
