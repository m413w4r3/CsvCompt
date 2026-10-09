#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
uv run ruff check backend
uv run ruff format --check backend
cd frontend
npm run lint
