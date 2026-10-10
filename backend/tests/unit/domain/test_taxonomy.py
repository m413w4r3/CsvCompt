from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.taxonomy import TaxonomyFormatError, load_taxonomy

ROOT = Path(__file__).resolve().parents[4]
SOURCE = (
    ROOT / ".nimbalyst" / "plan_implementation_comptes_familiaux" / "config" / "categories.v1.yaml"
)
COPY = ROOT / "config" / "categories.v1.yaml"


@pytest.mark.unit
def test_RULE_00_taxonomy_copy_is_byte_identical_to_authoritative_source() -> None:
    assert COPY.read_bytes() == SOURCE.read_bytes()


@pytest.mark.unit
def test_RULE_00_yaml_has_unique_ids_valid_parents_and_immutable_categories() -> None:
    taxonomy = load_taxonomy(COPY)
    ids = [category.category_id for category in taxonomy.categories]
    assert len(ids) == len(set(ids))
    assert all(
        category.parent_category_id is None or category.parent_category_id in ids
        for category in taxonomy.categories
    )
    with pytest.raises((AttributeError, TypeError)):
        taxonomy.version = 2  # type: ignore[misc]


@pytest.mark.unit
def test_RULE_00_bad_version_and_duplicate_id_have_explicit_taxonomy_errors(tmp_path: Path) -> None:
    malformed = COPY.read_text(encoding="utf-8").replace("version: 1", "version: 1.0", 1)
    target = tmp_path / "taxonomy.yaml"
    target.write_text(malformed, encoding="utf-8")
    with pytest.raises(TaxonomyFormatError, match="version doit être un entier"):
        load_taxonomy(target)

    duplicate = COPY.read_text(encoding="utf-8").replace("id: income_salary_a", "id: groceries", 1)
    target.write_text(duplicate, encoding="utf-8")
    with pytest.raises(TaxonomyFormatError, match="ID de catégorie dupliqué: groceries"):
        load_taxonomy(target)
