#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
uv run pytest -q backend/tests -m 'not opt_in_real_data' --cov=backend/app --cov-report=term-missing
