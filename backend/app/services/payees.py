"""Payee storage with exact matching, audited aliases and reversible merges."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classification.matcher import PayeeKey, derive_payee_key, key_metadata
from app.db.models import AuditEvent, Payee, PayeeAlias, Transaction
from app.importers.normalize import LabelParts, identity_label, parse_ca_label, search_label

AliasScope = Literal["exact", "with_context"]

_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", re.IGNORECASE)
_LONG_DIGITS_RE = re.compile(r"(?<!\d)(?:\d[\s-]?){7,}\d(?!\d)")
_REFERENCE_WORD_RE = re.compile(r"\b(?:IBAN|MANDAT|RUM|ICS|SEPA|REFERENCE|REF)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PayeeDetails:
    """Read model with flags decoded from the persisted exact key."""

    payee_id: str
    display_name: str
    normalized_name: str
    is_active: bool
    ambiguous: bool
    is_intermediary: bool


@dataclass(frozen=True, slots=True)
class PayeeMergePreview:
    """Human-review snapshot of every transaction and alias a merge will move."""

    source_payee_id: str
    target_payee_id: str
    transaction_ids: tuple[str, ...]
    alias_ids: tuple[str, ...]
    transaction_count: int
    snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class AmbiguousAliasMatch:
    """Search-label collision to present for audit; never a merge instruction."""

    search_key: str
    payee_ids: tuple[str, ...]
    alias_ids: tuple[str, ...]


def get_payee(session: Session, payee_id: str) -> Payee | None:
    """Read one payee without changing its state."""

    return session.get(Payee, payee_id)


def list_payees(session: Session, *, active_only: bool = True) -> list[PayeeDetails]:
    """List payees with ambiguity/intermediary flags decoded from their keys."""

    statement = select(Payee).order_by(Payee.display_name, Payee.payee_id)
    if active_only:
        statement = statement.where(Payee.is_active == 1)
    return [payee_details(payee) for payee in session.scalars(statement)]


def payee_details(payee: Payee) -> PayeeDetails:
    """Expose flags encoded in the v1 key without requiring schema columns."""

    metadata = key_metadata(payee.normalized_name) or {}
    entity_kind = metadata.get("entity_kind")
    return PayeeDetails(
        payee_id=payee.payee_id,
        display_name=payee.display_name,
        normalized_name=payee.normalized_name,
        is_active=payee.is_active == 1,
        ambiguous=entity_kind == "marketplace",
        is_intermediary=entity_kind == "intermediary",
    )


def create_payee(
    session: Session,
    label_parts: LabelParts,
    *,
    actor: str,
) -> Payee | None:
    """Explicitly create the payee represented by safe exact parser signals."""

    key = derive_payee_key(label_parts)
    if key is None:
        return None
    existing = session.scalar(select(Payee).where(Payee.normalized_name == key.normalized_name))
    if existing is not None:
        return existing if existing.is_active == 1 else None
    _require_actor(actor)
    payee = Payee(display_name=key.merchant_name, normalized_name=key.normalized_name)
    session.add(payee)
    session.flush()
    _add_audit(
        session,
        target_type="payee",
        target_id=payee.payee_id,
        action="payee_created_exact",
        actor=actor,
        after={
            "normalized_name": key.normalized_name,
            "ambiguous": key.ambiguous,
            "is_intermediary": key.is_intermediary,
        },
    )
    return payee


def create_payee_for_transaction(
    session: Session,
    transaction: Transaction,
    *,
    actor: str,
) -> Payee | None:
    """Create/attach an exact key explicitly; unsupported labels stay unresolved."""

    key = derive_payee_key(parse_ca_label(transaction.raw_label))
    if key is None:
        return None
    if transaction.payee_id is not None:
        current = session.get(Payee, transaction.payee_id)
        if current is not None and current.is_active == 1:
            return current if current.normalized_name == key.normalized_name else None
        return None

    existing = session.scalar(select(Payee).where(Payee.normalized_name == key.normalized_name))
    matched_alias: PayeeAlias | None = None
    if existing is None:
        alias_match = _payee_from_exact_alias(session, key, transaction)
        if alias_match is not None:
            existing, matched_alias = alias_match
    if existing is None:
        existing = create_payee(session, parse_ca_label(transaction.raw_label), actor=actor)
    if existing is None or existing.is_active != 1:
        return None
    _attach_payee(
        session,
        transaction,
        existing,
        key,
        actor,
        alias_id=matched_alias.payee_alias_id if matched_alias is not None else None,
    )
    return existing


def ensure_payee_for_transaction(
    session: Session,
    transaction: Transaction,
) -> Payee | None:
    """Attach only an exact canonical key or an explicitly approved exact alias.

    No payee is created here. A missing/nearby key remains unresolved; neither
    `search_label` equality nor a fuzzy merchant candidate is considered.
    """

    key = derive_payee_key(parse_ca_label(transaction.raw_label))
    if key is None:
        return None
    payee = session.scalar(
        select(Payee).where(
            Payee.normalized_name == key.normalized_name,
            Payee.is_active == 1,
        )
    )
    matched_alias: PayeeAlias | None = None
    if payee is None:
        alias_match = _payee_from_exact_alias(session, key, transaction)
        if alias_match is not None:
            payee, matched_alias = alias_match
    if payee is None:
        return None
    if transaction.payee_id is not None:
        return payee if transaction.payee_id == payee.payee_id else None
    _attach_payee(
        session,
        transaction,
        payee,
        key,
        actor="system:exact-key",
        alias_id=matched_alias.payee_alias_id if matched_alias is not None else None,
    )
    return payee


def add_payee_alias(
    session: Session,
    payee_id: str,
    alias_text: str,
    *,
    scope: AliasScope,
    evidence_transaction_ids: list[str] | tuple[str, ...],
    actor: str,
    context: dict[str, object] | None = None,
) -> PayeeAlias:
    """Add an exact alias with auditable transaction proof and explicit scope."""

    _require_actor(actor)
    payee = session.get(Payee, payee_id)
    if payee is None or payee.is_active != 1:
        raise ValueError("payee actif introuvable")
    safe_alias = _validate_alias(alias_text)
    context_payload = _validate_context(scope, context)
    proof_ids = tuple(dict.fromkeys(evidence_transaction_ids))
    if not proof_ids:
        raise ValueError("une preuve de transaction est requise pour un alias")
    proof_rows = list(
        session.scalars(
            select(Transaction).where(
                Transaction.transaction_id.in_(proof_ids),
                Transaction.payee_id == payee_id,
            )
        )
    )
    if len(proof_rows) != len(proof_ids):
        raise ValueError("les transactions de preuve doivent appartenir au payee cible")

    normalized_alias = _alias_key(safe_alias, scope, context_payload)
    canonical_collision = next(
        (
            candidate
            for candidate in session.scalars(select(Payee))
            if candidate.payee_id != payee_id
            and (
                candidate.display_name == safe_alias
                or (key_metadata(candidate.normalized_name) or {}).get("merchant_name")
                == safe_alias
            )
        ),
        None,
    )
    if canonical_collision is not None:
        raise ValueError("cet alias exact correspond déjà à un autre payee")
    existing = session.scalar(
        select(PayeeAlias).where(PayeeAlias.normalized_alias == normalized_alias)
    )
    if existing is not None:
        if existing.payee_id == payee_id:
            return existing
        raise ValueError("cet alias exact appartient déjà à un autre payee")

    alias = PayeeAlias(
        payee_id=payee_id,
        alias_text=safe_alias,
        normalized_alias=normalized_alias,
        # Both scopes match an exact normalized string; `with_context` adds
        # guards in the serialized key, never a pattern matcher.
        match_kind="exact",
        approved_by=actor,
    )
    session.add(alias)
    session.flush()
    _add_audit(
        session,
        target_type="payee_alias",
        target_id=alias.payee_alias_id,
        action="payee_alias_added",
        actor=actor,
        after={
            "alias_id": alias.payee_alias_id,
            "payee_id": payee_id,
            "alias_text": safe_alias,
            "scope": scope,
            "context": context_payload,
            "evidence_transaction_ids": list(proof_ids),
        },
    )
    return alias


def remove_payee_alias(session: Session, alias_id: str, *, actor: str) -> None:
    """Remove an alias while retaining its complete prior state in immutable audit."""

    _require_actor(actor)
    alias = session.get(PayeeAlias, alias_id)
    if alias is None:
        raise ValueError("alias introuvable")
    before = {
        "alias_id": alias.payee_alias_id,
        "payee_id": alias.payee_id,
        "alias_text": alias.alias_text,
        "normalized_alias": alias.normalized_alias,
        "match_kind": alias.match_kind,
        "approved_by": alias.approved_by,
        "version": alias.version,
    }
    _add_audit(
        session,
        target_type="payee_alias",
        target_id=alias_id,
        action="payee_alias_removed",
        actor=actor,
        before=before,
    )
    session.delete(alias)


def find_ambiguous_aliases(session: Session) -> list[AmbiguousAliasMatch]:
    """Return search-label collisions across payees for human audit only."""

    groups: dict[str, list[PayeeAlias]] = defaultdict(list)
    aliases = session.scalars(
        select(PayeeAlias)
        .join(Payee, Payee.payee_id == PayeeAlias.payee_id)
        .where(Payee.is_active == 1)
        .order_by(PayeeAlias.payee_id, PayeeAlias.payee_alias_id)
    )
    for alias in aliases:
        search_key = str(search_label(alias.alias_text))
        if search_key:
            groups[search_key].append(alias)
    return [
        AmbiguousAliasMatch(
            search_key=search_key,
            payee_ids=tuple(sorted({alias.payee_id for alias in rows})),
            alias_ids=tuple(sorted(alias.payee_alias_id for alias in rows)),
        )
        for search_key, rows in sorted(groups.items())
        if len({alias.payee_id for alias in rows}) > 1
    ]


def preview_payee_merge(
    session: Session,
    source_payee_id: str,
    target_payee_id: str,
) -> PayeeMergePreview:
    """Preview every transaction and alias that an explicit merge would move."""

    _validate_merge_pair(session, source_payee_id, target_payee_id)
    transaction_ids = tuple(
        session.scalars(
            select(Transaction.transaction_id)
            .where(Transaction.payee_id == source_payee_id)
            .order_by(Transaction.transaction_id)
        )
    )
    alias_ids = tuple(
        session.scalars(
            select(PayeeAlias.payee_alias_id)
            .where(PayeeAlias.payee_id == source_payee_id)
            .order_by(PayeeAlias.payee_alias_id)
        )
    )
    digest = _merge_digest(session, source_payee_id, target_payee_id, transaction_ids, alias_ids)
    return PayeeMergePreview(
        source_payee_id=source_payee_id,
        target_payee_id=target_payee_id,
        transaction_ids=transaction_ids,
        alias_ids=alias_ids,
        transaction_count=len(transaction_ids),
        snapshot_sha256=digest,
    )


def merge_payees(
    session: Session,
    source_payee_id: str,
    target_payee_id: str,
    *,
    preview: PayeeMergePreview,
    actor: str,
    confirm: bool,
    confirm_intermediary_store: bool = False,
) -> AuditEvent:
    """Perform a previewed, human-confirmed and fully auditable payee merge."""

    _require_actor(actor)
    if not confirm:
        raise ValueError("la fusion exige une confirmation explicite")
    current_preview = preview_payee_merge(session, source_payee_id, target_payee_id)
    if (
        preview.source_payee_id != source_payee_id
        or preview.target_payee_id != target_payee_id
        or preview != current_preview
    ):
        raise ValueError("l'aperçu de fusion est obsolète")
    source = session.get(Payee, source_payee_id)
    target = session.get(Payee, target_payee_id)
    assert source is not None and target is not None
    if _is_intermediary_store_pair(source, target) and not confirm_intermediary_store:
        raise ValueError("fusion intermédiaire/magasin exige une confirmation explicite")

    collisions = set(
        session.scalars(
            select(PayeeAlias.normalized_alias).where(PayeeAlias.payee_id == target_payee_id)
        )
    ) & set(
        session.scalars(
            select(PayeeAlias.normalized_alias).where(PayeeAlias.payee_id == source_payee_id)
        )
    )
    if collisions:
        raise ValueError("la fusion créerait des alias en conflit")

    transactions = list(
        session.scalars(
            select(Transaction)
            .where(Transaction.payee_id == source_payee_id)
            .order_by(Transaction.transaction_id)
        )
    )
    aliases = list(
        session.scalars(
            select(PayeeAlias)
            .where(PayeeAlias.payee_id == source_payee_id)
            .order_by(PayeeAlias.payee_alias_id)
        )
    )
    before = {
        "source_payee": {
            "payee_id": source.payee_id,
            "display_name": source.display_name,
            "normalized_name": source.normalized_name,
            "is_active": source.is_active,
        },
        "target_payee_id": target_payee_id,
        "transaction_links": [
            {"transaction_id": transaction.transaction_id, "payee_id": transaction.payee_id}
            for transaction in transactions
        ],
        "aliases": [
            {
                "payee_alias_id": alias.payee_alias_id,
                "payee_id": alias.payee_id,
            }
            for alias in aliases
        ],
    }
    for transaction in transactions:
        transaction.payee_id = target_payee_id
    for alias in aliases:
        alias.payee_id = target_payee_id
    source.is_active = 0
    event = _add_audit(
        session,
        target_type="payee_merge",
        target_id=source_payee_id,
        action="payee_merged",
        actor=actor,
        before=before,
        after={
            "source_payee_id": source_payee_id,
            "target_payee_id": target_payee_id,
            "transaction_ids": list(preview.transaction_ids),
            "transaction_count": preview.transaction_count,
            "alias_ids": list(preview.alias_ids),
            "preview_sha256": preview.snapshot_sha256,
            "intermediary_store_confirmed": confirm_intermediary_store,
        },
    )
    session.flush()
    return event


def unmerge_payees(session: Session, merge_audit_event_id: str, *, actor: str) -> AuditEvent:
    """Restore all pre-merge payee links; audit history itself remains append-only."""

    _require_actor(actor)
    merge = session.get(AuditEvent, merge_audit_event_id)
    if (
        merge is None
        or merge.target_type != "payee_merge"
        or merge.action != "payee_merged"
        or merge.before_json is None
    ):
        raise ValueError("événement de fusion introuvable ou invalide")
    already_undone = session.scalar(
        select(AuditEvent.audit_event_id).where(
            AuditEvent.target_type == "payee_merge",
            AuditEvent.action == "payee_unmerged",
            AuditEvent.after_json.like(f'%"merge_audit_event_id":"{merge_audit_event_id}"%'),
        )
    )
    if already_undone is not None:
        raise ValueError("cette fusion a déjà été annulée")
    try:
        snapshot = json.loads(merge.before_json)
        source = snapshot["source_payee"]
        target_payee_id = snapshot["target_payee_id"]
        transaction_links = snapshot["transaction_links"]
        aliases = snapshot["aliases"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("la sauvegarde de fusion est invalide") from error

    source_payee = session.get(Payee, source["payee_id"])
    target_payee = session.get(Payee, target_payee_id)
    if (
        source_payee is None
        or target_payee is None
        or source_payee.is_active != 0
        or target_payee.is_active != 1
    ):
        raise ValueError("l'état des payees a changé depuis la fusion")
    for link in transaction_links:
        transaction = session.get(Transaction, link["transaction_id"])
        if transaction is None or transaction.payee_id != target_payee_id:
            raise ValueError("une transaction a changé depuis la fusion; annulation refusée")
    for item in aliases:
        alias = session.get(PayeeAlias, item["payee_alias_id"])
        if alias is None or alias.payee_id != target_payee_id:
            raise ValueError("un alias a changé depuis la fusion; annulation refusée")

    for link in transaction_links:
        transaction = session.get(Transaction, link["transaction_id"])
        assert transaction is not None
        transaction.payee_id = link["payee_id"]
    for item in aliases:
        alias = session.get(PayeeAlias, item["payee_alias_id"])
        assert alias is not None
        alias.payee_id = item["payee_id"]
    source_payee.is_active = source["is_active"]
    event = _add_audit(
        session,
        target_type="payee_merge",
        target_id=source_payee.payee_id,
        action="payee_unmerged",
        actor=actor,
        before={"merge_audit_event_id": merge_audit_event_id},
        after={
            "merge_audit_event_id": merge_audit_event_id,
            "restored_transaction_ids": [link["transaction_id"] for link in transaction_links],
            "restored_alias_ids": [item["payee_alias_id"] for item in aliases],
        },
    )
    session.flush()
    return event


def _payee_from_exact_alias(
    session: Session,
    key: PayeeKey,
    transaction: Transaction,
) -> tuple[Payee, PayeeAlias] | None:
    aliases = list(
        session.scalars(
            select(PayeeAlias)
            .where(PayeeAlias.alias_text == key.merchant_name)
            .order_by(PayeeAlias.payee_alias_id)
        )
    )
    matches: list[tuple[Payee, PayeeAlias]] = []
    for alias in aliases:
        try:
            payload = json.loads(alias.normalized_alias)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("merchant_name") != key.merchant_name:
            continue
        scope = payload.get("scope")
        if scope == "exact":
            pass
        elif scope == "with_context":
            context = payload.get("context")
            if not isinstance(context, dict):
                continue
            if context.get("operation_type") != key.operation_type:
                continue
            if context.get("sign") is not None and context.get("sign") != _sign(
                transaction.amount_cents
            ):
                continue
        else:
            continue
        payee = session.get(Payee, alias.payee_id)
        if payee is not None and payee.is_active == 1:
            matches.append((payee, alias))
    payee_ids = {payee.payee_id for payee, _alias in matches}
    return matches[0] if len(payee_ids) == 1 else None


def _alias_key(
    alias_text: str,
    scope: AliasScope,
    context: dict[str, object] | None,
) -> str:
    return json.dumps(
        {"context": context, "merchant_name": alias_text, "scope": scope, "version": 1},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_alias(alias_text: str) -> str:
    if not isinstance(alias_text, str):
        raise ValueError("alias doit être un texte")
    safe_alias = identity_label(alias_text).value
    compact = "".join(
        character for character in safe_alias if not character.isspace() and character != "-"
    )
    if (
        not safe_alias
        or len(safe_alias) > 160
        or any(character.isdigit() for character in safe_alias)
        or _IBAN_RE.search(compact)
        or _LONG_DIGITS_RE.search(safe_alias)
        or _REFERENCE_WORD_RE.search(safe_alias)
    ):
        raise ValueError("alias contient une référence ou un numéro")
    return safe_alias


def _validate_context(
    scope: AliasScope,
    context: dict[str, object] | None,
) -> dict[str, object] | None:
    if scope not in ("exact", "with_context"):
        raise ValueError("portée d'alias invalide")
    if scope == "exact":
        if context:
            raise ValueError("la portée exact ne prend pas de contexte")
        return None
    if not isinstance(context, dict):
        raise ValueError("la portée with_context exige un contexte")
    operation_type = context.get("operation_type")
    sign = context.get("sign")
    if not isinstance(operation_type, str) or operation_type not in {
        "Paiement par carte",
        "Prélèvement",
    }:
        raise ValueError("contexte with_context invalide")
    if sign is not None and (type(sign) is not int or sign not in (-1, 1)):
        raise ValueError("le signe du contexte doit être -1 ou 1")
    return {"operation_type": operation_type, **({"sign": sign} if sign is not None else {})}


def _validate_merge_pair(session: Session, source_id: str, target_id: str) -> None:
    if source_id == target_id:
        raise ValueError("deux payees distincts sont requis")
    source = session.get(Payee, source_id)
    target = session.get(Payee, target_id)
    if source is None or target is None or source.is_active != 1 or target.is_active != 1:
        raise ValueError("les deux payees doivent être actifs")


def _merge_digest(
    session: Session,
    source_id: str,
    target_id: str,
    transaction_ids: tuple[str, ...],
    alias_ids: tuple[str, ...],
) -> str:
    source = session.get(Payee, source_id)
    target = session.get(Payee, target_id)
    assert source is not None and target is not None
    payload = {
        "source": [source.payee_id, source.normalized_name, source.version, source.is_active],
        "target": [target.payee_id, target.normalized_name, target.version, target.is_active],
        "transactions": [
            [row.transaction_id, row.version, row.payee_id]
            for row in session.scalars(
                select(Transaction)
                .where(Transaction.transaction_id.in_(transaction_ids))
                .order_by(Transaction.transaction_id)
            )
        ]
        if transaction_ids
        else [],
        "aliases": [
            [row.payee_alias_id, row.version, row.payee_id, row.normalized_alias]
            for row in session.scalars(
                select(PayeeAlias)
                .where(PayeeAlias.payee_alias_id.in_(alias_ids))
                .order_by(PayeeAlias.payee_alias_id)
            )
        ]
        if alias_ids
        else [],
        "target_alias_ids": list(
            session.scalars(
                select(PayeeAlias.payee_alias_id)
                .where(PayeeAlias.payee_id == target_id)
                .order_by(PayeeAlias.payee_alias_id)
            )
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_intermediary_store_pair(source: Payee, target: Payee) -> bool:
    source_kind = (key_metadata(source.normalized_name) or {}).get("entity_kind")
    target_kind = (key_metadata(target.normalized_name) or {}).get("entity_kind")
    return {source_kind, target_kind} == {"intermediary", "merchant"}


def _attach_payee(
    session: Session,
    transaction: Transaction,
    payee: Payee,
    key: PayeeKey,
    actor: str,
    *,
    alias_id: str | None = None,
) -> None:
    _require_actor(actor)
    previous = transaction.payee_id
    transaction.payee_id = payee.payee_id
    _add_audit(
        session,
        target_type="transaction",
        target_id=transaction.transaction_id,
        action="payee_attached_exact",
        actor=actor,
        before={"payee_id": previous},
        after={
            "payee_id": payee.payee_id,
            "normalized_name": key.normalized_name,
            **({"alias_id": alias_id} if alias_id is not None else {}),
        },
    )


def _add_audit(
    session: Session,
    *,
    target_type: str,
    target_id: str,
    action: str,
    actor: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        target_type=target_type,
        target_id=target_id,
        action=action,
        actor=actor,
        before_json=_json(before),
        after_json=_json(after),
    )
    session.add(event)
    return event


def _json(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sign(amount_cents: int) -> int:
    if type(amount_cents) is not int or amount_cents == 0:
        raise ValueError("signe de transaction invalide")
    return 1 if amount_cents > 0 else -1


def _require_actor(actor: str) -> None:
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("acteur requis pour une mutation")
