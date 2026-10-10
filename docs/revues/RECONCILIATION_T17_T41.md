# Réconciliation factuelle T17 / T41

Date : 2026-10-10. Inspection de `HEAD=644c286` et de l’arbre de travail courant. Les fichiers applicatifs/test déjà modifiés par d’autres travaux ont été consultés et testés, jamais modifiés ici. Aucun contenu de `csv_references/`, `data/`, `tmp.zip` ou `.env*` n’a été lu.

## T17 — réserves N-1 à N-6 de `REVUE_T17_CONFIRMATION.md`

Les statuts ci-dessous décrivent le code et les tests présents dans l’arbre courant, pas leur intégration dans un commit. La revue indépendante T17 est livrée dans [`REVUE_T17_G4.md`](REVUE_T17_G4.md) : verdict PASS pour T17 et N-4 clôturée. Cette revue ne valide pas le gate G4.

| Constat | Statut factuel | Preuve dans le code et exécution | Action restante |
|---|---|---|---|
| N-1 — Une proposition sur une allocation supersédée ne peut être rejetée ; le retrait reste bloqué | **Corrigé et revu indépendamment** | `backend/app/services/allocations.py` rejette avec audit les propositions liées à l’instantané d’allocations remplacé ; `backend/tests/integration/test_refund_links_service.py:746` couvre les jambes source/cible, réallocation/retrait et rejeu (`MONEY_16`). Revue et suite ciblée : **58 passed**, détails dans [`REVUE_T17_G4.md`](REVUE_T17_G4.md). | Conserver le test `MONEY_16` en CI. |
| N-2 — Le prorata pouvait produire une part négative/supérieure à la capacité et échouer à la confirmation | **Corrigé et revu indépendamment** | `backend/app/domain/money_split.py:90-220` implémente une répartition bornée au plus fort reste ; `backend/app/services/refund_links.py:238-309` passe les capacités restantes par catégorie et traduit l’infeasibilité en erreur applicative. `MONEY_15` couvre 1 à 8 catégories, la somme exacte, le déterminisme, les plafonds et les ordres de liens. Revue et suite ciblée : **58 passed**. | Numéroter et arbitrer formellement la proposition ADR T17; arbitrage réservé à l’orchestrateur. |
| N-3 — Remplacement sur une clé active déjà utilisée remontait une `IntegrityError` brute | **Corrigé et revu indépendamment** | Le remplacement vérifie le doublon actif avant insertion (`backend/app/services/refund_links.py:785-801`) et `_translate_integrity_errors` protège les chemins d’écriture. `MONEY_17` est inclus dans la suite ciblée : **58 passed**; revue dans [`REVUE_T17_G4.md`](REVUE_T17_G4.md). | Conserver le code d’erreur stable `REFUND_LINK_ALREADY_EXISTS`. |
| N-4 — Les garde-fous SQL des parts n’avaient pas de preuve par mutation suffisante | **Corrigé et revu indépendamment** | `scripts/verify_schema.py:2883,3690-3712` retire séparément `trg_refund_link_allocation_parts_insert_guard`, `trg_refund_links_confirmed_attribution_insert` et `..._update`. Les contrôles ciblés détectent les trois mutants avec leurs messages attendus. `uv run python scripts/verify_schema.py` → **PASS verify_schema**; `make verify-contracts` → succès. Détails et verdict dans [`REVUE_T17_G4.md`](REVUE_T17_G4.md). | Aucune action restante sur N-4; conserver ces mutations dans le vérificateur. |
| N-5 — La transition SQL `rejected → confirmed` n’était pas interdite | **Corrigé et revu indépendamment** | La migration `0002_t17_refund_safety` ajoute le trigger de transition ; la preuve MONEY-19 est dans `scripts/verify_schema.py`. `uv run python scripts/verify_schema.py` → **PASS verify_schema**; revue dans [`REVUE_T17_G4.md`](REVUE_T17_G4.md). | Conserver la preuve de transition dans le vérificateur. |
| N-6 — Le prorata ignorait les capacités restantes, refusant certaines répartitions pourtant possibles | **Corrigé et revu indépendamment** | `backend/app/services/refund_links.py:245-262` calcule la capacité restante par allocation (`brut − usage confirmé`) et l’agrège par catégorie ; distribution ensuite plafonnée par allocation (`:280-305`). Les tests `MONEY_15` et `MONEY_20` et les scénarios de capacité de la revue sont passés : suite ciblée **58 passed**. | Conserver les tests bornés et le départage déterministe par ID d’allocation. |

La commande financière exécutée était :

```text
uv run pytest -q backend/tests/unit/domain/test_money_split.py backend/tests/integration/test_refund_links_service.py backend/tests/integration/test_allocations_service.py backend/tests/integration/test_migration_0002.py
53 passed, 1 warning in 12.89s (StarletteDeprecationWarning)
```

Résultats complémentaires : `uv run python scripts/verify_schema.py` → `PASS verify_schema`; `make verify-contracts` → succès, 34 fixtures contrôlées, 4 autotests passés et backlog reconnu (46 tickets, 12 gates). Aucun de ces résultats ne constitue une revue indépendante.

## T41 — six constats majeurs de `REVUE_T41.md`

Le code T41 est dans le commit `644c286` (`G3`), comme le montrent `git log --oneline -- backend/app/services/backups.py docs/BACKUP.md backend/tests/integration/test_backups.py`. Les six constats majeurs ont une correction présente et vérifiée ; les réserves G3 ultérieures m-G3C-04..06 restent ouvertes (section suivante).

| Constat | Statut factuel | Preuve dans le code et exécution | Action restante |
|---|---|---|---|
| M-01 — Le snapshot ne se terminait pas sous écritures concurrentes | **Corrigé** | `backend/app/services/backups.py:137-149,603-647` utilise `pages=-1` dans un worker tuable, avec délai maximal et nettoyage. `test_OPS_01_wal_backup_and_restore_finish_during_large_concurrent_writes` (l. 231) vérifie snapshot et restauration sous écrivain ; `test_OPS_01_backup_timeout_is_clear_and_leaves_no_staging_residue` vérifie le timeout. Les deux sont inclus dans la suite de 28 tests passée. |
| M-02 — La rétention supprimait potentiellement la sauvegarde créée ou des fichiers non pertinents | **Corrigé** | `backend/app/services/backups.py:687-715` trie les sauvegardes automatiques par horodatage de nom, vérifie chaque archive, conserve la plus récente et le chemin publié. `test_OPS_01_retention_uses_valid_named_auto_backups_and_never_prunes_output` (l. 1279) couvre mtime futur, archives invalides et fichiers manuels ; inclus dans les **28 passed**. |
| M-03 — Injection de formule dans l’export CSV | **Corrigé** | `backend/app/services/backups.py:1213-1222,1263-1273` préfixe les cellules textuelles dangereuses sans modifier les colonnes de montants. `test_OPS_02_restore_fixture_import_to_distinct_database_exactly` (l. 629-729) teste `=`, `@`, `+`, `-`, tabulation et retour chariot en CSV et les montants exacts ; inclus dans les **28 passed**. |
| M-04 — Sauvegarde/export privés pouvaient être créés dans une zone du dépôt non ignorée | **Corrigé** | `.gitignore:1-20` ignore les zones privées et les exports ; `backend/app/services/backups.py:282-294,1237-1240` refuse une destination de sauvegarde/export sous une zone du dépôt non autorisée. `test_OPS_03_exports_under_unignored_repository_paths_are_refused` (l. 977) vérifie les deux refus sans fichier créé ; inclus dans les **28 passed**. `scripts/check_no_private_data.sh` fournit également une garde ciblant les archives et exports. |
| M-05 — Un SIGKILL pendant la restauration laissait une cible mixte | **Corrigé** | `backend/app/services/backups.py:1382-1415,1418-1455,1645-1780` installe une génération complète derrière un pointeur `current`, vérifie avant publication et nettoie/reprend les staging au prochain appel. `test_OPS_02_restore_kill_points_keep_whole_generation_and_allow_retry` (l. 1098) tue six étapes, vérifie l’ancienne ou la nouvelle génération entière, puis réessaie avec succès ; inclus dans les **28 passed**. |
| M-06 — Une sauvegarde altérée sans triggers pouvait être acceptée et restaurée | **Corrigé** | `backend/app/services/backups.py:379-440` compare les objets `sqlite_master` normalisés (tables, index, triggers, vues) avec le schéma généré depuis la migration de la révision. `test_OPS_02_rejects_corrupt_incoherent_future_or_truncated_backup[missing_triggers]` (l. 826-872) retire tous les triggers et confirme le rejet ; inclus dans les **28 passed**. |

Commande réellement exécutée :

```text
uv run pytest -q backend/tests/integration/test_backups.py
28 passed, 1 warning in 45.05s (StarletteDeprecationWarning)
```

Cette suite synthétique prouve les scénarios indiqués ; elle ne remplace pas les critères de validation de G11.

## État G3 connexe — revue, ADR et CI

- `docs/revues/REVUE_G3_DURCISSEMENT.md` et `REVUE_G3_DURCISSEMENT_CONFIRMATION.md` sont les rapports de référence. La confirmation avait trouvé le BLOCK M-G3C-01 et les réserves m-G3C-04..06.
- **M-G3C-01** : corrigé dans le code/test local, en attente de revue indépendante. Aucune levée de G3 n’est déduite ici.
- **m-G3C-04, m-G3C-05, m-G3C-06** : ouverts. La confirmation décrit respectivement un `.tmp` résiduel qui peut bloquer une restauration, un verrou vulnérable à une manipulation externe, et une purge trop large de fichiers `.bin` non référencés.
- ADR-030 est acceptée dans `docs/DECISIONS.md` (l. 221 et suivantes). Le contrôle de solde prévu par ADR-030.6 (T14b : delta entre exports successifs) reste à planifier ; T14b n’est pas un ticket du backlog fourni.
- CI-01 / proxy Vite : `frontend/vite.config.ts:10` porte `changeOrigin: true` dans le diff local. Les modifications CI concernées ne sont pas poussées.
- Formatage local : `uv run ruff format --check backend scripts` → **116 files already formatted**. Non poussé.

## Tickets restants du backlog et préconditions de G4

Liste issue de `.nimbalyst/plan_implementation_comptes_familiaux/backlog.yaml`, dans l’ordre topologique recommandé par dépendances. T41 est exclu car livré/commité ; T42 reste à clore et à prouver malgré des éléments déjà dans `644c286`.

| Ordre | Ticket | Dépend de | Précondition / blocage |
|---:|---|---|---|
| 1 | T19 — Règles déterministes, simulation et priorité (G4) | T17, T18 | Revue T17 livrée; T19 reste à livrer avant l’évaluation G4. L’arbitrage formel de la proposition ADR prorata est réservé à l’orchestrateur. |
| 2 | T20 — Audit, décisions et annulation reventilation (G4) | T17, T19 | Après T19 et la revue T17 ; preuve d’annulation et audit à produire. |
| 3 | Évaluation G4 | T16, T17, T18, T19, T20 | Gate actuellement **non évalué** ; T19/T20 et revues indépendantes sont les préconditions manquantes. |
| 4 | T21 — API revue groupée et décisions (G5) | T19, T20 | Après clôture G4. |
| 5 | T22 — Page revue (G5) | T21, T15 | Après API et dépendance T15. |
| 6 | T23 — Métriques de qualité classification (G5) | T21 | Après T21. |
| 7 | T24 — Métriques financières canoniques (G6) | T17, T14, T01 | T17 revu ; T14 déjà commité. |
| 8 | T25 — Endpoints séries, catégories et drilldown (G6) | T24, T06 | Après T24. |
| 9 | T26 — Golden dataset et propriétés (G6) | T24, T25 | Après T24/T25. |
| 10 | T27 — Filtres partagés, période 2026 et URLs (G7) | T25, T07 | Après T25. |
| 11 | T28 — Cockpit KPIs, séries et Sankey (G7) | T25, T27 | Après T25/T27. |
| 12 | T29 — Dépenses, marchands, revenus et drilldowns (G7) | T25, T27 | Après T25/T27. |
| 13 | T30 — Formats FR, accessibilité et états incomplets (G7) | T28, T29 | Après T28/T29. |
| 14 | T31 — Adaptateur LLM opt-in (G8) | T05, T21 | Après API revue T21. |
| 15 | T32 — Prompt et parseur textuel tolérant (G8) | T16, T31 | Après T31. |
| 16 | T33 — Consentement, caviardage, suggestions (G8) | T21, T32, T22 | Après T21/T22/T32. |
| 17 | T34 — Évaluations corpus dégradé / fake provider (G8) | T33 | Après T33. |
| 18 | T35 — Vue enfants, charges et aides liées (G9) | T25, T29 | Après T25/T29. |
| 19 | T36 — Budgets, scénarios et prévision (G9) | T25, T24, T27 | Après métriques et filtres. |
| 20 | T37 — Détection et revue d’abonnements (G9) | T18, T25 | Après T25. |
| 21 | T38 — Moteur de faits vérifiables (G10) | T24, T25, T36 | Après métriques et budgets. |
| 22 | T39 — Synthèse IA contrôlée par preuves (G10) | T31, T38 | Après T31/T38. |
| 23 | T40 — Page insights (G10) | T39, T27 | Après T39/T27. |
| 24 | T42 — Sécurité locale, origines et confidentialité (G11) | T05, T12 | Des éléments sont commités ; ticket non déclaré terminé : compléter les preuves OPS/SEC et la revue. Peut avancer en parallèle des lots G4-G10. |
| 25 | T43 — Packaging local, commandes et manuel (G11) | T41, T42, T30 | Après T42 et T30. |
| 26 | T44 — E2E complet cycle mensuel (G11) | T43, T22, T30, T34, T35, T36, T37, T40 | Après tous les parcours fonctionnels listés. |
| 27 | T45 — Audit final, corrections, release v1 locale (G11) | T44 | Dernière étape, après tous les gates et revues. |

### Préconditions explicites pour G4

1. **Satisfaite pour la revue T17** : [`REVUE_T17_G4.md`](REVUE_T17_G4.md) confirme N-1, N-2, N-3, N-5 et N-6, et clôt N-4 avec trois preuves par mutation. L’arbitrage formel de la proposition ADR prorata reste réservé à l’orchestrateur.
2. Livrer T19 puis T20, avec tests et revues requis par le backlog.
3. Réévaluer T16-T18 et les réserves/domaines dépendants du moteur d’allocations ; établir les preuves propres au gate. Aucun gate n’est marqué validé dans ce rapport.

## Retour au format AGENTS.md

Ticket : T17 / T41 — réconciliation documentaire, rôle OPS/DOMAIN, non-auteur des changements applicatifs.

Objectif livré : comparaison des six réserves T17 et des six constats majeurs T41 avec le code courant, tests ciblés exécutés, suivi corrigé et backlog restant ordonné.

Fichiers changés : `docs/revues/RECONCILIATION_T17_T41.md`, `docs/ETAT_IMPLEMENTATION.md`, `docs/REGISTRE_RISQUES.md`.

Interfaces modifiées : aucune.

Tests ajoutés : aucun.

Commandes réellement exécutées + résultats : T17 ciblé **53 passed** ; T41 ciblé **28 passed** ; `uv run python scripts/verify_schema.py` → **PASS verify_schema** ; `make verify-contracts` → succès (34 fixtures, 4 autotests, backlog 46 tickets / 12 gates / 87 catégories / 81 références) ; `uv run ruff format --check backend scripts` → **116 files already formatted**.

Invariant vérifié : traitements financiers testés en centimes entiers ; prorata borné ; confirmation/refus des liens versionnés et auditables ; restauration gardant une génération complète, avec schéma et origines `originals/` contrôlés.

Limites / cas non couverts : aucune suite complète exécutée ; pas de test opt-in réel ; pas de revue indépendante de T17 corrigé ; aucun gate validé par ce rapport.

Risques / sécurité / données privées : données bancaires réelles, secrets, `.env*`, `csv_references/`, `data/` et `tmp.zip` non lus ; tests sur fixtures synthétiques et bases temporaires. Modifications applicatives préexistantes laissées intactes.

Relecture recommandée : reviewer indépendant pour T17 N-1..N-6 ; reviewer OPS pour les constats G3 restants et m-G3C-04..06 ; orchestrateur pour évaluer G3/G4 après clôture des préconditions.
