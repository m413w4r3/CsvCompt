#!/usr/bin/env bash
# E2E Playwright de la page de revue : backend réel, base SQLite temporaire synthétique.
# Import de la fixture par l'API, rattachement de contreparties, puis scénario complet.
set -euo pipefail
repo="$(git rev-parse --show-toplevel)"
cd "$repo"

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/comptes-familiaux-review-e2e.XXXXXX")"
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
database="$tmp_dir/review.sqlite"
export DATABASE_URL="sqlite:///$database" DATA_DIR="$tmp_dir/data"

uv run python -m app.db.migrate upgrade >/dev/null
uv run python scripts/e2e_review_seed.py \
  "$repo/backend/tests/fixtures/scenario_famille_2026.csv" "$database" "$tmp_dir/data" >"$tmp_dir/seed.json"

APP_HOST=127.0.0.1 APP_PORT="$SMOKE_API_PORT" \
  CORS_ALLOWED_ORIGINS='["http://127.0.0.1:4173","http://localhost:4173"]' \
  uv run --project "$repo" uvicorn app.main:create_app --factory --app-dir "$repo/backend" \
  --host 127.0.0.1 --port "$SMOKE_API_PORT" >"$tmp_dir/backend.log" 2>&1 &
backend_pid=$!
for attempt in $(seq 1 120); do
  if curl --silent --fail "http://127.0.0.1:$SMOKE_API_PORT/api/v1/health" >/dev/null; then break; fi
  sleep 0.25
done
if ! curl --silent --fail "http://127.0.0.1:$SMOKE_API_PORT/api/v1/health" >/dev/null; then
  echo "ERREUR : backend de revue indisponible." >&2
  exit 1
fi
cd "$repo/frontend"
# Build de production servi par `vite preview` (CSP d'exécution) : le serveur de développement
# injecte un script inline de rafraîchissement que cette CSP bloque.
npm run build >/dev/null
SMOKE_API_PORT="$SMOKE_API_PORT" npx playwright test --config tests/e2e/playwright.review.config.ts "$@"
