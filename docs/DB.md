# Base SQLite et migrations

## Source de vérité

`docs/examples/schema.sql` est le schéma SQLite de référence gelé au gate G0. La révision `0001_initial` exécute le fichier embarqué `backend/app/db/migrations/sql/0001_initial.sql`, dont les octets doivent rester identiques à la référence. Les tests comparent cette identité, puis comparent tables, index et triggers de `sqlite_master` après migration.

Les modèles SQLAlchemy 2.x de `backend/app/db/models.py` couvrent toutes les tables. Les montants restent des `Integer` en centimes signés; les dates et instants sont convertis depuis et vers du texte SQLite ISO, avec des objets `date` et des `datetime` conscients du fuseau UTC côté Python. Les clés d'entités sont des UUIDv4 texte générés côté application. Les champs `version` activent le verrouillage optimiste de l'ORM.

Les modèles décrivent les colonnes et les relations utiles. Ils ne remplacent pas les `CHECK`, index partiels et triggers de la référence; ces gardes restent l'autorité de la base. La création par `metadata.create_all()` n'est pas prise en charge. La colonne générée `expires_at_utc` est produite par SQLite et n'apparaît pas dans `PRAGMA table_info`; elle est contrôlée avec `PRAGMA table_xinfo`.

Chaque connexion issue de `create_sqlite_engine()` active `foreign_keys` et `recursive_triggers`, ainsi que le mode WAL et les paramètres de concurrence. L'environnement Alembic utilise cette même fabrique et les paramètres `Settings`.

## Appliquer et contrôler les migrations

À la racine du dépôt, l'URL vient de `DATABASE_URL` (sinon `Settings.database_url`):

```sh
uv run python -m app.db.migrate upgrade
uv run python -m app.db.migrate current
uv run python -m app.db.migrate check
```

`upgrade` crée les répertoires manquants avec des droits privés raisonnables et crée une nouvelle base fichier en mode `0600`. `current` affiche la révision enregistrée. `check` compare le schéma présent avec la tête linéaire d'Alembic et retourne un code non nul si la base est absente ou obsolète. Le contrôle health n'ouvre pas une base absente; il expose `not_initialized`, `outdated` ou `ok`, et inclut la révision connue pour une base existante.

Avant une migration en attente sur une base contenant déjà des objets SQLite, `upgrade` crée une image cohérente avec l'API `sqlite3.Connection.backup()`. Cette API inclut les pages encore en WAL; une copie du seul fichier principal n'est pas utilisée. La sauvegarde va dans `data/backups/`, est vérifiée par `PRAGMA integrity_check`, puis accompagnée d'un manifeste minimal avec révision source, taille et SHA-256. Le fichier et le manifeste ont le mode `0600`; le répertoire de sauvegardes nouvellement créé a le mode `0700`. Si la sauvegarde ou sa vérification échoue, l'upgrade s'arrête avant Alembic. La copie complète de SQLite préserve aussi les secrets présents dans `app_settings`, dont la clé HMAC si elle existe.

Cette sauvegarde pré-migration est une copie de la base SQLite. Elle ne fabrique pas une archive complète des CSV originaux. Les sauvegardes applicatives plus larges doivent aussi inclure les fichiers `data/originals/` selon leur propre procédure.

## Restaurer manuellement une sauvegarde de migration

Arrêter le backend avant de restaurer. Garder une copie de la base actuelle et de ses fichiers `-wal` / `-shm`, puis vérifier la sauvegarde sans l'ouvrir en écriture:

```sh
sqlite3 data/backups/<sauvegarde>.sqlite 'PRAGMA integrity_check;'
```

Le résultat doit être `ok`. Copier la sauvegarde dans un fichier temporaire situé à côté de la base cible, vérifier ce fichier une seconde fois, puis lui donner le mode `0600`. Une fois le backend arrêté, déplacer l'ancienne base et ses éventuels fichiers WAL/SHM à l'écart, installer atomiquement le temporaire sous le nom de la base, puis relancer `uv run python -m app.db.migrate check`. Ne jamais remplacer une base ouverte par SQLite; ne jamais réutiliser un WAL provenant de l'ancienne base avec l'image restaurée.

## Downgrade

Un downgrade vers `base` est permis si les tables métier sont vides. Dès qu'une table contient des données, la révision refuse le downgrade par défaut. Après avoir créé et vérifié une sauvegarde avec les mêmes garanties de mode, manifeste et `integrity_check`, l'action destructive exige les deux variables explicites et le chemin de cette sauvegarde:

```sh
COMPTES_ALLOW_DATA_LOSS_DOWNGRADE=1 \
COMPTES_DOWNGRADE_BACKUP=data/backups/<sauvegarde>.sqlite \
uv run alembic -c alembic.ini downgrade base
```

Une révision appliquée n'est jamais réécrite. En cas de défaut, créer une nouvelle migration corrective vers l'avant; ne pas restaurer une sauvegarde sans avoir contrôlé la cohérence des fichiers liés.

## Ajouter une migration

1. Vérifier la tête courante et convenir du nouveau changement de schéma avec le propriétaire des migrations du lot.
2. Ajouter un seul fichier `backend/app/db/migrations/versions/NNNN_description.py`, dont `down_revision` pointe sur l'unique tête précédente. Utiliser du SQL SQLite explicite pour les contraintes, index partiels et triggers; ne pas compter sur l'autogénération à partir des modèles.
3. Fournir `upgrade()` et un `downgrade()` qui refuse explicitement les pertes de données. Si la restauration est la seule voie sûre, le downgrade doit exiger une sauvegarde vérifiée et un choix explicite.
4. Ajouter des tests sur fichier temporaire couvrant la nouvelle révision, les invariants touchés, la parité attendue et la sauvegarde. Ajouter un test ou une mise à jour contrôlée de la référence SQL lorsqu'elle change; ne pas modifier `0001_initial.sql` après application.
5. Exécuter les vérifications du ticket et garder les révisions linéaires. Une évolution du schéma de référence se fait par une migration nouvelle et une revue contrôlée, jamais en changeant silencieusement l'historique.
