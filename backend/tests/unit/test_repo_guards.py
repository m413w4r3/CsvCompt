"""Garde-fous de dépôt pour la confidentialité et les invariants de code (CI-01, SEC-06)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_CI_01_aucune_donnee_interdite_suivie_par_git() -> None:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True)
    paths = [item.decode() for item in result.stdout.split(b"\0") if item]
    forbidden: list[str] = []
    for path in paths:
        lower = path.lower()
        base = Path(lower).name
        forbidden_csv = lower.endswith(".csv") and not lower.startswith("backend/tests/fixtures/")
        if "csv_references/" in f"{lower}/" or forbidden_csv:
            forbidden.append(path)
        if re.search(r"\.sqlite(?:3)?(?:[-.].*|[0-9]+)?$", base):
            forbidden.append(path)
        if base == ".env" or base.startswith(".env.") and base != ".env.example":
            forbidden.append(path)
    assert not forbidden, f"Chemins interdits suivis par Git : {forbidden}"


def test_SEC_06_code_applicatif_sans_insert_or_replace() -> None:
    violations: list[str] = []
    for source in (ROOT / "backend/app").rglob("*.py"):
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bINSERT\s+OR\s+REPLACE\b", line, re.IGNORECASE):
                violations.append(f"{source.relative_to(ROOT)}:{number}")
    assert not violations, f"INSERT OR REPLACE interdit : {violations}"


def test_CI_01_tests_reels_exclus_des_commandes_par_defaut() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    backend_script = (ROOT / "scripts/test_backend.sh").read_text(encoding="utf-8")
    integration_script = (ROOT / "scripts/test_integration.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "not opt_in_real_data" in backend_script
    assert "not opt_in_real_data" in integration_script
    assert "test-backend" in makefile and "test-integration" in makefile
    assert "COMPTES_REAL_CSV" not in makefile + backend_script + integration_script + workflow


def test_MONEY_02_aucun_float_dans_le_code_metier() -> None:
    # Liste d'exceptions documentée et vide : aucun fichier n'est exempté.
    exceptions: set[str] = set()
    violations: list[str] = []
    for source in (ROOT / "backend/app").rglob("*.py"):
        relative = source.relative_to(ROOT).as_posix()
        if relative in exceptions:
            continue
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bfloat\s*\(", line) or re.search(r"\:\s*float\b", line):
                violations.append(f"{relative}:{number}")
    assert not violations, f"float interdit pour les montants : {violations}"
