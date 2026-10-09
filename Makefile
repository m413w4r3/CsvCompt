SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

.PHONY: lint typecheck test-backend test-frontend test-integration test-e2e-smoke verify-contracts privacy check fixtures install clean
.NOTPARALLEL: check

lint:
	./scripts/lint.sh

typecheck:
	uv run mypy backend/app
	cd frontend && npm run typecheck

test-backend:
	./scripts/test_backend.sh

test-frontend:
	./scripts/test_frontend.sh

test-integration:
	./scripts/test_integration.sh

test-e2e-smoke:
	./scripts/test_e2e_smoke.sh

verify-contracts:
	./scripts/verify_contracts.sh

privacy:
	scripts/check_no_private_data.sh --with-untracked

check: lint typecheck test-backend test-frontend test-integration test-e2e-smoke verify-contracts privacy

fixtures:
	@set -euo pipefail; before="$$(find backend/tests/fixtures -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum)"; \
	  uv run python backend/tests/fixtures/generate_fixtures.py; \
	  uv run python backend/tests/fixtures/verify_fixtures.py; \
	  uv run python backend/tests/fixtures/generate_fixtures.py; \
	  after="$$(find backend/tests/fixtures -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum)"; \
	  test "$$before" = "$$after" || { echo 'ERREUR : génération de fixtures non déterministe.' >&2; exit 1; }; \
	  echo 'OK : fixtures synthétiques déterministes.'

install:
	uv sync --all-groups
	cd frontend && npm ci

clean:
	rm -rf frontend/dist frontend/coverage .pytest_cache .mypy_cache .ruff_cache .coverage
