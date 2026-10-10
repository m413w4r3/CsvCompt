# Revue indépendante T41 — sauvegarde/restauration cohérentes et exports (OPS)

## Verdict

**PASS sous réserves.** Les critères d'acceptation du backlog sont satisfaits et prouvés de bout en bout : snapshot WAL cohérent, restauration vers une base distincte avec données identiques, calculs inchangés. OPS-01 et OPS-02 ont des tests nommés qui passent. Une restauration suivie d'un réimport reste idempotente.

Six constats **MAJEURS** doivent toutefois être corrigés, ou arbitrés par ADR, **avant la validation du gate G11** :

- sauvegarde qui ne se termine jamais sous écritures soutenues ;
- rétention qui peut supprimer la sauvegarde qu'elle vient de vérifier ;
- injection de formules dans l'export CSV ;
- fichiers `.cfbackup` et exports qui peuvent aboutir non ignorés dans le dépôt ;
- état mixte possible après un arrêt brutal pendant la restauration ;
- schéma restauré non contrôlé : une sauvegarde sans aucun trigger est acceptée.

Aucun constat BLOQUANT.

Périmètre : l'arbre de travail non commité au 2026-10-10 (`backend/app/services/backups.py`, `backend/tests/integration/test_backups.py`, `scripts/backup.sh`, `scripts/restore.sh`, `docs/BACKUP.md`), copié dans `/tmp/revue_t41/`. Les essais n'ont utilisé que des fixtures synthétiques (`backend/tests/fixtures/`) et des bases temporaires sous `/tmp/revue_t41_work_*`. `csv_references/`, `data/` et `tmp.zip` n'ont pas été ouverts. Aucun fichier du dépôt réel n'a été modifié, sauf ce rapport.

> Note de méthode : la recette `rsync` fournie copie aussi `.env` (0600) dans `/tmp/revue_t41/`. Je l'ai supprimé de la copie sans le lire, pour qu'aucun essai ne pointe vers une configuration réelle. Ajouter `--exclude .env` à la recette pour les prochaines revues.

## Constats

### MAJEUR M-01 — La sauvegarde ne se termine jamais quand la base est écrite en continu

[`_snapshot_database`](/home/nill/Desktop/compte/mescompts/backend/app/services/backups.py:284) appelle `source.backup(target, pages=256, sleep=0.05)`. Une copie en plusieurs étapes redémarre à chaque commit d'une autre connexion entre deux étapes. Dès que la base dépasse quelques centaines de pages et qu'un écrivain commite plus souvent que la durée d'une passe, la copie recommence indéfiniment. Il n'y a ni échéance ni erreur : le CLI reste bloqué. La cohérence n'est pas en cause, seulement la terminaison. Elle compte pourtant pour R-07 : une sauvegarde lancée avant une action massive (règles, IA, import) peut ne jamais aboutir.

**Reproduction** (`/tmp/revue_t41_scripts/exp_livelock.py`, base de 40 Mo, soit 10 389 pages, plus un écrivain `BEGIN IMMEDIATE`/`COMMIT`) :

| Période d'écriture | Résultat |
|---|---|
| 0,5 s | sauvegarde OK en 1,39 s |
| 0,2 s | OK en 1,45 s |
| 0,05 s | **toujours en cours après 60 s** (1 122 commits pendant l'essai, code 3) |
| 0,05 s, variante `pages=-1` | **OK en 1,34 s** |

En WAL, `pages=-1` copie toute la base dans une seule transaction de lecture et ne bloque pas les écrivains. **Recommandation** : `pages=-1` (ou `VACUUM INTO`) et une échéance explicite. Hors périmètre T41 : la même construction existe dans [`app/db/migrate.py`](/home/nill/Desktop/compte/mescompts/backend/app/db/migrate.py:252), pour la sauvegarde pré-migration. À signaler au propriétaire de T06.

Le test OPS-01 ne voit pas ce cas : sa base fait moins de 256 pages, donc la copie se fait en une seule étape.

### MAJEUR M-02 — La rétention peut supprimer la sauvegarde qu'elle vient de vérifier, et annoncer un succès

[`_prune_backups`](/home/nill/Desktop/compte/mescompts/backend/app/services/backups.py:314) trie par `st_mtime_ns` seul, puis supprime au-delà de `keep`. Le code ne protège pas le fichier qui vient d'être créé et vérifié, et ne tient pas compte de la validité des autres fichiers. [`create_backup`](/home/nill/Desktop/compte/mescompts/backend/app/services/backups.py:424) retourne ensuite un `BackupResult` qui pointe vers un fichier supprimé. Le CLI affiche `backup=created-and-verified`.

**Reproduction** (`/tmp/revue_t41_scripts/exp_retention.py`) :

- **A** : deux sauvegardes automatiques ont un `mtime` dans le futur (+1 h). Causes possibles : horloge recalée par NTP, copie `cp -p` depuis une autre machine. On crée ensuite une sauvegarde avec `retention_count=2`. Résultat : `new verified backup still exists: False`. Le fichier retourné n'existe plus, et les deux fichiers conservés n'ont pas été revérifiés.
- **B** : une sauvegarde ancienne valide, deux plus récentes tronquées, puis une nouvelle avec `retention_count=2`. Résultat : `kept 2, valid among kept 1, old valid kept: False`. La rétention garde une archive corrompue et supprime une archive valide.
- **C** : avec `--output /dossier/family-backup-manual.cfbackup`, la rétention s'applique à tout `/dossier`. Le CLI a une rétention de 10 par défaut. Un fichier utilisateur `family-backup-photos.cfbackup` du même dossier est supprimé (`unrelated user file still exists: False`).
- **D** (positif) : deux sauvegardes créées à la même microseconde ont des noms distincts grâce au suffixe UUID. Pas de collision.

**Recommandation** :

- exclure toujours de la suppression le fichier qui vient d'être publié ;
- trier selon l'horodatage du nom ou du manifeste, pas le `mtime` ;
- ne compter dans la rétention que les sauvegardes vérifiées, ou ne jamais supprimer la dernière sauvegarde vérifiée ;
- n'appliquer la rétention qu'au répertoire automatique `data/backups/`.

### MAJEUR M-03 — Injection de formules dans l'export CSV

[`export_data`](/home/nill/Desktop/compte/mescompts/backend/app/services/backups.py:774) écrit `raw_label`, `payee_name`, `account_alias`, etc. tels quels. Le libellé d'un virement reçu est fixé par un tiers, l'émetteur de la référence SEPA. Un libellé `=HYPERLINK(...)`, `@SUM(...)`, `+...` ou `-...` devient une formule à l'ouverture dans un tableur. Ni le code ni `docs/BACKUP.md` ne traitent ce cas.

**Reproduction** (`/tmp/revue_t41_scripts/exp_export.py`) : on importe `overlap_A.csv` après avoir remplacé deux libellés par `=HYPERLINK("http://example.invalid","x") carte` et `@SUM(1+1) carte`. Résultat : `first chars of exported raw_label cells: ['=', '@', 'P']`, `cells starting with = or @: 2`.

**Recommandation** : préfixer d'une apostrophe, ou d'une tabulation, toute cellule **texte** qui commence par `= + - @ \t \r`. Ne pas toucher `amount_cents` ni `amount_decimal`, qui commencent légitimement par `-`. Documenter la neutralisation et ajouter un test (libellé `=1+1`, montant négatif intact).

Par ailleurs, l'export utilise la virgule comme séparateur. Un tableur en locale française l'ouvre sur une seule colonne. C'est un point d'ergonomie : le documenter ou proposer `;`.

### MAJEUR M-04 — Une sauvegarde ou un export peut aboutir dans le dépôt, non ignoré par Git et non détecté par la garde INV-11

Les deux scripts ([`backup.sh`](/home/nill/Desktop/compte/mescompts/scripts/backup.sh:5), [`restore.sh`](/home/nill/Desktop/compte/mescompts/scripts/restore.sh:5)) font `cd "$project_root"` avant `exec`. Tout chemin relatif passé par l'utilisateur est donc résolu **depuis la racine du dépôt**, pas depuis son répertoire courant. La commande d'export documentée (`uv run python -m app.services.backups export ...`) s'exécute elle aussi depuis la racine.

`.gitignore` n'ignore ni `*.cfbackup`, ni les exports `.json`. Un `.csv` hors fixtures est bien refusé par la garde. [`scripts/check_no_private_data.sh`](/home/nill/Desktop/compte/mescompts/scripts/check_no_private_data.sh:4) ne recherche ni `*.cfbackup`, ni le contenu d'une archive zip. Une sauvegarde contient la clé HMAC et tous les libellés.

**Reproduction** :

1. `cd /tmp && /tmp/revue_t41/scripts/backup.sh --output rel-test.cfbackup` crée `/tmp/revue_t41/rel-test.cfbackup`, à la racine du projet. `/tmp/rel-test.cfbackup` n'existe pas.
2. Dans un dépôt Git jetable (`git init` sur la copie), on indexe une vraie `.cfbackup` et un export JSON (`git add -A`). `check_no_private_data.sh` répond `OK : aucune donnée privée détectée (284 fichier(s) inspecté(s))`, code 0.
3. Dans le dépôt réel, `git check-ignore rel-test.cfbackup export.json` ne renvoie rien. Seul `data/backups/…` est ignoré.

**Recommandation** :

- résoudre les chemins relatifs avant le `cd`, ou ne pas faire de `cd` et utiliser `uv run --project` ;
- ajouter `*.cfbackup` (et une convention d'export, par exemple `exports/`) à `.gitignore` ;
- faire échouer `check_no_private_data.sh` sur `*.cfbackup`, ou sur tout zip qui contient `manifest.json` et `database.sqlite`.

`.gitignore` et le script de garde sont hors des `allowed_paths` de T41 : l'arbitrage revient à l'orchestrateur.

### MAJEUR M-05 — Un arrêt brutal pendant la restauration laisse un état mixte non signalé et bloque une nouvelle tentative

[`restore_backup`](/home/nill/Desktop/compte/mescompts/backend/app/services/backups.py:958) installe la cible en six renommages successifs :

1. base vers `.old` ;
2. `-wal` vers `.old` ;
3. `-shm` vers `.old` ;
4. `originals` vers `.old` ;
5. installation de la base ;
6. installation de `originals`.

Il n'y a ni journal de restauration, ni marqueur « en cours », ni procédure `--recover`. `docs/BACKUP.md` reconnaît la limite. Mais l'état mixte n'est pas détectable par l'application, et la commande documentée de reprise échoue.

**Reproduction** (`/tmp/revue_t41_scripts/exp_kill.py`) : la cible existante contient `overlap_B`, la sauvegarde contient `overlap_A`. `SIGKILL` est envoyé avant le renommage n.

| Kill | Base cible | `originals/` | Nouvelle restauration |
|---|---|---|---|
| @1 | ancienne | ancienne | OK |
| @2, @3, @4 | **absente** (dans `.target.sqlite.<tok>.old`) | ancienne | **refusée** (« archives sans base de sécurité exploitable ») |
| @5 | absente | absente | OK |
| @6 | **NOUVELLE** | **absente → MIXTE** | **refusée** (« Une archive de source référencée par la base est absente », car la sauvegarde de sécurité de la cible mixte échoue) |
| après @6 | nouvelle | nouvelle | OK |

Dans tous les cas, les `.old` (0600) et les répertoires `.restore-*-stage-*` restent, sans nettoyage ni signalement. Avec un kill @6, le répertoire de staging conserve une copie complète des archives. Avec un kill @2, la cible garde un `-wal`/`-shm` orphelin sans base principale. Je n'ai pas testé ce que SQLite en fait si une base est recréée à ce chemin : c'est un risque résiduel.

La sauvegarde de sécurité permet une récupération manuelle, ce qui limite la gravité. L'oracle OPS-02 (« pas de données mixtes ») n'est cependant garanti que hors crash.

**Recommandation** :

- soit installer base et archives sous **un seul répertoire** remplacé par un seul `rename` ;
- soit écrire un journal `.restore-in-progress` (jeton, chemins `.old`), refusé au démarrage de l'application et rejouable ou annulable par `restore --recover`.

### MAJEUR M-06 — Le schéma n'est pas contrôlé : une sauvegarde sans triggers ni invariants est acceptée et restaurable

[`_database_summary`](/home/nill/Desktop/compte/mescompts/backend/app/services/backups.py:143) et [`_verify_restored_database`](/home/nill/Desktop/compte/mescompts/backend/app/services/backups.py:841) contrôlent la révision Alembic, la présence des 8 tables clés, leurs comptes de lignes, `integrity_check` et `foreign_key_check`. Ils ne comparent jamais `sqlite_master` (tables, index, triggers, CHECK) au schéma de référence. Le manifeste contient des SHA-256 sans clé : ils détectent une corruption accidentelle, pas une modification délibérée. Rien n'en avertit dans `docs/BACKUP.md`.

**Reproduction** (`/tmp/revue_t41_scripts/exp_tamper.py`) : on extrait `database.sqlite` d'une sauvegarde valide, on fait `DROP` des **73 triggers** et on multiplie tous les `amount_cents` par 1 000. On recalcule tailles et SHA-256 dans le manifeste. Résultat : `ACCEPTED by verify_backup`. La restauration produirait une base de travail privée de tous les garde-fous INV/ADR-019. Une sauvegarde est souvent stockée sur un support externe, moins sûr que la base active.

**Recommandation** : comparer `sqlite_master` de la base extraite à celui que produit la migration de tête (réutiliser `scripts/verify_schema.py` ou une empreinte normalisée du schéma). Documenter que l'intégrité vérifiée n'est **pas** une authenticité : seule une signature ou un HMAC local la fournirait, à arbitrer par ADR.

Contrôles positifs du même essai : `../evil.bin`, `/abs.bin`, `originals/../../x.bin`, `originals/sub/a.bin` et `originals/..bin` sont refusés. L'extraction utilise des destinations calculées, jamais le nom du membre : pas de zip-slip, pas de lien symbolique. Une révision plus ancienne (`0000_old`) est refusée.

### MINEUR m-01 — Les fichiers non `.bin` du stockage cible sont détruits sans être sauvegardés

Les fichiers non `.bin` de `originals/` ne sont pas collectés ([`_collect_originals`](/home/nill/Desktop/compte/mescompts/backend/app/services/backups.py:298) ne prend que `*.bin`), donc la sauvegarde de sécurité ne les contient pas. Après une restauration réussie, l'ancien répertoire est supprimé par `rmtree` ([ligne 1002](/home/nill/Desktop/compte/mescompts/backend/app/services/backups.py:1002)). `exp_tamper.py` §5 : `notes-personnelles.txt` et `.abc.tmp` disparaissent, et ne sont pas dans `restore-safety-*.cfbackup`.

Recommandation : refuser la restauration si `originals/` contient autre chose que des `<uuid>.bin`, ou conserver le `.old`.

### MINEUR m-02 — Restauration acceptée alors qu'un autre processus écrit dans la cible

`exp_tamper.py` §6 : une connexion tient un `BEGIN IMMEDIATE` sur la base cible. La restauration réussit, puis le `COMMIT` de cette connexion est perdu sans erreur : il part dans l'inode déplacé. `docs/BACKUP.md` demande d'arrêter le backend, mais rien ne le vérifie.

Recommandation : tenter `BEGIN EXCLUSIVE` (ou `PRAGMA locking_mode=EXCLUSIVE` + écriture) sur la cible avant le remplacement, et refuser si la base est occupée.

### MINEUR m-03 — Copies privées résiduelles après un arrêt brutal de la sauvegarde

Un `SIGKILL` avant publication (`exp_kill_backup.py`) laisse deux choses, que rien ne nettoie ni ne signale :

- `/tmp/comptes-backup-*/database.sqlite`, une copie complète de la base (0700/0600) ;
- `.family-backup-…cfbackup.<hex>.tmp` dans `data/backups/`.

La rétention ne voit pas ces `.tmp`. La vérification extrait aussi la base complète dans `/tmp` (`comptes-verify-*`).

Recommandation : placer le staging dans `data/backups/.staging/` (0700) et purger les résidus au démarrage, comme `import_preview_store`.

### MINEUR m-04 — Compatibilité de schéma « exacte » : les sauvegardes deviendront irrestaurables à la première migration `0002`

[Lignes 564-569](/home/nill/Desktop/compte/mescompts/backend/app/services/backups.py:564) : une révision antérieure de l'historique Alembic est refusée au même titre qu'une révision inconnue. Après une mise à jour de l'application, toutes les `.cfbackup` existantes deviennent inutilisables par cet outil. Le risque est latent tant que seule `0001` existe.

Recommandation : ADR. Par exemple, accepter une révision ancêtre, restaurer, puis migrer avec la sauvegarde pré-migration.

### MINEUR m-05 — Aucune ADR pour les choix T41

Format `.cfbackup` non chiffré, clé HMAC incluse (conforme à ADR-023), compatibilité exacte, restauration uniquement vers une cible distincte, installation non atomique sur deux chemins : aucun de ces choix n'est consigné dans `docs/DECISIONS.md`, alors que la Definition of Done l'exige.

### MINEUR m-06 — Divers

- La publication repose sur `os.link` ([ligne 249](/home/nill/Desktop/compte/mescompts/backend/app/services/backups.py:249)) et sur un contrôle 0600. FAT et exFAT (clés USB), ainsi que certains montages réseau ou FUSE, n'ont ni liens physiques ni permissions POSIX. `--output` vers une clé USB échouerait donc, proprement : c'est une analyse, **non reproduite** faute de montage. À documenter : sauvegarder localement, puis copier.
- Dans [`_with_decimal_fields`](/home/nill/Desktop/compte/mescompts/backend/app/services/backups.py:619), une clé `X_decimal` déjà présente dans l'historique de décision est écrasée en silence (`exp_export.py` : `"amount_decimal":"PRE"` devient `"-123.45"`). Un seul flottant dans un `before_json`/`after_json` fait échouer l'export entier. C'est un choix « fail-closed » acceptable, mais à documenter, car T20 (IA) pourra écrire des scores.
- Rien n'exclut d'éventuels futurs secrets dans `app_settings` : la sauvegarde copie toute la table. Aujourd'hui seule la clé HMAC y figure et la clé LLM reste en variable d'environnement (vérifié dans `app_secrets.py` et `config.py`). Ajouter un test garde-fou « aucune ligne `is_secret=1` autre que la clé HMAC ».
- Les membres sont décompressés vers `/tmp` sans plafond indépendant du manifeste, que l'attaquant contrôle. Le risque de bombe de décompression est faible, car l'usage est local.
- Un import `no_op` qui supprime son archive entre le listage et la lecture des `originals` fait échouer la sauvegarde. C'est un échec bruyant, donc acceptable. Les archives d'imports commités après le snapshot sont incluses comme orphelines, sans conséquence.

## Couverture par rapport au backlog et à la matrice

| Exigence | Test nommé | Couverture |
|---|---|---|
| OPS-01 « Sauvegarde alors que WAL actif → restauration cohérente, integrity_check=ok » | `test_OPS_01_wal_backup_stays_consistent_during_concurrent_writes` | Partielle. Snapshot cohérent (parents = enfants) sous écrivain concurrent, base en WAL (vérifié : `journal_mode=wal`). En revanche : **pas de restauration** dans ce test ; aucune preuve que l'écrivain était encore actif au moment du snapshot (pas d'assertion `parents < 300`) ; base < 256 pages, donc M-01 invisible ; pas de second processus. |
| OPS-02 « Restore sur base temporaire **puis commit** ; pas de mixte ni d'orphelins ; mêmes KPI » | `test_OPS_02_restore_fixture_import_to_distinct_database_exactly`, `…rejects_corrupt…` (4 variantes), `…mid_restore_failure_rolls_back…`, `…refuses_active_database…` | Bonne pour l'égalité de toutes les tables, les sommes par compte et par mois, les empreintes, les archives et le retour arrière sur exception. **Manque « puis commit »** : aucun test ne réimporte après restauration. Je l'ai vérifié manuellement (§ Commandes : `no_op`, puis `known`, puis sommes identiques) ; à transformer en test. Pas de test de crash (M-05), de rétention à `mtime` biaisé (M-02), d'injection CSV (M-03) ni de manifeste forgé (M-06). |
| Backlog « WAL snapshot cohérent ; restore testé DB distincte, calculs inchangés » | voir ci-dessus | Satisfait. |
| Doc utilisateur exécutable telle quelle | — | Oui, si on lance depuis la racine du dépôt : `backup.sh`, `verify`, `restore.sh`, `migrate check` et `export` fonctionnent (§ Commandes). Il manque les avertissements M-03, M-04 (chemins relatifs) et M-06 (intégrité ≠ authenticité). |

Points vérifiés **conformes** :

- **Base et archives** : refus d'écraser une destination existante ; publication atomique, sans remplacement ; 0600 pour les fichiers et 0700 pour les répertoires, après restauration aussi ; refus de la base active, de son `-wal`/`-shm` et de son stockage.
- **Restauration** : sauvegarde de sécurité vérifiée de la cible existante ; second contrôle des empreintes à l'extraction, ce qui ferme le TOCTOU entre la vérification et l'extraction ; staging dans les répertoires cibles, donc pas de `rename` entre volumes.
- **Vérification** : archive tronquée, octet altéré, manifeste incohérent et schéma futur sont refusés.
- **Confidentialité** : messages d'erreur et sorties CLI sans chemin ni libellé ; argparse silencieux ; manifeste sans libellé, empreinte bancaire ni secret ; clé HMAC incluse et documentée (ADR-023) ; clé LLM absente ; exports sans `app_settings` ni `bank_fingerprint`.
- **Montants** : exacts sur des entiers arbitraires (`0`, `±1`, `±99`, `±10^13`, `2^63`) ; `bool` et `float` sont refusés.

## Commandes exécutées (copie `/tmp/revue_t41/`, Python 3.12, `uv`)

| Commande | Résultat |
|---|---|
| `uv run pytest -q backend/tests -m 'not opt_in_real_data' -p no:cacheprovider` | `3 failed, 329 passed, 2 skipped, 4 deselected`. Les 3 échecs (`test_T06_downgrade_succeeds_only_with_explicit_verified_backup`, `test_T06_schema_reference_bytes…`, `test_T06_ORM_05…`) viennent de l'ajout parallèle d'une 29e table `refund_lin…` (`assert 29 == 28`). Ils sont **hors T41**. |
| `uv run pytest -q backend/tests/integration/test_backups.py` | `10 passed` |
| `uv run ruff check` + `ruff format --check` (backups.py, test_backups.py) | `All checks passed!`, `2 files already formatted` |
| `uv run mypy backend/app/services/backups.py` (strict) | `Success: no issues found in 1 source file` |
| `bash -n scripts/backup.sh scripts/restore.sh` | OK. `set -euo pipefail` présent, arguments transmis par `"$@"`, guillemets corrects. **shellcheck non installé** : non exécuté. |
| E2E réel : `e2e_setup.py` (scenario_famille_2026 + overlap_A), puis `DATABASE_URL=… DATA_DIR=… scripts/backup.sh` | `backup=created-and-verified`, fichier 0600 dans `src-data/backups/` |
| `scripts/backup.sh --output …/custom.cfbackup --retention 3`, puis à nouveau | `created-and-verified`, puis `backup_error=Le fichier de destination existe déjà; il n'a pas été écrasé.` (rc 1) |
| `uv run python -m app.services.backups verify <family-backup>` | `backup=verified` |
| `scripts/restore.sh <backup> --target-db …/restored/r.sqlite --target-data-dir …/restored/r-data` | `restore=verified` |
| `scripts/restore.sh <backup> --target-db <base active> …` | `backup_error=La restauration exige une base distincte de la base active.` (rc 1) |
| `DATABASE_URL=sqlite:///…/r.sqlite uv run python -m app.db.migrate check` | `status=ok revision=0001_initial head=0001_initial` |
| `e2e_compare.py` | `tables identical: True` (30 tables, 171 transactions, 171 source_rows), `sum cents per account/month identical: True`, `originals identical: True 2`, permissions `0o600 0o700 ['0o600']` |
| `export --format json` / `--format csv` | `export=created`, fichiers 0600 |
| `exp_reimport.py` (restauration, puis réimport) | `re-import A on restored: 200 no_op` ; `overlap_B … statuses: ['known']` ; `import B on restored: committed` ; mêmes sommes que la source après le même import |
| `ulimit -f 120; scripts/backup.sh` | `backup_error=La capture cohérente de la base SQLite a échoué.`, aucun fichier publié ni résidu |
| `exp_livelock.py`, `exp_retention.py`, `exp_export.py`, `exp_kill.py`, `exp_kill_backup.py`, `exp_tamper.py` | voir M-01 à M-06 et m-01 à m-03 |

Les scripts jetables sont dans `/tmp/revue_t41_scripts/`. Ils sont exécutés par `uv run python -I` depuis `/tmp/revue_t41`, et le helper commun est chargé par `exec`.

## Recommandations prioritaires

1. M-01 : `pages=-1` ou `VACUUM INTO`, plus une échéance ; ajouter un test OPS-01 sur une base de plus de 256 pages avec un écrivain soutenu, dans un second **processus**.
2. M-02 : rétention qui ne touche ni le fichier courant ni la dernière sauvegarde vérifiée, ordonnée par l'horodatage du nom ou du manifeste, limitée à `data/backups/` ; tests avec `mtime` futur et avec archives corrompues.
3. M-03 : neutralisation des cellules texte, documentée et testée.
4. M-04 : résolution des chemins relatifs, `.gitignore` et garde INV-11 (orchestrateur).
5. M-05 : répertoire cible unique ou journal de restauration et `--recover` ; test de crash par sous-processus et `SIGKILL`.
6. M-06 : contrôle du schéma restauré ; avertissement « intégrité ≠ authenticité » dans BACKUP.md.
7. Convertir le réimport après restauration en test `test_OPS_02_…_then_commit`, et ajouter une restauration dans OPS-01.
8. ADR T41 (m-04, m-05).

Proposition pour `docs/REGISTRE_RISQUES.md` : rattacher M-01, M-02 et M-05 à R-07 (« ouvert, mitigé partiellement par T41 »), et M-03, M-04 et M-06 à un risque de confidentialité et de sécurité des exports et sauvegardes.

## Retour au format AGENTS.md

```text
Ticket : T41 (revue indépendante, rôle OPS — non-auteur)
Objectif livré : revue adversariale de backups.py, des scripts backup/restore, de BACKUP.md et de test_backups.py ; verdict PASS sous réserves (0 BLOQUANT, 6 MAJEURS, 6 MINEURS).
Fichiers changés : docs/revues/REVUE_T41.md (seul fichier écrit dans le dépôt réel).
Interfaces modifiées : aucune.
Tests ajoutés : aucun dans le dépôt ; scripts jetables /tmp/revue_t41_scripts/{common,exp_reimport,exp_export,exp_retention,exp_livelock,exp_kill,exp_kill_backup,exp_tamper,exp_walmode,e2e_setup,e2e_compare}.py.
Commandes réellement exécutées + résultats : voir « Commandes exécutées » (suite 329 passed / 3 failed hors T41 ; test_backups 10 passed ; ruff, format et mypy OK ; bash -n OK ; shellcheck absent ; E2E backup, verify, restore, compare et export OK).
Invariant vérifié : INV-02 (montants exportés exacts depuis des entiers, float refusé) ; INV-11 (manifeste, erreurs et CLI sans PII, mais M-04 : une sauvegarde ou un export peut entrer dans Git sans être détecté) ; idempotence du réimport après restauration (no_op, compte known).
Limites / cas non couverts : shellcheck non disponible ; FAT/exFAT non reproduit (aucun montage possible sans root) ; comportement de SQLite face à un -wal orphelin après un kill @2 non testé ; aucun test multi-processus de deux restaurations simultanées.
Risques / sécurité / données privées : aucune donnée réelle ouverte ; .env copié par la recette rsync supprimé de la copie sans lecture ; répertoires résiduels /tmp/comptes-backup-* créés par mes propres essais (kill, os._exit) supprimés ; bases synthétiques laissées sous /tmp/revue_t41_work_*.
Relecture recommandée : correction des M-01 à M-06 par l'auteur T41, puis contre-revue ciblée (rétention, crash de restauration, contrôle de schéma) ; arbitrage orchestrateur pour M-04 (.gitignore, garde) et m-04/m-05 (ADR).
```
