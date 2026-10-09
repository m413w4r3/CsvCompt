#!/usr/bin/env python3
"""Vérifie les fixtures T03 sans dépendance au futur parseur applicatif."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any


HEADER = ["Date", "Libellé", "Débit euros", "Crédit euros", ""]
HOLDER = "MADAME   TEST EXEMPLE OU MONSIEUR DEMO FICTIF"
DATE_PATTERN = re.compile(r"^\d{2}/\d{2}/\d{4}$")
BALANCE_PATTERN = re.compile(r"^Solde au (\d{2}/\d{2}/\d{4}) ([0-9 \u00a0]+,\d{2}) €$")
DOWNLOAD_PATTERN = re.compile(r"^Téléchargement du (\d{2}/\d{2}/\d{4})$")
PERIOD_PATTERN = re.compile(
    r"^Liste des opérations du compte entre le (\d{2}/\d{2}/\d{4}) et le (\d{2}/\d{2}/\d{4})$"
)
EMAIL_PATTERN = re.compile(rb"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
PRIVATE_DIGITS_PATTERN = re.compile(rb"[0-9]{8,}")
IBAN_PATTERN = re.compile(rb"FR76 ?([0-9A-Z]{4} ?){5}[0-9A-Z]{3}", re.IGNORECASE)
OUT_OF_PERIOD_BEHAVIOR = (
    "OUT_OF_PERIOD : ambiguïté bloquante pour sa clé de jour, fichier non commité tant que non résolu"
)
PROVISIONAL_BEHAVIOR = "jour provisoire, mois non complet, recommander export chevauchant"


class FixtureError(Exception):
    pass


def parse_date_safe(value: str) -> date:
    year, month, day = int(value[6:10]), int(value[3:5]), int(value[0:2])
    return date(year, month, day)


def parse_money(value: str) -> int:
    if not re.fullmatch(r"[0-9]+(?:[ \u00a0][0-9]{3})*,[0-9]{2}", value):
        raise ValueError("format monétaire invalide")
    euros, cents = value.replace("\u00a0", " ").replace(" ", "").split(",", 1)
    return int(euros) * 100 + int(cents)


def identity_fingerprint(account: str, booked: date, label: str, signed_cents: int) -> str:
    # LF et CRLF désignent ici le même libellé logique dans les variantes d'encodage.
    canonical_label = label.replace("\r\n", "\n").replace("\r", "\n")
    identity = json.dumps(
        [account, booked.isoformat(), canonical_label, signed_cents],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def line_ending_name(raw: bytes) -> str:
    if b"\r" in raw:
        if raw.replace(b"\r\n", b"").find(b"\n") >= 0:
            return "MIXED"
        if raw.replace(b"\r\n", b"").find(b"\r") >= 0:
            return "MIXED"
        return "CRLF"
    return "LF"


def decode_fixture(raw: bytes, declared: str, name: str) -> tuple[str, list[tuple[int, str]]]:
    anomalies: list[tuple[int, str]] = []
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    expects_bom = declared == "utf-8-bom"
    if has_bom != expects_bom:
        raise FixtureError(f"{name}: BOM inattendu ou manquant")
    if declared == "utf-8-bom":
        return raw.decode("utf-8-sig", errors="strict"), anomalies
    if declared == "utf-8":
        return raw.decode("utf-8", errors="strict"), anomalies
    if declared == "cp1252":
        text = raw.decode("cp1252", errors="strict")
        if raw and "Téléchargement" not in text and "Date;Libellé" not in text:
            raise FixtureError(f"{name}: octets non conformes au CP1252 déclaré")
        return text, anomalies
    if declared == "cp1252-invalid":
        try:
            raw.decode("cp1252", errors="strict")
        except UnicodeDecodeError as error:
            line_number = raw[:error.start].count(b"\n") + 1
            anomalies.append((line_number, "INVALID_ENCODING"))
            return raw.decode("cp1252", errors="replace"), anomalies
        raise FixtureError(f"{name}: l'octet CP1252 attendu invalide est décodable")
    raise FixtureError(f"{name}: encodage inconnu dans le manifeste")


def analyze_csv(text: str, raw: bytes, name: str) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[tuple[int, str]],
    dict[str, dict[str, Any]], dict[str, int],
]:
    sections: list[dict[str, Any]] = []
    groups: dict[str, int] = {}
    golden_operations: dict[str, dict[str, Any]] = {}
    anomalies: list[tuple[int, str]] = []
    if not raw:
        return sections, [], [(0, "EMPTY_FILE")], golden_operations, groups

    parser = csv.reader(io.StringIO(text, newline=""), delimiter=";", quotechar='"', strict=True)
    current: dict[str, Any] | None = None
    current_header = False
    header_count = 0
    expected_header_line: int | None = None
    blank_records: list[tuple[int, int]] = []
    operation_lines: dict[int, list[int]] = {}
    period_expected_lines: dict[int, int] = {}
    provisional_days: set[tuple[int, str]] = set()
    seen_bad_header_line = False
    last_line = 0
    pending_download_date: str | None = None

    def start_section(account: str, account_line: int) -> int:
        sections.append({
            "account": account,
            "operation_count": 0,
            "debit_cents": 0,
            "credit_cents": 0,
            "min_date": None,
            "max_date": None,
            "header_balance_cents": None,
            "download_date": None,
            "period_declared": False,
            "declared_period_start": None,
            "declared_period_end": None,
        })
        operation_lines[len(sections) - 1] = []
        period_expected_lines[len(sections) - 1] = account_line + 3
        return len(sections) - 1

    while True:
        record_start = last_line + 1
        try:
            row = next(parser)
        except StopIteration:
            break
        except csv.Error:
            anomalies.append((record_start, "UNCLOSED_QUOTE"))
            break
        last_line = parser.line_num

        if row == []:
            if current is not None and current_header:
                blank_records.append((len(sections) - 1, record_start))
            continue

        first = row[0] if row else ""
        download_match = DOWNLOAD_PATTERN.fullmatch(first)
        if download_match:
            pending_download_date = parse_date_safe(download_match.group(1)).isoformat()
            continue

        if first.startswith("Compte "):
            if current is not None and not current["period_declared"]:
                previous_index = len(sections) - 1
                anomalies.append((period_expected_lines[previous_index], "MISSING_PERIOD"))
            if any(section["account"] == first for section in sections):
                anomalies.append((record_start, "DUPLICATE_ACCOUNT_SECTION"))
            current = sections[start_section(first, record_start)]
            current["download_date"] = pending_download_date
            pending_download_date = None
            current_header = False
            header_count = 0
            expected_header_line = None
            seen_bad_header_line = False
            continue

        if current is None:
            continue

        balance_match = BALANCE_PATTERN.fullmatch(first)
        if balance_match:
            try:
                current["header_balance_cents"] = parse_money(balance_match.group(2))
            except ValueError:
                pass
            continue

        period_match = PERIOD_PATTERN.fullmatch(first)
        if period_match:
            current["period_declared"] = True
            current["declared_period_start"] = parse_date_safe(period_match.group(1)).isoformat()
            current["declared_period_end"] = parse_date_safe(period_match.group(2)).isoformat()
            expected_header_line = record_start + 2  # une ligne vide sépare le titre de l'en-tête
            continue

        if row == HEADER:
            header_count += 1
            if header_count > 1:
                anomalies.append((record_start, "DUPLICATE_HEADER"))
            current_header = True
            continue

        if DATE_PATTERN.fullmatch(first):
            if not current_header:
                if not seen_bad_header_line:
                    anomalies.append((record_start, "MISSING_HEADER"))
                    seen_bad_header_line = True
                continue
            if len(row) != 5:
                anomalies.append((record_start, "BAD_FIELD_COUNT"))
                continue
            try:
                booked = parse_date_safe(first)
            except ValueError:
                anomalies.append((record_start, "INVALID_DATE"))
                continue

            debit_text, credit_text = row[2], row[3]
            if debit_text and credit_text:
                anomalies.append((record_start, "BOTH_AMOUNTS"))
                continue
            if debit_text.startswith("-") or credit_text.startswith("-"):
                anomalies.append((record_start, "NEGATIVE_AMOUNT"))
                continue
            if not debit_text and not credit_text:
                anomalies.append((record_start, "ZERO_AMOUNT"))
                continue
            try:
                debit = parse_money(debit_text) if debit_text else 0
                credit = parse_money(credit_text) if credit_text else 0
            except ValueError:
                anomalies.append((record_start, "NON_NUMERIC_AMOUNT"))
                continue
            if debit == 0 and credit == 0:
                anomalies.append((record_start, "ZERO_AMOUNT"))
                continue

            index = len(sections) - 1
            current["operation_count"] += 1
            current["debit_cents"] += debit
            current["credit_cents"] += credit
            iso_day = booked.isoformat()
            if current["min_date"] is None or iso_day < current["min_date"]:
                current["min_date"] = iso_day
            if current["max_date"] is None or iso_day > current["max_date"]:
                current["max_date"] = iso_day
            operation_lines[index].append(record_start)
            signed = credit - debit
            groups_key = identity_fingerprint(current["account"], booked, row[1], signed)
            groups[groups_key] = groups.get(groups_key, 0) + 1

            label = row[1]
            kind = label.splitlines()[0] if label.splitlines() else ""
            debit_kinds = {
                "Paiement par carte", "Prélèvement", "Virement émis", "Remboursement de prêt",
                "Règlement", "Cotisation", "Retrait au distributeur",
            }
            credit_kinds = {"Virement en votre faveur", "Avoir"}
            if kind in debit_kinds and not (debit > 0 and credit == 0):
                anomalies.append((record_start, "INCOHERENT_LABEL_DIRECTION"))
            if kind in credit_kinds and not (credit > 0 and debit == 0):
                anomalies.append((record_start, "INCOHERENT_LABEL_DIRECTION"))

            period_start = current["declared_period_start"]
            period_end = current["declared_period_end"]
            if (period_start is not None and iso_day < period_start) or (
                period_end is not None and iso_day > period_end
            ):
                anomalies.append((record_start, "OUT_OF_PERIOD"))
            elif current["download_date"] is not None and iso_day >= current["download_date"]:
                provisional_key = (index, iso_day)
                if provisional_key not in provisional_days:
                    anomalies.append((record_start, "PROVISIONAL_DAY"))
                    provisional_days.add(provisional_key)
            golden_match = re.search(r"(?:^|\n)(S(?:0[1-9]|1[0-2])) ", label)
            if golden_match:
                golden_operations[golden_match.group(1)] = {
                    "date": booked.isoformat(),
                    "signed_cents": signed,
                }
            if "<script>" in label.lower():
                anomalies.append((record_start, "SCRIPT_LABEL"))
            if "ignore les instructions précédentes" in label.lower():
                anomalies.append((record_start, "PROMPT_INJECTION"))
            if len(label) >= 5000:
                anomalies.append((record_start, "LONG_LABEL"))
            if any(ord(char) > 0xFFFF for char in label):
                anomalies.append((record_start, "UNICODE_EXOTIC"))
    if current is not None and not current_header:
        if not seen_bad_header_line:
            anomalies.append((expected_header_line or last_line + 1, "MISSING_HEADER"))
    if current is not None and not current["period_declared"]:
        anomalies.append((period_expected_lines[len(sections) - 1], "MISSING_PERIOD"))

    for section_index, blank_line in blank_records:
        if any(line > blank_line for line in operation_lines.get(section_index, [])):
            anomalies.append((blank_line, "INTERLEAVED_BLANK_LINES"))
            break

    duplicates = [
        {"fingerprint": fingerprint, "count": count}
        for fingerprint, count in sorted(groups.items()) if count > 1
    ]
    return sections, duplicates, anomalies, golden_operations, groups


def check_privacy(raw: bytes, text: str, name: str) -> None:
    if PRIVATE_DIGITS_PATTERN.search(raw):
        raise FixtureError(f"{name}: suite de 8 chiffres consécutifs ou plus")
    if IBAN_PATTERN.search(raw):
        raise FixtureError(f"{name}: motif IBAN FR76 détecté")
    if EMAIL_PATTERN.search(raw):
        raise FixtureError(f"{name}: adresse e-mail détectée")
    if raw and HOLDER not in text.splitlines():
        raise FixtureError(f"{name}: titulaire synthétique attendu absent")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("MADAME", "MONSIEUR")) and line != HOLDER:
            raise FixtureError(f"{name}: titulaire non synthétique")


def normalize_anomalies(values: list[tuple[int, str]]) -> list[tuple[int, str]]:
    return sorted(values)


def verify_one(root: Path, relative_path: str, expected: dict[str, Any]) -> None:
    path = root / relative_path
    if not path.is_file():
        raise FixtureError(f"{relative_path}: fixture manquante")
    raw = path.read_bytes()
    text, encoding_anomalies = decode_fixture(raw, expected["encoding"], relative_path)
    actual_eol = line_ending_name(raw)
    if actual_eol != expected["line_ending"]:
        raise FixtureError(f"{relative_path}: fins de ligne {actual_eol}, attendu {expected['line_ending']}")
    if raw:
        ending = b"\r\n" if expected["line_ending"] == "CRLF" else b"\n"
        if not raw.endswith(ending * 3):
            raise FixtureError(f"{relative_path}: les lignes vides terminales attendues sont absentes")
    check_privacy(raw, text, relative_path)

    sections, duplicate_groups, anomalies, actual_golden, identity_counts = analyze_csv(text, raw, relative_path)
    actual_anomalies = normalize_anomalies(encoding_anomalies + anomalies)
    expected_anomalies = normalize_anomalies([
        (int(item["line_number"]), str(item["code"])) for item in expected["anomalies"]
    ])
    if sections != expected["sections"]:
        raise FixtureError(f"{relative_path}: sections/comptes/sommes/dates/soldes différents du manifeste")
    if duplicate_groups != expected["duplicate_groups"]:
        raise FixtureError(f"{relative_path}: multiplicités différentes du manifeste")
    if "golden" in expected:
        expected_golden = expected["golden"]
        if actual_golden != expected_golden["operations"]:
            raise FixtureError(f"{relative_path}: opérations S01–S12 différentes du manifeste")
        actual_signed = {key: int(value["signed_cents"]) for key, value in actual_golden.items()}
        totals = {
            "regular_income_cents": actual_signed["S01"] + actual_signed["S02"],
            "net_consumption_cents": -sum(actual_signed[key] for key in ("S03", "S04", "S05", "S11")),
            "personal_allocation_cents": -actual_signed["S08"],
            "net_savings_transfer_cents": -(actual_signed["S06"] + actual_signed["S07"]),
            "bank_net_flow_cents": sum(actual_signed.values()),
        }
        if totals != expected_golden["totals"]:
            raise FixtureError(f"{relative_path}: totaux golden différents du manifeste")
    if actual_anomalies != expected_anomalies:
        raise FixtureError(
            f"{relative_path}: anomalies {actual_anomalies!r}, attendu {expected_anomalies!r}"
        )
    if not isinstance(expected["expected_behavior"], str) or not expected["expected_behavior"].strip():
        raise FixtureError(f"{relative_path}: comportement attendu absent")
    if expected["expected_status"] not in {"committed", "previewed", "rejected"}:
        raise FixtureError(f"{relative_path}: statut attendu inconnu")
    if any(not isinstance(item.get("expected_behavior"), str) for item in expected["anomalies"]):
        raise FixtureError(f"{relative_path}: comportement d'anomalie absent")
    anomaly_codes = {item["code"] for item in expected["anomalies"]}
    if "OUT_OF_PERIOD" in anomaly_codes:
        if expected["expected_status"] != "previewed":
            raise FixtureError(f"{relative_path}: une ligne hors période doit empêcher le commit")
        if not any(
            item["code"] == "OUT_OF_PERIOD" and item["expected_behavior"] == OUT_OF_PERIOD_BEHAVIOR
            for item in expected["anomalies"]
        ):
            raise FixtureError(f"{relative_path}: attente OUT_OF_PERIOD doit être bloquante")
    if "MISSING_PERIOD" in anomaly_codes:
        if expected["expected_status"] != "previewed" or expected["expected_behavior"] != "bloquant (ADR-017)":
            raise FixtureError(f"{relative_path}: une section sans plage est bloquante")
    if "DUPLICATE_ACCOUNT_SECTION" in anomaly_codes:
        if expected["expected_status"] != "rejected":
            raise FixtureError(f"{relative_path}: sections associées au même compte doivent être rejetées")

    case = expected.get("case_oracle")
    if case:
        if case.get("test_id") != "IMP-11" or case.get("truth_occurrences") != 2:
            raise FixtureError(f"{relative_path}: oracle IMP-11 invalide")
        actual_visible = identity_counts.get(case["identity_fingerprint"], 0)
        if actual_visible != case["visible_occurrences_in_file"]:
            raise FixtureError(f"{relative_path}: occurrences IMP-11 différentes du manifeste")
        if len(sections) != 1:
            raise FixtureError(f"{relative_path}: IMP-11 attend une section")
        section = sections[0]
        day = case["provisional_date"]
        if case["role"] == "boundary-export":
            if expected["expected_behavior"] != PROVISIONAL_BEHAVIOR:
                raise FixtureError(f"{relative_path}: attente IMP-11 de journée provisoire divergente")
            if section["download_date"] != day or section["declared_period_end"] != day:
                raise FixtureError(f"{relative_path}: la borne provisoire doit égaler téléchargement et fin de période")
            if (11, "PROVISIONAL_DAY") not in actual_anomalies:
                raise FixtureError(f"{relative_path}: journée provisoire non détectée")
        elif case["role"] == "non-overlapping-followup":
            if expected["expected_behavior"] != PROVISIONAL_BEHAVIOR:
                raise FixtureError(f"{relative_path}: attente IMP-11 d'export chevauchant divergente")
            if section["declared_period_start"] <= day or (11, "OUT_OF_PERIOD") not in actual_anomalies:
                raise FixtureError(f"{relative_path}: export suivant doit être non chevauchant et bloqué hors période")
            if expected["expected_status"] != "previewed":
                raise FixtureError(f"{relative_path}: ambiguïté hors période doit bloquer le commit")
        elif case["role"] == "overlapping-followup":
            if not (section["declared_period_start"] <= day <= section["declared_period_end"]):
                raise FixtureError(f"{relative_path}: le suivi doit chevaucher le jour provisoire")
            if (11, "OUT_OF_PERIOD") in actual_anomalies or case.get("reconciled_occurrences") != 2:
                raise FixtureError(f"{relative_path}: le chevauchement doit réconcilier deux occurrences")
        else:
            raise FixtureError(f"{relative_path}: rôle IMP-11 inconnu")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent,
        help="racine de fixtures (utile pour vérifier une copie altérée)",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    manifest_path = root / "manifest.yaml"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"ERREUR : manifest.yaml illisible ({error.__class__.__name__}).", file=sys.stderr)
        return 1

    expected_fixtures = manifest.get("fixtures", {})
    expected_paths = set(expected_fixtures)
    actual_paths = {path.relative_to(root).as_posix() for path in root.rglob("*.csv")}
    if expected_paths != actual_paths:
        print("ERREUR : liste des CSV différente de manifest.yaml.", file=sys.stderr)
        return 1

    for relative_path in sorted(expected_fixtures):
        try:
            verify_one(root, relative_path, expected_fixtures[relative_path])
        except (FixtureError, KeyError, TypeError, ValueError, UnicodeDecodeError, csv.Error) as error:
            print(f"ECHEC : {relative_path}: {error}", file=sys.stderr)
            return 1
    provisional = {
        Path(name).name: item.get("case_oracle")
        for name, item in expected_fixtures.items() if item.get("case_oracle", {}).get("test_id") == "IMP-11"
    }
    try:
        a = provisional["provisional_day_A.csv"]
        b = provisional["provisional_day_B.csv"]
        overlap = provisional["provisional_day_B_overlap.csv"]
        if not (a["identity_fingerprint"] == b["identity_fingerprint"] == overlap["identity_fingerprint"]):
            raise FixtureError("IMP-11: la clé de transaction diffère entre les trois fichiers")
        if not (
            a["visible_occurrences_in_file"] == 1
            and b["visible_occurrences_in_file"] == 1
            and overlap["visible_occurrences_in_file"] == 2
            and overlap.get("reconciled_occurrences") == max(
                a["visible_occurrences_in_file"], overlap["visible_occurrences_in_file"]
            )
        ):
            raise FixtureError("IMP-11: le chevauchement ne produit pas la vérité n=2")
    except (KeyError, TypeError) as error:
        print(f"ERREUR : oracle IMP-11 incomplet ({error.__class__.__name__}).", file=sys.stderr)
        return 1
    except FixtureError as error:
        print(f"ERREUR : {error}", file=sys.stderr)
        return 1
    print(f"OK : {len(expected_fixtures)} fixtures vérifiées, manifest concordant, confidentialité vérifiée.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
