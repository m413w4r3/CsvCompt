# Sauvegarde et restauration locales

Cette procédure protège une base SQLite en mode WAL et les archives `.bin` des imports. Une sauvegarde est un fichier `.cfbackup` privé qui contient un snapshot SQLite, les sources d'import disponibles et un manifeste versionné. Le snapshot utilise l'API SQLite `Connection.backup()` en une passe (`pages=-1`) ; il inclut les transactions commises présentes dans le WAL.

La durée maximale d'un snapshot est configurable avec `COMPTES_BACKUP_TIMEOUT_SECONDS` (secondes entières, défaut `30`). À l'échéance, le processus de copie est arrêté, le staging incomplet est supprimé et la commande signale `BACKUP_TIMEOUT`. Le même délai et la même copie en une passe protègent la sauvegarde pré-migration.

## Sauvegarder

```sh
scripts/backup.sh
```

Le fichier vérifié est créé par défaut dans `data/backups/`. La rétention s'applique uniquement à ce répertoire automatique : elle vérifie les archives dont le nom respecte le motif de l'application, les trie par horodatage du nom et ne compte que les archives valides. Elle conserve toujours la sauvegarde nouvellement publiée et au moins une sauvegarde vérifiée. `--output` désactive la rétention, même si le nom du fichier ressemble à une sauvegarde automatique.

On peut choisir un emplacement et une rétention automatique différente :

```sh
scripts/backup.sh --output /chemin/prive/sauvegarde.cfbackup
scripts/backup.sh --retention 12
```

Les chemins relatifs fournis aux scripts sont interprétés depuis le répertoire courant de l'appelant. Sous la racine du dépôt, les sorties ne sont admises que dans des dossiers ignorés (`data/`, `exports/`, `.staging/`) ; choisissez de préférence un dossier privé extérieur au dépôt. Les fichiers `.cfbackup` sont ignorés par Git et la garde de confidentialité refuse les sauvegardes et exports `family-export-*.json` / `family-export-*.csv`, suivis ou non.

Le staging de toute sauvegarde, y compris avec `--output`, réside dans `data_dir/backups/.staging/` avec le mode `0700`. Un appel suivant purge les résidus laissés par un arrêt brutal. Si la destination se trouve sur un autre système de fichiers, l'archive est copiée dans un temporaire privé adjacent à la destination pour préserver la publication atomique ; le prochain appel visant ce dossier purge ces temporaires applicatifs. La publication finale refuse d'écraser un fichier existant. Le fichier porte les permissions `0600` et le répertoire automatique `0700`.

Le manifeste expose seulement la version du format et du schéma, l'horodatage UTC, les empreintes SHA-256 des fichiers contenus, quelques comptes de lignes, les résultats d'intégrité SQLite et l'indication de présence de la clé HMAC. Il ne contient ni libellé, compte bancaire, empreinte d'identité, ni valeur secrète.

La clé HMAC d'identité des comptes est conservée dans `app_settings` avec `is_secret=1`. Conformément à l'ADR-023, elle est incluse dans la base sauvegardée : sans cette clé, un réimport calculerait de nouvelles empreintes et ne reconnaîtrait plus automatiquement les comptes existants. Aucun autre réglage marqué secret n'est accepté dans une sauvegarde. La sauvegarde est une donnée privée au même niveau que la base. La clé LLM n'est pas stockée dans cette base et ne fait pas partie de la sauvegarde. Le format `.cfbackup` n'est pas chiffré : conservez-le dans un emplacement privé et chiffrez le support externe si nécessaire.

La publication emploie un lien physique atomique et des permissions POSIX. FAT/exFAT, certains partages réseau et des systèmes FUSE peuvent ne pas prendre en charge ces opérations ; créez alors la sauvegarde sur un disque local pris en charge, puis copiez le fichier vérifié vers le support externe. La cible de restauration doit aussi permettre les liens symboliques et leur remplacement atomique : restaurez sur un système de fichiers local compatible.

## Vérifier

```sh
uv run python -m app.services.backups verify data/backups/family-backup-....cfbackup
```

La vérification contrôle le format et le manifeste, les SHA-256, `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, les comptes de tables clés, la présence des sources référencées et le schéma `sqlite_master` (tables, index, triggers et vues, avec SQL normalisé). Le schéma attendu est produit à l'exécution en appliquant la migration correspondant à la révision déclarée. Une révision connue ancêtre de la tête installée est admise ; une révision inconnue est refusée.

Les tailles sont bornées indépendamment du manifeste : au plus 256 Mio par membre et 1 Gio au total après décompression. Les fichiers décompressés pour vérifier une sauvegarde automatique sont placés dans `data/backups/.staging/`, puis supprimés.

**L'intégrité n'est pas l'authenticité.** SHA-256, les contrôles SQLite et la comparaison au schéma détectent des altérations accidentelles ou des incohérences structurelles. Le manifeste n'est pas signé : une personne capable de modifier l'archive et de recalculer les empreintes peut toujours falsifier ses données tout en conservant un schéma valide.

## Tester une restauration sans toucher à la base active

La base et ses archives sont publiées ensemble sous une génération. Utilisez ces chemins :

```sh
restore_root=/chemin/prive/test-restore
scripts/restore.sh data/backups/family-backup-....cfbackup \
  --target-db "$restore_root/current/database.sqlite" \
  --target-data-dir "$restore_root/current/data"
```

Fermez l'application avant la restauration. La cible doit être distincte de la base active et respecter le layout `current/database.sqlite` + `current/data`. Les composants sont construits et vérifiés sous `generations/<jeton>/`, puis un unique remplacement atomique du lien `current` publie la génération complète. Un arrêt avant ce remplacement laisse l'ancienne génération active ; un arrêt après laisse la nouvelle. Une nouvelle restauration purge les stages et marqueurs orphelins avant de recommencer. Les générations précédentes restent dans `generations/` afin de ne pas supprimer une base encore ouverte par une connexion ; après fermeture de l'application, elles peuvent être supprimées manuellement.

Pour une archive située dans `data/backups/`, la restauration en fait une copie privée `.restore-archive-*.tmp` sous la racine cible pendant qu'elle détient les verrous source et cible ; une rétention concurrente ne peut donc pas supprimer la source en cours de restauration. Un arrêt brutal laisse cette copie en mode `0600`, supprimée au prochain appel de restauration.

Si la cible existe, une sauvegarde de sécurité `.cfbackup` est créée dans sa génération actuelle avant la bascule. Le remplacement tente `BEGIN EXCLUSIVE` sur la base cible et refuse la restauration si un autre processus y détient une écriture. Le répertoire cible `originals/` doit contenir uniquement des fichiers `<uuid>.bin`; les fichiers supplémentaires ne sont ni détruits ni oubliés silencieusement.

Une sauvegarde d'une révision ancêtre connue est vérifiée contre son schéma historique, restaurée dans une génération privée, puis migrée à la tête par `upgrade_to_head`. Cette migration crée sa propre sauvegarde vérifiée pré-migration dans la génération avant toute évolution du schéma. La génération n'est publiée qu'après réussite de la migration et des nouveaux contrôles.

Après un test, contrôlez le schéma de la copie sans la démarrer dans l'application :

```sh
DATABASE_URL="sqlite:////chemin/prive/test-restore/current/database.sqlite" \
  uv run python -m app.db.migrate check
```

Comparez ensuite les opérations et décisions dans l'application. Ne restaurez jamais sur la base courante : choisissez une cible distincte, puis validez son contenu avant d'en faire votre nouvelle base de travail.

## Exporter opérations, ventilations et décisions

```sh
uv run python -m app.services.backups export --format json --output /chemin/prive/family-export-20261010.json
uv run python -m app.services.backups export --format csv --output /chemin/prive/family-export-20261010.csv
uv run python -m app.services.backups export --format csv --delimiter ';' \
  --output /chemin/prive/family-export-20261010-fr.csv
```

Le séparateur CSV par défaut est la virgule ; utilisez `--delimiter ';'` pour un tableur configuré en français. Toute cellule texte commençant par `=`, `+`, `-`, `@`, tabulation ou retour chariot est préfixée par une apostrophe. `amount_cents` reste un entier et `amount_decimal` conserve son signe décimal : ces deux champs monétaires ne sont pas neutralisés.

JSON sépare les collections `transactions`, `allocations` et `decisions`. Les montants sont fournis en centimes entiers (`amount_cents`) et en chaîne décimale exacte (`amount_decimal`, point comme séparateur), sans calcul flottant. Dans les champs JSON de décision, une clé `*_decimal` déjà présente est remplacée par la représentation exacte dérivée de son champ `*_cents`; une valeur numérique flottante dans ces décisions fait échouer l'export (comportement fail-closed).

Les exports ne comprennent pas `app_settings`, la clé HMAC, les empreintes bancaires ou d'identité, ni la clé LLM. Les libellés des opérations restent des données privées ; les exports sont créés en `0600` et ne doivent pas être ajoutés au dépôt.

## En cas d'erreur

Ne supprimez pas l'archive d'origine ni la sauvegarde de sécurité créée avant une restauration. Une sauvegarde corrompue, au schéma inconnu, ou contenant un trigger absent/modifié est refusée avant la publication. En cas d'occupation de la cible, arrêtez les connexions à cette base puis relancez la restauration. En cas d'arrêt brutal, relancez la restauration vers le même layout : le pointeur sélectionne intégralement l'ancienne ou la nouvelle génération. Vérifiez la sauvegarde de sécurité avant de supprimer une génération précédente.
