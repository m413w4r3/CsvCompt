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

backend_port="$(uv run python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
db_file="$tmp_dir/smoke.sqlite"
: > "$db_file"
APP_HOST=127.0.0.1 APP_PORT="$backend_port" DATABASE_URL="sqlite:///$db_file" DATA_DIR="$tmp_dir" \
  uv run uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port "$backend_port" >"$tmp_dir/backend.log" 2>&1 &
backend_pid=$!

BACKEND_URL="http://127.0.0.1:$backend_port/api/v1/health" uv run python - <<'PY'
import os
import time
import urllib.error
import urllib.request

url = os.environ["BACKEND_URL"]
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            if response.status == 200:
                break
    except (OSError, urllib.error.URLError):
        time.sleep(0.25)
else:
    raise SystemExit("ERREUR : backend smoke indisponible après 30 s.")
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
BACKEND_URL="http://127.0.0.1:$backend_port/api/v1/health" npm run test:e2e:smoke
