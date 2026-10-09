# Comptes familiaux

Application **locale**, mono-utilisateur, de suivi et d'analyse des finances d'un compte commun familial à partir d'exports CSV Crédit Agricole : import fiable et idempotent, ventilation semi-automatique (règles, mémoire, revue humaine), analytique détaillée et commentaires IA optionnels. Aucune connexion bancaire ; les CSV sont fournis manuellement chaque mois, et l'application fonctionne entièrement hors ligne (le LLM est facultatif et désactivé par défaut).

## Statut

**Spécification + implémentation en cours.** Ce dépôt contient aujourd'hui les spécifications d'exécution et les conventions de collaboration (ticket T00). **Aucune fonctionnalité applicative n'est encore implémentée** ; l'avancement réel est suivi dans `docs/ETAT_IMPLEMENTATION.md`.

Données de départ : opérations **2026** uniquement ; le CSV 2025 n'est qu'une fixture de compatibilité facultative (voir `docs/DECISIONS.md`, ADR-003).

## Stack prévue

- **Backend** : Python, FastAPI, SQLite, SQLAlchemy, Alembic ; API REST locale versionnée `/api/v1`, liée à `127.0.0.1`.
- **Frontend** : React, TypeScript, Vite, Apache ECharts.
- **Tests** : pytest, Vitest / Testing Library, Playwright.
- **LLM (optionnel)** : endpoint compatible OpenAI, appelé depuis le backend uniquement ; réponses attendues en texte souple, pas en JSON strict.

## Commandes standard (prévues, créées au ticket T08)

Aucune de ces commandes n'existe encore.

| Commande | Rôle | Statut |
|---|---|---|
| `make lint` | lint Python et TypeScript | prévue, créée au ticket T08 |
| `make typecheck` | typage Python et TypeScript | prévue, créée au ticket T08 |
| `make test-backend` | tests pytest backend | prévue, créée au ticket T08 |
| `make test-frontend` | tests Vitest / Testing Library | prévue, créée au ticket T08 |
| `make test-integration` | tests d'intégration (SQLite temporaire, migrations) | prévue, créée au ticket T08 |
| `make test-e2e-smoke` | parcours Playwright minimal | prévue, créée au ticket T08 |
| `make check` | enchaîne l'ensemble ci-dessus | prévue, créée au ticket T08 |

Voir `docs/CONVENTIONS_TESTS.md` pour la commande standard, le nommage et les marqueurs. Déjà disponible : `scripts/check_no_private_data.sh` (garde pré-commit de confidentialité).

## Confidentialité

- `csv_references/`, `data/`, les bases SQLite, `tmp.zip` et `.env` sont **ignorés par Git** et ne doivent jamais entrer dans le dépôt, les logs ou les captures.
- La CI n'utilise que des fixtures synthétiques. Le test sur un vrai CSV est un test local opt-in activé par la variable `COMPTES_REAL_CSV`.
- Copier `.env.example` vers `.env` pour la configuration locale ; ne jamais committer `.env`.

## Où trouver la spécification

Les spécifications font autorité et résident dans `.nimbalyst/plan_implementation_comptes_familiaux/` :

- `PLAN_ORCHESTRATION.md` : objectifs, invariants, tickets T00..T45, gates G0..G11
- `backlog.yaml` : tâches machine-readable
- `docs/MATRICE_TESTS.md` : scénarios et oracles de tests
- `docs/PROTOCOLE_LLM_TOLERANT.md` : protocole LLM en texte souple
- `config/categories.v1.yaml` : taxonomie versionnée

Conventions de travail pour agents et contributeurs : `AGENTS.md`. Décisions : `docs/DECISIONS.md`. Risques : `docs/REGISTRE_RISQUES.md`.
