# Conventions de tests

Complète `.nimbalyst/plan_implementation_comptes_familiaux/docs/MATRICE_TESTS.md` (oracles) et `PLAN_ORCHESTRATION.md` §H (plan de tests). Statut : conventions posées au ticket T00 ; les commandes `make` sont **prévues et créées au ticket T08**, elles n'existent pas encore.

## Commande standard

Ordre d'enchaînement (PLAN §H « Pratique CI locale ») :

```text
make lint -> make typecheck -> make test-backend -> make test-frontend -> make test-integration -> make test-e2e-smoke -> make check
```

`make check` exécute l'ensemble. Si `make` est indisponible sur la cible, des équivalents sous `scripts/` seront fournis et la différence documentée. Avant chaque proposition de commit, exécuter aussi `scripts/check_no_private_data.sh` (disponible dès maintenant).

Tant que T08 n'est pas livré, un sous-agent exécute directement les outils concernés (ex. `python -m pytest backend/tests -m "not opt_in_real_data"`) et indique les commandes exactes dans son rapport.

## Données de tests

- La CI et les suites par défaut n'utilisent **que** des fixtures synthétiques sans PII (`backend/tests/fixtures/`) et des bases SQLite temporaires.
- `csv_references/` et `data/` ne sont **jamais lus** par les tests de CI.
- Le test sur un vrai CSV est **opt-in local** : activé uniquement si `COMPTES_REAL_CSV` pointe vers le fichier ; sinon `skip` explicite. Aucun libellé réel dans les assertions, snapshots ou sorties.

## Nommage : IDs de MATRICE_TESTS

Chaque test qui couvre un scénario de la matrice porte son ID dans son nom (et/ou en marqueur) pour permettre la traçabilité :

- Python : `test_<ID_avec_underscores>_<description_courte>`, ex. `test_IMP_03_deux_paiements_identiques_dans_un_fichier`, `test_MONEY_02_split_inexact_rejete`, `test_CSV_06_csv_reel_local` (opt-in). L'ID `IMP-03` s'écrit `IMP_03` dans l'identifiant Python.
- Fichiers : regroupés par domaine (`test_import_reconciliation.py`, `test_money_allocations.py`) ; un fichier peut couvrir plusieurs IDs.
- Frontend (Vitest/Playwright) : l'ID figure au début du titre, ex. `it("UI-03 : affiche 'données insuffisantes'")`.
- Un test peut aussi déclarer son ID par marqueur lorsque c'est pratique : `@pytest.mark.matrice("IMP-03")` (marqueur `matrice` déclaré à T08).
- Tout scénario de la matrice non encore automatisé reste `À réaliser` ; ne jamais le déclarer couvert sans test exécuté.

## Marqueurs pytest prévus

Déclarés dans la configuration pytest au ticket T05/T08 (`--strict-markers`) :

| Marqueur | Contenu | Exécuté en CI |
|---|---|---|
| `unit` | fonctions pures : parsing, argent, dates, identités, allocateur, métriques, parseur LLM | oui |
| `integration` | SQLite temporaire réelle, migrations, transactions/rollback, concurrence | oui |
| `contract` | contrats HTTP `/api/v1` : validation, pagination, erreurs, cohérence des drilldowns | oui |
| `golden` | chiffres exacts en centimes sur un univers synthétique connu | oui |
| `opt_in_real_data` | test sur données réelles locales, activé par `COMPTES_REAL_CSV` | **non** (local uniquement) |

Les niveaux `metamorphic`, `concurrency`, `security`, `e2e`, `a11y` de la matrice s'ajoutent comme marqueurs lorsque leurs tickets les introduisent.

## Règles

1. **Un test qui échoue n'est jamais supprimé, désactivé (`skip`/`xfail` injustifié) ni affaibli** (assertion assouplie, tolérance ajoutée, données adaptées) sans une ADR dans `docs/DECISIONS.md` validée par l'orchestrateur. Un échec est documenté et corrigé à la source, ou consigné dans `docs/REGISTRE_RISQUES.md`.
2. Les montants sont comparés **à 1 centime près, en entiers** ; aucun `approx`, aucune tolérance en euros, aucun `float`.
3. Chaque ticket ajoute d'abord un test qui échoue, puis le code qui le fait passer lorsque le comportement est réparable.
4. Les erreurs de logique monétaire sont couvertes par des cas explicites (éventuellement property-based en complément).
5. Pas de réseau sortant dans les tests ; le LLM est simulé par un faux serveur / mock.
6. Une affirmation « testé » dans un rapport exige la commande exacte et son résultat réel ; les tests non lancés sont listés comme tels.
