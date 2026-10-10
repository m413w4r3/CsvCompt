# Revue indépendante de confirmation T17 — levée du BLOCK

Revue du correctif non commité (arbre de travail du 2026-10-10) qui répond à [`REVUE_T17.md`](REVUE_T17.md) (BLOCK sur `1066748`). Le relecteur n'a écrit ni T17 ni la première revue. Il ne corrige rien : les écarts sont documentés ici.

Copie isolée : `rsync` du dépôt vers `/tmp/revue_t17b`, sans `.git`, `.venv`, `node_modules`, `csv_references`, `data`, `tmp.zip` ni `nimbalyst-local`. Ensuite `uv sync`, puis purge des `__pycache__` copiés. Scripts jetables dans `/tmp/revue_t17b_scripts/` : `common.py`, `test_adv_t17.py`, `test_adv_t17_more.py`, `test_adv_t17_unique.py`, `fuzz_t17.py` et `schema_checks.py`, tous exécutés avec `python -I`. Seules des données synthétiques ont servi. `csv_references/`, `data/` et `tmp.zip` n'ont pas été ouverts.

## Verdict

**PASS sous réserves.** Le BLOCK est levé :

- les deux constats bloquants sont corrigés et prouvés ;
- les deux majeurs et le mineur initiaux sont traités.

Les invariants financiers ont été vérifiés sur 2 400 séquences aléatoires et par écritures SQL brutes. Résultat : aucun net négatif, aucun dépassement de plafond, aucune mutation bancaire, une idempotence respectée et une concordance exacte avec un oracle indépendant.

La revue relève toutefois **deux nouveaux constats MAJEURS de robustesse et de contrat**, sans corruption de données : SQLite échoue en mode fermé. Ils doivent être corrigés, avec tests, **avant l'ouverture du gate G4 et avant toute exposition REST de ces services (T21)** :

- **N-1** : une proposition peut devenir impossible à rejeter et bloquer définitivement le retrait.
- **N-2** : le prorata ADR-007 produit une part négative à partir de 4 catégories, et la confirmation lève une `IntegrityError` brute.

Ces deux corrections sont locales. Une courte re-vérification ciblée suffit : relancer les reproductions ci-dessous et le fuzz. Une nouvelle revue complète n'est pas nécessaire.

## Statut des constats initiaux

| Constat REVUE_T17 | Statut | Preuve |
|---|---|---|
| **BLOQUANT** — plafond par allocation ignorant le prorata | **Levé** | Parts figées dans `refund_link_allocation_parts` ([`refund_links.py:219`](/home/nill/Desktop/compte/mescompts/backend/app/services/refund_links.py:219), `_confirmed_allocation_usage` l. 273). Le trigger `trg_refund_links_confirmed_allocation_caps_insert/update` cumule les liens ciblés et les parts ([`0001_initial.sql:921`](/home/nill/Desktop/compte/mescompts/backend/app/db/migrations/sql/0001_initial.sql:921)). Scénario rejoué : achat −20 000, parts égales, prorata 10 000 puis ciblé 8 000. Résultat : `REFUND_TARGET_ALLOCATION_CAP_EXCEEDED`, net `{5000, 5000}`. Ordre inverse (ciblé puis prorata) : refus. Proposition ciblée créée **avant** la confirmation du prorata : refus à la confirmation. SQL brut, ciblé après des parts saturant l'allocation : `refund_link_target_allocation_cap_exceeded`. La mutation « refund target (allocation) cap » est détectée par `verify_schema.py`. |
| **BLOQUANT** — retrait ADR-027 absent | **Levé** | [`clear_allocations()`](/home/nill/Desktop/compte/mescompts/backend/app/services/allocations.py:524) écrit une décision `clear` (avant/après), un audit `allocation.retracted`, `retracted_at_utc` et `retracted_by_decision_id`. Il avance la version et donne `unclassified` avec un résidu égal au montant. Un rejeu à la version courante n'ajoute ni audit ni version. Une version périmée lève `VersionConflictError`. Les colonnes bancaires sont identiques avant et après. Une restauration du `before_json` par de nouvelles allocations (IDs distincts) fonctionne. Un clear et un remplacement concurrents (10 courses à 2 threads) donnent exactement un gagnant et un `VersionConflictError`. Un lien confirmé sur l'une ou l'autre jambe bloque le retrait. Après déplacement du lien par supersession, l'achat devient retirable et le crédit reste protégé. Réserve T20 : voir R-2. |
| **MAJEUR** — trigger inter-comptes absent | **Levé** | `s.account_id = t.account_id` figure dans les triggers d'INSERT et d'UPDATE. L'INSERT brut est refusé (test `MONEY_13`). L'UPDATE brut du `target_transaction_id` d'une proposition vers un autre compte est refusé (`refund_link_direction_invalid`, script `test_adv_t17_more.py`). La preuve par mutation ne couvre que le trigger d'INSERT : voir N-4. |
| **MAJEUR** — `docs/ALLOCATIONS.md` obsolète | **Levé** | Le document décrit maintenant le retrait, les liens par allocation, les parts matérialisées, l'unicité hors `rejected` et la limite « SQL direct hors contrat ». `DB.md` est aligné. Une phrase est contredite en pratique par N-1 : « Une proposition doit être rejetée » (l. 71-73). |
| **MINEUR** — IDs de matrice | **Levé** | `MATRICE_TESTS_ADDENDUM.md` définit MONEY-10 à MONEY-14 et les compléments T17 de KPI-03 et KPI-07. Les tests portent ces IDs (`test_MONEY_11_*`, `test_MONEY_12_*`, `test_MONEY_13_*`, `test_MONEY_14_*`, `test_KPI_03_*`, `test_KPI_07_*`). La concurrence et le rejeu des liens sont couverts par `test_MONEY_14_concurrent_confirmation_*` et `test_MONEY_14_concurrent_replacements_*`. |

## Nouveaux constats

### MAJEUR N-1 — Une proposition devient impossible à rejeter après réallocation d'une jambe ; le retrait est alors bloqué définitivement

`allocate()` ne refuse un remplacement que si un lien **confirmé** existe ([`allocations.py:463`](/home/nill/Desktop/compte/mescompts/backend/app/services/allocations.py:463)). Une proposition peut donc garder un `source_allocation_id` ou un `target_allocation_id` qui pointe vers une allocation supersédée.

Le trigger `trg_refund_links_confirmed_direction_update` ([`0001_initial.sql:722`](/home/nill/Desktop/compte/mescompts/backend/app/db/migrations/sql/0001_initial.sql:722)) s'exécute sur **tout** UPDATE d'un `refund_of`, y compris le passage `proposed → rejected`. Il exige des allocations actives. [`reject_refund_link()`](/home/nill/Desktop/compte/mescompts/backend/app/services/refund_links.py:615) lève alors une `IntegrityError` brute `refund_link_direction_invalid`, et non une `AppError`.

La proposition reste `proposed` pour toujours. `clear_allocations()` sur cette jambe renvoie alors indéfiniment `ALLOCATION_HAS_PROPOSED_REFUND_LINK`, ce qui contredit MONEY-12 et `ALLOCATIONS.md` : « Une proposition doit être rejetée ».

**Reproduction** : `test_stale_proposal_after_credit_reallocation_cannot_be_rejected` et `..._purchase_reallocation_...` dans `test_adv_t17.py`.

1. Proposer un lien d'un avoir de 5 000.
2. Réallouer le crédit en `refund 4000 + income_other 1000`.
3. `reject` → `IntegrityError: refund_link_direction_invalid`.
4. `clear` → `ALLOCATION_HAS_PROPOSED_REFUND_LINK`.

Même résultat côté achat avec une proposition ciblée. Le cas apparaît naturellement dans le fuzz : 39 occurrences sur 2 400 séquences.

**Piste** : à arbitrer par l'orchestrateur, sans correction par le relecteur.

- Dans `allocate()`, refuser `ALLOCATION_HAS_PROPOSED_REFUND_LINK` ou rejeter les propositions concernées avec audit.
- Restreindre les contrôles d'activité du trigger d'UPDATE aux transitions vers `proposed` ou `confirmed`.
- Ajouter le cas à MONEY-12 et MONEY-14.

### MAJEUR N-2 — `prorate_link` peut produire une part négative ou supérieure à la capacité de la dernière catégorie

[`prorate_link()`](/home/nill/Desktop/compte/mescompts/backend/app/domain/money_split.py:111) applique ADR-007 : arrondi demi loin de zéro, puis reste à la dernière catégorie. Il ne borne pas ce reste. À partir de **4 catégories**, les arrondis supérieurs cumulés dépassent le montant. Avec 3 catégories, l'erreur cumulée reste inférieure à 1 centime et le reste ne peut pas devenir négatif.

| Allocations expense (centimes) | Avoir | `prorate_link` |
|---|---|---|
| 4 × 2 500 | 2 | 1, 1, 1, **−1** |
| 3 000 / 3 000 / 3 000 / 1 000 | 5 | 2, 2, 2, **−1** |
| 7 × 1 | 4 | 1 ×6, **−2** |

Le service accepte la **proposition**. La confirmation lève ensuite une `IntegrityError` brute (`CHECK amount_cents > 0` de `refund_link_allocation_parts`), et non une `AppError`.

Symétriquement, pour 7 × 1 centime et un avoir de 3, le reste de 3 dépasse la capacité de 1 de la dernière catégorie. Le lien est refusé (`REFUND_TARGET_ALLOCATION_CAP_EXCEEDED`) alors que la capacité globale est de 7. La garde SQL empêche toute écriture erronée : aucune donnée n'est faussée. En revanche, le contrat de domaine (« une part peut valoir zéro », donc ≥ 0) est violé. Le test de propriété [`test_MONEY_10_prorata_properties_total_and_input_order`](/home/nill/Desktop/compte/mescompts/backend/tests/unit/domain/test_money_split.py:111) ne couvre que **2** catégories.

Fuzz : 41 confirmations ou remplacements terminés en `CHECK constraint failed` sur 2 400 séquences.

**Reproduction** : `test_prorate_link_negative_last_part[*]` et `test_prorate_residual_exceeds_last_capacity` dans `test_adv_t17.py`.

**ADR requis** : il s'agit d'une formule, donc pas de changement silencieux. Il faut définir la répartition prorata bornée, par exemple « plus forts restes » (Hamilton) avec départage par ID de catégorie, chaque part étant dans `[0, capacité]`. En attendant, rejeter à la proposition avec une `AppError` stable. Étendre la propriété à 1 à 7 catégories et à de petits montants.

### MINEUR N-3 — `replace_refund_link` sur une clé active existante lève une `IntegrityError` brute

Cas : remplacer un lien confirmé vers un achat où la même allocation source a déjà un lien `proposed` ou `confirmed` actif (même cible ou allocation cible). Le service ne vérifie pas l'index `uq_transaction_links_active` avant l'insertion ([`refund_links.py:787`](/home/nill/Desktop/compte/mescompts/backend/app/services/refund_links.py:787)). Il en résulte `IntegrityError: UNIQUE constraint failed: index 'uq_transaction_links_active'` au lieu de `REFUND_LINK_ALREADY_EXISTS`.

**Reproduction** : `test_adv_t17_unique.py`, 1 passed. Fuzz : 30 occurrences sur 2 400.

Comme pour N-1 et N-2, aucun gestionnaire global ne traduit `IntegrityError` dans `backend/app`. T21 renverrait donc une erreur 500.

### MINEUR N-4 — Garde-fous SQL des parts sans preuve par mutation ni test

Sept mutants survivent à `verify_schema(..., prove_mutations=False)` complet (`schema_checks.py`). Aucun test `backend/tests` n'asserte leurs messages :

- suppression de `trg_refund_link_allocation_parts_insert_guard`, `..._update_forbidden` ou `..._delete_forbidden` ;
- suppression de `trg_refund_links_confirmed_attribution_insert` ou `..._update` ;
- retrait de `s.account_id = t.account_id` dans le trigger d'**UPDATE** ;
- retrait de la branche « parts » de `trg_allocations_referenced_links_immutable`.

Le comportement réel est correct : les écritures brutes sont refusées avec `refund_link_part_target_invalid`, `refund_link_part_immutable`, `refund_link_part_delete_forbidden`, `refund_link_attribution_total_mismatch`, `refund_link_direction_invalid` et `allocation_referenced_by_confirmed_refund_link`. Mais la garantie revendiquée par `ALLOCATIONS.md` (« SQLite vérifie positivité, appartenance, somme exacte… ») n'est pas protégée contre une régression.

**Piste** : ajouter ces mutants à `verify_constraint_mutation_proofs`.

### MINEUR N-5 — Machine d'états des liens non imposée par SQL

Un UPDATE brut `rejected → confirmed` d'un lien ciblé est accepté, sans audit. Les plafonds restent respectés et le net reste ≥ 0. `ALLOCATIONS.md` ne déclare « hors contrat » que l'écriture directe de **parts**.

**Piste** : ajouter un trigger de transition (`proposed → confirmed|rejected` uniquement), à l'image d'IMP-15, ou documenter la limite dans un ADR.

### MINEUR N-6 — Le prorata ignore la capacité restante (refus conservateurs)

La répartition et la distribution intra-catégorie, par ID d'allocation croissant, utilisent la capacité **brute** des allocations, pas la capacité restante.

- Achat 10 000 + 10 000 avec un lien ciblé de 8 000 sur la première allocation : tout prorata dont la part de cette catégorie dépasse 2 000 est refusé. Après un prorata de 4 000, même un avoir de 2 centimes est refusé, alors que 8 000 restent disponibles.
- Deux allocations de la même catégorie, la première saturée : un prorata est refusé même si la seconde a de la place.

Le comportement est sûr (échec fermé) et déterministe, mais non documenté. Il est à préciser dans `ALLOCATIONS.md`, éventuellement dans l'ADR de N-2.

## Vérifications adversariales (résumé)

- **Variantes du scénario BLOCK** (`test_adv_t17.py`, 26 passed). Toutes affirment le comportement constaté :
  - arrondis impairs : 3 catégories à 3 334 / 3 333 / 3 334 avec des avoirs de 1, 2 et 10 001 ;
  - allocation à 1 centime (1 / 9 999) ;
  - 7 catégories ;
  - montants d'environ 10¹⁵ centimes, en entiers stricts ;
  - avoir supérieur à l'achat, multi-achats, plafond source ;
  - rejet puis nouvelle proposition ;
  - remplacement prorata → ciblé → autre achat, puis retrait de l'achat libéré.
  - Dans chaque cas, les parts totalisent le lien et chaque net est ≥ 0, avec `Σ net = Σ expense − Σ liens`.
- **SQL brut contournant le service.** Refusés : part dépassant une allocation, somme des parts ≠ lien, part sur l'allocation d'un autre achat, lien prorata inséré directement `confirmed`, part ajoutée après confirmation, UPDATE ou DELETE de part, lien ciblé sur une allocation saturée par des parts, retrait d'une allocation référencée par des parts, UPDATE inter-comptes.
  - Accepté, comme documenté : une attribution non proportionnelle dans les plafonds.
  - Accepté, non documenté : N-5.
  - Garantie restante hors trigger : le respect de la formule ADR-007 (documenté). PRAGMA `foreign_keys=1` et `recursive_triggers=1` vérifiés sur la connexion applicative. Sur une connexion `sqlite3` nue (`foreign_keys=0`), les triggers des parts restent actifs.
- **Propriété** (`fuzz_t17.py`, graines 11, 22, 33 et 44 × 600 = **2 400 séquences**, 8 à 22 opérations chacune) :
  - **Opérations tirées** : achats ventilés de 1 à 7 catégories, complets ou partiels ; crédits `refund` ou `refund + income_other` ; propositions ciblées ou prorata avec des montants limites ; confirmations, rejets, remplacements, retraits, réallocations, rejeux, versions périmées.
  - **Vérifié après chaque opération** : plafonds par allocation, source et achat ; références actives ; parts égales à un **oracle prorata indépendant** ; `net_effect_by_category` égal à un recalcul SQL indépendant ; nets ≥ 0 ; résidu exact ; colonnes bancaires inchangées ; au plus un audit par mutation ; aucun audit sur un refus ou un rejeu.
  - **Résultat : 0 violation.** Des `IntegrityError` brutes subsistent (N-1 : 39, N-2 : 41, N-3 : 30).
  - **Volume** : 1 339 confirmations, 579 remplacements, 1 248 retraits, 1 788 rejeux idempotents et 857 conflits de version attendus.
- **Concurrence.** Deux confirmations prorata se disputent la même capacité, 15 fois : chaque fois un `ok` et un `REFUND_TARGET_CAP_EXCEEDED`. Les nets restent ≥ 0.
- **Migration.**
  - `schema.sql` et `0001_initial.sql` sont identiques.
  - Les colonnes du modèle `RefundLinkAllocation` correspondent à la migration.
  - 29 tables dans les modèles comme dans la migration. Les valeurs codées en dur sont à jour (`test_models.py:124`, `test_schema_parity.py:66`). `OWNED_TABLES` contient la nouvelle table.
  - Après un upgrade puis un `downgrade base` sur une base vide, il ne reste que `alembic_version`.
- **INV-02.** Aucun `float` ni `Decimal` dans `allocations.py`, `refund_links.py` et `money_split.py`. Tous les montants sont des `int` stricts.
- **INV-11.** Messages d'erreur statiques. Les audits ne contiennent que des UUID, des catégories, des montants et des dimensions : aucun libellé ni numéro. Le texte d'une `IntegrityError` contient le SQL et des UUID, sans libellé.

## Réserves (hors bloquant)

- **R-1** : corriger N-1 et N-2 avant G4/T21 ; N-3 à N-6 peuvent être consignés au registre des risques.
- **R-2 (T20)** : la restauration du `before_json` d'un `clear` fonctionne avec `origin=manual`. Une allocation d'origine `rule` ne peut pas être restaurée comme `rule` : `allocate(origin="rule")` lève `RULE_REQUIRES_UNCLASSIFIED_TRANSACTION`, car une décision existe. La restauration crée une décision `confirm`, pas `undo`, sans `undone_by_decision_id`. T20 doit fixer l'origine restaurée et le chaînage de l'annulation.
- **R-3** : la sémantique de rejeu diffère. Les allocations exigent la version courante ; les liens tolèrent `expected + 1`. Les deux comportements sont documentés ; à harmoniser dans le contrat T21.
- **R-4** : `allowed_paths` T17 du backlog ne cite que `allocations.py`. Le correctif touche le schéma, les modèles, `refund_links.py`, `verify_schema.py` et les docs, ce qu'autorise ADR-027 (§ Conséquences). À noter dans `ETAT_IMPLEMENTATION.md`.

## Couverture par rapport au backlog T17

| Exigence | État |
|---|---|
| test_refs MONEY-01/02/03/07/08 | Couverts par des tests nommés, qui passent. |
| Acceptance « splits signés exacts, types admissibles, versions optimistes » | Couverte, dont la concurrence (INV-08, MONEY-14). La propriété prorata ne couvre que 2 catégories (N-2). |
| proof_required « diff et contrats mis à jour » | Oui. Une phrase de `ALLOCATIONS.md` est contredite par N-1. |
| « tests effectivement exécutés » | Oui, voir ci-dessous. |
| « revue indépendante » | REVUE_T17 et la présente confirmation. |
| « risques résiduels » | N-1 à N-6 et R-2 à R-4, à reporter dans `REGISTRE_RISQUES.md`. |
| Manque pour un PASS sans réserve | Corrections et tests de N-1 et N-2. ADR pour la répartition prorata bornée. Mutants N-4 ajoutés à `verify_schema.py`. Traduction en `AppError` de N-3. |

## Commandes exécutées (dans `/tmp/revue_t17b`)

| Commande | Résultat |
|---|---|
| `uv run pytest -q backend/tests -m 'not opt_in_real_data'` | **351 passed, 1 failed, 2 skipped, 4 deselected**. L'échec est **hors T17** : `test_quality_imports.py::test_scenario_famille_2026_fixture_is_committed_and_read_as_coverage`, qui attend `completeness == partial` et obtient `not_started` (lot imports/couverture d'une autre session). |
| `uv run pytest -q` sur les fichiers T17 (`test_money_split`, `test_allocations_service`, `test_refund_links_service`, `test_schema_parity`, `test_models`) | **50 passed** |
| `uv run ruff check backend scripts` | All checks passed |
| `uv run ruff format --check backend scripts` | 2 fichiers à reformater, **hors T17** : `services/import_commit.py`, `services/imports.py` |
| `uv run mypy` | Success: no issues found in 58 source files |
| `uv run python scripts/verify_schema.py` | PASS verify_schema, preuves de mutation incluses |
| `uv run python scripts/verify_metrics_examples.py` | PASS : 29 oracles, 8 splits, 2 cas de priorité, 2 liens rejetés aux plafonds |
| `uv run python -I -m pytest -s --rootdir=/tmp/revue_t17b_scripts /tmp/revue_t17b_scripts/test_adv_t17.py` | 26 passed (contre-exemples N-1, N-2 et N-5 affirmés) |
| `… test_adv_t17_more.py` / `… test_adv_t17_unique.py` | 2 passed / 1 passed |
| `uv run python -I /tmp/revue_t17b_scripts/fuzz_t17.py <seed> 600 <tmp>` pour les graines 11, 22, 33 et 44 | 2 400 séquences, 0 violation, `IntegrityError` N-1/N-2/N-3 comptées |
| `uv run python -I /tmp/revue_t17b_scripts/schema_checks.py` | Downgrade propre, parité OK, 7 mutants survivants (N-4) |

## Retour au format AGENTS.md

**Ticket :** T17 (confirmation de la levée du BLOCK)

**Objectif livré :** revue indépendante adversariale du correctif. Verdict **PASS sous réserves** : BLOCK levé, N-1 et N-2 à corriger avant G4/T21.

**Fichiers changés :** `docs/revues/REVUE_T17_CONFIRMATION.md` uniquement dans le dépôt réel. Scripts et bases jetables sous `/tmp/revue_t17b_scripts/` et `/tmp/revue_t17b/`.

**Interfaces modifiées :** aucune.

**Tests ajoutés :** aucun dans le dépôt. Tests et fuzz jetables dans `/tmp/revue_t17b_scripts/`.

**Commandes réellement exécutées + résultats :** voir le tableau ci-dessus.

**Invariant vérifié :**

- INV-02 : entiers stricts, ni `float` ni `Decimal`.
- Plafonds ADR-027 et ADR-024.3 par allocation, source et achat, y compris le cumul prorata + ciblé, au niveau du service et de SQL.
- Nets catégoriels ≥ 0 et `Σ = coût − liens` (KPI-03, KPI-07).
- Transaction bancaire jamais modifiée.
- Retrait auditable, versionné et idempotent (MONEY-12).
- Garde inter-comptes à l'INSERT comme à l'UPDATE (MONEY-13).
- INV-11 : audit et erreurs sans libellé.

**Limites / cas non couverts :**

- Pas de test d'API : pas d'endpoint T17, relève de T21.
- Annulation T20 non testée au-delà d'une restauration manuelle.
- Le fuzz ne couvre ni plusieurs comptes, ni le filtre `account_ids`, ni la concurrence multi-processus.

**Risques / sécurité / données privées :**

- Aucun fichier réel, libellé réel ou secret n'a été lu ni écrit.
- Risques fonctionnels :
  - proposition impossible à rejeter et retrait bloqué (N-1) ;
  - `IntegrityError` remontant comme erreur 500 une fois exposée (N-1 à N-3) ;
  - formule prorata non bornée (N-2) ;
  - garde-fous des parts sans preuve de non-régression (N-4).

**Relecture recommandée :**

- Corriger N-1, N-2 et N-3 avec des tests MONEY-11, MONEY-12 et MONEY-14 nommés.
- Rédiger l'ADR de la répartition prorata bornée (N-2, N-6).
- Ajouter les mutants N-4.
- Décider pour N-5.
- Relancer `test_adv_t17*.py` et `fuzz_t17.py` : on attend 0 `IntegrityError` hors SQL brut.

**Hypothèses :** ADR-027 et l'addendum MONEY-10 à MONEY-14 font foi. Les échecs imports/format appartiennent aux sessions parallèles.

**Erreurs détectées :** N-1 à N-6 (voir ci-dessus). Aucune violation d'invariant financier.

**Instructions de reproduction :**

1. Recréer la copie avec la commande `rsync` de la mission, puis `uv sync`.
2. Supprimer les `__pycache__` copiés.
3. Exécuter les commandes du tableau. Les scripts sont dans `/tmp/revue_t17b_scripts/` ; leurs scénarios clés sont décrits dans N-1 à N-6 pour pouvoir les réécrire s'ils disparaissent.
