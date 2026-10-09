#!/usr/bin/env python3
"""Profileur local opt-in du CSV Crédit Agricole; sortie limitée aux agrégats."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any


HEADER = ("date", "libellé", "débit euros", "crédit euros", "")
BALANCE_RE = re.compile(r"^Solde au\s+(\d{2}/\d{2}/\d{4})\s+(.+?)\s*€?\s*$", re.I)
START_BALANCE_RE = re.compile(r"^Solde (?:initial|de début|précédent)\s*:?\s*(.+?)\s*€?\s*$", re.I)
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
TYPE_LABELS = (
    "Paiement par carte", "Prélèvement", "Virement en votre faveur",
    "Virement émis", "Retrait au distributeur", "Avoir", "Chèque",
    "Frais bancaires", "Cotisation", "Remboursement", "Échéance de prêt",
)
EXPECTED_PATH = Path(__file__).resolve().parents[1] / "data" / "private_expectations.json"


class ProfileError(Exception):
    """Erreur volontairement dépourvue de contenu source."""


def _decode(path: Path) -> tuple[str, str]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise ProfileError from None
    if raw.startswith(b"\xef\xbb\xbf"):
        encoding, codec = "utf-8-bom", "utf-8-sig"
    else:
        try:
            raw.decode("utf-8", errors="strict")
            encoding, codec = "utf-8", "utf-8"
        except UnicodeDecodeError:
            encoding, codec = "cp1252", "cp1252"
    try:
        return raw.decode(codec, errors="strict"), encoding
    except UnicodeDecodeError:
        raise ProfileError from None


def _money(text: str) -> int:
    cleaned = text.replace("\u00a0", "").replace("\u202f", "").replace(" ", "").strip()
    if not re.fullmatch(r"\d+(?:,\d{1,2})?", cleaned):
        raise ProfileError
    euros, sep, cents = cleaned.partition(",")
    return int(euros) * 100 + (int(cents.ljust(2, "0")) if sep else 0)


def _date(text: str) -> date:
    try:
        return datetime.strptime(text, "%d/%m/%Y").date()
    except ValueError:
        raise ProfileError from None


def _kind(label: str) -> str:
    first = label.splitlines()[0].strip() if label.splitlines() else ""
    for known in TYPE_LABELS:
        if first.casefold().startswith(known.casefold()):
            return known
    return "Autre"


def profile(path: Path) -> dict[str, Any]:
    source, encoding = _decode(path)
    reader = csv.reader(source.splitlines(keepends=True), delimiter=";", quotechar='"', strict=True)
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    header_seen = False
    duplicates: Counter[tuple[str, str, int, int, str]] = Counter()
    kinds: Counter[str] = Counter()
    high_amount_count = 0
    try:
        for row in reader:
            if not row or all(not value.strip() for value in row):
                continue
            if len(row) == 1:
                value = row[0].strip().lstrip("\ufeff")
                balance_match = BALANCE_RE.match(value)
                if balance_match:
                    current = {
                        "section": f"section-{len(sections) + 1:02d}",
                        "operation_count": 0, "debit_cents": 0, "credit_cents": 0,
                        "min_date": None, "max_date": None,
                        "header_balance_cents": _money(balance_match.group(2)),
                        "_dates": [], "_start_balance": None,
                    }
                    sections.append(current)
                    header_seen = False
                    continue
                start_match = START_BALANCE_RE.match(value)
                if start_match and current is not None:
                    current["_start_balance"] = _money(start_match.group(1))
                    continue
            normalized = tuple(cell.strip().lstrip("\ufeff").casefold() for cell in row)
            if normalized == HEADER:
                if current is None:
                    current = {
                        "section": f"section-{len(sections) + 1:02d}",
                        "operation_count": 0, "debit_cents": 0, "credit_cents": 0,
                        "min_date": None, "max_date": None,
                        "header_balance_cents": None, "_dates": [], "_start_balance": None,
                    }
                    sections.append(current)
                header_seen = True
                continue
            if len(row) >= 1 and DATE_RE.fullmatch(row[0].strip()):
                if current is None or not header_seen or len(row) != 5:
                    raise ProfileError
                op_date = _date(row[0].strip())
                label = row[1]
                debit = _money(row[2]) if row[2].strip() else 0
                credit = _money(row[3]) if row[3].strip() else 0
                if bool(debit) == bool(credit):
                    raise ProfileError
                current["operation_count"] += 1
                current["debit_cents"] += debit
                current["credit_cents"] += credit
                current["_dates"].append(op_date)
                kinds[_kind(label)] += 1
                if max(debit, credit) >= 1_000_000:
                    high_amount_count += 1
                duplicates[(current["section"], op_date.isoformat(), debit, credit, label)] += 1
    except (csv.Error, ValueError, OverflowError):
        raise ProfileError from None

    if not sections:
        raise ProfileError
    duplicate_multiplicities = [count for count in duplicates.values() if count > 1]
    net = sum(s["credit_cents"] - s["debit_cents"] for s in sections)
    opening = [s["_start_balance"] for s in sections]
    closing = [s["header_balance_cents"] for s in sections]
    if opening and all(value is not None for value in opening) and all(value is not None for value in closing):
        expected = sum(closing) - sum(opening)
        consistency: dict[str, Any] = {
            "status": "cohérent" if net == expected else "écart",
            "net_flow_cents": net, "expected_variation_cents": expected,
        }
    else:
        consistency = {"status": "non applicable — solde de début inconnu", "net_flow_cents": net}
    for item in sections:
        dates = item.pop("_dates")
        item.pop("_start_balance")
        if dates:
            item["min_date"] = min(dates).isoformat()
            item["max_date"] = max(dates).isoformat()
    return {
        "encoding": encoding,
        "sections": sections,
        "duplicate_groups": len(duplicate_multiplicities),
        "duplicate_max_multiplicity": max(duplicate_multiplicities, default=1),
        "operation_types": dict(sorted(kinds.items())),
        "amount_abs_at_least_10000_eur_count": high_amount_count,
        "consistency": consistency,
    }


def _public_expectations(report: dict[str, Any]) -> dict[str, Any]:
    """Garde uniquement des métriques anonymes pour le fichier local d'attentes."""
    return {k: v for k, v in report.items() if k != "encoding"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Profil privé local, sortie limitée aux agrégats.")
    parser.add_argument("csv_path", nargs="?", help="Chemin du CSV; sinon COMPTES_REAL_CSV.")
    parser.add_argument("--skip-if-unset", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--assert-expected", action="store_true")
    parser.add_argument("--write-expected", action="store_true")
    args = parser.parse_args(argv)
    if args.assert_expected and args.write_expected:
        print("Erreur : options d'attentes incompatibles.", file=sys.stderr)
        return 2
    input_path = args.csv_path or os.environ.get("COMPTES_REAL_CSV")
    if not input_path:
        message = "non configuré — test opt-in ignoré"
        print(json.dumps({"status": message}, ensure_ascii=False) if args.as_json else message)
        return 0 if args.skip_if_unset else 2
    try:
        report = profile(Path(input_path))
        anonymized = _public_expectations(report)
        if args.assert_expected:
            try:
                expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise ProfileError from None
            if expected != anonymized:
                print("Échec : agrégats différents des attentes locales.", file=sys.stderr)
                return 1
        if args.write_expected:
            EXPECTED_PATH.parent.mkdir(parents=True, exist_ok=True)
            EXPECTED_PATH.write_text(json.dumps(anonymized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.as_json else _format_text(report))
        return 0
    except ProfileError:
        print("Erreur : fichier illisible ou format CSV non reconnu.", file=sys.stderr)
        return 1


def _format_text(report: dict[str, Any]) -> str:
    lines = [f"Encodage : {report['encoding']}"]
    for section in report["sections"]:
        lines.append(
            f"{section['section']} : {section['operation_count']} opérations ; "
            f"dates {section['min_date'] or '—'} → {section['max_date'] or '—'} ; "
            f"débits {section['debit_cents']} cts ; crédits {section['credit_cents']} cts ; "
            f"solde d'en-tête {section['header_balance_cents']} cts"
        )
    lines.extend([
        f"Groupes strictement identiques : {report['duplicate_groups']} ; multiplicité maximale : {report['duplicate_max_multiplicity']}",
        "Types : " + (", ".join(f"{key}={value}" for key, value in report["operation_types"].items()) or "aucune opération"),
        f"Opérations à |montant| ≥ 10 000 € : {report['amount_abs_at_least_10000_eur_count']}",
        "Cohérence : " + report["consistency"]["status"],
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
