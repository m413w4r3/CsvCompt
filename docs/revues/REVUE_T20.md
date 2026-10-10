# Revue indépendante T20

Date : 2026-10-11 · Base relue : `da34ec3` (T20) · Périmètre : gate G4

## Verdict : BLOCK (B-01 levé ; B-02, B-03 et M-01..03 restent les constats de cette revue)

Les scénarios de migration, d’annulation et de concurrence exécutés sur des
données synthétiques sont largement concluants. Le gate reste bloqué par une
décision de lot non arbitrée, une falsification plausible de snapshot acceptée
et un motif littéral de règle écrit dans le journal d’audit. Les gardes T20 ont
des preuves par mutation pour les contraintes principales, avec deux lacunes
de preuve détaillées ci-dessous.

## Constats classés

### B-01 — BLOQUANT levé : ADR-029 acceptée dans docs/DECISIONS.md, commit da34ec3

Le constat initial était que `docs/DECISIONS.md` n’avait pas d’ADR-029 acceptée
et que `docs/RULES.md` qualifiait encore le commit partiel entre opérations de
« proposition ADR-029 ». Ce point est levé par l’acceptation de l’ADR-029 dans
`docs/DECISIONS.md`, commit `da34ec3` ; `docs/RULES.md` reflète désormais cet
arbitrage. La revue T19 avait également relevé ce point pour G4
(`docs/revues/REVUE_T19.md`, B-01).

Reproduction synthétique : injection d’une erreur lors de la trace de la
deuxième opération. La première reste appliquée et tracée, la deuxième est
absente; avec le `batch_id` fourni à l’avance, `undo_rule_application()` annule
atomiquement l’unique ligne appliquée. Le comportement est cohérent et
récupérable; l’arbitrage de l’application partielle manque toujours.

### B-02 — MAJEUR, snapshot falsifié accepté et montant restauré partiellement

Le test T20 ne falsifie qu’une catégorie inconnue et obtient
`UNDO_SNAPSHOT_INVALID` (`backend/tests/integration/test_review_service.py`,
test `test_RULE_04_invalid_snapshot_returns_contract_error_without_writes`).
Une falsification arithmétiquement plausible passe cependant la validation.

Reproduction synthétique : après deux ventilations manuelles complètes d’un
débit de −4 000 centimes, j’ai remplacé dans le `before_json` de la décision
cible le montant de l’allocation antérieure par −1 000, en retirant puis
rétablissant le trigger d’immuabilité pour simuler une altération stockée. Le
service a accepté l’annulation, n’a pas renvoyé `UNDO_SNAPSHOT_INVALID` et a
restauré une allocation de −1 000 centimes avec un résidu de −3 000 centimes.
Le snapshot valide
en apparence n’est pas rapproché de son contenu antérieur audité ni d’une
empreinte; `_validate_restore_parts()` ne vérifie que `abs(total) <= montant`
(`backend/app/services/review.py:278-353`). Cela brise la restitution exacte
et l’égalité allocation + résidu attendue pour cette opération.

### B-03 — MAJEUR, motif littéral de règle présent dans `audit_events`

Une règle créée avec un marqueur synthétique unique dans `label_pattern` a
laissé ce texte dans `audit_events.after_json` (`rule.created`).
`_rule_snapshot()` sérialise la condition puis `create_rule()` la transmet à
`_audit()` (`backend/app/classification/rule_store.py:546-618`). Un motif borné
peut contenir un extrait copié d’un libellé bancaire. C’est le risque B-02 déjà
décrit dans `docs/revues/REVUE_T19.md`, que `docs/REVIEW.md` reconnaît comme
préexistant. Les événements d’annulation T20 testés ne copient pas le libellé;
la contrainte demandée pour l’ensemble du journal reste donc violée.

Les triggers d’append-only sur `audit_events` rejettent effectivement un
`UPDATE` et un `DELETE` directs, vérifiés sur base synthétique. Aucun appel de
logging ni interpolation de libellé dans les erreurs génériques de
`review.py` n’a été trouvé; cette observation ne neutralise pas la fuite
persistante ci-dessus.

### M-01 — MOYEN, une annulation en chaîne s’arrête sur l’ancienne version

Sur une opération synthétique de −10 001 centimes, deux allocations avec des
dimensions différentes ont été écrites, puis remplacées par une ventilation
complète. L’annulation de la décision la plus récente restitue exactement les
deux catégories, les montants et les dimensions; leur somme reste −10 001.
L’annulation suivante de la décision antérieure, avec la version courante et un
contenu actif identique à son `after_json`, échoue sans écriture avec
`UNDO_STATE_DIVERGED`: `_check_state_for_undo()` exige toujours que la version
enregistrée soit la version courante (`review.py:249-275`).

Ce refus est conservateur et sûr. Le contrat ne précise pas si annuler en
chaîne plusieurs décisions dont l’état a été restauré doit être permis; la
limite mérite une décision ou une mention explicite dans `docs/REVIEW.md`.

### M-02 — MOYEN, les gardes SQL acceptent des liens d’annulation impossibles

Les deux triggers valident que la cible du lien est une décision `undo` de la
même transaction, mais n’imposent pas que la source soit une décision
annulable ni une exécution réellement appliquée. Le service vérifie ces
conditions avant son propre UPDATE (`review.py:470`, `553-555`); la base ne les
garantit pas pour un autre écrivain SQL.

Reproductions synthétiques par DML direct :

- deux décisions `undo` de la même transaction ont été reliées l’une à l’autre;
  les deux UPDATE ont été acceptés par
  `trg_classification_decisions_undo_guard`
  (`0003_t20_review.sql:106-127`), ce qui crée un cycle dans les traces;
- une exécution `skipped_manual`, `dry_run=1`, a été liée à une décision
  `undo`; l’UPDATE a été accepté par
  `trg_rule_executions_update_guard` (`0003_t20_review.sql:73-98`). Le trigger
  ne requiert pas `OLD.outcome='applied'` ni `OLD.dry_run=0`.

### M-03 — MOYEN, des mutations de garde SQL échappent à l’oracle

`uv run python scripts/verify_schema.py` passe et détecte les mutations
configurées : retrait du trigger d’insertion, du trigger de cohérence de lot,
des quatre index uniques, du trigger d’immutabilité des exécutions et du
trigger d’annulation des décisions. Des mutations négatives additionnelles
ont été lancées contre `verify_review_undo_guards()` :

- retirer `trg_classification_decisions_delete_forbidden` ne fait pas échouer
  l’oracle;
- retirer le prédicat `d.action = 'undo'` des deux triggers de lien ne fait pas
  échouer l’oracle;
- retirer le prédicat `d.transaction_id = NEW.transaction_id` des deux
  triggers de lien ne fait pas échouer l’oracle.

Dans chacun de ces trois cas, la commande a imprimé `ESCAPED ...` après que
`verify_review_undo_guards()` a passé. Son cas négatif modifie aussi
`after_json`, ce qui suffit à faire échouer un trigger intact même si le
prédicat du lien est absent. En plus de la lacune d’oracle, M-02 montre que les
deux contrôles de statut source sont absents des triggers eux-mêmes.

## Vérifications demandées

### Migration, schéma et sauvegardes

- Migration réelle d’une base synthétique déjà en `0002` contenant deux
  exécutions historiques : résultat `0003_t20_review`, deux `batch_id` distincts
  (singleton chacun), identifiants de décision et snapshots avant/après
  conservés, `integrity_check=ok`.
- Migration échouée après le backfill, au moment de créer l’index unique sur
  des données synthétiques incohérentes : exception remontée, révision toujours
  `0002_t17_refund_safety`, aucune colonne 0003 résiduelle,
  `integrity_check=ok`. Le rollback du parcours échoué est atomique.
- Les fichiers 0001/0002 ne sont pas modifiés dans le commit T20. Le test de
  gel et `verify_schema.py` confirment l’empreinte des deux fichiers 0001.
- `test_schema_parity.py` compare les objets `sqlite_master` migrés à
  `schema.sql` et vérifie les colonnes ORM de toutes les tables; résultats
  attendus : 29 tables, 39 index, 81 triggers. `verify_schema.py` compare aussi
  directement la chaîne 0001→0003 à `docs/examples/schema.sql`.
- Le downgrade 0003→0002 exige toujours le drapeau explicite et une sauvegarde
  vérifiée; les tests exécutent ensuite le downgrade 0002→0001 avec une
  sauvegarde correspondant à 0002.
- Le diff de `backups.py` ajoute `transaction_version` à l’export de décisions
  et à l’en-tête CSV. Round-trip synthétique : export avec versions présentes,
  sauvegarde/restauration en `0003_t20_review`, lignes
  `(decision_id, transaction_version, undone_by_decision_id)` identiques.

### Annulation, invariants et audit

- Restitution manuelle exacte : deux allocations, sommes en centimes et toutes
  les dimensions vérifiées; le montant bancaire n’est pas modifié.
- Rejeu d’annulation sans écriture, divergence de version et divergence
  d’allocations actives sans écriture : vérifiés par tests et scénario
  synthétique.
- Deux appels concurrents d’annulation du même lot : un succès, un rejeu
  idempotent, une seule décision undo par opération (`test_review_service.py`).
- Décision humaine postérieure : refus sans confirmation et snapshot des tables
  inchangé; avec confirmation, état antérieur restauré et opération toujours
  protégée comme décision humaine.
- Lot partiellement appliqué : le cas synthétique ci-dessus est annulé pour
  toutes les opérations réellement tracées. Lot comportant une allocation
  modifiée par une décision humaine : le contrat de confirmation est vérifié.
- Snapshot de catégorie invalide : `UNDO_SNAPSHOT_INVALID`, aucune écriture.
  Snapshot falsifié mais plausible : voir B-02, il est accepté.
- Les chemins monétaires T20 n’utilisent pas de `float`; les montants restent
  des `int` stricts. Les cas T17 de remboursements et `refund_links` passent
  dans la suite complète.
- Le service d’annulation produit des événements sans libellé synthétique;
  l’événement `rule.created` fuit toutefois le motif comme indiqué en B-03.

### Modifications des assertions de tests existants

Examen exhaustif du diff T20 pour `test_migration_0002.py`, `test_migrations.py`
et `test_schema_parity.py` : **aucune assertion n’a été supprimée**. Les
modifications sont des adaptations de révision ou de schéma; verdict :
**adaptation légitime**, sans affaiblissement observé.

- `test_migration_0002.py` : révision attendue de `0002_t17_refund_safety` à
  `0003_t20_review` (upgrade réussi, `alembic_version`, retour de
  `upgrade_to_head()` et révision après refus de downgrade); comparaison des
  snapshots remplacée par l’égalité des mêmes lignes projetées sur les colonnes
  présentes avant migration; retour de `upgrade_to_head()` au niveau du test
  de downgrade actualisé. Deux appels au service `allocate()` utilisés
  seulement pour préparer l’historique ont été remplacés par un seeding SQL
  synthétique compatible avec le schéma historique; ils ne portaient pas
  d’assertion de résultat. Le helper ajouté vérifie le nombre de lignes et la
  somme des allocations.
- `test_migrations.py` : révisions de tête attendues actualisées en
  `0003_t20_review` pour l’idempotence, l’upgrade avec le drapeau de perte de
  données et le health check; révision après refus de downgrade actualisée;
  texte attendu de l’erreur changé de « downgrade 0002 » à « downgrade T20 ».
  Le test de downgrade ajoute une étape 0003→0002 puis sauvegarde à 0002 avant
  le 0002→0001 déjà testé. La sauvegarde finale référencée devient donc celle
  de 0002.
- `test_schema_parity.py` : seules les assertions de nombre changent (index
  `34→39`, trigger `75→81`); l’égalité complète des objets migrés et de la
  référence SQL reste en place.
- `backups.py` : aucun changement d’assertion dans les tests existants; la
  colonne exportée nouvelle est couverte par le round-trip synthétique exécuté
  pour cette revue.

## Commandes exécutées et résultats

Dans `/tmp/revue_t20`, copie isolée de `HEAD`, avec `COMPTES_REAL_CSV` retirée
de l’environnement. Aucun fichier de `csv_references/`, `data/`, `tmp.zip` ou
`.env*` n’a été ouvert.

- `env -u COMPTES_REAL_CSV UV_PROJECT_ENVIRONMENT=/tmp/revue_t20_env uv run pytest -q backend/tests/integration/test_migration_0003.py backend/tests/integration/test_review_service.py backend/tests/integration/test_schema_parity.py backend/tests/integration/test_migration_0002.py backend/tests/integration/test_migrations.py` — **25 passed**, 1 avertissement Starlette.
- `env -u COMPTES_REAL_CSV UV_PROJECT_ENVIRONMENT=/tmp/revue_t20_env uv run pytest -q backend/tests -m 'not opt_in_real_data'` — **436 passed, 4 deselected**, 1 avertissement Starlette, 151,70 s. Les 4 tests opt-in sur vraies données n’ont pas été lancés.
- `env -u COMPTES_REAL_CSV UV_PROJECT_ENVIRONMENT=/tmp/revue_t20_env uv run ruff check backend` — **All checks passed**.
- `env -u COMPTES_REAL_CSV UV_PROJECT_ENVIRONMENT=/tmp/revue_t20_env uv run ruff format --check backend` — **120 files already formatted**.
- `env -u COMPTES_REAL_CSV UV_PROJECT_ENVIRONMENT=/tmp/revue_t20_env make verify-contracts` — **code 0**; oracles métriques, schéma/mutations, 34 fixtures, auto-tests du profil local opt-in et validation du plan passent. Le premier appel depuis l’archive sans métadonnées Git a affiché une erreur `git rev-parse`; la copie a ensuite reçu un index Git local sans commit et la commande a été relancée avec succès.
- `env -u COMPTES_REAL_CSV UV_PROJECT_ENVIRONMENT=/tmp/revue_t20_env uv run python scripts/verify_schema.py` — **PASS verify_schema**, code 0; gel 0001, chaîne migrée, parité de schéma et mutations incluses.
- Mutations synthétiques supplémentaires avec `verify_review_undo_guards()` et DML sur le schéma compilé : retrait du trigger delete décision, du filtre `action='undo'` et du filtre de transaction ont tous échappé à l’oracle; le DML a accepté un cycle undo→undo et le lien d’une exécution `skipped_manual`/`dry_run`.
- `scripts/check_no_private_data.sh --with-untracked` — **OK, 315 fichiers inspectés**, rapport non suivi inclus.

## Retour selon AGENTS.md

- **Ticket :** T20.
- **Objectif livré :** revue indépendante adversariale des migrations, annulations,
  journaux, invariants monétaires, protections SQL, ajustements de tests et
  preuves d’exécution.
- **Fichiers changés :** `docs/revues/REVUE_T20.md` seulement dans la copie
  isolée. Aucun changement au dépôt source, aucun commit.
- **Interfaces modifiées :** aucune par la revue.
- **Tests ajoutés :** aucun. Tests existants et scénarios synthétiques ponctuels
  réellement exécutés comme listés ci-dessus.
- **Invariant vérifié :** migration additive préservant l’historique, snapshots
  intacts, annulation idempotente et concurrente, versions sous verrou, montant
  bancaire inchangé, dimensions/centimes exacts lorsque le snapshot est intact,
  refund_links et montants entiers.
- **Limites / cas non couverts :** arbitrage ADR-029 absent; falsification
  plausible de snapshot acceptée; motifs de règle dans l’audit; annulation en
  chaîne d’une ancienne décision refusée après restauration de son état; gardes
  SQL qui acceptent deux liens invalides et mutations de suppression/lien undo
  qui échappent à l’oracle.
- **Risques / sécurité / données privées :** B-03; données synthétiques
  uniquement, aucun fichier privé interdit consulté. Vérification
  `--with-untracked` vert sur les 315 fichiers de la copie isolée.
- **Relecture recommandée :** arbitrage de l’orchestrateur sur ADR-029 et
  correction des constats B-02/B-03 avant validation du gate G4.
