"""Versioned analytics endpoints backed by canonical financial definitions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.analytics import queries
from app.analytics.definitions import (
    METRIC_DEFINITIONS,
    AnalyticsFilters,
    load_analytics_dataset,
)
from app.core.errors import AppError
from app.db.session import DbSession
from app.domain.types import FlowKind
from app.services.app_secrets import read_hmac_key
from app.services.categories import require_database_ready
from app.services.idempotency import sign_cursor, verify_cursor
from app.services.transactions import SnapshotUnavailable, TransactionSnapshotStore

router = APIRouter(prefix="/analytics", tags=["analytics"])

MAX_SAFE_FRONTEND_INTEGER = 2**53 - 1
_NUMERIC_SUFFIXES = ("_cents", "_basis_points")


def _protect_integers(value: Any) -> tuple[Any, bool]:
    """Null every integer a JavaScript client cannot hold exactly and flag its container.

    Applies to cents and basis points (ADR-032). The flag `total_out_of_range` marks the object
    that held the value and is propagated to the response root; the value is never rounded.
    """
    if isinstance(value, list | tuple):
        items = [_protect_integers(item) for item in value]
        return [item for item, _ in items], any(flag for _, flag in items)
    if not isinstance(value, Mapping):
        return value, False
    protected: dict[str, Any] = {}
    unsafe_here = False
    unsafe_below = False
    for key, item in value.items():
        if (
            isinstance(key, str)
            and key.endswith(_NUMERIC_SUFFIXES)
            and type(item) is int
            and abs(item) > MAX_SAFE_FRONTEND_INTEGER
        ):
            protected[key] = None
            unsafe_here = True
            continue
        protected[key], flagged = _protect_integers(item)
        unsafe_below = unsafe_below or flagged
    if unsafe_here:
        protected["total_out_of_range"] = True
    return protected, unsafe_here or unsafe_below


def analytics_filters(
    request: Request,
    period_start: date,
    period_end: date,
    account_ids: Annotated[list[UUID], Query()],
    category_ids: Annotated[list[str] | None, Query()] = None,
    flow_kind: Annotated[list[str] | None, Query()] = None,
    commitment_transaction_ids: Annotated[list[UUID] | None, Query()] = None,
    mode: Literal["treasury", "family_life"] = "treasury",
    personal_mode: Literal["envelope", "observed"] = "envelope",
) -> AnalyticsFilters:
    """Parse the common, explicit 2026 period/account filters."""
    if not account_ids:
        raise AppError("VALIDATION_ERROR", 422, "Sélectionnez au moins un compte.")
    if period_end < period_start:
        raise AppError("VALIDATION_ERROR", 422, "La période analytique est invalide.")
    if period_start < date(2026, 1, 1) or period_end > date(2026, 12, 31):
        raise AppError("VALIDATION_ERROR", 422, "L'analytique v1 est limitée aux dates de 2026.")
    if category_ids and any(not item or len(item) > 120 for item in category_ids):
        raise AppError("VALIDATION_ERROR", 422, "Un filtre de catégorie est invalide.")
    valid_flows = {item.value for item in FlowKind}
    flow_values = tuple(sorted(set(flow_kind or ())))
    if set(flow_values) - valid_flows:
        raise AppError("VALIDATION_ERROR", 422, "Un filtre de flux est invalide.")
    return AnalyticsFilters(
        period_start=period_start,
        period_end=period_end,
        account_ids=tuple(sorted({str(item) for item in account_ids})),
        category_ids=tuple(sorted(set(category_ids or ()))),
        flow_kinds=flow_values,
        mode=mode,
        personal_mode=personal_mode,
        commitment_transaction_ids=tuple(
            sorted({str(item) for item in commitment_transaction_ids or ()})
        ),
        as_of=request.app.state.clock().date(),
    )


AnalyticsFilterDependency = Annotated[AnalyticsFilters, Depends(analytics_filters)]


def _check_ready(request: Request) -> None:
    require_database_ready(request.app.state.db_engine)


def _translate_query_error(error: ValueError) -> AppError:
    reason = str(error)
    if reason == "unknown_account":
        return AppError("VALIDATION_ERROR", 422, "Un compte du filtre est inconnu.")
    if reason == "unknown_category":
        return AppError("VALIDATION_ERROR", 422, "Une catégorie du filtre est inconnue.")
    if reason == "invalid_commitment":
        return AppError(
            "VALIDATION_ERROR",
            422,
            "Un engagement doit être une échéance de prêt sélectionnée dans la période.",
        )
    if reason in {"unknown_metric", "metric_not_seriesable", "invalid_delta_metric"}:
        return AppError("VALIDATION_ERROR", 422, "La métrique analytique demandée est invalide.")
    if reason == "invalid_bucket":
        return AppError("VALIDATION_ERROR", 422, "La granularité de série est invalide.")
    if reason == "delta_base_metric_required":
        return AppError("VALIDATION_ERROR", 422, "Une métrique de base est requise pour cet écart.")
    if reason == "account_required":
        return AppError("VALIDATION_ERROR", 422, "Sélectionnez au moins un compte.")
    return AppError("VALIDATION_ERROR", 422, "Les filtres analytiques sont invalides.")


def _call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        result = function(*args, **kwargs)
    except ValueError as error:
        raise _translate_query_error(error) from error
    protected, out_of_range = _protect_integers(result)
    protected["total_out_of_range"] = out_of_range or bool(protected.get("total_out_of_range"))
    return cast(dict[str, Any], protected)


@router.get("/overview")
def get_overview(
    request: Request, session: DbSession, filters: AnalyticsFilterDependency
) -> dict[str, Any]:
    _check_ready(request)
    return _call(queries.overview, session, filters)


@router.get("/cashflow")
def get_cashflow(
    request: Request, session: DbSession, filters: AnalyticsFilterDependency
) -> dict[str, Any]:
    _check_ready(request)
    return _call(queries.cashflow, session, filters)


@router.get("/spending-hierarchy")
def get_spending_hierarchy(
    request: Request, session: DbSession, filters: AnalyticsFilterDependency
) -> dict[str, Any]:
    _check_ready(request)
    return _call(queries.spending_hierarchy, session, filters)


@router.get("/income")
def get_income(
    request: Request, session: DbSession, filters: AnalyticsFilterDependency
) -> dict[str, Any]:
    _check_ready(request)
    return _call(queries.income, session, filters)


@router.get("/series")
def get_series(
    request: Request,
    session: DbSession,
    filters: AnalyticsFilterDependency,
    metric_definition_id: str = Query(min_length=3, max_length=80),
    bucket: Literal["day", "week", "month"] = "month",
) -> dict[str, Any]:
    _check_ready(request)
    return _call(queries.series, session, filters, metric_definition_id, bucket)


@router.get("/merchants")
def get_merchants(
    request: Request, session: DbSession, filters: AnalyticsFilterDependency
) -> dict[str, Any]:
    _check_ready(request)
    return _call(queries.merchants, session, filters)


@router.get("/family")
def get_family(
    request: Request, session: DbSession, filters: AnalyticsFilterDependency
) -> dict[str, Any]:
    _check_ready(request)
    return _call(queries.family, session, filters)


@router.get("/savings")
def get_savings(
    request: Request, session: DbSession, filters: AnalyticsFilterDependency
) -> dict[str, Any]:
    _check_ready(request)
    return _call(queries.savings, session, filters)


@router.get("/data-quality")
def get_data_quality(
    request: Request, session: DbSession, filters: AnalyticsFilterDependency
) -> dict[str, Any]:
    _check_ready(request)
    return _call(queries.data_quality, session, filters)


@router.get("/drilldown")
def get_drilldown(
    request: Request,
    session: DbSession,
    filters: AnalyticsFilterDependency,
    metric_definition_id: str = Query(min_length=3, max_length=80),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    include_labels: bool = False,
) -> dict[str, Any]:
    _check_ready(request)
    if metric_definition_id not in METRIC_DEFINITIONS:
        raise AppError("VALIDATION_ERROR", 422, "La métrique analytique demandée est invalide.")
    if metric_definition_id == "monthly_delta.v1":
        raise AppError(
            "VALIDATION_ERROR",
            422,
            "L'écart mensuel doit être détaillé avec une métrique de base.",
        )
    key = read_hmac_key(session)
    if key is None:
        raise AppError("DATABASE_NOT_READY", 503, "La pagination locale n'est pas initialisée.")
    cursor_filters = {
        "filters": filters.as_dict(),
        "metric_definition_id": metric_definition_id,
        "include_labels": include_labels,
        "limit": limit,
    }
    now = (
        request.app.state.clock()
        .astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    store = getattr(request.app.state, "transaction_snapshot_store", None)
    if not isinstance(store, TransactionSnapshotStore):
        raise AppError("DATABASE_BUSY", 503, "La pagination locale est momentanément occupée.")
    created_snapshot = False
    if cursor:
        payload = verify_cursor(cursor, key, cursor_filters)
        snapshot_id = payload.get("snapshot_id")
        snapshot_at = payload.get("snapshot_at")
        last_date = payload.get("last_booked_on")
        last_id = payload.get("last_id")
        if not all(
            isinstance(value, str) for value in (snapshot_id, snapshot_at, last_date, last_id)
        ):
            raise AppError("CURSOR_INVALID", 422, "Le curseur analytique est invalide.")
        assert isinstance(snapshot_id, str)
        assert isinstance(snapshot_at, str)
        assert isinstance(last_date, str)
        assert isinstance(last_id, str)
        last_key = (date.fromisoformat(last_date), last_id)
    else:
        snapshot_id, _read_session = store.create(
            request.app.state.transaction_snapshot_engine, now
        )
        snapshot_at = now
        last_key = None
        created_snapshot = True
    try:
        with store.use(snapshot_id) as (read_session, snapshot_at):
            try:
                dataset = load_analytics_dataset(read_session, filters)
            except ValueError as error:
                raise _translate_query_error(error) from error
            result = _call(
                queries.drilldown,
                dataset,
                metric_definition_id,
                last_key=last_key,
                limit=limit,
                include_labels=include_labels,
            )
            next_key = result["page"].pop("next_key")
            if next_key is not None:
                result["page"]["next_cursor"] = sign_cursor(
                    {
                        "filters": cursor_filters,
                        "snapshot_id": snapshot_id,
                        "snapshot_at": snapshot_at,
                        "last_booked_on": next_key[0].isoformat(),
                        "last_id": next_key[1],
                    },
                    key,
                )
            else:
                result["page"]["next_cursor"] = None
            result["page"]["snapshot_at"] = snapshot_at
    except SnapshotUnavailable as error:
        if created_snapshot:
            store.discard(snapshot_id)
        raise AppError(
            "CURSOR_STALE", 409, "Le snapshot analytique a expiré; relancez la requête."
        ) from error
    except BaseException:
        store.discard(snapshot_id)
        raise
    if result["page"]["next_cursor"] is None:
        store.discard(snapshot_id)
    return result
