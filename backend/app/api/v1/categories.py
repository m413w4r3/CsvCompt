"""Lecture de l'arbre de catégories et de ses compatibilités de flux."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.core.errors import AppError
from app.db.models import Category, CategoryFlowKind
from app.db.session import DbSession
from app.domain.taxonomy import load_taxonomy
from app.services.categories import require_database_ready

router = APIRouter()


class CategoryResponse(BaseModel):
    category_id: str
    parent_category_id: str | None
    label: str
    taxonomy_version: int
    allowed_flow_kinds: list[str]
    is_active: bool
    children: list[CategoryResponse]


class CategoriesResponse(BaseModel):
    items: list[CategoryResponse]
    taxonomy_version: int
    taxonomy_sha256: str
    dimensions: dict[str, list[str]]


@router.get("/categories", response_model=CategoriesResponse)
def get_categories(
    request: Request,
    session: DbSession,
    taxonomy_version: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    engine = request.app.state.db_engine
    require_database_ready(engine)
    statement = select(Category).order_by(Category.sort_order, Category.category_id)
    if taxonomy_version is not None:
        statement = statement.where(Category.taxonomy_version == taxonomy_version)
    categories = session.scalars(statement).all()
    ids = [category.category_id for category in categories]
    pairs = (
        session.execute(
            select(CategoryFlowKind.category_id, CategoryFlowKind.flow_kind).where(
                CategoryFlowKind.category_id.in_(ids)
            )
        ).all()
        if ids
        else []
    )
    flows_by_id: dict[str, list[str]] = {}
    for category_id, flow_kind in pairs:
        flows_by_id.setdefault(category_id, []).append(flow_kind)
    for values in flows_by_id.values():
        values.sort()

    by_id: dict[str, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []
    for category in categories:
        node = {
            "category_id": category.category_id,
            "parent_category_id": category.parent_category_id,
            "label": category.label,
            "taxonomy_version": category.taxonomy_version,
            "allowed_flow_kinds": flows_by_id.get(category.category_id, []),
            "is_active": bool(category.is_active),
            "children": [],
        }
        by_id[category.category_id] = node
        if category.parent_category_id is None or category.parent_category_id not in by_id:
            roots.append(node)
        else:
            by_id[category.parent_category_id]["children"].append(node)

    taxonomy = load_taxonomy()
    return {
        "items": roots,
        "taxonomy_version": taxonomy.version,
        "taxonomy_sha256": taxonomy.sha256,
        "dimensions": {name: list(values) for name, values in taxonomy.dimensions},
    }


@router.get("/categories/{category_id}", response_model=CategoryResponse)
def get_category(category_id: str, request: Request, session: DbSession) -> dict[str, Any]:
    require_database_ready(request.app.state.db_engine)
    category = session.get(Category, category_id)
    if category is None:
        raise AppError("NOT_FOUND", 404, "La catégorie demandée n'existe pas.")
    flows = session.scalars(
        select(CategoryFlowKind.flow_kind)
        .where(CategoryFlowKind.category_id == category_id)
        .order_by(CategoryFlowKind.flow_kind)
    ).all()
    children = session.scalars(
        select(Category)
        .where(Category.parent_category_id == category_id)
        .order_by(Category.sort_order, Category.category_id)
    ).all()
    return {
        "category_id": category.category_id,
        "parent_category_id": category.parent_category_id,
        "label": category.label,
        "taxonomy_version": category.taxonomy_version,
        "allowed_flow_kinds": list(flows),
        "is_active": bool(category.is_active),
        "children": [
            {
                "category_id": child.category_id,
                "parent_category_id": child.parent_category_id,
                "label": child.label,
                "taxonomy_version": child.taxonomy_version,
                "allowed_flow_kinds": [],
                "is_active": bool(child.is_active),
                "children": [],
            }
            for child in children
        ],
    }
