"""Audited, versioned refund-of links between existing bank transactions."""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.errors import VersionConflictError
from app.db.models import Allocation, AuditEvent, Transaction, TransactionLink
from app.db.session import session_scope, write_transaction
from app.domain.money_split import MoneySplitError, prorate_link

LinkStatus = Literal["proposed", "confirmed", "rejected"]


@dataclass(frozen=True, slots=True)
class RefundLinkState:
    transaction_link_id: str
    source_transaction_id: str
    source_allocation_id: str | None
    target_transaction_id: str
    link_kind: str
    amount_cents: int
    target_allocation_id: str | None
    status: LinkStatus
    version: int
    superseded_by_transaction_link_id: str | None


@dataclass(frozen=True, slots=True)
class RefundCategoryEffect:
    """Net positive expense cents by category, with links scoped to both legs."""

    by_category_cents: dict[str, int]
    matched_refunds_cents: int
    ignored_out_of_scope_refunds_cents: int


def _fail(code: str, status: int, message: str) -> AppError:
    return AppError(code, status, message)


def _strict_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise _fail("REFUND_LINK_INVALID", 422, f"{field} doit être un entier strict.")
    return value


def _actor_value(actor: str) -> str:
    if not isinstance(actor, str) or not actor.strip():
        raise _fail("REFUND_LINK_INVALID", 422, "Un acteur non vide est requis.")
    return actor


def _link_state(row: TransactionLink) -> RefundLinkState:
    return RefundLinkState(
        transaction_link_id=row.transaction_link_id,
        source_transaction_id=row.source_transaction_id,
        source_allocation_id=row.source_allocation_id,
        target_transaction_id=row.target_transaction_id,
        link_kind=row.link_kind,
        amount_cents=row.amount_cents,
        target_allocation_id=row.target_allocation_id,
        status=row.status,  # type: ignore[arg-type]
        version=row.version,
        superseded_by_transaction_link_id=row.superseded_by_transaction_link_id,
    )


def _active_refund_allocations(session: Session, transaction_id: str) -> list[Allocation]:
    return list(
        session.scalars(
            select(Allocation)
            .where(
                Allocation.transaction_id == transaction_id,
                Allocation.flow_kind == "refund",
                Allocation.amount_cents > 0,
                Allocation.superseded_by_allocation_id.is_(None),
                Allocation.retracted_at_utc.is_(None),
            )
            .order_by(Allocation.allocation_id)
        )
    )


def _active_expense_allocations(session: Session, transaction_id: str) -> list[Allocation]:
    return list(
        session.scalars(
            select(Allocation)
            .where(
                Allocation.transaction_id == transaction_id,
                Allocation.flow_kind == "expense",
                Allocation.amount_cents < 0,
                Allocation.superseded_by_allocation_id.is_(None),
                Allocation.retracted_at_utc.is_(None),
            )
            .order_by(Allocation.category_id, Allocation.allocation_id)
        )
    )


def _load_pair(
    session: Session, source_transaction_id: str, target_transaction_id: str
) -> tuple[Transaction, Transaction, list[Allocation], list[Allocation]]:
    source = session.get(Transaction, source_transaction_id)
    target = session.get(Transaction, target_transaction_id)
    if source is None or target is None:
        raise _fail("TRANSACTION_NOT_FOUND", 404, "Transaction source ou achat introuvable.")
    if source.transaction_id == target.transaction_id:
        raise _fail(
            "REFUND_LINK_INVALID", 422, "Un remboursement ne peut pas cibler sa transaction source."
        )
    if source.amount_cents <= 0 or target.amount_cents >= 0:
        raise _fail(
            "REFUND_LINK_DIRECTION_INVALID",
            422,
            "Un lien refund_of doit relier un crédit à un débit.",
        )
    if source.account_id != target.account_id:
        raise _fail(
            "REFUND_SCOPE_MISMATCH",
            422,
            "L'avoir et l'achat doivent appartenir au même compte pour être appariés.",
        )
    source_allocations = _active_refund_allocations(session, source_transaction_id)
    if not source_allocations:
        raise _fail(
            "REFUND_SOURCE_ALLOCATION_REQUIRED",
            422,
            "Le crédit doit porter une allocation refund confirmée.",
        )
    target_allocations = _active_expense_allocations(session, target_transaction_id)
    if not target_allocations:
        raise _fail(
            "REFUND_TARGET_ALLOCATION_REQUIRED",
            422,
            "L'achat doit porter au moins une allocation expense confirmée.",
        )
    return source, target, source_allocations, target_allocations


def _select_source_allocation(
    source_allocations: Sequence[Allocation], source_allocation_id: str | None
) -> Allocation:
    if source_allocation_id is not None:
        for allocation in source_allocations:
            if allocation.allocation_id == source_allocation_id:
                return allocation
        raise _fail(
            "REFUND_SOURCE_ALLOCATION_INVALID",
            422,
            "source_allocation_id ne désigne pas une allocation refund active du crédit.",
        )
    if len(source_allocations) == 1:
        return source_allocations[0]
    raise _fail(
        "REFUND_SOURCE_ALLOCATION_REQUIRED",
        422,
        "Choisissez l'allocation refund source du crédit.",
    )


def _validate_target_allocation(
    target_allocation_id: str | None, target_allocations: Sequence[Allocation]
) -> Allocation | None:
    if target_allocation_id is None:
        return None
    for allocation in target_allocations:
        if allocation.allocation_id == target_allocation_id:
            return allocation
    raise _fail(
        "REFUND_TARGET_ALLOCATION_INVALID",
        422,
        "target_allocation_id doit désigner une allocation expense active de l'achat.",
    )


def _confirmed_link_sum(
    session: Session,
    *,
    field: str,
    value: str,
    exclude_link_id: str | None = None,
) -> int:
    column = (
        TransactionLink.source_transaction_id
        if field == "source"
        else TransactionLink.target_transaction_id
        if field == "target"
        else TransactionLink.target_allocation_id
        if field == "target_allocation"
        else TransactionLink.source_allocation_id
    )
    statement = select(func.coalesce(func.sum(TransactionLink.amount_cents), 0)).where(
        column == value,
        TransactionLink.link_kind == "refund_of",
        TransactionLink.status == "confirmed",
        TransactionLink.superseded_by_transaction_link_id.is_(None),
    )
    if exclude_link_id is not None:
        statement = statement.where(TransactionLink.transaction_link_id != exclude_link_id)
    return int(session.scalar(statement) or 0)


def _validate_capacities(
    session: Session,
    *,
    source_allocation: Allocation,
    target_transaction_id: str,
    target_allocations: Sequence[Allocation],
    target_allocation: Allocation | None,
    amount_cents: int,
    exclude_link_id: str | None = None,
) -> None:
    source_used = _confirmed_link_sum(
        session,
        field="source_allocation",
        value=source_allocation.allocation_id,
        exclude_link_id=exclude_link_id,
    )
    if source_used + amount_cents > source_allocation.amount_cents:
        raise _fail(
            "REFUND_SOURCE_CAP_EXCEEDED",
            409,
            "Les liens confirmés dépasseraient le montant de l'allocation refund du crédit.",
        )

    target_used = _confirmed_link_sum(
        session,
        field="target",
        value=target_transaction_id,
        exclude_link_id=exclude_link_id,
    )
    target_capacity = sum(-allocation.amount_cents for allocation in target_allocations)
    if target_used + amount_cents > target_capacity:
        raise _fail(
            "REFUND_TARGET_CAP_EXCEEDED",
            409,
            "Les liens confirmés dépasseraient les allocations expense de l'achat.",
        )
    if target_allocation is not None:
        allocation_used = _confirmed_link_sum(
            session,
            field="allocation",
            value=target_allocation.allocation_id,
            exclude_link_id=exclude_link_id,
        )
        if allocation_used + amount_cents > -target_allocation.amount_cents:
            raise _fail(
                "REFUND_TARGET_ALLOCATION_CAP_EXCEEDED",
                409,
                "Les liens dépasseraient le montant de l'allocation expense ciblée.",
            )


def _link_json(
    *,
    transaction_link_id: str,
    source_transaction_id: str,
    source_allocation_id: str,
    target_transaction_id: str,
    amount_cents: int,
    target_allocation_id: str | None,
    status: str,
) -> dict[str, str | int | None]:
    return {
        "transaction_link_id": transaction_link_id,
        "source_transaction_id": source_transaction_id,
        "source_allocation_id": source_allocation_id,
        "target_transaction_id": target_transaction_id,
        "link_kind": "refund_of",
        "amount_cents": amount_cents,
        "target_allocation_id": target_allocation_id,
        "status": status,
    }


def _add_audit(
    session: Session,
    *,
    audit_event_id: str,
    target_id: str,
    action: str,
    actor: str,
    before: dict[str, str | int | None] | None,
    after: dict[str, str | int | None] | None,
) -> None:
    session.add(
        AuditEvent(
            audit_event_id=audit_event_id,
            target_type="transaction_link",
            target_id=target_id,
            action=action,
            actor=actor,
            before_json=None
            if before is None
            else json.dumps(before, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            after_json=None
            if after is None
            else json.dumps(after, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    )


def create_refund_link(
    engine: Engine,
    source_transaction_id: str,
    target_transaction_id: str,
    amount_cents: int,
    *,
    actor: str,
    target_allocation_id: str | None = None,
    source_allocation_id: str | None = None,
    expected_source_version: int | None = None,
    expected_target_version: int | None = None,
) -> RefundLinkState:
    """Create a proposed link; call :func:`confirm_refund_link` to activate it."""
    actor_value = _actor_value(actor)
    amount = _strict_int(amount_cents, "amount_cents")
    if amount <= 0:
        raise _fail("REFUND_LINK_INVALID", 422, "Le montant du lien doit être positif.")
    link_id = str(uuid.uuid4())
    audit_id = str(uuid.uuid4())
    with write_transaction(engine) as session:
        source, target, source_allocations, target_allocations = _load_pair(
            session, source_transaction_id, target_transaction_id
        )
        if expected_source_version is not None and source.version != _strict_int(
            expected_source_version, "expected_source_version"
        ):
            raise VersionConflictError()
        if expected_target_version is not None and target.version != _strict_int(
            expected_target_version, "expected_target_version"
        ):
            raise VersionConflictError()
        source_allocation = _select_source_allocation(source_allocations, source_allocation_id)
        target_allocation = _validate_target_allocation(target_allocation_id, target_allocations)
        _validate_capacities(
            session,
            source_allocation=source_allocation,
            target_transaction_id=target_transaction_id,
            target_allocations=target_allocations,
            target_allocation=target_allocation,
            amount_cents=amount,
        )
        existing = session.scalar(
            select(TransactionLink).where(
                TransactionLink.source_transaction_id == source_transaction_id,
                TransactionLink.target_transaction_id == target_transaction_id,
                TransactionLink.link_kind == "refund_of",
                TransactionLink.source_allocation_id == source_allocation.allocation_id,
                TransactionLink.target_allocation_id == target_allocation_id,
                TransactionLink.superseded_by_transaction_link_id.is_(None),
                TransactionLink.status.in_(("proposed", "confirmed")),
            )
        )
        if existing is not None:
            if existing.amount_cents == amount and existing.status == "proposed":
                link_id = existing.transaction_link_id
            else:
                raise _fail(
                    "REFUND_LINK_ALREADY_EXISTS",
                    409,
                    "Un lien actif existe déjà pour cette source et cette cible.",
                )
        else:
            after = _link_json(
                transaction_link_id=link_id,
                source_transaction_id=source_transaction_id,
                source_allocation_id=source_allocation.allocation_id,
                target_transaction_id=target_transaction_id,
                amount_cents=amount,
                target_allocation_id=target_allocation_id,
                status="proposed",
            )
            _add_audit(
                session,
                audit_event_id=audit_id,
                target_id=link_id,
                action="refund_link.proposed",
                actor=actor_value,
                before=None,
                after=after,
            )
            session.add(
                TransactionLink(
                    transaction_link_id=link_id,
                    source_transaction_id=source_transaction_id,
                    source_allocation_id=source_allocation.allocation_id,
                    target_transaction_id=target_transaction_id,
                    link_kind="refund_of",
                    amount_cents=amount,
                    target_allocation_id=target_allocation_id,
                    status="proposed",
                )
            )
            session.flush()
    return get_refund_link_state(engine, link_id)


def confirm_refund_link(
    engine: Engine,
    transaction_link_id: str,
    *,
    actor: str,
    expected_version: int,
) -> RefundLinkState:
    """Confirm a proposed link with locked cap validation and an audit event."""
    actor_value = _actor_value(actor)
    expected = _strict_int(expected_version, "expected_version")
    audit_id = str(uuid.uuid4())
    with write_transaction(engine) as session:
        link = session.get(TransactionLink, transaction_link_id)
        if link is None:
            raise _fail("REFUND_LINK_NOT_FOUND", 404, "Lien de remboursement introuvable.")
        if link.version != expected:
            raise VersionConflictError()
        if link.superseded_by_transaction_link_id is not None:
            raise _fail("REFUND_LINK_SUPERSEDED", 409, "Ce lien a déjà été remplacé.")
        if link.status == "confirmed":
            pass
        elif link.status == "rejected":
            raise _fail("REFUND_LINK_REJECTED", 409, "Un lien rejeté ne peut pas être confirmé.")
        else:
            source, _target, source_allocations, target_allocations = _load_pair(
                session, link.source_transaction_id, link.target_transaction_id
            )
            del source
            source_allocation = _select_source_allocation(
                source_allocations, link.source_allocation_id
            )
            target_allocation = _validate_target_allocation(
                link.target_allocation_id, target_allocations
            )
            _validate_capacities(
                session,
                source_allocation=source_allocation,
                target_transaction_id=link.target_transaction_id,
                target_allocations=target_allocations,
                target_allocation=target_allocation,
                amount_cents=link.amount_cents,
                exclude_link_id=link.transaction_link_id,
            )
            before = _link_json(
                transaction_link_id=link.transaction_link_id,
                source_transaction_id=link.source_transaction_id,
                source_allocation_id=source_allocation.allocation_id,
                target_transaction_id=link.target_transaction_id,
                amount_cents=link.amount_cents,
                target_allocation_id=link.target_allocation_id,
                status=link.status,
            )
            after = {**before, "status": "confirmed"}
            _add_audit(
                session,
                audit_event_id=audit_id,
                target_id=link.transaction_link_id,
                action="refund_link.confirmed",
                actor=actor_value,
                before=before,
                after=after,
            )
            link.status = "confirmed"
            link.version += 1
            session.flush()
    return get_refund_link_state(engine, transaction_link_id)


def reject_refund_link(
    engine: Engine,
    transaction_link_id: str,
    *,
    actor: str,
    expected_version: int,
) -> RefundLinkState:
    """Reject a proposed link; a confirmed link is immutable except replacement."""
    actor_value = _actor_value(actor)
    expected = _strict_int(expected_version, "expected_version")
    audit_id = str(uuid.uuid4())
    with write_transaction(engine) as session:
        link = session.get(TransactionLink, transaction_link_id)
        if link is None:
            raise _fail("REFUND_LINK_NOT_FOUND", 404, "Lien de remboursement introuvable.")
        if link.version != expected:
            raise VersionConflictError()
        if link.superseded_by_transaction_link_id is not None:
            raise _fail("REFUND_LINK_SUPERSEDED", 409, "Ce lien a déjà été remplacé.")
        if link.status == "confirmed":
            raise _fail(
                "REFUND_LINK_CONFIRMED_PROTECTED",
                409,
                "Un lien confirmé ne peut pas être rejeté en place; remplacez-le par supersession.",
            )
        if link.status == "rejected":
            pass
        else:
            before = _link_json(
                transaction_link_id=link.transaction_link_id,
                source_transaction_id=link.source_transaction_id,
                source_allocation_id=link.source_allocation_id or "",
                target_transaction_id=link.target_transaction_id,
                amount_cents=link.amount_cents,
                target_allocation_id=link.target_allocation_id,
                status=link.status,
            )
            after = {**before, "status": "rejected"}
            _add_audit(
                session,
                audit_event_id=audit_id,
                target_id=link.transaction_link_id,
                action="refund_link.rejected",
                actor=actor_value,
                before=before,
                after=after,
            )
            link.status = "rejected"
            link.version += 1
            session.flush()
    return get_refund_link_state(engine, transaction_link_id)


def replace_refund_link(
    engine: Engine,
    transaction_link_id: str,
    *,
    target_transaction_id: str,
    amount_cents: int,
    actor: str,
    expected_version: int,
    target_allocation_id: str | None = None,
    source_allocation_id: str | None = None,
) -> RefundLinkState:
    """Supersede a confirmed link with a new confirmed version and before/after audit."""
    actor_value = _actor_value(actor)
    amount = _strict_int(amount_cents, "amount_cents")
    if amount <= 0:
        raise _fail("REFUND_LINK_INVALID", 422, "Le montant du lien doit être positif.")
    expected = _strict_int(expected_version, "expected_version")
    replacement_id = str(uuid.uuid4())
    audit_id = str(uuid.uuid4())
    with write_transaction(engine) as session:
        old = session.get(TransactionLink, transaction_link_id)
        if old is None:
            raise _fail("REFUND_LINK_NOT_FOUND", 404, "Lien de remboursement introuvable.")
        if old.version != expected:
            raise VersionConflictError()
        if old.status != "confirmed" or old.superseded_by_transaction_link_id is not None:
            raise _fail(
                "REFUND_LINK_NOT_ACTIVE_CONFIRMED",
                409,
                "Seul un lien confirmé actif peut être remplacé.",
            )
        if (
            old.target_transaction_id == target_transaction_id
            and old.amount_cents == amount
            and old.target_allocation_id == target_allocation_id
            and (source_allocation_id is None or source_allocation_id == old.source_allocation_id)
        ):
            return get_refund_link_state(engine, old.transaction_link_id)

        _source, _target, source_allocations, target_allocations = _load_pair(
            session, old.source_transaction_id, target_transaction_id
        )
        selected_source_allocation_id = source_allocation_id or old.source_allocation_id
        source_allocation = _select_source_allocation(
            source_allocations, selected_source_allocation_id
        )
        target_allocation = _validate_target_allocation(target_allocation_id, target_allocations)
        _validate_capacities(
            session,
            source_allocation=source_allocation,
            target_transaction_id=target_transaction_id,
            target_allocations=target_allocations,
            target_allocation=target_allocation,
            amount_cents=amount,
            exclude_link_id=old.transaction_link_id,
        )

        before = _link_json(
            transaction_link_id=old.transaction_link_id,
            source_transaction_id=old.source_transaction_id,
            source_allocation_id=old.source_allocation_id or "",
            target_transaction_id=old.target_transaction_id,
            amount_cents=old.amount_cents,
            target_allocation_id=old.target_allocation_id,
            status=old.status,
        )
        after = _link_json(
            transaction_link_id=replacement_id,
            source_transaction_id=old.source_transaction_id,
            source_allocation_id=source_allocation.allocation_id,
            target_transaction_id=target_transaction_id,
            amount_cents=amount,
            target_allocation_id=target_allocation_id,
            status="confirmed",
        )
        _add_audit(
            session,
            audit_event_id=audit_id,
            target_id=old.transaction_link_id,
            action="refund_link.superseded",
            actor=actor_value,
            before=before,
            after=after,
        )
        session.flush()

        old.superseded_by_transaction_link_id = replacement_id
        old.superseded_by_audit_event_id = audit_id
        old.version += 1
        # The old confirmed row must be inactive before the replacement is inserted;
        # deferred foreign keys validate replacement_id and audit_id at commit.
        session.flush()

        session.add(
            TransactionLink(
                transaction_link_id=replacement_id,
                source_transaction_id=old.source_transaction_id,
                source_allocation_id=source_allocation.allocation_id,
                target_transaction_id=target_transaction_id,
                link_kind="refund_of",
                amount_cents=amount,
                target_allocation_id=target_allocation_id,
                status="confirmed",
            )
        )
        session.flush()
    return get_refund_link_state(engine, replacement_id)


def get_refund_link_state(engine: Engine, transaction_link_id: str) -> RefundLinkState:
    with session_scope(engine) as session:
        row = session.get(TransactionLink, transaction_link_id)
        if row is None:
            raise _fail("REFUND_LINK_NOT_FOUND", 404, "Lien de remboursement introuvable.")
        return _link_state(row)


def net_effect_by_category(
    engine: Engine,
    purchase_transaction_id: str,
    *,
    account_ids: Collection[str] | None = None,
) -> RefundCategoryEffect:
    """Return purchase expense net of confirmed in-scope refunds by category.

    Expenses become positive consumption cents. Proposed/rejected/superseded
    links never count. When ``account_ids`` is supplied, both legs must be in
    that explicit scope; otherwise an out-of-scope refund is reported separately.
    """
    selected_accounts = None if account_ids is None else set(account_ids)
    with session_scope(engine) as session:
        purchase = session.get(Transaction, purchase_transaction_id)
        if purchase is None:
            raise _fail("TRANSACTION_NOT_FOUND", 404, "Achat introuvable.")
        expenses = _active_expense_allocations(session, purchase_transaction_id)
        result: dict[str, int] = {}
        for allocation in expenses:
            result[allocation.category_id] = (
                result.get(allocation.category_id, 0) - allocation.amount_cents
            )
        links = list(
            session.scalars(
                select(TransactionLink)
                .where(
                    TransactionLink.target_transaction_id == purchase_transaction_id,
                    TransactionLink.link_kind == "refund_of",
                    TransactionLink.status == "confirmed",
                    TransactionLink.superseded_by_transaction_link_id.is_(None),
                )
                .order_by(TransactionLink.transaction_link_id)
            )
        )
        matched = ignored_out_of_scope = 0
        for link in links:
            source = session.get(Transaction, link.source_transaction_id)
            if source is None:
                raise _fail("REFUND_LINK_INVALID", 409, "Le crédit lié est introuvable.")
            if source.account_id != purchase.account_id:
                ignored_out_of_scope += link.amount_cents
                continue
            if selected_accounts is not None and (
                source.account_id not in selected_accounts
                or purchase.account_id not in selected_accounts
            ):
                ignored_out_of_scope += link.amount_cents
                continue
            if link.target_allocation_id is not None:
                target = next(
                    (item for item in expenses if item.allocation_id == link.target_allocation_id),
                    None,
                )
                if target is None:
                    raise _fail(
                        "REFUND_TARGET_ALLOCATION_INVALID",
                        409,
                        "Le lien confirmé cible une allocation expense qui n'est plus active.",
                    )
                attribution = {target.category_id: link.amount_cents}
            else:
                try:
                    attribution = prorate_link(
                        link.amount_cents,
                        [(item.category_id, item.amount_cents) for item in expenses],
                    )
                except MoneySplitError as error:
                    raise _fail(
                        "REFUND_LINK_INVALID",
                        409,
                        "Les allocations expense ne permettent plus l'imputation du lien.",
                    ) from error
            for category_id, amount in attribution.items():
                result[category_id] = result.get(category_id, 0) - amount
            matched += link.amount_cents

        return RefundCategoryEffect(
            by_category_cents={key: result[key] for key in sorted(result)},
            matched_refunds_cents=matched,
            ignored_out_of_scope_refunds_cents=ignored_out_of_scope,
        )
