"""Chargement, validation et contrôle d'évolution de la taxonomie YAML."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.flow_kinds import allowed_flow_kinds
from app.domain.types import FlowKind


class TaxonomyError(ValueError):
    """Erreur explicite de lecture ou de cohérence de taxonomie."""


class TaxonomyFormatError(TaxonomyError):
    """Le document YAML ne respecte pas le format attendu."""


class TaxonomyCompatibilityError(TaxonomyError):
    """Une évolution supprime ou modifie silencieusement un contrat stable."""


@dataclass(frozen=True, slots=True)
class CategorySpec:
    category_id: str
    label: str
    parent_category_id: str | None
    family: str
    allowed_flow_kinds: tuple[FlowKind, ...]
    is_active: bool = True
    sort_order: int = 0


@dataclass(frozen=True, slots=True)
class Taxonomy:
    version: int
    sha256: str
    categories: tuple[CategorySpec, ...]
    dimensions: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def by_id(self) -> dict[str, CategorySpec]:
        return {category.category_id: category for category in self.categories}


_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TAXONOMY_PATH = _ROOT / "config" / "categories.v1.yaml"
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def load_taxonomy(path: Path | str = DEFAULT_TAXONOMY_PATH) -> Taxonomy:
    """Lit les octets, valide le YAML et renvoie un modèle immuable."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as error:
        raise TaxonomyFormatError("PyYAML doit être installé pour charger la taxonomie") from error
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as error:
        raise TaxonomyFormatError(f"Impossible de lire la taxonomie: {error.strerror}") from error
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise TaxonomyFormatError(f"YAML de taxonomie invalide: {error}") from error
    if not isinstance(document, dict):
        raise TaxonomyFormatError("La racine de la taxonomie doit être un mapping")

    version = document.get("version")
    if type(version) is not int or version < 1:
        raise TaxonomyFormatError("taxonomy.version doit être un entier strict supérieur à zéro")

    categories: list[CategorySpec] = []
    seen: set[str] = set()
    parent_map: dict[str, str | None] = {}
    sort_order = 0

    def visit(items: Any, family: str, parent_id: str | None = None) -> None:
        nonlocal sort_order
        if not isinstance(items, list):
            raise TaxonomyFormatError(f"La famille {family!r} doit être une liste")
        for item in items:
            if not isinstance(item, dict):
                raise TaxonomyFormatError(f"Entrée de catégorie invalide dans {family!r}")
            category_id, label = item.get("id"), item.get("label")
            if not isinstance(category_id, str) or not _ID_PATTERN.fullmatch(category_id):
                raise TaxonomyFormatError(f"ID invalide dans la famille {family!r}")
            if category_id in seen:
                raise TaxonomyFormatError(f"ID de catégorie dupliqué: {category_id}")
            if not isinstance(label, str) or not label.strip():
                raise TaxonomyFormatError(f"Libellé vide ou invalide pour {category_id}")
            active = item.get("active", True)
            if type(active) is not bool:
                raise TaxonomyFormatError(f"active doit être booléen pour {category_id}")
            flows = tuple(sorted(allowed_flow_kinds(category_id), key=lambda flow: flow.value))
            if not flows:
                raise TaxonomyFormatError(
                    f"Aucun flux admissible défini dans le code pour {category_id}"
                )
            seen.add(category_id)
            parent_map[category_id] = parent_id
            categories.append(
                CategorySpec(
                    category_id, label.strip(), parent_id, family, flows, active, sort_order
                )
            )
            sort_order += 1
            children = item.get("children", [])
            if children:
                visit(children, family, category_id)

    visit(document.get("expense_categories"), "expense")
    visit(document.get("income_categories"), "income")
    visit(document.get("non_consumption_flows"), "non_consumption")

    for category_id, parent_id in parent_map.items():
        if parent_id is not None and parent_id not in seen:
            raise TaxonomyFormatError(f"Parent {parent_id!r} absent pour {category_id}")
        ancestors: set[str] = set()
        cursor: str | None = category_id
        while cursor is not None:
            if cursor in ancestors:
                raise TaxonomyFormatError(f"Cycle de parents détecté à {cursor}")
            ancestors.add(cursor)
            cursor = parent_map.get(cursor)

    raw_dimensions = document.get("classification_dimensions")
    if not isinstance(raw_dimensions, dict):
        raise TaxonomyFormatError("classification_dimensions doit être un mapping")
    dimensions: list[tuple[str, tuple[str, ...]]] = []
    for name, values in raw_dimensions.items():
        if not isinstance(name, str) or not isinstance(values, list) or not values:
            raise TaxonomyFormatError("Chaque dimension doit avoir un nom et une liste de valeurs")
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise TaxonomyFormatError(f"Valeur vide ou invalide dans la dimension {name!r}")
        if len(values) != len(set(values)):
            raise TaxonomyFormatError(f"Valeur dupliquée dans la dimension {name!r}")
        dimensions.append((name, tuple(values)))
    expected_dimensions = {
        "beneficiary",
        "cadence",
        "flexibility",
        "acquisition",
        "decision",
        "origin",
    }
    if {name for name, _values in dimensions} != expected_dimensions:
        raise TaxonomyFormatError("Les dimensions transversales ne correspondent pas au contrat v1")

    return Taxonomy(
        version=version,
        sha256=hashlib.sha256(raw).hexdigest(),
        categories=tuple(categories),
        dimensions=tuple(dimensions),
    )


def validate_taxonomy_evolution(
    taxonomy: Taxonomy,
    existing: dict[str, dict[str, Any]],
    existing_pairs: set[tuple[str, str]],
) -> None:
    """Refuse suppression/renommage et retrait de paire sans arbitrage ADR."""
    new_by_id = taxonomy.by_id
    missing = sorted(set(existing) - set(new_by_id))
    if missing:
        raise TaxonomyCompatibilityError(
            "Suppression ou renommage d'ID interdit; conservez les IDs et marquez-les "
            "active: false: " + ", ".join(missing)
        )
    changed: list[str] = []
    for category_id in sorted(set(existing) & set(new_by_id)):
        old_pairs = {flow for cid, flow in existing_pairs if cid == category_id}
        new_pairs = {flow.value for flow in new_by_id[category_id].allowed_flow_kinds}
        if old_pairs != new_pairs:
            changed.append(category_id)
    if changed:
        raise TaxonomyCompatibilityError(
            "Modification des flux admissibles interdite sans ADR pour: " + ", ".join(changed)
        )
