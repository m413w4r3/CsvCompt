from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.db.models import AuditEvent, Category, CategoryFlowKind
from app.db.session import create_sqlite_engine, session_scope
from app.domain.taxonomy import (
    Taxonomy,
    TaxonomyCompatibilityError,
    load_taxonomy,
)
from app.domain.types import FlowKind
from app.services.categories import sync_taxonomy


@pytest.mark.integration
def test_RULE_00_sync_is_idempotent_audited_and_matches_all_domain_pairs(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'taxonomy.sqlite'}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    taxonomy = load_taxonomy()

    first = sync_taxonomy(engine, taxonomy)
    second = sync_taxonomy(engine, taxonomy)
    assert first["inserted"] == len(taxonomy.categories)
    assert second["inserted"] == second["updated"] == second["deactivated"] == 0
    with session_scope(engine) as session:
        category_ids = set(session.scalars(select(Category.category_id)).all())
        pairs = {
            (category_id, flow_kind)
            for category_id, flow_kind in session.execute(
                select(CategoryFlowKind.category_id, CategoryFlowKind.flow_kind)
            )
        }
        audit_count = session.scalar(select(func.count()).select_from(AuditEvent))
    expected_pairs = {
        (category.category_id, flow.value)
        for category in taxonomy.categories
        for flow in category.allowed_flow_kinds
    }
    assert category_ids == set(taxonomy.by_id)
    assert pairs == expected_pairs
    assert len(category_ids) == 87
    assert len(pairs) == 88
    assert audit_count == len(taxonomy.categories)
    engine.dispose()


@pytest.mark.integration
def test_RULE_00_sync_deactivates_controlled_ids_and_never_deletes(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'taxonomy.sqlite'}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    taxonomy = load_taxonomy()
    sync_taxonomy(engine, taxonomy)
    categories = tuple(
        replace(category, is_active=False)
        if category.category_id == "groceries_organic"
        else category
        for category in taxonomy.categories
    )
    disabled = Taxonomy(taxonomy.version + 1, taxonomy.sha256, categories, taxonomy.dimensions)
    result = sync_taxonomy(engine, disabled)
    assert result["deactivated"] == 1
    with session_scope(engine) as session:
        category = session.get(Category, "groceries_organic")
        assert category is not None and category.is_active == 0
        assert session.scalar(select(func.count()).select_from(Category)) == len(
            taxonomy.categories
        )
    engine.dispose()


@pytest.mark.integration
def test_RULE_00_label_changes_are_allowed_and_audited(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'taxonomy.sqlite'}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    taxonomy = load_taxonomy()
    sync_taxonomy(engine, taxonomy)
    changed = Taxonomy(
        taxonomy.version + 1,
        taxonomy.sha256,
        tuple(
            replace(category, label="Libellé synthétique modifié")
            if category.category_id == "groceries_organic"
            else category
            for category in taxonomy.categories
        ),
        taxonomy.dimensions,
    )
    result = sync_taxonomy(engine, changed)
    assert result["updated"] == 87
    with session_scope(engine) as session:
        category = session.get(Category, "groceries_organic")
        assert category is not None and category.label == "Libellé synthétique modifié"
        assert category.taxonomy_version == 2
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.target_id == "groceries_organic",
                AuditEvent.action == "taxonomy_category_updated",
            )
        )
        assert event is not None
    engine.dispose()


@pytest.mark.integration
def test_RULE_00_flow_pair_changes_are_refused_without_adr(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'taxonomy.sqlite'}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    taxonomy = load_taxonomy()
    sync_taxonomy(engine, taxonomy)
    changed = Taxonomy(
        taxonomy.version + 1,
        taxonomy.sha256,
        tuple(
            replace(category, allowed_flow_kinds=(FlowKind.INCOME_OTHER,))
            if category.category_id == "groceries_organic"
            else category
            for category in taxonomy.categories
        ),
        taxonomy.dimensions,
    )
    with pytest.raises(TaxonomyCompatibilityError, match="Modification des flux admissibles"):
        sync_taxonomy(engine, changed)
    engine.dispose()


@pytest.mark.integration
def test_RULE_00_removed_or_renamed_ids_are_refused_without_mutation(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'taxonomy.sqlite'}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    taxonomy = load_taxonomy()
    sync_taxonomy(engine, taxonomy)
    removed = replace(
        taxonomy,
        categories=tuple(c for c in taxonomy.categories if c.category_id != "groceries_organic"),
    )
    with pytest.raises(TaxonomyCompatibilityError, match="Suppression ou renommage"):
        sync_taxonomy(engine, removed)
    renamed = replace(
        taxonomy,
        categories=tuple(
            replace(c, category_id="groceries_bio") if c.category_id == "groceries_organic" else c
            for c in taxonomy.categories
        ),
    )
    with pytest.raises(TaxonomyCompatibilityError, match="Suppression ou renommage"):
        sync_taxonomy(engine, renamed)
    engine.dispose()


@pytest.mark.integration
def test_RULE_00_unmigrated_database_refuses_taxonomy_writes(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'not-migrated.sqlite'}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    engine = create_sqlite_engine(settings.database_url)
    from app.core.errors import AppError

    with pytest.raises(AppError, match="La base de données n'est pas prête") as error:
        sync_taxonomy(engine, load_taxonomy())
    assert error.value.code == "DATABASE_NOT_READY"
    assert not (tmp_path / "not-migrated.sqlite").exists()
    engine.dispose()


@pytest.mark.integration
def test_RULE_00_added_id_is_accepted_when_domain_matrix_is_extended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.domain.taxonomy as taxonomy_module

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'taxonomy.sqlite'}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    taxonomy = load_taxonomy()
    original_allowed_flow_kinds = taxonomy_module.allowed_flow_kinds
    monkeypatch.setattr(
        taxonomy_module,
        "allowed_flow_kinds",
        lambda category_id: (
            frozenset({FlowKind.EXPENSE})
            if category_id == "test_new_category"
            else original_allowed_flow_kinds(category_id)
        ),
    )
    # L'ID nouveau est d'abord ajouté à la matrice domaine du scénario contrôlé.
    sync_taxonomy(engine, taxonomy)
    source_path = Path(__file__).resolve().parents[3] / "config" / "categories.v1.yaml"
    document = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    document["expense_categories"][0]["children"].append(
        {"id": "test_new_category", "label": "Catégorie synthétique"}
    )
    copy = tmp_path / "added.yaml"
    copy.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    added = load_taxonomy(copy)
    result = sync_taxonomy(engine, added)
    assert result["inserted"] == 1
    with session_scope(engine) as session:
        assert session.get(Category, "test_new_category") is not None
        assert session.get(CategoryFlowKind, ("test_new_category", "expense")) is not None
    engine.dispose()
