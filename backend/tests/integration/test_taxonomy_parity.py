from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.db.models import Category, CategoryFlowKind
from app.db.session import create_sqlite_engine, session_scope
from app.domain.flow_kinds import FLOW_TO_CATEGORIES
from app.domain.taxonomy import load_taxonomy
from app.services.categories import sync_taxonomy


@pytest.mark.integration
def test_RULE_00_migrated_database_pairs_equal_yaml_code_and_domain_model(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'parity.sqlite'}",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    taxonomy = load_taxonomy()
    sync_taxonomy(engine, taxonomy)
    with session_scope(engine) as session:
        db_ids = {row[0] for row in session.execute(select(Category.category_id))}
        db_pairs = {
            (category_id, flow_kind)
            for category_id, flow_kind in session.execute(
                select(CategoryFlowKind.category_id, CategoryFlowKind.flow_kind)
            )
        }
    yaml_ids = {category.category_id for category in taxonomy.categories}
    code_pairs = {
        (category, flow.value)
        for flow, categories in FLOW_TO_CATEGORIES.items()
        for category in categories
    }
    assert db_ids == yaml_ids == set().union(*FLOW_TO_CATEGORIES.values())
    assert db_pairs == code_pairs
    assert len(db_ids) == 87
    engine.dispose()
