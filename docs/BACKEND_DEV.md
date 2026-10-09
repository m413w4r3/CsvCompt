# Développement du backend

## Installation et lancement

Python 3.12 et `uv` sont requis. À la racine du dépôt :

```sh
uv sync
uv run python -m app
```

Le serveur lit `APP_HOST` et `APP_PORT` depuis l'environnement ou `.env` (facultatif, local et ignoré par Git). Par défaut, il écoute `127.0.0.1:9513`. L'hôte non loopback est refusé, sauf activation explicite de `ALLOW_NON_LOOPBACK=true`. La clé LLM (`LLM_API_KEY`) est une configuration backend de type secret et ne doit jamais être journalisée ni renvoyée par une API.

`GET /api/v1/health` est utilisable hors ligne et ne crée pas la base. Son contrat stable comprend `status`, `api_version` (`"1"`), `storage_status` et `llm_enabled`. Le stockage vaut `ok` si la base est migrée à la révision courante, `degraded` si elle est absente, non initialisée ou en retard de migration, et `unavailable` si elle est illisible ou corrompue. Des champs additionnels `version`, `time_utc`, `database` et `offline_ready` donnent des détails sans remplacer ces quatre champs.

## Vérifications

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy backend/app
```

Les marqueurs pytest sont `unit`, `integration`, `contract`, `golden` et `opt_in_real_data`. Les tests ordinaires utilisent des bases SQLite temporaires et les fixtures synthétiques ; les CSV réels ne sont jamais parcourus. Tout test réel doit être opt-in explicite via `COMPTES_REAL_CSV`.

## Structure

- `backend/app/core/` : paramètres, erreurs, rédaction et garde minimale de bind.
- `backend/app/api/v1/` : routes HTTP versionnées ; le contrôle health ne dépend pas du LLM.
- `backend/app/db/session.py` : fabrique SQLite, PRAGMA par connexion, transactions et dépendance injectable.
- `backend/tests/` : tests unitaires, de contrat et d'intégration.

Les handlers d'erreur exposent l'enveloppe `{ "error": { "code", "message", "details" } }`. Les corps de requête ne sont pas journalisés. La garde complète Origin/CSRF est réservée au ticket T42. Un point d'extension dans `create_app` accueillera ultérieurement le build frontend statique.
