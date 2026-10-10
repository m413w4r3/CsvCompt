#!/usr/bin/env bash
set -euo pipefail
repo="$(git rev-parse --show-toplevel)"
cd "$repo"

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/comptes-familiaux-e2e.XXXXXX")"
backend_pid=""
cleanup() {
  if [[ -n "$backend_pid" ]]; then
    kill "$backend_pid" 2>/dev/null || true
    wait "$backend_pid" 2>/dev/null || true
  fi
  rm -rf "$tmp_dir"
}
trap cleanup EXIT INT TERM

# Le même port dédié est lu par le backend et par le proxy Vite /api.
if [[ -z "${SMOKE_API_PORT:-}" ]]; then
  SMOKE_API_PORT="$(uv run python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
fi
export SMOKE_API_PORT

ready_db="$tmp_dir/migrated/smoke.sqlite"
mkdir -p "$(dirname "$ready_db")" "$tmp_dir/empty"
DATABASE_URL="sqlite:///$ready_db" DATA_DIR="$tmp_dir/migrated" \
  uv run python -m app.db.migrate upgrade

empty_db="$tmp_dir/empty/smoke.sqlite"
: > "$empty_db"

start_backend() {
  local database_file="$1" data_dir="$2"
  APP_HOST=127.0.0.1 APP_PORT="$SMOKE_API_PORT" DATABASE_URL="sqlite:///$database_file" DATA_DIR="$data_dir" \
    uv run --project "$repo" uvicorn app.main:create_app --factory --app-dir "$repo/backend" \
      --host 127.0.0.1 --port "$SMOKE_API_PORT" \
      >"$tmp_dir/backend.log" 2>&1 &
  backend_pid=$!
  SMOKE_EXPECTED_STORAGE="$3" uv run --project "$repo" python - <<'PY'
import json
import os
import time
import urllib.error
import urllib.request

url = f"http://127.0.0.1:{os.environ['SMOKE_API_PORT']}/api/v1/health"
expected = os.environ["SMOKE_EXPECTED_STORAGE"]
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            health = json.loads(response.read())
        if health.get("storage_status") == expected:
            break
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        time.sleep(0.25)
else:
    raise SystemExit(f"ERREUR : health smoke indisponible ou storage_status != {expected}.")
PY
}

stop_backend() {
  if [[ -n "$backend_pid" ]]; then
    kill "$backend_pid" 2>/dev/null || true
    wait "$backend_pid" 2>/dev/null || true
    backend_pid=""
  fi
}

cd "$repo/frontend"
npm run build
required_browser_paths="$(npx playwright install --dry-run chromium 2>/dev/null | sed -n 's/^  Install location:    //p')"
chromium_path="$(sed -n '1p' <<< "$required_browser_paths")"
headless_path="$(sed -n '$p' <<< "$required_browser_paths")"
browser_available=1
for browser_path in "$chromium_path" "$headless_path"; do
  if [[ -z "$browser_path" || ! -d "$browser_path" ]] || \
     ! find "$browser_path" -type f -perm -u+x -print -quit | grep -q .; then
    browser_available=0
  fi
done
if [[ "$browser_available" -eq 0 ]]; then
  echo "Installation Playwright Chromium (timeout 120 s)…"
  if ! timeout 120s npx playwright install chromium; then
    if [[ "${CI:-0}" == "1" ]]; then
      echo "ERREUR : Chromium Playwright indisponible en CI." >&2
      exit 1
    fi
    echo "SKIPPED (navigateur indisponible) : Chromium n'a pas pu être installé." >&2
    exit 0
  fi
fi

start_backend "$ready_db" "$tmp_dir/migrated" ok
SMOKE_API_PORT="$SMOKE_API_PORT" npm run test:e2e:smoke -- --grep 'base migrée'
stop_backend

start_backend "$empty_db" "$tmp_dir/empty" degraded
SMOKE_API_PORT="$SMOKE_API_PORT" npm run test:e2e:smoke -- --grep 'sans migration'
