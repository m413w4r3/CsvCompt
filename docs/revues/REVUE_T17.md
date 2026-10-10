# Revue indépendante T17 — checkpoint `1066748`

## Verdict

**BLOCK** — ne pas valider T17 ni ouvrir le gate G4. Un mélange de liens proratisés et ciblés peut dépasser le plafond d’une allocation et produire une consommation négative sur cette allocation d’achat. Le retrait auditable prévu par ADR-027 reste également absent du service.

La revue porte sur l’archive propre de `1066748`. Les essais ont utilisé des comptes, opérations et catégories synthétiques dans `/tmp/revue_t17/`. `csv_references/`, `data/` et `tmp.zip` n’ont pas été ouverts. Aucun code ni test n’a été modifié dans le dépôt réel par cette revue.

## Constats

### BLOQUANT — Le plafond d’allocation cible ignore les remboursements proratisés

Dans le service, le cumul utilisé pour vérifier une allocation cible ne sélectionne que les liens dont `target_allocation_id` vaut exactement cette allocation. Un lien proratisé a `target_allocation_id = NULL`, donc sa part imputée à cette catégorie est omise. Les triggers d’insertion et de mise à jour font le même cumul limité aux liens ciblés explicitement. Voir [`refund_links.py`](/home/nill/Desktop/compte/mescompts/backend/app/services/refund_links.py:236) et [`0001_initial.sql`](/home/nill/Desktop/compte/mescompts/backend/app/db/migrations/sql/0001_initial.sql:789).

**Reproduction** : `uv run --locked --group dev pytest -q test_review_t17_adversarial.py` depuis `/tmp/revue_t17/` donne `2 passed`. Le test `test_MONEY_03_target_allocation_cap_omits_prior_prorata_attribution` crée un achat de −20 000 centimes, alloué à parts égales à `groceries_supermarket` et `restaurants`, puis confirme :

1. un lien proratisé de 10 000 centimes, soit 5 000 sur chaque allocation ;
2. un lien ciblé de 8 000 centimes sur l’allocation `groceries_supermarket` de 10 000.

Les deux liens sont acceptés. `net_effect_by_category()` renvoie `groceries_supermarket: -3000` et `restaurants: 5000`. La catégorie ciblée a donc reçu 13 000 centimes de remboursement sur une allocation de dépense de 10 000. Le total global reste arithmétiquement égal à 2 000, ce qui masque la violation au test de somme. Ce cas contredit le plafond par allocation d’ADR-027 et les traitements décrits dans [`METRICS.md`](/home/nill/Desktop/compte/mescompts/docs/METRICS.md:20).

La vérification `verify_schema.py` passe, mais ses cas de plafond testent séparément un lien ciblé ou le plafond global ; elle ne couvre pas le cumul entre lien proratisé et lien ciblé. Le test existant [`test_MONEY_10_refund_source_and_target_allocation_caps_are_enforced`](/home/nill/Desktop/compte/mescompts/backend/tests/integration/test_refund_links_service.py:253) échoue à dépasser le plafond global avant de prouver le plafond propre à l’allocation ciblée.

### BLOQUANT — Le retrait auditable promis par ADR-027 n’est pas implémenté

Le schéma et les modèles portent `retracted_at_utc` et `retracted_by_decision_id`, et les triggers encadrent ce retrait. Pourtant [`clear_allocations()`](/home/nill/Desktop/compte/mescompts/backend/app/services/allocations.py:510) renvoie toujours `ALLOCATION_CLEAR_UNSUPPORTED_SCHEMA` dès qu’il existe une allocation active. ADR-027 demande explicitement le retrait et indique que T17 adapte `clear_allocations`; ce retrait doit permettre l’état non ventilé pour l’annulation T20. Le test [`test_clear_allocations_reports_schema_limit_without_mutating_active_rows`](/home/nill/Desktop/compte/mescompts/backend/tests/integration/test_allocations_service.py:370) confirme l’ancien comportement au lieu de couvrir le contrat accepté.

Les corrections par remplacement restent historisées, mais il n’existe pas de chemin de service pour retirer une allocation sans la remplacer, créer la décision et l’audit correspondants, puis recalculer le résidu. La compatibilité de l’annulation T20 (« rollback décision, pas transaction source ») n’est donc pas établie.

### MAJEUR — Le schéma accepte des liens inter-comptes que le service rejette

[`_load_pair()`](/home/nill/Desktop/compte/mescompts/backend/app/services/refund_links.py:110) exige que les deux transactions appartiennent au même compte. Le trigger `trg_refund_links_confirmed_direction_insert` vérifie le sens, les allocations actives et les catégories, mais ne compare pas les `account_id` source et cible ([`0001_initial.sql`](/home/nill/Desktop/compte/mescompts/backend/app/db/migrations/sql/0001_initial.sql:682)).

**Reproduction** : le second test scratch `test_ADR_021_raw_database_trigger_accepts_cross_account_refund_link` insère directement via le modèle SQLAlchemy un lien `confirmed` entre deux comptes distincts. L’insertion passe les triggers ; le lecteur analytique l’ignore ensuite et le signale comme hors périmètre. Le service rejette ce lien avec `REFUND_SCOPE_MISMATCH` (test synthétique existant : [`test_MONEY_10_same_account_scope_proposed_rejected_and_confirmed_supersession`](/home/nill/Desktop/compte/mescompts/backend/tests/integration/test_refund_links_service.py:411)). Il manque donc une défense schéma cohérente avec le contrat et le service.

### MAJEUR — `docs/ALLOCATIONS.md` décrit encore l’ancien schéma

Les sections « Limites du schéma v1 à arbitrer » affirment que `source_allocation_id` n’existe pas, qu’un seul crédit `refund` est permis, que le retrait auditable est impossible et que les liens rejetés bloquent une nouvelle proposition ([`ALLOCATIONS.md`](/home/nill/Desktop/compte/mescompts/docs/ALLOCATIONS.md:110)). Ces affirmations contredisent ADR-027, les colonnes et index de la migration, les modèles et les services. Le document dit aussi que le schéma n’est pas modifié par T17. La preuve « diff et contrats mis à jour » du backlog n’est donc pas satisfaite.

### MINEUR — Les preuves de versions et de remboursements ne sont pas toutes reliées à des IDs de matrice

La matrice contient MONEY-01 à MONEY-09, mais aucun ID `ALLOC-*` ou `REF-*`. Les tests de plafonds, prorata multi-catégories, périmètre et supersession utilisent `MONEY-10`, ID absent de [`MATRICE_TESTS.md`](/home/nill/Desktop/compte/mescompts/.nimbalyst/plan_implementation_comptes_familiaux/docs/MATRICE_TESTS.md:23). Ils ne peuvent donc pas être comptés comme tests nommés par un ID valide de la matrice.

Le backlog T17 référence MONEY-01/02/03/07/08 ([`backlog.yaml`](/home/nill/Desktop/compte/mescompts/.nimbalyst/plan_implementation_comptes_familiaux/backlog.yaml:439)). Ces cas ont des tests nommés. En revanche, le stale-version et la concurrence d’allocations sont couverts par des tests nommés `test_approved_rule_only_applies_to_a_virgin_transaction_and_stale_version_conflicts` et `test_INV_08_concurrent_sessions_produce_one_stale_version_conflict`, sans ID de matrice. Aucun test du lot ne vérifie le conflit de version ou la concurrence sur confirmation/remplacement de liens.

## Couverture et preuves T17

| Critère / preuve | État constaté |
|---|---|
| MONEY-01 — split signé exact, somme conservée | Couvert pour le split `-10000` 70/30 et l’allocation complète/idempotente ; vecteurs ADR-007 également vérifiés par tests hors identifiant matrice. |
| MONEY-02 — rejet atomique d’un ratio invalide | Couvert par test de domaine et vérification d’absence d’écriture. |
| MONEY-03 — remboursement simple et flux bancaire | Couvert pour un avoir sur une catégorie ; ne couvre pas les plafonds mélangés prorata/ciblé. |
| MONEY-07 / MONEY-08 — allocations personnelles et retrait liquide | Cas synthétiques valides et catégories/directions incorrectes testés. Le test MONEY-08 ne prouve pas le retrait auditable d’une allocation d’achat. |
| Types admissibles et signes | Partiellement couvert par catégories positives/négatives et rejets de flux incompatibles ; pas de propriété exhaustive du contrat T16/T17. |
| Version optimiste et concurrence | Code sérialise avec `write_transaction()` et tests d’allocations couvrent une version périmée et deux écritures concurrentes ; ces tests ne portent pas un ID de matrice. La concurrence des liens n’est pas testée. |
| Plafonds source/cible par allocation, prorata et supersession | Scénarios dédiés présents sous l’ID inexistant MONEY-10 ; le mélange prorata/ciblé reproduit un dépassement. |
| Retrait d’allocation auditable et annulation T20 | Non couvert au niveau service ; le test existant attend le refus. Le schéma seul est testé par `verify_schema.py`. |
| KPI-03 / KPI-07 cités par ADR-024.3 | Pas de test nommé KPI-03/KPI-07 dans le lot T17. Le test de service proraté vérifie la somme catégorielle, pas les contrats de vues/drilldown du lot analytique. |
| Diff et contrats mis à jour | **Échec** : `docs/ALLOCATIONS.md` contredit ADR-027 et le schéma checkpoint. |
| Tests exécutés, revue indépendante, risques | Tests exécutés ci-dessous ; cette revue fournit la revue indépendante et les risques résiduels. |

La migration `0001_initial.sql` et `docs/examples/schema.sql` sont identiques. Les modèles exposent les colonnes d’allocation source/cible et de retrait. Les fonctions de split et de plafonnement utilisent des entiers stricts ; l’arrondi ADR-007 est entier et demi loin de zéro. Aucun usage de `float` pour l’argent n’a été observé dans les fichiers T17 examinés.

## Commandes exécutées

Dans la copie isolée construite par `git archive 1066748 | tar -x -C /tmp/revue_t17` :

- `uv run --locked --group dev pytest backend/tests/unit/domain/test_money_split.py backend/tests/integration/test_allocations_service.py backend/tests/integration/test_refund_links_service.py` — **31 passed**, avertissement de dépréciation Starlette/httpx.
- `uv run --locked --group dev pytest -q test_review_t17_adversarial.py` — **2 passed** ; ces tests passent en affirmant les contre-exemples décrits plus haut, pas en validant le comportement métier.
- `uv run --locked --group dev python scripts/verify_schema.py` — **PASS verify_schema**, incluant les tests de mutation du schéma.
- `uv run --locked --group dev python scripts/verify_metrics_examples.py` — **PASS**, 29 oracles métriques, 8 splits et autres cas annoncés par le script.

Les chemins relatifs ci-dessus sont relatifs à `/tmp/revue_t17/`. Aucun test n’a lu les sources de données réelles. Le fichier de test adversarial et l’environnement `uv` ne sont que dans `/tmp/revue_t17/`.

## Retour au format AGENTS.md

**Ticket :** T17

**Objectif livré :** Revue indépendante adversariale du checkpoint `1066748`; verdict **BLOCK**.

**Fichiers changés :** `docs/revues/REVUE_T17.md` uniquement dans le dépôt réel. Tests temporaires sous `/tmp/revue_t17/`.

**Interfaces modifiées :** Aucune.

**Tests ajoutés :** Aucun dans le dépôt. Deux tests scratch pour le plafond combiné prorata/ciblé et l’acceptation d’un lien inter-comptes par trigger.

**Commandes réellement exécutées + résultats :** Voir « Commandes exécutées » ci-dessus. Les 31 tests existants passent ; les contre-exemples adversariaux reproduisent les deux écarts et `verify_schema.py` passe sans les détecter.

**Invariant vérifié :** Les calculs de split observés restent en centimes entiers, signés, exacts et ordonnés selon ADR-007. Le trigger global de remboursements conserve le plafond global d’achat dans le cas testé ; le plafond par allocation n’est pas conservé pour les liens proratisés combinés à des liens ciblés.

**Limites / cas non couverts :** Pas de test de course entre confirmations de liens, de répétition idempotente des mutations de lien, ni d’annulation de retrait au travers du service T20. KPI-03/KPI-07 relèvent de tests ultérieurs et ne sont pas prouvés ici.

**Risques / sécurité / données privées :** Aucun fichier réel, libellé réel, compte réel ou secret n’a été lu ou ajouté. Risques financiers principaux : dépassement par catégorie et absence de retrait service auditable. Risque d’intégrité additionnel : le schéma laisse passer les liens inter-comptes créés hors service.

**Relecture recommandée :** Corriger le plafond cumulé proratisé dans service et triggers avec tests ciblés, implémenter le retrait ADR-027 et le chemin de reprise T20, aligner `docs/ALLOCATIONS.md`, puis ajouter les IDs MONEY/ALLOC/REF correspondants à la matrice et au backlog avant nouvelle revue indépendante.

**Hypothèses :** ADR-027 acceptée dans `docs/DECISIONS.md` prévaut sur les passages plus anciens de `docs/ALLOCATIONS.md`.

**Erreurs détectées :** Dépassement par allocation via prorata + lien ciblé ; `clear_allocations()` reste bloqué ; trigger inter-comptes moins strict que le service ; contrat documentation obsolète ; IDs `MONEY-10` absents de la matrice.

**Instructions de reproduction :** Recréer la copie par `git archive 1066748 | tar -x -C /tmp/revue_t17`, puis exécuter les quatre commandes listées plus haut. Le test scratch `test_review_t17_adversarial.py` est présent dans cette copie temporaire.
