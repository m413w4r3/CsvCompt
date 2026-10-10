# État d’implémentation — Comptes familiaux

> Suivi factuel. Un ticket n’est déclaré livré que si le commit correspondant existe ; un gate n’est jamais réputé validé par la seule présence du code.
> Réconciliation effectuée le 2026-10-10 sur `HEAD=644c286`, avec modifications locales présentes.

## Jalons et gates

| Gate | Statut factuel au 2026-10-10 | Preuve / condition restante |
|---|---|---|
| G0 | Statut historique documenté, non réévalué ici | Revues G0 dans `docs/revues/` ; aucune nouvelle vérification menée dans cette réconciliation |
| G1 | Statut historique documenté, non réévalué ici | `docs/revues/REVUE_G1.md` |
| G2 | Statut historique documenté, non réévalué ici | `docs/revues/REVUE_G2.md`, `docs/revues/REVUE_G3.md` |
| G3 | **PASS** (revue indépendante n°6, 0 bloquant, 0 majeur) | `docs/revues/REVUE_G3_DURCISSEMENT_CONFIRMATION_6.md` ; M-G3C-01 et M-G3C-09 corrigés (commits `10856f5`, `802b5dd`, `336105a`, `131c864`) ; 425 tests backend, Vitest 50, E2E import 3/3 et smoke 2/2, contrats et confidentialité verts ; CI GitHub verte sur `131c864`. Réserves ouvertes : m-G3C-04..06 (REGISTRE_RISQUES). |
| G4 | **PASS sous réserves consignées** (arbitrage orchestrateur du 2026-10-11) | T16..T20 livrés et commités (T19 `9e680be`, T20 `da34ec3`, `565df4e`, correctifs 0004). Revues indépendantes : T17 PASS (`REVUE_T17_G4.md`), T19 (`REVUE_T19.md`) : B-01 levé par ADR-029, M-01 couvert par `test_rules_engine_hardening.py` ; T20 (`REVUE_T20.md`, `REVUE_T20_CONFIRMATION.md`) : B-01, M-01, M-02 résolus ; B-02 (réécriture cohérente des empreintes par un écrivain DB) et B-03 (motif en clair dans `rules.scope_json`/sauvegardes) acceptés comme limites documentées (ADR-035, R-26, PRIVACY.md) ; M-03 corrigé : `verify_schema.py` 38/45 mutants détectés + 7/45 redondances prouvées (45/45 expliqués). Suite 453 tests, mypy, contrats verts. Non réexaminé par un relecteur tiers : le correctif M-03 (oracle exécutable, preuve automatique). |
| G5 | Non évalué | Dépend de T19, T20 puis T21-T23 |
| G6 | Non évalué | Dépend de T24-T26 |
| G7 | Non évalué | Dépend de T27-T30 |
| G8 | Non évalué | Dépend de T31-T34 |
| G9 | Non évalué | Dépend de T35-T37 |
| G10 | Non évalué | Dépend de T38-T40 |
| G11 | Non évalué | T41 est commité ; T42-T45 et les dépendances amont restent à clôturer |

## Tickets livrés / en cours

Les identifiants de commit ci-dessous ont été vérifiés dans `git log --oneline`. Les tickets T00 à T18 ont des commits correspondant aux éléments listés dans l’historique de la branche ; T19-T40 restent à traiter selon le backlog. Les mentions de livraison ci-dessous ne valent pas validation de gate.

| Ticket | État factuel | Commit(s) vérifiés | Preuve / revue |
|---|---|---|---|
| T00..T04 | Livrés et commités | `dbf46ca`, `0a6572e`, `310066e`, `2ac50be`, `f60215c` | Revues G0 archivées |
| T05..T08 | Livrés et commités | `2ebdabf`, `9dcb3f0`, `39290d2`, `a4e6d2e` ; corrections G1 `e58d240`, `04efd55` | Revue G1 archivée |
| T09..T11 | Livrés et commités | `3d55f9c`, `6a553d2`, `cbb30dd` ; corrections G2 `bec0ba5`, `ef5c4b1` | Revues G2/G3 archivées |
| T12..T13 | Livrés et commités | `4359a48` | Durcissement G3 et revue de confirmation à rapprocher des réserves restantes |
| T14..T15 | Livrés et commités | `aeb7b32` | T14b (delta entre exports consécutifs) reste à planifier selon ADR-030.6 |
| T16 | Livré et commité | `4a3c7c4` | Revue indépendante G4 encore requise |
| T17 | Livré et commité | `1066748` ; `e5a10f3` | N-1..N-6 corrigés ; N-4 clos par preuves par mutation (`verify_schema.py`). Revue indépendante PASS (`REVUE_T17_G4.md`). ADR-033 acceptée. |
| T18 | Livré et commité | `7870c7b` | Revue indépendante G4 encore requise |
| T19 | Livré et commité | `9e680be`, tests `1db5c9f` | Revue indépendante : B-01 levé (ADR-029) ; 6 tests persistants M-01. |
| T20 | Livré et commité (services ; routes REST en T21) | `da34ec3`, `5c91f59`, `565df4e` + oracle exhaustif | Migration 0003 puis 0004 ; voir la ligne G4 pour les constats. |
| T21..T40 | À faire / non attestés comme livrés | — | Dépendances et ordre backlog dans le rapport de réconciliation |
| T41 | Livré et commité dans l’intégration G3 | `644c286` | 28 tests sauvegarde/restauration passés ; les six majeurs de `REVUE_T41.md` sont corrigés. m-G3C-04..06 ouverts. |
| T42 | En cours / non validé comme ticket terminé | `644c286` contient des éléments sécurité transverses | Dépendances backlog T05 et T12 ; preuve complète et revue OPS à établir |
| T43..T45 | À faire | — | Dépendances détaillées en fin du rapport de réconciliation |

## Revues récentes et changements locaux

- `REVUE_G3_DURCISSEMENT_CONFIRMATION.md` : BLOCK sur M-G3C-01. Corrigé (pool dédié de 4 connexions, plafond 3 snapshots LRU, TTL 60 s, balayage actif, debounce UI, 503 contractuels). Les revues n°2 à n°5 ont successivement relevé M-G3C-09 (arrêt du balayage) ; la conception finale (store fermé sous verrou, aucune manipulation d'annulation) est validée PASS par la revue n°6.
- ADR-030 et ADR-032 sont consignées dans `docs/DECISIONS.md`. T14b reste reportée (ADR-030.6).
- CI-01 corrigé (`changeOrigin: true`, `frontend/vite.config.ts`) ; formatage ruff appliqué ; CI GitHub verte sur `131c864` (5 jobs).
- Réserves ouvertes : m-G3C-04 (`.tmp` résiduel bloque la restauration), m-G3C-05 (inode du verrou), m-G3C-06 (purge de tout `*.bin`). N-4 (T17) : preuves par mutation encore partielles.
- G4 : voir la ligne de gate ; réserves restantes : m-G3C-04..06 (G3), limites B-02/B-03 de T20 (R-26, PRIVACY.md).

## Preuves exécutées pour cette réconciliation

- `uv run pytest -q backend/tests/unit/domain/test_money_split.py backend/tests/integration/test_refund_links_service.py backend/tests/integration/test_allocations_service.py backend/tests/integration/test_migration_0002.py` → **53 passed**, 1 avertissement StarletteDeprecationWarning.
- `uv run pytest -q backend/tests/integration/test_backups.py` → **28 passed**, 1 avertissement StarletteDeprecationWarning.
- `uv run python scripts/verify_schema.py` → **PASS verify_schema**, y compris les preuves par mutation des garde-fous de remboursements.
- `make verify-contracts` → **OK**, 34 fixtures, autotests 4/4, backlog 46 tickets / 12 gates / 87 IDs de catégories / 81 références de tests.
- `uv run ruff format --check backend scripts` → **116 files already formatted**.

Ces tests utilisent uniquement des données et bases synthétiques. Aucun test opt-in réel n’a été lancé.

## À faire

Se reporter à la section « Tickets restants du backlog » de `docs/revues/RECONCILIATION_T17_T41.md` : elle liste les dépendances et les préconditions de G4 dans l’ordre recommandé. Tout est commité et poussé ; prochaine étape : T19 puis T20 (précondition G4).
