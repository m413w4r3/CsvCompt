# Contrôle qualité et CI

## Commande complète

Depuis la racine, après installation, `make check` enchaîne les contrôles dans cet ordre et s'arrête au premier échec : lint Ruff/ESLint, typage mypy/TypeScript, tests backend, tests frontend, tests d'intégration, smoke E2E, vérificateurs de contrats, puis contrôle de confidentialité.

```sh
make clean install check
```

`make install` exécute `uv sync --all-groups` et `npm ci`. `make clean` supprime les builds et caches de vérification, sans toucher aux bases utilisateur. `make fixtures` régénère les fixtures synthétiques, les vérifie et compare les empreintes avant/après deux générations.

## Cibles individuelles

```sh
make lint
make typecheck
make test-backend
make test-frontend
make test-integration
make test-e2e-smoke
make verify-contracts
make privacy
```

Les tests backend excluent toujours `opt_in_real_data`. Les tests d'intégration sélectionnent le marqueur `integration`. Le smoke migre une base SQLite temporaire sous `/tmp` avant de démarrer le backend prêt, puis démarre un second backend avec une base vide. Playwright vérifie les libellés « Serveur local connecté » et « Base locale à initialiser ou à migrer » à partir des réponses réelles de ces deux backends. Le frontend est construit avant le test. Les deux bases temporaires et les processus serveur sont nettoyés à la fin. En CI, une installation de navigateur impossible est une erreur ; en local hors CI, l'échec d'installation hors ligne affiche `SKIPPED`.

## Sans GNU make

Les scripts `scripts/lint.sh`, `scripts/test_backend.sh`, `scripts/test_frontend.sh`, `scripts/test_integration.sh`, `scripts/test_e2e_smoke.sh`, `scripts/verify_contracts.sh` et `scripts/check.sh` fournissent les équivalents exécutables. `scripts/check.sh` enchaîne tout comme `make check`. Pour le typage seul, les commandes sont `uv run mypy backend/app` puis `cd frontend && npm run typecheck` ; cette paire n'a pas de script autonome dédié.

`frontend/package.json` fournit aussi `npm run check` (lint, typage, Vitest et build) et `npm run test:e2e:smoke`. Ils ne remplacent pas la séquence globale, car les contrats, tests backend et contrôles de données sont pilotés à la racine.

## CI et données

GitHub Actions sépare backend, frontend, contrats, confidentialité et smoke E2E. Chaque suite utilise les fixtures synthétiques suivies sous `backend/tests/fixtures/` et des bases temporaires. Aucune étape ne définit `COMPTES_REAL_CSV`, ne lit `csv_references/` ou `data/`, et aucun artefact de test ou rapport n'est publié. Les tests marqués `opt_in_real_data` sont exclus explicitement des commandes pytest par défaut. Les permissions du workflow sont limitées à `contents: read` ; aucun secret n'est requis.

Le garde `make privacy` appelle `scripts/check_no_private_data.sh --with-untracked`. Les tests `test_repo_guards.py` couvrent également les chemins interdits, l'usage SQL `INSERT OR REPLACE`, l'exclusion des tests réels par défaut et l'heuristique anti-float dans `backend/app`.

## Durées indicatives et dépannage

Sur une machine de développement courante, installation initiale : 1 à 5 minutes selon le cache/réseau ; `make check` : 1 à 3 minutes avec Chromium installé. Le premier smoke peut prendre davantage de temps pour télécharger Chromium (limite de 120 secondes).

- `uv` absent ou mauvaise version Python : installer Python 3.12 et `uv`, puis relancer `make install`.
- Échec `npm ci` : vérifier Node 24 et le réseau npm, puis relancer `make install`.
- Navigateur absent : `cd frontend && npx playwright install chromium`, puis `make test-e2e-smoke`.
- Backend smoke non joignable : vérifier que le loopback local et la création de fichiers temporaires sont autorisés ; la base smoke ne se trouve jamais dans `data/`.
- Échec d'un vérificateur de contrat : le script affiche son oracle et quitte en erreur ; le pipeline ne masque ni n'assouplit un échec.
