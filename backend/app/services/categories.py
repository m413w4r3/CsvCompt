"""Synchronisation idempotente de la taxonomie avec SQLite."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.db.migrate import database_state
from app.db.models import AuditEvent, Category, CategoryFlowKind
from app.db.session import write_transaction
from app.domain.taxonomy import (
    Taxonomy,
    validate_taxonomy_evolution,
)


def require_database_ready(engine: Engine) -> None:
    settings = Settings(database_url=str(engine.url), _env_file=None)
    state, revision = database_state(settings)
    if state != "ok":
        raise AppError(
            "DATABASE_NOT_READY",
            503,
            "La base de données n'est pas prête.",
            {"storage_status": state, "revision": revision},
        )


def _dump(data: dict[str, Any] | None) -> str | None:
    return None if data is None else json.dumps(data, ensure_ascii=False, sort_keys=True)


def sync_taxonomy(engine: Engine, taxonomy: Taxonomy) -> dict[str, int | str]:
    """Synchronise sans suppression, avec validation préalable et audit append-only."""
    require_database_ready(engine)
    with write_transaction(engine) as session:
        return _sync_in_session(session, taxonomy)


def _sync_in_session(session: Session, taxonomy: Taxonomy) -> dict[str, int | str]:
    rows = session.scalars(select(Category).order_by(Category.category_id)).all()
    current = {
        row.category_id: {
            "parent_category_id": row.parent_category_id,
            "label": row.label,
            "taxonomy_version": row.taxonomy_version,
            "is_active": bool(row.is_active),
            "sort_order": row.sort_order,
        }
        for row in rows
    }
    pairs = {
        (str(category_id), str(flow_kind))
        for category_id, flow_kind in session.execute(
            select(CategoryFlowKind.category_id, CategoryFlowKind.flow_kind)
        )
    }
    validate_taxonomy_evolution(taxonomy, current, pairs)

    inserted = updated = deactivated = 0
    for spec in taxonomy.categories:
        before = current.get(spec.category_id)
        before_audit = (
            None
            if before is None
            else {
                **before,
                "allowed_flow_kinds": sorted(
                    flow for category_id, flow in pairs if category_id == spec.category_id
                ),
            }
        )
        after_fields = {
            "parent_category_id": spec.parent_category_id,
            "label": spec.label,
            "taxonomy_version": taxonomy.version,
            "is_active": spec.is_active,
            "sort_order": spec.sort_order,
        }
        after_audit = {
            **after_fields,
            "allowed_flow_kinds": sorted(flow.value for flow in spec.allowed_flow_kinds),
        }
        if before is None:
            session.add(
                Category(
                    category_id=spec.category_id,
                    **after_fields,
                )
            )
            inserted += 1
            action = "taxonomy_category_created"
        elif before != after_fields:
            category = session.get(Category, spec.category_id)
            assert category is not None
            if category.is_active and not spec.is_active:
                deactivated += 1
            category.parent_category_id = spec.parent_category_id
            category.label = spec.label
            category.taxonomy_version = taxonomy.version
            category.is_active = int(spec.is_active)
            category.sort_order = spec.sort_order
            updated += 1
            action = "taxonomy_category_updated"
        else:
            action = ""
        if action:
            session.add(
                AuditEvent(
                    target_type="category",
                    target_id=spec.category_id,
                    action=action,
                    actor="taxonomy_sync",
                    before_json=_dump(before_audit),
                    after_json=_dump(after_audit),
                )
            )
        for flow_kind in spec.allowed_flow_kinds:
            pair = (spec.category_id, flow_kind.value)
            if pair not in pairs:
                session.add(CategoryFlowKind(category_id=pair[0], flow_kind=pair[1]))
                pairs.add(pair)

    # La spécification v1 ne retire pas les IDs; `active: false` les désactive.
    # Les paires historiques sont conservées et aucune DELETE n'est émise.
    return {
        "taxonomy_version": taxonomy.version,
        "taxonomy_sha256": taxonomy.sha256,
        "inserted": inserted,
        "updated": updated,
        "deactivated": deactivated,
    }
