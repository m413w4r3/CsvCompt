# Revue indépendante T21 — API de revue groupée et décisions

- Relecteur : session indépendante (non auteure du code). Copie isolée `/tmp/revue_t21` (sans `.git`, `data/`, `csv_references/`, `tmp.zip`), scripts jetables dans `/tmp/revue_t21_scripts` et fichiers `test_zz_attack*.py` ajoutés **uniquement dans la copie**. Aucun fichier du dépôt modifié hors de ce rapport.
- Code revu : `backend/app/api/v1/review.py` (non commité dans l'arbre de travail), `backend/tests/integration/test_review_api.py`, services T17/T19/T20 appelés.
- Référentiel : backlog T21 (RULE-05), `docs/REVIEW.md`, `docs/API.md` (revue), ADR-025, 027..036, REVIEW-01..12.

## Verdict : **PASS sous réserves**

Aucun constat BLOQUANT. Les gardes financières critiques (portée, atomicité, versions, priorité humaine, idempotence, entiers, curseur, confidentialité) tiennent sous attaque. Deux constats MAJEURS de cohérence fonctionnelle (action `ignore` sans effet observable ; détail de groupe incohérent avec la file filtrée) et des lacunes de tests (3 mutants survivants) doivent être traités ou consignés avant la clôture de G5/T22.

## Baseline exécutée (copie)

| Commande | Résultat |
|---|---|
| `uv run pytest backend/tests/integration/test_review_api.py` | 14 passed |
| `uv run pytest backend/tests` (suite complète) | EXIT 0, tout vert |
| `uv run ruff check backend` | All checks passed |
| `uv run mypy backend/app` | Success, 69 fichiers |
| `make verify-contracts` | OK (schéma, fixtures, 46 tickets / 12 gates) |

## Constats

### MAJ-01 — `ignore` ne change aucun état observable (réponse « ignored », file inchangée)
- Preuve (`test_zz_attack.py::test_A4_ignore`) : `POST /review/decisions` `action=ignore` sur une transaction sans suggestion → `200 {"status":"ignored"}`, version 1→2, décision `reject` écrite ; puis `GET /review/queue` → groupe toujours `unclassified`, `transaction_count=2`.
- Cause : `_status()` ne renvoie `ignored` que si la dernière suggestion est `rejected` ; `_write_marker` ne rejette que les suggestions `review` existantes. Sans suggestion, rien ne matérialise l'ignorance.
- Impact : l'utilisateur croit avoir écarté l'opération ; elle reste en tête de file ; une version est consommée (conflits UI parasites). L'annulation (`undo`) du marqueur fonctionne (200, `unclassified`).
- Recommandation : persister explicitement l'état « ignoré » (suggestion `rejected` synthétique ou statut dérivé de la décision `reject` la plus récente) et ajouter le test de file correspondant.

### MAJ-02 — Détail de groupe recalculé sans filtres : membres ≠ groupe de la file filtrée
- Preuve (`test_A8_detail_vs_filter`) : file filtrée sur `period_start=period_end=2026-05-02` → 1 groupe (`transaction_count=1`, `amount_cents=-1234`) ; `GET /review/queue/{id}` du même identifiant → 2 transactions (`no-store` présent). L'identifiant ne dépend que du `payee|statut|drapeaux`, donc il est identique entre vues filtrées et non filtrées.
- Impact : viole « agrégats = Σ membres » vu par l'utilisateur ; la page de revue (T22) affichera des membres hors filtre. Aucune écriture indirecte (les actions exigent une sélection explicite), d'où MAJEUR et non BLOQUANT.
- Recommandation : accepter les mêmes filtres sur le détail (et les lier à l'identifiant ou au curseur), ou documenter que le détail est non filtré et exposer `transaction_count` détail vs file.

### MIN-01 — Lacunes de tests révélées par les mutants (voir section Mutants)
Gardes sans test tueur : refus `HUMAN_DECISION_PROTECTED` d'`ignore`/`mark_for_review` sur transaction allouée (M4) ; doublon d'identifiant dans la sélection → `REVIEW_INVALID` (M8) ; version périmée d'une règle future avant simulation (M11). Absents aussi : REVIEW-12 « ≥500 séquences seedées » (le test « 500 transactions » est une sélection unique, pas une propriété), concurrence REVIEW-05/09, double undo / undo après réallocation, falsification de curseur, marchand ambigu, lot de règles via l'API, `ignore`/`mark_for_review`, undo d'un lot partiellement obsolète.

### MIN-02 — Détail de groupe O(N) sans pagination ni borne
- Preuve (`test_B7_perf_detail`) : groupe de 1 000 membres → 3,1 s, 386 ko ; file de 1 000 → 0,11 s. Une évaluation du moteur par membre ; le détail reconstruit toute la file. Un groupe de 10 000 dépasserait ~30 s. REVIEW-12 ne couvre que la file.
- Recommandation : paginer/limiter le détail ou évaluer à la demande ; ajouter un test de performance du détail.

### MIN-03 — `docs/API.md` : exemple d'annulation non conforme au contrat réel
Exemple `{"expected_version":2,"reason":"…"}` → réponse avec `audit_event_id`. Le code exige `actor` (`extra="forbid"` rejette `reason`) et répond `decision_id`, `undone_decision_id`, `transaction_id`, `version`, `status`, `idempotent`. Aucun test de contrat ne couvre ces exemples revue (le test de contrat ADR-025 ne les inclut pas). À aligner.

### MIN-04 — `suggestion_id` écrase silencieusement `allocations` et perd la provenance
`parts` fournies sont remplacées par le candidat sans erreur ; les dimensions (bénéficiaire, cadence…) du candidat ne sont pas reportées (`unknown`) ; la décision n'est pas liée à la suggestion (`suggestion_id` absent de la décision d'allocation). Le candidat est désormais filtré par le moteur quand une décision humaine existe (`SUGGESTION_NOT_FOUND`, `test_B6`), donc pas d'écrasement humain. Recommandation : rejeter la requête si les deux sont fournis ; tracer la suggestion confirmée.

### MIN-05 — Idempotence non atomique hors `/decisions` (risque résiduel, aucune double écriture prouvée)
Undo, rule-batch undo et refund-links : recherche de rejeu → service (transaction propre) → enregistrement de la réponse (transaction séparée). Preuves : crash simulé après create/confirm (`test_B4`, `test_B9`) → 1re réponse 500 ; rejeu même clé → 200 sans doublon (1 lien) grâce à l'idempotence des services ; 6 undo concurrents même clé → 6×200, 1 seule décision d'annulation (`test_B5`) ; 6 décisions concurrentes même clé → 6×200, état unique. Le verrou `_future_rule_lock` ne protège que le même processus. Acceptable en v1 mono-processus ; à documenter.

### MIN-06 — `mark_for_review` crée une suggestion manuelle à chaque appel
Deux appels (clés différentes) → deux `ClassificationSuggestion` (`test_A4`). Pas d'unicité ni de borne. Recommandation : réutiliser la suggestion `review` existante.

### MIN-07 — `future_rule` : limites de conception
- `ambiguous_reason` n'est jamais lu depuis `rule` : une règle justifiée sur marchand ambigu est impossible via l'API (conservateur, sûr ; refus `AMBIGUOUS_MARKETPLACE_GLOBAL_SCOPE` 409 confirmé pour un motif contenant le jeton, `test_B1`).
- La règle n'est pas tenue de couvrir `transaction_id` ; la simulation (≤10 000 lignes) est stockée en entier dans l'enregistrement d'idempotence.
- Brouillon retrouvé après interruption par comparaison acteur/horodatage/contenu : deux requêtes distinctes de même contenu dans la même milliseconde pourraient adopter la même règle (sans conséquence monétaire).

### MIN-08 — Périmètre de fichiers
`allowed_paths` T21 = `backend/app/api/v1/review.py` ; l'enregistrement du routeur (`api/v1/__init__.py`, `main.py`) et la documentation sont hors liste : à valider par l'orchestrateur (modifications nécessaires, non suspectes). `docs/ETAT_IMPLEMENTATION.md` indique encore T21 « à faire ».

## Attaques et résultats (preuves reproductibles : `test_zz_attack.py`, `test_zz_attack2.py`)

| # | Attaque | Résultat |
|---|---|---|
| 1 | `this_transaction` + voisin même contrepartie/montant | voisin inchangé (test existant + `B3` : voisin hors sélection `(1, ())`) ; `selection` non vide hors portée `selection` → 422 |
| 1 | IDs inexistants / doublons / sélection vide / `selection` sous `this_transaction` | 404 `TRANSACTION_NOT_FOUND` / 422 `REVIEW_INVALID` / 422 / 422 |
| 1 | `future_rule` | 0 allocation, 0 exécution, règle `draft`, `applied=false` ; motifs `*`, `**`, vide ou direction seule refusés 422 ; jeton marketplace refusé 409 |
| 2 | 3e élément invalide (`-99` pour `-100`) | 422, aucune des 3 transactions modifiée (atomicité, `A2`) |
| 2 | Montants `-10^30`, `-2^63`, `-2^53`, version `10^30`/`2^70`, float `-10001.0` | 422/409, jamais 500 ; montants hors 2^53 en sortie → `null` + `total_out_of_range` (`B8`) |
| 3 | Undo d'une décision antérieure après réallocation | 409 `UNDO_HUMAN_DECISION_CONFIRMATION_REQUIRED`, sans écriture |
| 3 | Double undo, clé différente | 200 identique, aucune décision supplémentaire |
| 3 | Engine/suggestion sur transaction humaine | candidat masqué, `human_decision_protected=true`, confirmation → 404 |
| 4 | Version périmée | 409 `VERSION_CONFLICT` / `REVIEW_SELECTION_CONFLICT` avec IDs+versions |
| 4 | Même clé, corps ou chemin différent | 409 `IDEMPOTENCY_CONFLICT` ; rejeu identique, une seule décision |
| 5 | Curseur falsifié / limite changée / garbage / réutilisé après fin | 422 `CURSOR_INVALID` ×3 / 409 `CURSOR_STALE` |
| 6 | Libellé/motif dans audits, idempotence, erreurs | absents (`B2`, `A11`) ; entrées hostiles de règle → 422 typés, jamais 500 |
| 6 | Origin | `evil.example`, `null` → 403 `ORIGIN_NOT_ALLOWED` ; `localhost:5173` → 200 ; `no-store` sur file et détail |

## Mutants (copie, `test_review_api.py`)

| Mutant | Garde retirée | Résultat |
|---|---|---|
| M2 | contrôle de version de sélection | tué |
| M3 | atomicité (erreur par élément avalée) | tué |
| M5 | `applied=false` de la règle future | tué |
| M6 | liaison curseur ↔ filtres | tué |
| M7 | rejeu d'idempotence | tué |
| M9 | `null` hors plage sûre | tué |
| M10 | portée `future_only` | tué |
| M1 | refus `selection` hors portée `selection` | survit — équivalent fonctionnel (`ids` n'est étendu que pour `selection`), défense en profondeur sans test |
| M4 | `HUMAN_DECISION_PROTECTED` d'`ignore` | **survit** — lacune de test |
| M8 | doublon dans la sélection | **survit** — lacune de test (le second `allocate` rejoue en silence avec la version courante) |
| M11 | version périmée avant simulation | survit — second contrôle avant `create_rule` ; lacune de test, faible impact |

## Couverture exigences → tests

| ID | Couverture | Évaluation |
|---|---|---|
| REVIEW-01 | `test_REVIEW_01_queue_groups_filters_and_no_store` | OK (marchand ambigu à part, statuts non testés) |
| REVIEW-02 | cursor + unsafe impact | OK |
| REVIEW-03 | détail ordonné sans application | partiel (ordre des sources non asserté) |
| REVIEW-04 | `…REVIEW_04_and_REVIEW_08…` | OK |
| REVIEW-05 | conflit atomique | partiel (pas de concurrence réelle) |
| REVIEW-06 | draft + simulation + reprise | OK |
| REVIEW-07 | split exact, somme/float | OK (propriété absente) |
| REVIEW-08 | rejeu / conflit | OK |
| REVIEW-09 | refund versionné | partiel (version transaction périmée seule non testée) |
| REVIEW-10 | undo restaure | partiel (pas de double undo ni réallocation) |
| REVIEW-11 | rechargement + protection | partiel (flag lu, écrasement non attaqué) |
| REVIEW-12 | file de 10 000 < 2 s | partiel (pas de ≥500 séquences, détail non mesuré) |

## Recommandations (ordre)
1. Corriger MAJ-01 et MAJ-02 (avec tests de file/détail filtrés).
2. Ajouter les tests tueurs M4, M8, M11, double undo/réallocation, curseur falsifié, marchand ambigu, REVIEW-12 propriétés.
3. Aligner `docs/API.md` (MIN-03), borner/paginer le détail (MIN-02), traiter MIN-04/06.
4. Consigner MIN-05/07 au registre des risques (mono-processus, simulation volumineuse).

## Format de retour AGENTS.md

```text
Ticket : T21 (revue indépendante)
Objectif livré : revue DOMAIN financière de l'API de revue ; rapport docs/revues/REVUE_T21.md
Fichiers changés : docs/revues/REVUE_T21.md (dépôt réel) ; fichiers de test jetables dans la copie /tmp/revue_t21 uniquement
Interfaces modifiées : aucune
Tests ajoutés : test_zz_attack.py, test_zz_attack2.py (copie isolée, non livrés)
Commandes réellement exécutées + résultats : pytest test_review_api.py 14 passed ; pytest backend/tests EXIT 0 ; ruff OK ; mypy OK (69 fichiers) ; make verify-contracts OK ; attaques dynamiques (A2–A11, B1–B9) ; 11 mutants (7 tués, 4 survivants dont 1 équivalent)
Invariant vérifié : INV-02 (centimes entiers, null hors 2^53), priorité humaine, atomicité des écritures, idempotence, non-divulgation de libellés
Limites / cas non couverts : concurrence multi-processus, rule-batch undo avec lot réel appliqué, frontend (T22), performance au-delà de 1 000 membres de détail
Risques / sécurité / données privées : aucune donnée réelle ouverte ; aucun libellé réel cité ; fichiers de la copie non conservés dans le dépôt
Relecture recommandée : confirmation après correction de MAJ-01/MAJ-02 et ajout des tests M4/M8/M11
```
