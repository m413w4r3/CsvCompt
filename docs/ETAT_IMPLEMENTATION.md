# État d’implémentation — Comptes familiaux

> Suivi factuel. Un ticket n’est déclaré livré que si le commit correspondant existe ; un gate n’est jamais réputé validé par la seule présence du code.
> Réconciliation effectuée le 2026-10-10 sur `HEAD=644c286`, avec modifications locales présentes.

## Jalons et gates

| Gate | Statut factuel au 2026-10-10 | Preuve / condition restante |
|---|---|---|
| G0 | Statut historique documenté, non réévalué ici | Revues G0 dans `docs/revues/` ; aucune nouvelle vérification menée dans cette réconciliation |
| G1 | Statut historique documenté, non réévalué ici | `docs/revues/REVUE_G1.md` |
| G2 | Statut historique documenté, non réévalué ici | `docs/revues/REVUE_G2.md`, `docs/revues/REVUE_G3.md` |
| G3 | **En attente de revue indépendante** | `REVUE_G3_DURCISSEMENT_CONFIRMATION.md` avait bloqué sur M-G3C-01 ; correction locale faite, revue en attente. m-G3C-04..06 restent ouverts. CI-01 et formatage sont corrigés localement, non poussés. |
| G4 | **Non évalué** | T17 est engagé mais ses corrections N-1..N-6 attendent la revue indépendante ; T19 et T20 ne sont pas livrés. Voir les préconditions et l’ordre dans `docs/revues/RECONCILIATION_T17_T41.md`. |
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
| T17 | Base livrée dans le commit de référence ; correctifs ultérieurs présents localement, non commités | `1066748` ; état de travail courant | N-1, N-2, N-3, N-5 et N-6 corrigés ; N-4 partiellement traité, mutations de preuve encore manquantes. Contre-revue indépendante à faire avant évaluation G4. Tests ciblés : 53 passés ; détail dans le rapport de réconciliation. |
| T18 | Livré et commité | `7870c7b` | Revue indépendante G4 encore requise |
| T19..T40 | À faire / non attestés comme livrés | — | Dépendances et ordre backlog dans le rapport de réconciliation |
| T41 | Livré et commité dans l’intégration G3 | `644c286` | 28 tests sauvegarde/restauration passés ; les six majeurs de `REVUE_T41.md` sont corrigés. m-G3C-04..06 ouverts. |
| T42 | En cours / non validé comme ticket terminé | `644c286` contient des éléments sécurité transverses | Dépendances backlog T05 et T12 ; preuve complète et revue OPS à établir |
| T43..T45 | À faire | — | Dépendances détaillées en fin du rapport de réconciliation |

## Revues récentes et changements locaux

- `REVUE_G3_DURCISSEMENT.md` et `REVUE_G3_DURCISSEMENT_CONFIRMATION.md` : le second rapport statuait BLOCK sur M-G3C-01 et relevait m-G3C-04..06. La correction de M-G3C-01 est maintenant présente localement ; elle attend une revue indépendante. m-G3C-04, m-G3C-05 et m-G3C-06 restent ouverts.
- ADR-030 est acceptée et consignée dans `docs/DECISIONS.md` (lignes 221 et suivantes). Elle reste la référence des choix G3, dont le report de T14b.
- M-G3C-01 : correction locale, revue indépendante en attente.
- CI-01 : `changeOrigin: true` est présent localement dans `frontend/vite.config.ts`; les changements ne sont pas poussés.
- Formatage : `uv run ruff format --check backend scripts` → `116 files already formatted`; changement local, non poussé.
- G3 ne peut pas être déclaré validé tant que la revue indépendante attendue et les constats bloquants ne sont pas clos. G4 n’a pas été évalué.

## Preuves exécutées pour cette réconciliation

- `uv run pytest -q backend/tests/unit/domain/test_money_split.py backend/tests/integration/test_refund_links_service.py backend/tests/integration/test_allocations_service.py backend/tests/integration/test_migration_0002.py` → **53 passed**, 1 avertissement StarletteDeprecationWarning.
- `uv run pytest -q backend/tests/integration/test_backups.py` → **28 passed**, 1 avertissement StarletteDeprecationWarning.
- `uv run python scripts/verify_schema.py` → **PASS verify_schema**, y compris les preuves par mutation des garde-fous de remboursements.
- `make verify-contracts` → **OK**, 34 fixtures, autotests 4/4, backlog 46 tickets / 12 gates / 87 IDs de catégories / 81 références de tests.
- `uv run ruff format --check backend scripts` → **116 files already formatted**.

Ces tests utilisent uniquement des données et bases synthétiques. Aucun test opt-in réel n’a été lancé.

## À faire

Se reporter à la section « Tickets restants du backlog » de `docs/revues/RECONCILIATION_T17_T41.md` : elle liste les dépendances et les préconditions de G4 dans l’ordre recommandé. Les corrections T17 locales et les changements CI locaux ne sont pas encore poussés.
