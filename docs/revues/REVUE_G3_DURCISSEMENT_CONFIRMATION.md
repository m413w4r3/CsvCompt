# Revue de confirmation du durcissement G3

- **Date** : 2026-10-10
- **Rôle** : relecteur indépendant. Je n'ai écrit ni le durcissement G3 ni la revue précédente (`REVUE_G3_DURCISSEMENT.md`).
- **Base** : arbre de travail non commité comparé à HEAD `1066748`. Exécution dans une copie isolée `/tmp/revue_g3h2` (sans `.git`, `.venv`, `node_modules`, `csv_references`, `data`, `tmp.zip`). Un `git init` local a été fait dans la copie, uniquement pour les gardes de contrat.
- **Verdict** : **BLOCK**, pour un seul motif nouveau. Les sept constats du BLOCK précédent sont levés. Le correctif de M-G3-04 introduit en revanche une régression de disponibilité (M-G3C-01) : la liste des opérations tombe en panne après environ 15 changements de filtre ou frappes dans la recherche en moins de 5 minutes. Le correctif reste local et ne touche pas aux données. Une fois corrigé et testé, je recommande PASS sous les réserves mineures ci-dessous.

## Périmètre

Documents lus : AGENTS.md, `docs/revues/REVUE_G3.md`, `docs/revues/REVUE_G3_DURCISSEMENT.md`, ADR-030 de `docs/DECISIONS.md`, `docs/IMPORT_COMMIT.md`, `docs/TRANSACTIONS_API.md`, `docs/API.md` et `docs/IMPORT_API.md` (passages sur le plafond, le curseur et les entiers).

Code relu :
- backend : `services/{imports,import_archive,import_commit,import_preview_store,transactions}.py`, `api/v1/{imports,transactions,accounts}.py` ;
- frontend : `pages/Transactions/index.tsx`, `app/router.tsx`, `lib/api/imports.ts`, `types/transactions.ts` ;
- tests : `test_g3_hardening_*.py`, `test_import_commit_atomic.py`, `Transactions.g3.test.tsx`, `tests/e2e/transactions.spec.ts`, `scripts/test_e2e_import.sh`.

`services/backups.py` (T41) n'a été lu que pour son interaction avec `originals/` et le verrou.

Hors périmètre : allocations, refund_links, schéma et migrations, sauvegardes, sécurité, classification.

Aucun fichier de `csv_references/`, `data/` ou `tmp.zip` n'a été ouvert. Toutes les données utilisées sont synthétiques : fixtures du dépôt et CSV générés.

## Statut des constats précédents

| Constat | Statut | Preuve |
|---|---|---|
| **B-G3-01** — purge de l'archive d'un commit actif | **Levé** | Le script précédent `purge_commit_race.py` donne `purged=0`, `commit_status=committed`, `committed_archive_exists=True`. Variante en **sous-processus** (`lock_variants.py race`) : un commit est suspendu dans un processus après l'écriture de l'archive, avec un mtime vieilli de 25 h. **Deux purges parallèles** sont lancées dans deux autres processus, avec l'horloge réelle puis avec **l'horloge avancée de 48 h** (équivalent d'un commit de plus de 24 h). Résultat : `removed=0` et `removed=0` dans les deux cas ; l'archive commitée existe. Crash du détenteur du verrou : le verrou est tenu, `purge=0` ; après SIGKILL du détenteur, `purge=1`. Le noyau libère bien le verrou. |
| **M-G3-02** — `.tmp` orphelins après SIGKILL | **Levé** | `kill_archive_tmp.py` : SIGKILL au moment de `os.replace`. Le `.tmp` reste après le kill et après un redémarrage immédiat (`archive_tmp_after_restart=1`). Il est purgé au redémarrage après 25 h (`archive_tmp_after_25h_restart=0`, `purge_audit_events=1`). Un `.tmp` en cours d'écriture est protégé par le verrou partagé (test `test_IMP_06_tmp_purge_never_removes_an_in_progress_writer_file`). L'âge vaut `max(mtime, ctime)`. Réserve : voir m-G3C-04. |
| **m-G3-05** — suppression sans audit | **Levé** | `purge_audit_failure.py` : `orphan_deleted=False`, `audit_events=0`. L'audit d'intention est commité avant tout `unlink` (`import_archive.py:163-189`). Il ne contient que des comptages, sans chemin ni identifiant. |
| **M-G3-03** — centimes non sûrs vers le frontend | **Levé** | `unsafe_account_calculation.py` (1 000 × −10^13) : `recalculated_balance_cents=None`, `delta_cents=None`, `total_out_of_range=True`. Balayage plus large (`sweep_unsafe_ints.py`) : un vrai commit, puis injection SQL synthétique de 1 000 transactions de −10^13 centimes, puis appel de **toutes les routes GET** de l'OpenAPI (10 routes appelées, 2 sautées faute d'identifiant). Résultat : **aucun entier de valeur absolue > 2^53−1** dans aucune réponse. `/transactions` renvoie `debit_total_cents=None`, `net_cents=None`, `total_out_of_range=True`, tandis que `credit_total_cents` reste un entier sûr. Le drapeau est cohérent : `False` par défaut côté Pydantic, `True` dès qu'un champ `*_cents` est neutralisé (`sanitize_api_cents`, appliqué au détail, à la liste, aux soldes et à l'aperçu). Le parseur bloque déjà en `AMOUNT_OUT_OF_RANGE` un fichier dont le total dépasse 10^13 ; seul un cumul de plusieurs imports peut donc franchir 2^53. UI : `formatNullableCents` affiche « hors plage entière sûre » pour les totaux de liste, de section et de ligne. Le test Vitest couvre `null` et `MAX_SAFE_INTEGER+1`. Une `errorElement` est déclarée sur la route `transactions` (`router.tsx:21`) en plus de la limite d'erreur interne. |
| **M-G3-04** — snapshot de pagination | **Levé fonctionnellement, régression introduite (M-G3C-01)** | `snapshot_race.py` : `first_page_count=21`, `next_page_count_after_commit=21`, `new_row_leaked_into_old_snapshot=False`. Le test d'auteur couvre un commit commencé avant la page 1 et finalisé après. Après redémarrage, le curseur répond `409 CURSOR_STALE` ; après expiration (TTL forcé à 1 s), aussi `409 CURSOR_STALE`. Un curseur falsifié répond `422 CURSOR_INVALID`. Le curseur ne contient que les clés `filters` (avec HMAC, sans `q`), `snapshot_id`, `snapshot_at`, `last_booked_on` et `last_id`. |
| **m-G3-06** — E2E : diagnostic résolu ou rejet | **Levé** | `resolution_status` est exposé (`imports.py:309`, `:688`). `scripts/test_e2e_import.sh` : **3/3 scénarios passent** (no_op, lien OUT_OF_PERIOD sans doublon avec `toHaveCount(1)`, axe en clair et sombre). |
| **m-G3-07** — doc de recherche et statut du curseur | **Levé** | `TRANSACTIONS_API.md:64-72` décrit la normalisation de la ponctuation. `API.md` et `TRANSACTIONS_API.md` annoncent `CURSOR_INVALID (422)` et `CURSOR_STALE (409)`, valeurs conformes à l'exécution. |
| **m-G3-08** — plafond de lignes et plafond d'octets | **Levé** | `humanError` traite `TOO_MANY_OPERATIONS` à part (« au plus 4 000 opérations »). Le plafond est annoncé avant l'envoi (`index.tsx:245`). API : 4 001 lignes → `413 TOO_MANY_OPERATIONS`. Le plafond est cohérent dans le service, l'API, `IMPORT_API.md`, `API.md` et `IMPORT_COMMIT.md`. |

## Nouveaux constats

### BLOQUANT

Aucun.

### MAJEUR

#### M-G3C-01 — Les snapshots épinglés épuisent le pool SQLAlchemy et la liste des opérations répond 500 après 30 s

**Cause.** `TransactionSnapshotStore.create` (`services/transactions.py:78`) prend une connexion du **pool partagé** de l'engine et la garde, avec une transaction de lecture ouverte, jusqu'à la dernière page, l'expiration (5 min) ou l'éviction. Le plafond est `max_snapshots=32`. L'engine utilise le `QueuePool` par défaut (`pool_size=5`, `max_overflow=10`, soit 15 connexions, avec une attente de 30 s). Ce plafond de 32 dépasse donc la capacité du pool. Toute première page qui a une page suivante crée un snapshot qui n'est jamais libéré si l'utilisateur ne va pas jusqu'au bout.

Or l'interface relance une première page **à chaque frappe** dans « Recherche du libellé » et à chaque changement de filtre. `load` dépend de `q` (`index.tsx:80-90`), et un `useEffect` sur `load` la relance sans délai d'attente (debounce). Sur un jeu de plus de 50 opérations, saisir une quinzaine de caractères en moins de 5 minutes suffit à déclencher la panne.

**Reproduction.** `uv run python -I /tmp/revue_g3h2_scripts/pagination_variants.py pool` (base synthétique `overlap_A`, `limit=1`).

Résultat :
- les premières pages 1 à 14 répondent `200` en 0,01 s ;
- la **15ᵉ répond `500 INTERNAL_ERROR` après 30,01 s** ; à ce moment, 14 snapshots sont ouverts et 14 connexions sont prises ;
- une nouvelle requête de liste répond encore `500` après 30,01 s ;
- un GET `/accounts` isolé, un aperçu et un commit séquentiels réussissent, car il reste une connexion libre.

La liste reste hors service jusqu'à l'expiration paresseuse des snapshots, soit jusqu'à 5 minutes. Des requêtes concurrentes peuvent en plus se disputer la dernière connexion.

Aucune donnée n'est altérée. En revanche, la page principale tombe en panne dans un usage nominal, et le 500 n'est pas un code contractuel (`DATABASE_BUSY 503` serait attendu).

**Recommandations** (à arbitrer par l'auteur) :
1. ouvrir les snapshots hors du pool partagé (engine ou connexion dédiés, par exemple `NullPool`), ou borner `max_snapshots` sous la capacité du pool en gardant une réserve ;
2. évincer le snapshot précédent lorsqu'une nouvelle première page est demandée pour la même navigation, ou évincer le plus ancien plutôt qu'échouer ;
3. ajouter un délai d'attente sur `q` côté UI ;
4. renvoyer `503 DATABASE_BUSY` plutôt qu'un 500 en cas de dépassement de délai du pool ;
5. ajouter un test qui enchaîne au moins 33 premières pages, puis vérifie une liste et un commit en moins d'une seconde.

### MINEURS

#### m-G3C-02 — Expiration paresseuse : une lecture WAL peut rester ouverte sans limite de durée

L'expiration ne s'exécute que dans `create` ou `use`, c'est-à-dire lors d'un appel à la liste. Avec un TTL forcé à 1 s et sans nouvel appel à la liste, l'entrée existe toujours après 1,5 s (`after_ttl_no_list_request_entries 1`). Pendant qu'un snapshot est ouvert, `PRAGMA wal_checkpoint(TRUNCATE)` répond `busy=1` (61/61 pages non tronquées, WAL de 251 352 octets) ; après `close_all`, il répond `(0,0,0)` et le WAL tombe à 0 octet.

Si l'utilisateur affiche une première page puis importe pendant des heures sans revenir à la liste, le WAL ne peut pas être remis à zéro. Le risque est faible pour un usage local. Recommandation : balayage périodique ou expiration au début de chaque requête.

#### m-G3C-03 — Système de fichiers sans `flock` : démarrage en échec et commit en 500 non contractuel

Avec `fcntl.flock` forcé à `OSError(ENOLCK)` (`lock_variants.py enolck`) :
- le commit répond `500 INTERNAL_ERROR` et ne laisse aucun fichier ; un nouvel essai réussit une fois le verrou rétabli ;
- le **démarrage échoue** sur `OSError: [Errno 37] No locks available`. Le lifespan ne supprime que `DatabaseBusyError` et `OperationalError` (`api/v1/imports.py:58`).

Le comportement est sûr (aucune purge sans verrou), mais l'erreur n'est pas expliquée. C'est le cas si `DATA_DIR` est placé sur NFS, SMB ou un FUSE sans verrous. Recommandation : erreur contractuelle explicite (par exemple `ARCHIVE_LOCK_UNAVAILABLE` 503), purge sautée avec journalisation au démarrage, et prérequis documenté.

Point voisin : un échec de `purge_expired_idempotency` dans le même bloc `suppress` saute aussi la purge des archives.

#### m-G3C-04 — Un `.tmp` résiduel bloque une restauration vers ce répertoire

Le verrou a été placé hors de `originals/` pour respecter le contrat de restauration. Un `.tmp` laissé par un SIGKILL reste toutefois **dans** `originals/` au moins 24 h, et jusqu'à un démarrage postérieur. `_validate_target_originals` refuse alors la cible (`restore_tmp.py` : « La restauration refuse originals/ contenant un fichier autre que <uuid>.bin. »).

Recommandation : écrire les temporaires dans un répertoire frère (par exemple `originals.staging/`, même système de fichiers) ou documenter le nettoyage manuel.

#### m-G3C-05 — Robustesse du verrou face à une manipulation externe

Si le fichier `.archive-lifecycle.lock` est supprimé pendant qu'un commit détient son verrou partagé, une purge crée un nouvel inode et obtient le verrou exclusif.

Avec l'horloge réelle, `max(mtime, ctime)` protège encore l'archive : `removed=0`, archive conservée. Avec une horloge avancée de 48 h (ou un commit de plus de 24 h), la purge supprime l'archive active : `removed=1`, `committed_archive_exists=False`.

Ce scénario exige une intervention extérieure dans le répertoire privé `0700`. Recommandation : après `flock`, vérifier que `fstat(fd).st_ino == stat(lock_path).st_ino`, sinon réessayer.

#### m-G3C-06 — La purge supprime tout `*.bin` non référencé, quel que soit son nom

`notes-utilisateur.bin`, vieilli, est supprimé. Les liens symboliques (`link.bin`, `.link.tmp`), le répertoire `dir.bin` et `foreign.txt` sont ignorés, et la cible externe du lien est intacte.

Le comportement est cohérent avec un répertoire privé réservé à l'application. Restreindre au motif `<uuid>.bin`, déjà imposé par la restauration, alignerait les deux contrats.

### Informations sans action requise

- Le verrou ne fuit pas de descripteur : 200 cycles réservation, écriture, libération, purge, plus 200 écritures en échec, font passer le nombre de descripteurs de 4 à 7. L'écart correspond aux connexions du pool et ne grandit pas avec le nombre de cycles. Modes constatés : verrou `0600`, `originals/` `0700`, répertoire de données `0700`.
- Aucun interblocage n'a été identifié : le commit prend le verrou partagé, en attente bloquante, avant `BEGIN IMMEDIATE` ; la purge ne bloque jamais (exclusif non bloquant) et saute tout l'inventaire. Un commit n'échoue donc jamais à cause d'une purge. Il attend au plus la durée de la purge, dont l'écriture d'audit est bornée par `busy_timeout` (5 s).
- Les imports `no_op` n'ont pas d'archive (`archive_relpath=NULL`, test `test_IMP_01_no_op_attempt_does_not_leave_a_second_archive`) ; aucun chemin ne produit `rejected`. L'inventaire ne retient que `status='committed'`, ce qui est correct.
- L'audit d'intention compte les candidats, pas les suppressions effectives. Un `unlink` en échec n'est pas tracé. Acceptable.

## Vérifications réellement exécutées (copie isolée)

| Commande | Résultat |
|---|---|
| `uv sync --all-groups` ; `cd frontend && npm ci` | OK |
| `uv run pytest -q -m 'not opt_in_real_data' --tb=line` | **413 passed, 4 deselected**, 1 avertissement (`StarletteDeprecationWarning`), 115 s. **0 échec** : les 8 échecs hors périmètre de la revue précédente ont disparu. |
| `uv run pytest -q test_g3_hardening_*.py test_import_commit_atomic.py` | 37 passed |
| `npm test -- --run` | 7 fichiers, **49 tests passés** |
| `npm run typecheck` ; `npm run lint` | OK ; OK |
| `make verify-contracts` | OK (46 tickets, 12 gates, 87 IDs de catégories, 81 tests référencés ; auto-test 4/4) |
| `bash scripts/test_e2e_import.sh` | **3/3 passés** (no_op 1,5 s, lien hors période sans doublon 1,0 s, axe en clair et sombre 5,9 s) |
| `bash scripts/check_no_private_data.sh --with-untracked` | OK, 299 fichiers inspectés |
| Scripts précédents portés (`/tmp/revue_g3h2_scripts/prev/`) : `purge_commit_race`, `purge_audit_failure`, `unsafe_account_calculation`, `snapshot_race`, `cursor_tamper`, `preview_safe`, `replay_race`, `kill_archive_tmp`, `kill_commit`, `committed_archive` | Tous conformes à l'attendu (voir le tableau des statuts) ; replay concurrent : `same_response=True` |
| Variantes nouvelles : `lock_variants.py` (course multi-processus, double purge, horloge +48 h, verrou supprimé, crash du détenteur, fichiers étrangers et liens symboliques, ENOLCK, descripteurs et modes), `pagination_variants.py` (pool, WAL, expiration, redémarrage), `sweep_unsafe_ints.py`, `restore_tmp.py` | Résultats cités dans les constats |
| Benchmark de commit à 4 000 lignes synthétiques uniques (`prev/benchmark_commit.py`), 3 essais | **4,465 s / 4,699 s / 4,505 s**, sous la cible de 10 s ; 4 001 lignes → `413 TOO_MANY_OPERATIONS` |

### Assertions de tests existants modifiées (liste exhaustive du diff)

**En périmètre :**
- `test_import_commit_atomic.py` : test renommé en `..._removes_unreferenced_archive`. Trois assertions de quarantaine (une archive, octets exacts, mode `0600`) sont remplacées par `assert archives == []`. Le changement est conforme à ADR-030.1. Les assertions de succès (octets, SHA-256, modes) sont conservées ailleurs.
- `test_imports_preview_api.py` : 3 lignes retirées, 7 ajoutées. `b"0001" not in response.content` et les contrôles de sous-chaîne `number not in response.text/caplog.text` sont remplacés par les scanners `assert_payload_has_no_account_numbers` et `assert_no_account_numbers_in_logs`, avec de nouveaux cas de collision d'UUID. Ce n'est pas un affaiblissement.
- `test_quality_imports.py` : 4 lignes retirées, 4 ajoutées. Trois « 0001 absent » passent au scanner ; `partial` devient `not_started` (ADR-030.3).
- `frontend/tests/e2e/transactions.spec.ts` : 0 assertion retirée, 26 ajoutées.

Ces changements sont identiques à ceux décrits dans la revue précédente ; aucun nouveau changement.

**Hors périmètre, simple comptage :** `test_allocations_service.py` (−3/+21), `test_migrations.py` (−6/+11), `test_schema_parity.py` (−4/+3), `test_money_split.py` (−1/+8), `test_models.py` (−1/+1). Ces fichiers relèvent des propriétaires T17, T41 et du schéma.

## Invariants

- **INV-02** : aucun `float`, `Decimal`, `round` ni division de montant dans les services et API relus. Le frontend formate en `bigint` (`index.tsx:298`) et vérifie `Number.isSafeInteger` sur les paramètres.
- **INV-11** : aucune donnée sensible relevée.
  - Audit de purge : seulement `archive_count` et `temporary_count`.
  - Curseur : sans `q` ni libellé ; filtres liés par HMAC.
  - Réponses d'erreur : sans chemin.
  - Aucun journal dans `import_archive`, `import_commit` ou `transactions`.
  - Candidats OUT_OF_PERIOD : identifiants opaques seulement.

## Recommandation

Corriger M-G3C-01 : pool, éviction et délai d'attente, avec un test de 33 premières pages ou plus. Relancer ensuite la liste et l'E2E. Dans le même lot si possible, traiter m-G3C-02 (expiration active) et m-G3C-03 (code 503 explicite et démarrage tolérant). m-G3C-04 à 06 peuvent être consignés dans `docs/REGISTRE_RISQUES.md`. Sous ces conditions, G3 peut être levé.

## Retour au format AGENTS.md

```text
Ticket : revue de confirmation du durcissement G3 (B-G3-01, M-G3-02..04, m-G3-05..08)
Objectif livré : revue adversariale exécutée ; verdict BLOCK sur un seul constat nouveau (M-G3C-01, régression de disponibilité introduite par le correctif de M-G3-04) ; tous les constats précédents sont levés.
Fichiers changés : docs/revues/REVUE_G3_DURCISSEMENT_CONFIRMATION.md uniquement (dépôt réel). Aucun code, test ni document de suivi modifié.
Interfaces modifiées : aucune.
Tests ajoutés : aucun dans le dépôt. Scripts jetables synthétiques dans /tmp/revue_g3h2_scripts : common.py, lock_variants.py, pagination_variants.py, sweep_unsafe_ints.py, restore_tmp.py, plus prev/ (scripts de la revue précédente portés vers la copie).
Commandes réellement exécutées + résultats : backend 413 passed, 0 failed ; ciblés G3 37 passed ; Vitest 49/49 ; typecheck et eslint OK ; make verify-contracts OK ; test_e2e_import.sh 3/3 ; check_no_private_data --with-untracked OK (299 fichiers) ; benchmark 4 000 lignes 4,47–4,70 s ; reproductions et variantes conformes au tableau ; M-G3C-01 reproduit (15e première page : 500 après 30,01 s).
Invariant vérifié : INV-02 (aucun float monétaire) ; INV-11 (audits, curseurs, réponses et journaux inspectés sans donnée sensible) ; aucun entier > 2^53−1 sur les routes GET.
Limites / cas non couverts : verrou testé sur ext4 local, ENOLCK simulé par monkeypatch ; aucun test sur un vrai NFS ; T41 relu uniquement pour son interaction avec originals/ ; aucune donnée réelle ; pas de test sous plusieurs workers uvicorn (le store de snapshots est local au processus, ce qui donnerait CURSOR_STALE entre workers).
Risques / sécurité / données privées : liste des opérations indisponible (500) après environ 15 premières pages non terminées en 5 min ; WAL non tronqué tant qu'un snapshot reste ouvert ; démarrage impossible sans flock ; un .tmp résiduel bloque la restauration vers ce répertoire. Aucun CSV réel, libellé réel, IBAN ni numéro de compte n'a été ouvert ou cité.
Relecture recommandée : propriétaire T15 pour M-G3C-01 et m-G3C-02 (snapshot store et délai d'attente dans l'UI) ; propriétaire T12 pour m-G3C-03 à 06 ; propriétaire T41 pour m-G3C-04 ; orchestrateur pour l'arbitrage de G3.
Hypothèses : application locale mono-processus ; DATA_DIR sur un système de fichiers local supportant flock.
Instructions de reproduction : copie isolée telle que prescrite, puis `cd /tmp/revue_g3h2 && uv run python -I /tmp/revue_g3h2_scripts/<script>.py [scénarios]`.
```
