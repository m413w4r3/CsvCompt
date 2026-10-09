#!/usr/bin/env python3
"""Auto-test stdlib du profileur privé, exclusivement sur fixtures synthétiques."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "backend" / "tests" / "fixtures"
PROFILE = ROOT / "scripts" / "real_csv_profile.py"
MANIFEST = json.loads((FIXTURES / "manifest.yaml").read_text(encoding="utf-8"))["fixtures"]
CHECK_FIXTURES = (
    "ca_2026_simple.csv",
    "ca_2026_utf8_crlf.csv",
    "ca_2026_utf8_crlf_bom.csv",
    "ca_2026_utf8_crlf_nbsp.csv",
    "ca_2026_multi_sections.csv",
    "overlap_A.csv",
    "scenario_famille_2026.csv",
)


class RealCSVProfileSelfTest(unittest.TestCase):
    def run_profile(self, fixture: str, *options: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(PROFILE), str(FIXTURES / fixture), *options],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env={key: value for key, value in os.environ.items() if key != "COMPTES_REAL_CSV"},
        )

    def test_CSV06_profile_aggregates_match_fixture_oracles(self) -> None:
        for fixture in CHECK_FIXTURES:
            with self.subTest(fixture=fixture):
                result = self.run_profile(fixture, "--json")
                self.assertEqual(result.returncode, 0, "le profilage synthétique doit réussir sans détail source")
                actual = json.loads(result.stdout)
                expected = MANIFEST[fixture]
                self.assertEqual(len(actual["sections"]), len(expected["sections"]))
                for got, want in zip(actual["sections"], expected["sections"], strict=True):
                    for field in ("operation_count", "debit_cents", "credit_cents", "min_date", "max_date", "header_balance_cents"):
                        self.assertEqual(got[field], want[field], f"agrégat divergent: {field}")
                counts = [entry["count"] for entry in expected["duplicate_groups"]]
                self.assertEqual(actual["duplicate_groups"], len(counts))
                self.assertEqual(actual["duplicate_max_multiplicity"], max(counts, default=1))

    def test_SEC04_no_fixture_label_is_printed(self) -> None:
        for fixture in CHECK_FIXTURES:
            with self.subTest(fixture=fixture):
                result = self.run_profile(fixture, "--json")
                self.assertEqual(result.returncode, 0)
                output = result.stdout + result.stderr
                with (FIXTURES / fixture).open(encoding="cp1252", errors="replace", newline="") as stream:
                    for row in csv.reader(stream, delimiter=";", quotechar='"'):
                        if len(row) == 5 and row[0].count("/") == 2 and row[1].strip():
                            self.assertNotIn(row[1], output)

    def test_SEC04_errors_do_not_disclose_input_rows(self) -> None:
        fixture = "adversarial/unclosed_quote_truncated.csv"
        result = self.run_profile(fixture)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Erreur : fichier illisible ou format CSV non reconnu.", result.stderr)
        self.assertNotIn("UNIQUE-SYNTHETIC", result.stdout + result.stderr)
        self.assertNotIn("OPERATION FICTIVE", result.stdout + result.stderr)

    def test_opt_in_skip_when_environment_unset(self) -> None:
        result = subprocess.run(
            [sys.executable, "-I", str(PROFILE), "--skip-if-unset"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env={key: value for key, value in os.environ.items() if key != "COMPTES_REAL_CSV"},
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "non configuré — test opt-in ignoré")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
