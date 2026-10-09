#!/usr/bin/env bash
set -euo pipefail
repo="$(git rev-parse --show-toplevel)"
cd "$repo"

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/comptes-familiaux-e2e.XXXXXX")"
ready_backend_pid=""
empty_backend_pid=""
cleanup() {
  for pid in "$ready_backend_pid" "$empty_backend_pid"; do
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  rm -rf "$tmp_dir"
}
trap cleanup EXIT INT TERM

# Migre d'abord une base dédiée au smoke. La base vide du second scénario reste séparée.
ready_db="$tmp_dir/migrated/smoke.sqlite"
mkdir -p "$(dirname "$ready_db")" "$tmp_dir/empty"
DATABASE_URL="sqlite:///$ready_db" DATA_DIR="$tmp_dir/migrated" \
  uv run python -m app.db.migrate upgrade

empty_db="$tmp_dir/empty/smoke.sqlite"
: > "$empty_db"
ready_port="$(uv run python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
empty_port="$(uv run python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
APP_HOST=127.0.0.1 APP_PORT="$ready_port" DATABASE_URL="sqlite:///$ready_db" DATA_DIR="$tmp_dir/migrated" \
  uv run uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port "$ready_port" >"$tmp_dir/backend.log" 2>&1 &
ready_backend_pid=$!
APP_HOST=127.0.0.1 APP_PORT="$empty_port" DATABASE_URL="sqlite:///$empty_db" DATA_DIR="$tmp_dir/empty" \
  uv run uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port "$empty_port" >"$tmp_dir/empty-backend.log" 2>&1 &
empty_backend_pid=$!

BACKEND_URL="http://127.0.0.1:$ready_port/api/v1/health" \
UNMIGRATED_BACKEND_URL="http://127.0.0.1:$empty_port/api/v1/health" uv run python - <<'PY'
import os
import time
import urllib.error
import urllib.request

for key in ("BACKEND_URL", "UNMIGRATED_BACKEND_URL"):
    url = os.environ[key]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    break
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    else:
        raise SystemExit(f"ERREUR : backend smoke indisponible après 30 s ({key}).")
PY

cd "$repo/frontend"
npm run build
required_browser_paths="$(npx playwright install --dry-run chromium 2>/dev/null | sed -n 's/^  Install location:    //p')"
browser_available=1
if [[ -z "$required_browser_paths" ]]; then
  browser_available=0
else
  while IFS= read -r browser_path; do
    [[ -d "$browser_path" ]] || browser_available=0
  done <<< "$required_browser_paths"
fi
if [[ "$browser_available" -eq 0 ]]; then
  echo "Installation Playwright Chromium (timeout 120 s)…"
  if ! timeout 120s npx playwright install chromium; then
    if [[ "${CI:-0}" == "1" ]]; then
      echo "ERREUR : Chromium Playwright indisponible en CI." >&2
      exit 1
    fi
    echo "SKIPPED (navigateur indisponible) : installation de Chromium impossible hors ligne." >&2
    exit 0
  fi
fi
BACKEND_URL="http://127.0.0.1:$ready_port/api/v1/health" \
UNMIGRATED_BACKEND_URL="http://127.0.0.1:$empty_port/api/v1/health" npm run test:e2e:smoke
