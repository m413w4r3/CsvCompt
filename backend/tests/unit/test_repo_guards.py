"""Garde-fous synthétiques du dépôt (CI-01, MONEY-02, SEC-06)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = "backend/tests/fixtures/"
FORBIDDEN_SUFFIXES = (".db", ".zip", ".xlsx", ".xls", ".ods", ".ofx", ".qif", ".qfx")
CODE_PATTERNS = (
    ("insert_or_replace", re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.I | re.S)),
    ("replace_into", re.compile(r"\bREPLACE\s+INTO\b", re.I | re.S)),
    (
        "prefix_with_or_replace",
        re.compile(r"\.prefix_with\s*\(\s*['\"]\s*OR\s+REPLACE", re.I | re.S),
    ),
    ("sqlalchemy_upsert", re.compile(r"\.on_conflict_do_update\s*\(", re.I)),
    ("float_call", re.compile(r"\bfloat\s*\(")),
    ("float_annotation", re.compile(r"(?::|->)\s*float\b")),
    # Tout /100 dans le code métier est interdit : la conversion est un risque de float.
    ("division_by_100", re.compile(r"(?<!/)\/\s*100\b")),
)
IBAN_PATTERN = re.compile(r"\b[A-Z]{2}[0-9]{2}(?:[ ]?[A-Z0-9]){11,30}\b", re.I)
CARD_CANDIDATE_PATTERN = re.compile(r"(?<![A-Z0-9])(?:[0-9][ -]?){13,19}(?![0-9])", re.I)


def _private_path_reason(path: str) -> str | None:
    normalized = path.replace("\\", "/").lower()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    base = normalized.rsplit("/", 1)[-1]
    if "csv_references/" in f"{normalized}/":
        return "csv_references"
    if normalized.startswith(f"{FIXTURES}private/"):
        return "private_fixture"
    if base.endswith(".csv.txt"):
        return "csv_text"
    if base.endswith(".csv") and not normalized.startswith(FIXTURES):
        return "csv_outside_fixtures"
    if base.endswith(FORBIDDEN_SUFFIXES):
        return "private_extension"
    if re.search(r"\.sqlite(?:3)?(?:[-.].*|[0-9]+)?$", base):
        return "sqlite"
    if base == ".env" or base.startswith(".env.") and base != ".env.example":
        return "dotenv"
    return None


def _code_violations(source: str) -> list[str]:
    return [name for name, pattern in CODE_PATTERNS if pattern.search(source)]


def _luhn_valid(candidate: str) -> bool:
    digits = [int(value) for value in candidate if value.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _iban_valid(candidate: str) -> bool:
    compact = re.sub(r"\s", "", candidate).upper()
    if not 15 <= len(compact) <= 34:
        return False
    rotated = compact[4:] + compact[:4]
    remainder = 0
    for value in rotated:
        digits = str(ord(value) - 55) if value.isalpha() else value
        for digit in digits:
            remainder = (remainder * 10 + int(digit)) % 97
    return remainder == 1


def _content_violations(path: str, source: str) -> list[str]:
    violations = (
        ["iban"]
        if any(_iban_valid(match.group()) for match in IBAN_PATTERN.finditer(source))
        else []
    )
    if path.lower().endswith(".py") and any(
        _luhn_valid(candidate.group()) for candidate in CARD_CANDIDATE_PATTERN.finditer(source)
    ):
        violations.append("payment_card")
    return violations


def _script_policy_violations(path: str, source: str) -> list[str]:
    normalized = path.replace("\\", "/")
    if not (normalized.startswith("scripts/") or normalized.startswith(".github/workflows/")):
        return []
    # Ce script ne fait qu'appliquer la politique explicite de test réel opt-in.
    if normalized in {
        "scripts/real_csv_profile.py",
        "scripts/tests_real_csv_profile_selftest.py",
        "scripts/check_no_private_data.sh",
    }:
        return []
    markers = (
        ("real_data_env", "COMPTES" + "_REAL_CSV"),
        ("private_data_path", r"\bdata/"),
        ("private_csv_reference", "csv_" + "references/"),
    )
    violations = []
    for name, marker in markers:
        found = re.search(marker, source) if name == "private_data_path" else marker in source
        if found:
            violations.append(name)
    return violations


def _tracked_paths() -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True)
    return [item.decode() for item in result.stdout.split(b"\0") if item]


def _text_for_path(relative: str) -> str | None:
    # Ne jamais ouvrir les répertoires de données privés, même si un fichier y est suivi.
    normalized = relative.replace("\\", "/").lower()
    if "csv_references/" in f"{normalized}/" or normalized.startswith("data/"):
        return None
    path = ROOT / relative
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def test_CI_01_aucune_donnee_interdite_suivie_par_git() -> None:
    try:
        paths = _tracked_paths()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("Le contrôle des chemins suivis requiert un index Git.")
    forbidden = [(path, reason) for path in paths if (reason := _private_path_reason(path))]
    assert not forbidden, f"Chemins suivis interdits : {forbidden}"


def test_SEC_06_code_applicatif_sans_replace_float_ou_division_centimes() -> None:
    violations: list[str] = []
    for source in (ROOT / "backend/app").rglob("*.py"):
        for rule in _code_violations(source.read_text(encoding="utf-8")):
            violations.append(f"{source.relative_to(ROOT)}:{rule}")
    assert not violations, f"Motifs dangereux dans backend/app : {violations}"


def test_CI_01_default_suites_excluent_les_donnees_reelles() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    backend_script = (ROOT / "scripts/test_backend.sh").read_text(encoding="utf-8")
    integration_script = (ROOT / "scripts/test_integration.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "not opt_in_real_data" in backend_script
    assert "not opt_in_real_data" in integration_script
    assert "test-backend" in makefile and "test-integration" in makefile
    assert "COMPTES" + "_REAL_CSV" not in makefile + backend_script + integration_script + workflow


def test_CI_01_script_et_workflow_sans_reference_aux_donnees_privees() -> None:
    candidates = [*ROOT.glob("scripts/*.sh"), *ROOT.glob(".github/workflows/*.yml")]
    violations = []
    for path in candidates:
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        violations.extend(
            f"{relative}:{rule}" for rule in _script_policy_violations(relative, source)
        )
    assert not violations, f"Références privées dans scripts/CI : {violations}"


def test_CI_01_texte_suivi_sans_iban_ni_numero_de_carte() -> None:
    try:
        paths = _tracked_paths()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("Le contrôle des contenus suivis requiert un index Git.")
    violations: list[str] = []
    for relative in paths:
        source = _text_for_path(relative)
        if source is None:
            continue
        violations.extend(f"{relative}:{rule}" for rule in _content_violations(relative, source))
    assert not violations, f"IBAN/carte détecté(s) dans les contenus suivis : {violations}"


def test_CI_01_heuristiques_hygiene_sur_sondes_synthetiques_tmp(tmp_path: Path) -> None:
    probe = tmp_path / "repo"
    probe.mkdir()

    assert _private_path_reason("backend/tests/fixtures/synthetic.csv") is None
    forbidden_paths = (
        "comptes.db",
        "export.zip",
        "releve.xlsx",
        "releve.xls",
        "releve.ods",
        "releve.ofx",
        "releve.qif",
        "releve.qfx",
        "releve.csv.txt",
        "backend/tests/fixtures/private/synthetic.csv",
        "csv_references/synthetic.csv",
        ".env.local",
    )
    for path in forbidden_paths:
        assert _private_path_reason(path) is not None, path

    code_cases = (
        ('insert(t).prefix_with("OR REPLACE")', "prefix_with_or_replace"),
        ("insert(t)\n    .on_conflict_do_update(index_elements=[t.id])", "sqlalchemy_upsert"),
        ("def amount(value) -> float: ...", "float_annotation"),
        ("amount: float = 1", "float_annotation"),
        ("c / 100", "division_by_100"),
    )
    for index, (contents, expected_rule) in enumerate(code_cases):
        file = probe / f"probe_{index}.py"
        file.write_text(contents, encoding="utf-8")
        assert expected_rule in _code_violations(file.read_text(encoding="utf-8")), contents
    assert not _code_violations("def cents(value: int) -> int: return value // 100")

    bban = "A" * 18
    rotated = bban + "DE00"
    remainder = 0
    for value in rotated:
        digits = str(ord(value) - 55) if value.isalpha() else value
        for digit in digits:
            remainder = (remainder * 10 + int(digit)) % 97
    check_digits = f"{98 - remainder:02d}"
    synthetic_iban = "DE" + check_digits + bban
    assert _content_violations("probe.txt", f"holder={synthetic_iban}") == ["iban"]
    assert not _content_violations("probe.txt", "holder=synthetic-only")
    card_digits = "4" + "1" * 15
    card_file = probe / "card_probe.py"
    card_file.write_text(f"CARD = {card_digits!r}\n", encoding="utf-8")
    assert _content_violations("card_probe.py", card_file.read_text(encoding="utf-8")) == [
        "payment_card"
    ]
    invalid_card_digits = card_digits[:-1] + ("0" if card_digits[-1] != "0" else "1")
    assert not _content_violations("safe.py", f"CARD = {invalid_card_digits!r}")

    real_env_marker = "COMPTES" + "_REAL_CSV"
    private_ref_marker = "csv_" + "references/statement.csv"
    bad_script = probe / "test_e2e_smoke.sh"
    script_cases = (
        (f"echo {real_env_marker}", "real_data_env"),
        ("cat data/statement.csv", "private_data_path"),
        (f"cat {private_ref_marker}", "private_csv_reference"),
    )
    for contents, expected_rule in script_cases:
        bad_script.write_text(contents, encoding="utf-8")
        assert expected_rule in _script_policy_violations(
            "scripts/test_e2e_smoke.sh", bad_script.read_text()
        )
    assert not _script_policy_violations("scripts/test_e2e_smoke.sh", "echo synthetic fixtures")
