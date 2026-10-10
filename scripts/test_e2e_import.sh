#!/usr/bin/env bash
set -euo pipefail
repo="$(git rev-parse --show-toplevel)"
cd "$repo"

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/comptes-familiaux-import-e2e.XXXXXX")"
backend_pid=""
cleanup() {
  if [[ -n "$backend_pid" ]]; then
    kill "$backend_pid" 2>/dev/null || true
    wait "$backend_pid" 2>/dev/null || true
  fi
  rm -rf "$tmp_dir"
}
trap cleanup EXIT INT TERM

if [[ -z "${SMOKE_API_PORT:-}" ]]; then
  SMOKE_API_PORT="$(uv run python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
fi
export SMOKE_API_PORT
database="$tmp_dir/import.sqlite"
DATABASE_URL="sqlite:///$database" DATA_DIR="$tmp_dir/data" uv run python -m app.db.migrate upgrade
APP_HOST=127.0.0.1 APP_PORT="$SMOKE_API_PORT" DATABASE_URL="sqlite:///$database" DATA_DIR="$tmp_dir/data" \
  uv run --project "$repo" uvicorn app.main:create_app --factory --app-dir "$repo/backend" \
  --host 127.0.0.1 --port "$SMOKE_API_PORT" >"$tmp_dir/backend.log" 2>&1 &
backend_pid=$!
for attempt in $(seq 1 120); do
  if curl --silent --fail "http://127.0.0.1:$SMOKE_API_PORT/api/v1/health" >/dev/null; then break; fi
  sleep 0.25
done
if ! curl --silent --fail "http://127.0.0.1:$SMOKE_API_PORT/api/v1/health" >/dev/null; then
  echo "ERREUR : backend import indisponible." >&2
  exit 1
fi
cd "$repo/frontend"
SMOKE_API_PORT="$SMOKE_API_PORT" npm run test:e2e -- tests/e2e/transactions.spec.ts
