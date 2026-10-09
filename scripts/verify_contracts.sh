#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
uv run python scripts/verify_metrics_examples.py
uv run python scripts/verify_schema.py
uv run python backend/tests/fixtures/verify_fixtures.py
uv run python scripts/tests_real_csv_profile_selftest.py
uv run python .nimbalyst/plan_implementation_comptes_familiaux/scripts/validate_plan.py
