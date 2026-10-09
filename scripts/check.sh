#!/usr/bin/env bash
# Équivalent séquentiel de `make check` pour les systèmes sans GNU make.
set -euo pipefail
repo="$(git rev-parse --show-toplevel)"
cd "$repo"
./scripts/lint.sh
uv run mypy backend/app
(cd frontend && npm run typecheck)
./scripts/test_backend.sh
./scripts/test_frontend.sh
./scripts/test_integration.sh
./scripts/test_e2e_smoke.sh
./scripts/verify_contracts.sh
scripts/check_no_private_data.sh --with-untracked
