"""Human review queue and versioned classification actions."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from threading import Lock
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.classification.engine import (
    SimulationScope,
    evaluate_transaction,
    simulate_rule,
)
from app.classification.matcher import key_metadata
from app.classification.rule_store import (
    RatioPart,
    RuleAction,
    RuleCondition,
    RuleDraft,
    create_rule,
    list_rules,
    validate_rule_draft,
)
from app.core.errors import AppError
from app.db.models import (
    Account,
    Allocation,
    AuditEvent,
    ClassificationDecision,
    ClassificationSuggestion,
    IdempotencyRecord,
    Payee,
    Transaction,
)
from app.db.session import DbSession, session_scope, write_transaction
from app.services.allocations import AllocationPart, allocate_in_session
from app.services.app_secrets import read_hmac_key
from app.services.categories import require_database_ready
from app.services.idempotency import (
    find_replay,
    request_fingerprint,
    save_response,
    sign_cursor,
    verify_cursor,
)
from app.services.refund_links import (
    confirm_refund_link,
    create_refund_link,
    get_refund_link_state,
    reject_refund_link,
)
from app.services.review import undo_decision, undo_rule_application
from app.services.snapshot_integrity import canonical_json, canonical_sha256
from app.services.transactions import (
    MAX_SAFE_FRONTEND_INTEGER,
    SnapshotUnavailable,
    TransactionSnapshotStore,
)

router = APIRouter(prefix="/review", tags=["review"])
_future_rule_lock = Lock()

ReviewStatus = Literal["unclassified", "suggested", "rule", "memory", "human", "ignored", "review"]
ReviewAction = Literal[
    "confirm", "classify", "ignore", "mark_for_review", "link_refund", "unlink_refund"
]
ReviewScope = Literal["this_transaction", "selection", "future_rule"]


class StrictModel(BaseModel):
    # UUID/date wire values use their canonical JSON strings; monetary and
    # version fields remain explicitly StrictInt.
    model_config = ConfigDict(extra="forbid")


class AllocationInput(StrictModel):
    flow_kind: StrictStr = Field(min_length=1, max_length=40)
    category_id: StrictStr = Field(min_length=1, max_length=100)
    amount_cents: StrictInt
    beneficiary: StrictStr = "unknown"
    cadence: StrictStr = "unknown"
    flexibility: StrictStr = "unknown"
    acquisition: StrictStr = "unknown"


class ExpectedVersion(StrictModel):
    transaction_id: UUID
    expected_version: StrictInt = Field(ge=1)
    allocations: list[AllocationInput] = Field(default_factory=list, max_length=20)


class ReviewDecisionRequest(StrictModel):
    transaction_id: UUID
    expected_version: StrictInt = Field(ge=1)
    actor: StrictStr = Field(min_length=1, max_length=160)
    action: ReviewAction
    scope: ReviewScope = "this_transaction"
    selection: list[ExpectedVersion] = Field(default_factory=list, max_length=500)
    allocations: list[AllocationInput] = Field(default_factory=list, max_length=20)
    suggestion_id: UUID | None = None
    source_transaction_id: UUID | None = None
    target_transaction_id: UUID | None = None
    amount_cents: StrictInt | None = None
    transaction_link_id: UUID | None = None
    expected_link_version: StrictInt | None = Field(default=None, ge=1)
    expected_source_version: StrictInt | None = Field(default=None, ge=1)
    expected_target_version: StrictInt | None = Field(default=None, ge=1)
    rule: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_scope_and_action(self) -> ReviewDecisionRequest:
        if self.scope == "selection" and not self.selection:
            raise ValueError("selection exige au moins une transaction et sa version.")
        if self.scope != "selection" and self.selection:
            raise ValueError("selection n'est accepté que pour la portée selection.")
        if self.scope == "future_rule" and (
            self.rule is None or self.action not in ("classify", "confirm")
        ):
            raise ValueError("future_rule exige une définition de règle explicite.")
        if self.scope != "future_rule" and self.rule is not None:
            raise ValueError("rule n'est accepté que pour la portée future_rule.")
        if (
            self.action in ("classify", "confirm")
            and self.scope != "future_rule"
            and not self.allocations
            and self.suggestion_id is None
        ):
            raise ValueError("Une allocation ou une suggestion à confirmer est requise.")
        if self.action == "link_refund" and (
            self.source_transaction_id is None
            or self.target_transaction_id is None
            or self.amount_cents is None
            or self.expected_source_version is None
            or self.expected_target_version is None
        ):
            raise ValueError(
                "Le lien exige source, cible, centimes et versions attendues des deux opérations."
            )
        if self.action == "unlink_refund" and (
            self.transaction_link_id is None or self.expected_link_version is None
        ):
            raise ValueError("Le retrait exige un lien et sa version attendue.")
        return self


class UndoRequest(StrictModel):
    expected_version: StrictInt = Field(ge=1)
    actor: StrictStr = Field(min_length=1, max_length=160)
    confirm_human_override: bool = False


class UndoBatchRequest(StrictModel):
    expected_versions: dict[str, StrictInt]
    actor: StrictStr = Field(min_length=1, max_length=160)
    confirm_human_override: bool = False


class RefundLinkRequest(StrictModel):
    source_transaction_id: UUID
    target_transaction_id: UUID
    amount_cents: StrictInt = Field(gt=0)
    actor: StrictStr = Field(min_length=1, max_length=160)
    expected_source_version: StrictInt = Field(ge=1)
    expected_target_version: StrictInt = Field(ge=1)


class RefundLinkVersionRequest(StrictModel):
    expected_version: StrictInt = Field(ge=1)
    actor: StrictStr = Field(min_length=1, max_length=160)


class QueuePage(BaseModel):
    items: list[dict[str, Any]]
    page: dict[str, Any]


class DecisionItemResponse(BaseModel):
    transaction_id: str
    decision_id: str | None = None
    version: int
    status: str
    residual_cents: int | None = None
    total_out_of_range: bool = False


class ReviewDecisionResponse(BaseModel):
    scope: str
    items: list[DecisionItemResponse]
    status: str


class RefundLinkResponse(BaseModel):
    transaction_link_id: str
    status: str
    version: int
    amount_cents: int | None = None
    total_out_of_range: bool = False


class FutureRuleResponse(BaseModel):
    simulation: dict[str, Any]
    rule: dict[str, Any]
    applied: bool


class ReviewGroupResponse(BaseModel):
    review_group_id: str
    status: str
    transaction_count: int
    transactions: list[dict[str, Any]]


class UndoResponse(BaseModel):
    decision_id: str
    undone_decision_id: str
    transaction_id: str
    version: int
    status: str
    idempotent: bool


class UndoBatchResponse(BaseModel):
    batch_id: str
    undone_ids: list[str]
    decision_ids: list[str]
    versions: dict[str, int]
    idempotent: bool


def _engine(request: Request) -> Engine:
    engine = cast(Engine, request.app.state.db_engine)
    require_database_ready(engine)
    return engine


def _safe_cents(value: int) -> tuple[int | None, bool]:
    if abs(value) > MAX_SAFE_FRONTEND_INTEGER:
        return None, True
    return value, False


def _safe_allocation_rows(rows: list[Allocation]) -> tuple[list[dict[str, Any]], bool]:
    result: list[dict[str, Any]] = []
    out_of_range = False
    for row in rows:
        amount, unsafe = _safe_cents(row.amount_cents)
        out_of_range = out_of_range or unsafe
        result.append(
            {
                "flow_kind": row.flow_kind,
                "category_id": row.category_id,
                "amount_cents": amount,
                "origin": row.origin,
                "beneficiary": row.beneficiary,
                "cadence": row.cadence,
                "flexibility": row.flexibility,
                "acquisition": row.acquisition,
            }
        )
    return result, out_of_range


def _validate_idempotency_key(value: str) -> str:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as error:
        raise AppError(
            "IDEMPOTENCY_KEY_INVALID", 422, "La clé d'idempotence doit être un UUID."
        ) from error
    if str(parsed) != value:
        raise AppError(
            "IDEMPOTENCY_KEY_INVALID", 422, "La clé d'idempotence doit être un UUID canonique."
        )
    return value


def _status(
    transaction: Transaction,
    rows: list[Allocation],
    latest: ClassificationSuggestion | None,
) -> str:
    if any(row.origin == "manual" for row in rows):
        return "human"
    if rows:
        return "rule" if all(row.origin == "rule" for row in rows) else "memory"
    if latest is not None and latest.status == "rejected":
        return "ignored"
    if latest is not None and latest.status == "review":
        return "review" if latest.method == "manual" else "suggested"
    return "unclassified"


def _review_candidates(session: Session, filters: dict[str, Any]) -> list[dict[str, Any]]:
    statement = select(Transaction, Account, Payee).join(Account).outerjoin(Payee)
    if filters["period_start"]:
        statement = statement.where(Transaction.booked_on >= filters["period_start"])
    if filters["period_end"]:
        statement = statement.where(Transaction.booked_on <= filters["period_end"])
    if filters["account_id"]:
        statement = statement.where(Transaction.account_id == filters["account_id"])
    if filters["direction"] == "debit":
        statement = statement.where(Transaction.amount_cents < 0)
    elif filters["direction"] == "credit":
        statement = statement.where(Transaction.amount_cents > 0)
    selected = session.execute(
        statement.order_by(Transaction.booked_on.desc(), Transaction.transaction_id)
    ).all()
    ids = [row[0].transaction_id for row in selected]
    allocations: dict[str, list[Allocation]] = {}
    if ids:
        for allocation in session.scalars(
            select(Allocation).where(
                Allocation.transaction_id.in_(ids),
                Allocation.superseded_by_allocation_id.is_(None),
                Allocation.retracted_at_utc.is_(None),
            )
        ):
            allocations.setdefault(allocation.transaction_id, []).append(allocation)
    latest_suggestions: dict[str, ClassificationSuggestion] = {}
    if ids:
        for suggestion in session.scalars(
            select(ClassificationSuggestion)
            .where(ClassificationSuggestion.transaction_id.in_(ids))
            .order_by(
                ClassificationSuggestion.transaction_id,
                ClassificationSuggestion.created_at_utc.desc(),
                ClassificationSuggestion.suggestion_id.desc(),
            )
        ):
            latest_suggestions.setdefault(suggestion.transaction_id, suggestion)
    groups: dict[str, dict[str, Any]] = {}
    for transaction, _account, payee in selected:
        current = allocations.get(transaction.transaction_id, [])
        status = _status(transaction, current, latest_suggestions.get(transaction.transaction_id))
        if filters["status"] and status != filters["status"]:
            continue
        if filters["status"] in ("unclassified", "suggested") and status not in (
            "unclassified",
            "suggested",
        ):
            continue
        residual = transaction.amount_cents - sum(row.amount_cents for row in current)
        if filters["status"] == "unclassified" and residual == 0:
            continue
        metadata = key_metadata(payee.normalized_name) if payee is not None else None
        quality_flags: list[str] = []
        if metadata is not None and metadata.get("entity_kind") == "marketplace":
            quality_flags.append("ambiguous_merchant")
        elif metadata is not None and metadata.get("entity_kind") == "intermediary":
            quality_flags.append("intermediary_to_review")
        label = transaction.raw_label.casefold()
        if any(token in label for token in ("virement", "vir sepa", "vir inst")):
            quality_flags.append("transfer_to_review")
        if any(token in label for token in ("pret", "prêt", "emprunt", "echeance")):
            quality_flags.append("loan_to_review")
        if any(token in label for token in ("retrait", "dab", "distributeur")):
            quality_flags.append("cash_to_review")
        if any(row.flow_kind.startswith("transfer_") for row in current):
            quality_flags.append("transfer_to_review")
        if any(row.flow_kind.startswith("loan_") for row in current):
            quality_flags.append("loan_to_review")
        if any(row.flow_kind == "cash_withdrawal_pending" for row in current):
            quality_flags.append("cash_to_review")
        quality_flags = sorted(set(quality_flags))
        group_basis = (
            f"payee:{payee.payee_id}"
            if payee is not None
            else f"occurrence:{transaction.transaction_id}"
        )
        # Ambiguous merchants and exceptional flow types are visibly separated.
        group_key = f"{group_basis}:{status}:{','.join(quality_flags)}"
        group = groups.setdefault(
            group_key,
            {
                "review_group_id": hashlib.sha256(group_key.encode()).hexdigest()[:32],
                "payee_id": payee.payee_id if payee else None,
                "counterparty": payee.display_name if payee else None,
                "status": status,
                "quality_flags": quality_flags,
                "transaction_count": 0,
                "amount_sum": 0,
                "impact_cents": 0,
                "_sort_impact_cents": 0,
                "latest_booked_on": transaction.booked_on.isoformat(),
                "transaction_ids": [],
                "priority": 0,
            },
        )
        group["transaction_count"] += 1
        group["amount_sum"] += transaction.amount_cents
        group["impact_cents"] += abs(residual)
        group["_sort_impact_cents"] += abs(residual)
        group["latest_booked_on"] = max(
            group["latest_booked_on"], transaction.booked_on.isoformat()
        )
        group["transaction_ids"].append(transaction.transaction_id)
        # Severe known review cases surface before routine unclassified items.
        group["priority"] = max(
            group["priority"], 4 if status == "suggested" else 3 if status == "unclassified" else 1
        )
    result = list(groups.values())
    result.sort(
        key=lambda item: (
            -int(item["_sort_impact_cents"]),
            -int(item["latest_booked_on"].replace("-", "")),
            item["review_group_id"],
        )
    )
    safe_result = []
    for group in result:
        amount, out_of_range = _safe_cents(group.pop("amount_sum"))
        impact, impact_out = _safe_cents(group.pop("impact_cents"))
        group["amount_cents"] = amount
        group["impact_cents"] = impact
        group["total_out_of_range"] = out_of_range or impact_out
        safe_result.append(group)
    return safe_result


@router.get("/queue", response_model=QueuePage)
def get_review_queue(
    request: Request,
    response: Response,
    session: DbSession,
    period_start: date | None = None,
    period_end: date | None = None,
    account_id: UUID | None = None,
    direction: Literal["debit", "credit"] | None = None,
    status: ReviewStatus | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if period_start and period_end and period_end < period_start:
        raise AppError("VALIDATION_ERROR", 422, "La période de revue est invalide.")
    _engine(request)
    key = read_hmac_key(session)
    store = getattr(request.app.state, "transaction_snapshot_store", None)
    if key is None or not isinstance(store, TransactionSnapshotStore):
        raise AppError("DATABASE_NOT_READY", 503, "La pagination de revue n'est pas initialisée.")
    filters = {
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
        "account_id": str(account_id) if account_id else None,
        "direction": direction,
        "status": status,
        "limit": limit,
    }
    now = (
        request.app.state.clock()
        .astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    snapshot_id: str
    last_key: tuple[int, int, str] | None = None
    created = False
    if cursor:
        decoded = verify_cursor(cursor, key, filters)
        snapshot_id = decoded.get("snapshot_id", "")
        snapshot_at = decoded.get("snapshot_at")
        position = decoded.get("last")
        if (
            not isinstance(snapshot_id, str)
            or not isinstance(snapshot_at, str)
            or not isinstance(position, list)
            or len(position) != 3
        ):
            raise AppError("CURSOR_INVALID", 422, "Le curseur de revue est invalide.")
        try:
            last_key = (int(position[0]), int(position[1]), str(position[2]))
        except (TypeError, ValueError) as error:
            raise AppError("CURSOR_INVALID", 422, "Le curseur de revue est invalide.") from error
    else:
        snapshot_at = now
        snapshot_id, _ = store.create(request.app.state.transaction_snapshot_engine, snapshot_at)
        created = True
    try:
        with store.use(snapshot_id) as (snapshot_session, snapshot_at):
            groups = _review_candidates(
                snapshot_session,
                {
                    **filters,
                    "period_start": period_start,
                    "period_end": period_end,
                },
            )

            def key_for(item: dict[str, Any]) -> tuple[int, int, str]:
                return (
                    -int(item.get("_sort_impact_cents", 0)),
                    -int(item["latest_booked_on"].replace("-", "")),
                    str(item["review_group_id"]),
                )

            if last_key is not None:
                groups = [item for item in groups if key_for(item) > last_key]
            page_items = groups[:limit]
            more = len(groups) > limit
            next_cursor = None
            if more and page_items:
                last = page_items[-1]
                next_cursor = sign_cursor(
                    {
                        "filters": filters,
                        "snapshot_id": snapshot_id,
                        "snapshot_at": snapshot_at,
                        "last": [
                            -int(last.get("_sort_impact_cents", 0)),
                            -int(last["latest_booked_on"].replace("-", "")),
                            str(last["review_group_id"]),
                        ],
                    },
                    key,
                )
            safe_items = [
                {
                    key: value
                    for key, value in item.items()
                    if key not in ("transaction_ids", "priority", "_sort_impact_cents")
                }
                for item in page_items
            ]
            page = {"limit": limit, "next_cursor": next_cursor, "snapshot_at": snapshot_at}
    except SnapshotUnavailable as error:
        if created:
            store.discard(snapshot_id)
        raise AppError(
            "CURSOR_STALE", 409, "Le snapshot de revue a expiré; relancez la file."
        ) from error
    except BaseException:
        store.discard(snapshot_id)
        raise
    if next_cursor is None:
        store.discard(snapshot_id)
    return {"items": safe_items, "page": page}


def _transaction_detail(
    session: Session, transaction: Transaction, account: Account
) -> dict[str, Any]:
    current = list(
        session.scalars(
            select(Allocation)
            .where(
                Allocation.transaction_id == transaction.transaction_id,
                Allocation.superseded_by_allocation_id.is_(None),
                Allocation.retracted_at_utc.is_(None),
            )
            .order_by(Allocation.allocation_id)
        )
    )
    residual = transaction.amount_cents - sum(row.amount_cents for row in current)
    safe, out = _safe_cents(transaction.amount_cents)
    residual_safe, residual_out = _safe_cents(residual)
    safe_allocations, allocations_out = _safe_allocation_rows(current)
    latest_suggestion = session.scalar(
        select(ClassificationSuggestion)
        .where(ClassificationSuggestion.transaction_id == transaction.transaction_id)
        .order_by(
            ClassificationSuggestion.created_at_utc.desc(),
            ClassificationSuggestion.suggestion_id.desc(),
        )
        .limit(1)
    )
    return {
        "transaction_id": transaction.transaction_id,
        "account_id": account.account_id,
        "booked_on": transaction.booked_on.isoformat(),
        "amount_cents": safe,
        "residual_cents": residual_safe,
        "total_out_of_range": out or residual_out or allocations_out,
        "version": transaction.version,
        "status": _status(transaction, current, latest_suggestion),
        "allocations": safe_allocations,
    }


@router.get("/queue/{review_group_id}", response_model=ReviewGroupResponse)
def get_review_group(review_group_id: str, request: Request, session: DbSession) -> dict[str, Any]:
    engine = _engine(request)
    groups = _review_candidates(
        session,
        {
            "period_start": None,
            "period_end": None,
            "account_id": None,
            "direction": None,
            "status": None,
        },
    )
    group = next((item for item in groups if item["review_group_id"] == review_group_id), None)
    if group is None:
        raise AppError("REVIEW_GROUP_NOT_FOUND", 404, "Le groupe de revue demandé n'existe pas.")
    transactions = []
    for transaction_id in group["transaction_ids"]:
        transaction = session.get(Transaction, transaction_id)
        if transaction is None:
            continue
        account = session.get(Account, transaction.account_id)
        if account is None:
            continue
        evaluation = evaluate_transaction(engine, transaction_id)
        history = session.scalars(
            select(ClassificationDecision)
            .where(ClassificationDecision.transaction_id == transaction_id)
            .order_by(ClassificationDecision.created_at_utc, ClassificationDecision.decision_id)
        ).all()
        suggestions = session.scalars(
            select(ClassificationSuggestion)
            .where(ClassificationSuggestion.transaction_id == transaction_id)
            .order_by(
                ClassificationSuggestion.created_at_utc, ClassificationSuggestion.suggestion_id
            )
        ).all()
        suggestion_rows = []
        for item in suggestions:
            amount = None
            out_of_range = False
            if item.suggested_amount_cents is not None:
                amount, out_of_range = _safe_cents(item.suggested_amount_cents)
            suggestion_rows.append(
                {
                    "suggestion_id": item.suggestion_id,
                    "source": item.method,
                    "category_id": item.category_id,
                    "flow_kind": item.proposed_flow_kind,
                    "amount_cents": amount,
                    "total_out_of_range": out_of_range,
                    "confidence": item.confidence_label,
                    "status": item.status,
                }
            )
        transactions.append(
            {
                **_transaction_detail(session, transaction, account),
                "history": [
                    {
                        "decision_id": row.decision_id,
                        "action": row.action,
                        "actor": row.actor,
                        "created_at_utc": row.created_at_utc.isoformat().replace("+00:00", "Z"),
                        "undone": row.undone_by_decision_id is not None,
                    }
                    for row in history
                ],
                "suggestions": suggestion_rows,
                "evaluation": {
                    "version": evaluation.transaction_version,
                    "human_decision_protected": evaluation.human_decision_protected,
                    "conflict_rule_ids": list(evaluation.conflict_rule_ids),
                    "candidates": [
                        {
                            "source": item.source,
                            "priority": item.priority_rank,
                            "category_id": item.category_id,
                            "flow_kind": item.flow_kind,
                            "amount_cents": _safe_cents(item.amount_cents)[0],
                            "total_out_of_range": _safe_cents(item.amount_cents)[1],
                            "confidence": item.confidence,
                            "rule_id": item.rule_id,
                            "rule_priority": item.rule_priority,
                            "suggestion_id": item.suggestion_id,
                        }
                        for item in evaluation.candidates
                    ],
                },
            }
        )
    return {
        "review_group_id": group["review_group_id"],
        "status": group["status"],
        "transaction_count": group["transaction_count"],
        "transactions": transactions,
    }


def _request_payload(body: ReviewDecisionRequest) -> dict[str, Any]:
    return body.model_dump(mode="json")


def _write_marker(session: Session, transaction: Transaction, *, action: str, actor: str) -> str:
    decision_id = str(uuid4())
    snapshot = canonical_json([])
    snapshot_hash = canonical_sha256([])
    latest_suggestion = session.scalar(
        select(ClassificationSuggestion)
        .where(ClassificationSuggestion.transaction_id == transaction.transaction_id)
        .order_by(
            ClassificationSuggestion.created_at_utc.desc(),
            ClassificationSuggestion.suggestion_id.desc(),
        )
        .limit(1)
    )
    session.add(
        ClassificationDecision(
            decision_id=decision_id,
            transaction_id=transaction.transaction_id,
            action="reject",
            actor=actor,
            suggestion_id=latest_suggestion.suggestion_id if latest_suggestion else None,
            before_json=snapshot,
            after_json=snapshot,
            before_json_sha256=snapshot_hash,
            after_json_sha256=snapshot_hash,
            transaction_version=transaction.version + 1,
        )
    )
    if action == "ignore":
        for suggestion in session.scalars(
            select(ClassificationSuggestion).where(
                ClassificationSuggestion.transaction_id == transaction.transaction_id,
                ClassificationSuggestion.status == "review",
            )
        ):
            suggestion.status = "rejected"
            suggestion.version += 1
    elif action == "review":
        session.add(
            ClassificationSuggestion(
                suggestion_id=str(uuid4()),
                transaction_id=transaction.transaction_id,
                proposed_flow_kind="unknown",
                category_id=None,
                suggested_amount_cents=None,
                method="manual",
                confidence_label="inconnu",
                status="review",
            )
        )
    transaction.version += 1
    session.add(
        AuditEvent(
            audit_event_id=str(uuid4()),
            target_type="transaction",
            target_id=transaction.transaction_id,
            action=f"classification.{action}",
            actor=actor,
            before_json=None,
            after_json=json.dumps({"decision_id": decision_id}, separators=(",", ":")),
        )
    )
    return decision_id


@router.post(
    "/decisions",
    response_model=ReviewDecisionResponse | RefundLinkResponse | FutureRuleResponse,
)
def apply_review_decision(
    body: ReviewDecisionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> dict[str, Any]:
    idempotency_key = _validate_idempotency_key(idempotency_key)
    engine = _engine(request)
    if body.scope == "future_rule":
        return _create_future_rule(body, engine, idempotency_key)
    if body.action in ("link_refund", "unlink_refund"):
        return _apply_refund_action(body, engine, idempotency_key)
    payload = _request_payload(body)
    path = "/api/v1/review/decisions"
    fingerprint = request_fingerprint("POST", path, payload)
    ids = [(str(body.transaction_id), body.expected_version)]
    if body.scope == "selection":
        ids.extend((str(item.transaction_id), item.expected_version) for item in body.selection)
        if len({item[0] for item in ids}) != len(ids):
            raise AppError(
                "REVIEW_INVALID", 422, "Une transaction apparaît plusieurs fois dans la sélection."
            )
    decision_results: list[dict[str, Any]] = []
    with write_transaction(engine) as session:
        replay = find_replay(
            session,
            idempotency_key=idempotency_key,
            http_method="POST",
            canonical_path=path,
            request_sha256=fingerprint,
        )
        if replay is not None:
            return replay[1]
        transactions: list[Transaction] = []
        conflicts: list[dict[str, int | str]] = []
        for transaction_id, version in ids:
            transaction = session.get(Transaction, transaction_id)
            if transaction is None:
                raise AppError("TRANSACTION_NOT_FOUND", 404, "Transaction introuvable.")
            if transaction.version != version:
                conflicts.append(
                    {"transaction_id": transaction_id, "current_version": transaction.version}
                )
            transactions.append(transaction)
        if conflicts:
            if body.scope == "selection":
                raise AppError(
                    "REVIEW_SELECTION_CONFLICT",
                    409,
                    "La sélection contient des versions périmées.",
                    {"conflicts": conflicts},
                )
            raise AppError(
                "VERSION_CONFLICT",
                409,
                "La transaction a été modifiée.",
                {"current_version": conflicts[0]["current_version"]},
            )
        if body.action in ("classify", "confirm"):
            parts = body.allocations
            if body.suggestion_id is not None:
                evaluation = evaluate_transaction(engine, str(body.transaction_id))
                candidate = next(
                    (
                        item
                        for item in evaluation.candidates
                        if item.suggestion_id == str(body.suggestion_id)
                    ),
                    None,
                )
                if candidate is None:
                    raise AppError(
                        "SUGGESTION_NOT_FOUND", 404, "La suggestion demandée n'existe pas."
                    )
                if candidate.category_id is None or candidate.flow_kind is None:
                    raise AppError(
                        "SUGGESTION_NOT_ACTIONABLE",
                        409,
                        "La suggestion ne contient pas une allocation confirmable.",
                    )
                parts = [
                    AllocationInput(
                        flow_kind=candidate.flow_kind,
                        category_id=candidate.category_id,
                        amount_cents=candidate.amount_cents,
                    )
                ]
            if body.scope == "selection" and body.suggestion_id is not None:
                raise AppError(
                    "REVIEW_SCOPE_INVALID",
                    422,
                    "Une suggestion ne peut être confirmée que sur cette transaction.",
                )
            per_transaction = {
                str(item.transaction_id): item.allocations for item in body.selection
            }
            for transaction in transactions:
                transaction_parts = per_transaction.get(transaction.transaction_id) or parts
                allocation_parts = [
                    AllocationPart(**part.model_dump()) for part in transaction_parts
                ]
                state = allocate_in_session(
                    session,
                    transaction.transaction_id,
                    allocation_parts,
                    mode="complete",
                    origin="manual",
                    actor=body.actor,
                    expected_version=transaction.version,
                )
                decision_results.append(
                    {
                        "transaction_id": transaction.transaction_id,
                        "decision_id": state.decision_id,
                        "version": state.version,
                        "status": state.state,
                        "residual_cents": _safe_cents(state.residual_cents)[0],
                        "total_out_of_range": _safe_cents(state.residual_cents)[1]
                        or any(_safe_cents(item.amount_cents)[1] for item in state.allocations),
                    }
                )
        elif body.action in ("ignore", "mark_for_review"):
            if len(transactions) != 1:
                raise AppError(
                    "REVIEW_SCOPE_INVALID",
                    422,
                    "Cette action ne s'applique qu'à une transaction à la fois.",
                )
            transaction = transactions[0]
            active_allocations = session.scalars(
                select(Allocation).where(
                    Allocation.transaction_id == transaction.transaction_id,
                    Allocation.superseded_by_allocation_id.is_(None),
                    Allocation.retracted_at_utc.is_(None),
                )
            ).all()
            if active_allocations:
                raise AppError(
                    "HUMAN_DECISION_PROTECTED",
                    409,
                    "Une allocation existante doit être corrigée explicitement.",
                )
            decision_id = _write_marker(
                session,
                transaction,
                action="ignore" if body.action == "ignore" else "review",
                actor=body.actor,
            )
            decision_results.append(
                {
                    "transaction_id": transaction.transaction_id,
                    "decision_id": decision_id,
                    "version": transaction.version,
                    "status": "ignored" if body.action == "ignore" else "review",
                }
            )
        else:
            raise AppError(
                "REVIEW_ACTION_INVALID",
                422,
                "L'action de remboursement doit utiliser l'endpoint de lien dédié.",
            )
        result = {
            "scope": body.scope,
            "items": decision_results,
            "status": "confirmed" if body.action in ("classify", "confirm") else body.action,
        }
        save_response(
            session,
            idempotency_key=idempotency_key,
            http_method="POST",
            canonical_path=path,
            request_sha256=fingerprint,
            response_status=200,
            response=result,
        )
    return result


def _idempotency_replay(
    engine: Engine,
    *,
    key: str,
    path: str,
    fingerprint: str,
    method: str = "POST",
) -> dict[str, Any] | None:
    with session_scope(engine) as session:
        replay = find_replay(
            session,
            idempotency_key=key,
            http_method=method,
            canonical_path=path,
            request_sha256=fingerprint,
        )
    return replay[1] if replay is not None else None


def _save_idempotency(
    engine: Engine,
    *,
    key: str,
    path: str,
    fingerprint: str,
    response: dict[str, Any],
    method: str = "POST",
) -> None:
    with write_transaction(engine) as session:
        save_response(
            session,
            idempotency_key=key,
            http_method=method,
            canonical_path=path,
            request_sha256=fingerprint,
            response_status=200,
            response=response,
        )


def _update_idempotency_response(engine: Engine, *, key: str, response: dict[str, Any]) -> None:
    with write_transaction(engine) as session:
        record = session.get(IdempotencyRecord, key)
        if record is None:
            raise AppError("IDEMPOTENCY_RECORD_MISSING", 500, "La réponse idempotente est absente.")
        record.response_status = 200
        record.response_json = json.dumps(
            response, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )


def _create_future_rule(
    body: ReviewDecisionRequest, engine: Engine, idempotency_key: str
) -> dict[str, Any]:
    # rule_store and idempotency_records are separate service transactions. Serialize
    # same-process requests so one idempotency key cannot create duplicate drafts.
    with _future_rule_lock:
        return _create_future_rule_locked(body, engine, idempotency_key)


def _create_future_rule_locked(
    body: ReviewDecisionRequest, engine: Engine, idempotency_key: str
) -> dict[str, Any]:
    if body.rule is None:
        raise AppError("REVIEW_INVALID", 422, "Une définition de règle est requise.")
    path = "/api/v1/review/decisions"
    fingerprint = request_fingerprint("POST", path, _request_payload(body))
    replay = _idempotency_replay(engine, key=idempotency_key, path=path, fingerprint=fingerprint)
    pending_at: datetime | None = None
    simulation_payload: dict[str, Any] | None = None
    if replay is not None:
        if replay.get("_future_rule_pending") is not True:
            return replay
        try:
            pending_at = datetime.fromisoformat(
                str(replay["reserved_at_utc"]).replace("Z", "+00:00")
            )
        except (KeyError, ValueError) as error:
            raise AppError(
                "IDEMPOTENCY_RECORD_INVALID", 500, "La réservation idempotente est invalide."
            ) from error
        simulation_payload = replay.get("simulation")
        if not isinstance(simulation_payload, dict):
            raise AppError(
                "IDEMPOTENCY_RECORD_INVALID", 500, "La simulation idempotente est absente."
            )
    try:
        raw = body.rule
        condition_data = dict(raw["condition"])
        action_data = dict(raw["action"])
        account_ids = condition_data.get("account_ids", ())
        if isinstance(account_ids, list):
            condition_data["account_ids"] = tuple(account_ids)
        for field_name in ("booked_from", "booked_through"):
            field_value = condition_data.get(field_name)
            if isinstance(field_value, str):
                condition_data[field_name] = date.fromisoformat(field_value)
        ratio_parts = action_data.get("ratios", ())
        if not isinstance(ratio_parts, (list, tuple)):
            raise TypeError("ratios doit être une liste")
        if isinstance(ratio_parts, list):
            action_data["ratios"] = tuple(RatioPart(**part) for part in ratio_parts)
        condition = RuleCondition(**condition_data)
        rule_action = RuleAction(**action_data)
        draft = RuleDraft(
            name=str(raw["name"]),
            condition=condition,
            action=rule_action,
            priority=raw.get("priority", 100),
            application_scope="future_only",
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AppError("REVIEW_INVALID", 422, "La définition de règle est invalide.") from error
    draft = validate_rule_draft(draft)
    if pending_at is None:
        with session_scope(engine) as session:
            transaction = session.get(Transaction, str(body.transaction_id))
            if transaction is None:
                raise AppError("TRANSACTION_NOT_FOUND", 404, "Transaction introuvable.")
            if transaction.version != body.expected_version:
                raise AppError(
                    "VERSION_CONFLICT",
                    409,
                    "La transaction a été modifiée.",
                    {"current_version": transaction.version},
                )
        simulation_result = simulate_rule(engine, draft, SimulationScope(max_transactions=10_000))
        simulation_payload = _simulation_json(simulation_result)
        current_time = datetime.now(UTC)
        # Rule timestamps are stored at millisecond precision; align the marker so
        # a same-millisecond creation remains discoverable after an interrupted write.
        pending_at = current_time.replace(microsecond=(current_time.microsecond // 1000) * 1000)
        _save_idempotency(
            engine,
            key=idempotency_key,
            path=path,
            fingerprint=fingerprint,
            response={
                "_future_rule_pending": True,
                "reserved_at_utc": pending_at.isoformat().replace("+00:00", "Z"),
                "simulation": simulation_payload,
            },
        )
    pending_at = pending_at.astimezone(UTC)
    record = next(
        (
            candidate
            for candidate in list_rules(engine)
            if candidate.created_by == body.actor.strip()
            and candidate.created_at_utc >= pending_at
            and candidate.name == draft.name
            and candidate.condition == draft.condition
            and candidate.action == draft.action
            and candidate.priority == draft.priority
            and candidate.application_scope == draft.application_scope
        ),
        None,
    )
    if record is None:
        with session_scope(engine) as session:
            transaction = session.get(Transaction, str(body.transaction_id))
            if transaction is None:
                raise AppError("TRANSACTION_NOT_FOUND", 404, "Transaction introuvable.")
            if transaction.version != body.expected_version:
                raise AppError(
                    "VERSION_CONFLICT",
                    409,
                    "La transaction a été modifiée.",
                    {"current_version": transaction.version},
                )
        record = create_rule(engine, draft, actor=body.actor)
    result = {
        "simulation": simulation_payload,
        "rule": {
            "rule_id": record.rule_id,
            "version": record.version,
            "status": record.status,
            "application_scope": record.application_scope,
        },
        "applied": False,
    }
    _update_idempotency_response(engine, key=idempotency_key, response=result)
    return result


def _apply_refund_action(
    body: ReviewDecisionRequest, engine: Engine, idempotency_key: str
) -> dict[str, Any]:
    if body.scope != "this_transaction":
        raise AppError(
            "REVIEW_SCOPE_INVALID", 422, "Un lien de remboursement vise une transaction précise."
        )
    path = "/api/v1/review/decisions"
    fingerprint = request_fingerprint("POST", path, _request_payload(body))
    replay = _idempotency_replay(engine, key=idempotency_key, path=path, fingerprint=fingerprint)
    if replay is not None:
        return replay
    if body.action == "link_refund":
        assert body.source_transaction_id is not None
        assert body.target_transaction_id is not None
        assert body.amount_cents is not None
        if str(body.transaction_id) not in (
            str(body.source_transaction_id),
            str(body.target_transaction_id),
        ):
            raise AppError(
                "REVIEW_INVALID", 422, "La transaction de la décision doit être une jambe du lien."
            )
        with session_scope(engine) as session:
            anchor = session.get(Transaction, str(body.transaction_id))
            if anchor is None:
                raise AppError("TRANSACTION_NOT_FOUND", 404, "Transaction introuvable.")
            if anchor.version != body.expected_version:
                raise AppError(
                    "VERSION_CONFLICT",
                    409,
                    "La transaction a été modifiée.",
                    {"current_version": anchor.version},
                )
        state = create_refund_link(
            engine,
            str(body.source_transaction_id),
            str(body.target_transaction_id),
            body.amount_cents,
            actor=body.actor,
            expected_source_version=body.expected_source_version,
            expected_target_version=body.expected_target_version,
        )
        result = {
            "transaction_link_id": state.transaction_link_id,
            "status": state.status,
            "version": state.version,
            "amount_cents": _safe_cents(state.amount_cents)[0],
            "total_out_of_range": _safe_cents(state.amount_cents)[1],
        }
    else:
        assert body.transaction_link_id is not None
        assert body.expected_link_version is not None
        link_state = get_refund_link_state(engine, str(body.transaction_link_id))
        if str(body.transaction_id) not in (
            link_state.source_transaction_id,
            link_state.target_transaction_id,
        ):
            raise AppError(
                "REVIEW_INVALID", 422, "La transaction de la décision doit être une jambe du lien."
            )
        with session_scope(engine) as session:
            anchor = session.get(Transaction, str(body.transaction_id))
            if anchor is None:
                raise AppError("TRANSACTION_NOT_FOUND", 404, "Transaction introuvable.")
            if anchor.version != body.expected_version:
                raise AppError(
                    "VERSION_CONFLICT",
                    409,
                    "La transaction a été modifiée.",
                    {"current_version": anchor.version},
                )
        state = reject_refund_link(
            engine,
            str(body.transaction_link_id),
            actor=body.actor,
            expected_version=body.expected_link_version,
        )
        result = {
            "transaction_link_id": state.transaction_link_id,
            "status": state.status,
            "version": state.version,
        }
    _save_idempotency(
        engine,
        key=idempotency_key,
        path=path,
        fingerprint=fingerprint,
        response=result,
    )
    return result


def _simulation_json(simulation: Any) -> dict[str, Any]:
    rows = []
    for row in simulation.rows:
        amount, out_of_range = _safe_cents(row.amount_cents)
        after = []
        for part in row.after:
            safe_part = dict(part)
            part_amount = safe_part.get("amount_cents")
            if isinstance(part_amount, int) and not isinstance(part_amount, bool):
                safe_part["amount_cents"], part_out = _safe_cents(part_amount)
                out_of_range = out_of_range or part_out
            after.append(safe_part)
        rows.append(
            {
                "transaction_id": row.transaction_id,
                "transaction_version": row.transaction_version,
                "booked_on": row.booked_on.isoformat(),
                "amount_cents": amount,
                "total_out_of_range": out_of_range,
                "after": after,
                "ambiguous": row.ambiguous,
            }
        )
    totals = {}
    for category, total in simulation.totals_by_category.items():
        amount, out_of_range = _safe_cents(total.amount_cents)
        totals[category] = {
            "transaction_count": total.transaction_count,
            "amount_cents": amount,
            "total_out_of_range": out_of_range,
        }
    return {
        "scanned_count": simulation.scanned_count,
        "matched_count": simulation.matched_count,
        "rows": rows,
        "totals_by_category": totals,
        "conflict_transaction_ids": list(simulation.conflict_transaction_ids),
        "human_decision_ignored_ids": list(simulation.human_decision_ignored_ids),
        "already_classified_ids": list(simulation.already_classified_ids),
        "ambiguous_transaction_ids": list(simulation.ambiguous_transaction_ids),
        "truncated": simulation.truncated,
        "database_changed": False,
    }


@router.post("/decisions/{decision_id}/undo", response_model=UndoResponse)
def undo_review_decision(
    decision_id: UUID,
    body: UndoRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> dict[str, Any]:
    idempotency_key = _validate_idempotency_key(idempotency_key)
    engine = _engine(request)
    payload = body.model_dump(mode="json")
    path = f"/api/v1/review/decisions/{decision_id}/undo"
    fingerprint = request_fingerprint("POST", path, payload)
    with write_transaction(engine) as session:
        replay = find_replay(
            session,
            idempotency_key=idempotency_key,
            http_method="POST",
            canonical_path=path,
            request_sha256=fingerprint,
        )
        if replay is not None:
            return replay[1]
        decision = session.get(ClassificationDecision, str(decision_id))
        if decision is None:
            raise AppError("DECISION_NOT_FOUND", 404, "La décision demandée n'existe pas.")
        transaction_id = decision.transaction_id
        transaction = session.get(Transaction, transaction_id)
        if transaction is None:
            raise AppError("TRANSACTION_NOT_FOUND", 404, "Transaction introuvable.")
        # The T20 service owns the undo transaction.
    undone = undo_decision(
        engine,
        str(decision_id),
        expected_version=body.expected_version,
        actor=body.actor,
        confirm_human_override=body.confirm_human_override,
    )
    result = {
        "decision_id": undone.undo_decision_id,
        "undone_decision_id": str(decision_id),
        "transaction_id": transaction_id,
        "version": undone.state.version,
        "status": undone.state.state,
        "idempotent": undone.idempotent,
    }
    with write_transaction(engine) as session:
        replay = find_replay(
            session,
            idempotency_key=idempotency_key,
            http_method="POST",
            canonical_path=path,
            request_sha256=fingerprint,
        )
        if replay is not None:
            return replay[1]
        save_response(
            session,
            idempotency_key=idempotency_key,
            http_method="POST",
            canonical_path=path,
            request_sha256=fingerprint,
            response_status=200,
            response=result,
        )
    return result


@router.post("/rule-batches/{batch_id}/undo", response_model=UndoBatchResponse)
def undo_review_rule_batch(
    batch_id: UUID,
    body: UndoBatchRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> dict[str, Any]:
    idempotency_key = _validate_idempotency_key(idempotency_key)
    engine = _engine(request)
    path = f"/api/v1/review/rule-batches/{batch_id}/undo"
    fingerprint = request_fingerprint("POST", path, body.model_dump(mode="json"))
    replay = _idempotency_replay(engine, key=idempotency_key, path=path, fingerprint=fingerprint)
    if replay is not None:
        return replay
    try:
        normalized_versions = {
            str(UUID(key)): value for key, value in body.expected_versions.items()
        }
    except ValueError as error:
        raise AppError("REVIEW_INVALID", 422, "Une version du lot est invalide.") from error
    undone = undo_rule_application(
        engine,
        str(batch_id),
        expected_versions=normalized_versions,
        actor=body.actor,
        confirm_human_override=body.confirm_human_override,
    )
    result = {
        "batch_id": undone.batch_id,
        "undone_ids": list(undone.undone_ids),
        "decision_ids": list(undone.undo_decision_ids),
        "versions": {state.transaction_id: state.version for state in undone.states},
        "idempotent": undone.idempotent,
    }
    _save_idempotency(
        engine,
        key=idempotency_key,
        path=path,
        fingerprint=fingerprint,
        response=result,
    )
    return result


@router.post("/refund-links", response_model=RefundLinkResponse)
def create_review_refund_link(
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    body: RefundLinkRequest,
) -> dict[str, Any]:
    idempotency_key = _validate_idempotency_key(idempotency_key)
    engine = _engine(request)
    path = "/api/v1/review/refund-links"
    payload = {
        "source_transaction_id": str(body.source_transaction_id),
        "target_transaction_id": str(body.target_transaction_id),
        "amount_cents": body.amount_cents,
        "actor": body.actor,
        "expected_source_version": body.expected_source_version,
        "expected_target_version": body.expected_target_version,
    }
    fingerprint = request_fingerprint("POST", path, payload)
    replay = _idempotency_replay(engine, key=idempotency_key, path=path, fingerprint=fingerprint)
    if replay is not None:
        return replay
    state = create_refund_link(
        engine,
        str(body.source_transaction_id),
        str(body.target_transaction_id),
        body.amount_cents,
        actor=body.actor,
        expected_source_version=body.expected_source_version,
        expected_target_version=body.expected_target_version,
    )
    result = {
        "transaction_link_id": state.transaction_link_id,
        "status": state.status,
        "version": state.version,
        "amount_cents": _safe_cents(state.amount_cents)[0],
        "total_out_of_range": _safe_cents(state.amount_cents)[1],
    }
    _save_idempotency(
        engine,
        key=idempotency_key,
        path=path,
        fingerprint=fingerprint,
        response=result,
    )
    return result


@router.post("/refund-links/{transaction_link_id}/confirm", response_model=RefundLinkResponse)
def confirm_review_refund_link(
    transaction_link_id: UUID,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    body: RefundLinkVersionRequest,
) -> dict[str, Any]:
    idempotency_key = _validate_idempotency_key(idempotency_key)
    engine = _engine(request)
    path = f"/api/v1/review/refund-links/{transaction_link_id}/confirm"
    fingerprint = request_fingerprint("POST", path, body.model_dump(mode="json"))
    replay = _idempotency_replay(engine, key=idempotency_key, path=path, fingerprint=fingerprint)
    if replay is not None:
        return replay
    state = confirm_refund_link(
        engine,
        str(transaction_link_id),
        actor=body.actor,
        expected_version=body.expected_version,
    )
    result = {
        "transaction_link_id": state.transaction_link_id,
        "status": state.status,
        "version": state.version,
        "amount_cents": _safe_cents(state.amount_cents)[0],
        "total_out_of_range": _safe_cents(state.amount_cents)[1],
    }
    _save_idempotency(
        engine,
        key=idempotency_key,
        path=path,
        fingerprint=fingerprint,
        response=result,
    )
    return result


@router.delete("/refund-links/{transaction_link_id}", response_model=RefundLinkResponse)
def unlink_review_refund_link(
    transaction_link_id: UUID,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    body: RefundLinkVersionRequest,
) -> dict[str, Any]:
    idempotency_key = _validate_idempotency_key(idempotency_key)
    engine = _engine(request)
    path = f"/api/v1/review/refund-links/{transaction_link_id}"
    fingerprint = request_fingerprint("DELETE", path, body.model_dump(mode="json"))
    replay = _idempotency_replay(
        engine, key=idempotency_key, path=path, fingerprint=fingerprint, method="DELETE"
    )
    if replay is not None:
        return replay
    state = reject_refund_link(
        engine,
        str(transaction_link_id),
        actor=body.actor,
        expected_version=body.expected_version,
    )
    result = {
        "transaction_link_id": state.transaction_link_id,
        "status": state.status,
        "version": state.version,
    }
    _save_idempotency(
        engine,
        key=idempotency_key,
        path=path,
        fingerprint=fingerprint,
        response=result,
        method="DELETE",
    )
    return result
