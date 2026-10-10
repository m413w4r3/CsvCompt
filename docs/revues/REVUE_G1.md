# Revue critique indépendante — Gate G1 (T05, T06, T07, T08)

- **Relecteur** : Claude Opus 5.5 (session indépendante, non auteur des livrables)
- **Date** : 2026-10-10
- **Objet revu** : `HEAD` = `9dcb3f0` (fichiers non commités du dépôt ignorés : `docs/ETAT_IMPLEMENTATION.md`, `env.example`, travaux `backend/app/importers` en cours)
- **Méthode** : copie propre `git archive HEAD | tar -x -C /tmp/revue_g1/`, `uv sync`, `npm ci`, index Git local sans commit (`git init && git add -A`) pour que les scripts fondés sur `git ls-files` fonctionnent. Toutes les expériences destructives ont été faites dans `/tmp` uniquement. `csv_references/` et `data/` n'ont jamais été lus. Aucune donnée réelle n'apparaît ci-dessous ; toutes les valeurs (IBAN, carte, libellés, clés) sont synthétiques.
- **Verdict** : **G1 PASS sous réserves** (voir §4). 0 BLOQUANT, 7 MAJEUR, 16 MINEUR.

---

## 1. Exécution de la suite sur environnement propre

| Commande (dans `/tmp/revue_g1`) | Résultat réel |
|---|---|
| `uv sync` | exit 0 |
| `uv run pytest -q -m "not opt_in_real_data" backend/tests` | `32 passed, 1 warning in 1.34s` (warning : `StarletteDeprecationWarning` httpx) |
| `uv run ruff check .` | `All checks passed!` |
| `uv run ruff format --check backend` | exit 0 |
| `uv run mypy backend/app` | `Success: no issues found in 21 source files` |
| `cd frontend && npm ci` | exit 0 (avertissement `allow-scripts` pour `esbuild` postinstall, sans effet sur le build) |
| `npm run lint` / `npm run typecheck` | exit 0 / exit 0 |
| `npm run test -- --run` | `Test Files 3 passed (3)`, `Tests 37 passed (37)` |
| `npm run build` | `✓ built in 1.17s` (JS 361 kB, CSS 11,75 kB) |
| `npm audit` | `found 0 vulnerabilities` |
| `python3 -I scripts/verify_schema.py` | exit 0 (toutes les lignes `OK …`, dont SEC-06, IMP-06, IMP-07, ADR-019, ADR-024) |
| `python3 -I scripts/verify_metrics_examples.py` | exit 0 |
| `python3 -I backend/tests/fixtures/verify_fixtures.py` | exit 0 |
| `./scripts/check_no_private_data.sh --with-untracked` | `OK : aucune donnée privée détectée (180 fichier(s) inspecté(s)).` |
| `./scripts/verify_contracts.sh` | exit 0 (`OK dossier : 46 tickets, 12 gates, 87 IDs de catégories, 81 tests référencés.`) |
| `CI=1 ./scripts/test_e2e_smoke.sh` | `2 passed (1.4s)` (Chromium Playwright déjà présent localement) |

Remarque : un premier passage sans index Git échoue (`test_CI_01_aucune_donnee_interdite_suivie_par_git` → `fatal: not a git repository`, `check_no_private_data.sh` → exit 2). Ce comportement est attendu (les gardes inspectent `git ls-files`), mais la suite n'est donc pas exécutable depuis une archive non versionnée (voir m-17).

Preuve que les tests ne touchent jamais `data/` : création de `data/db/comptes.sqlite` sentinelle, `chmod 000 data`, puis `uv run pytest -q -m "not opt_in_real_data" backend/tests` → `32 passed`. Le fichier sentinelle est intact.

---

## 2. Constats

### BLOQUANT

Aucun pour les critères G1 proprement dits. **MAJ-01 et MAJ-02 doivent toutefois être levés avant le premier endpoint d'écriture** (LOT 2/3), sinon ils deviennent bloquants pour G2/G3.

### MAJEUR

#### MAJ-01 — `get_db` commite **après** l'envoi de la réponse : un échec d'écriture est renvoyé comme un succès, sans aucun log (T05/T06, `backend/app/db/session.py:65-80`)

- **Preuve** : FastAPI installé `0.143.0`. Depuis FastAPI 0.118, le code de sortie d'une dépendance `yield` à portée « request » s'exécute après l'envoi de la réponse. Expérience (base migrée temporaire) :

  ```python
  @app.post("/api/v1/probe_bad")
  def bad(db: DbSession):
      db.add(Account(alias="Synthétique", account_role="role_invalide"))  # CHECK violé au flush
      return {"created": True}
  ```

  Sortie réelle :

  ```text
  fastapi 0.143.0
  ok : 200 {"created":true}
  bad: 200 {"created":true}
  lignes en base: [('current', 1)]
  statut 200 | logs capturés: ''
  ```

- **Impact** : intégrité et traçabilité. Le client reçoit `200` et la donnée n'existe pas. Le même mécanisme masquera un `database is locked` au commit, une violation de trigger ou un échec de FK différée. Aucun log n'est écrit et aucun `INTERNAL_ERROR` ni `DATABASE_BUSY` n'est renvoyé. Cela contredit la priorité n°1 (intégrité des données).
- **Correction proposée** : ne jamais commiter dans le teardown de la dépendance. Deux options : `Depends(get_db, scope="function")` (FastAPI ≥ 0.121), ou bien une session sans commit implicite, avec un commit explicite dans la couche service avant de construire la réponse. Ajouter un test de contrat : CHECK violé → `4xx/5xx` normalisé et 0 ligne.
- **Ticket porteur** : T05 (fabrique, dépendance) avec T06. **À corriger avant T09+ en écriture.**

#### MAJ-02 — Sessions ORM en `BEGIN` différé : un conflit concurrent donne `database is locked` immédiat au lieu de `StaleDataError` / `409 VERSION_CONFLICT` (T05/T06, `session.py:39-44, 65-74`)

- **Preuve** : deux `Session(eng)` lisent la même ligne `accounts`. S1 modifie et commite, puis S2 modifie et commite :

  ```text
  == verrou optimiste (deux sessions)
    -> OperationalError database is locked après 0.000s (busy_timeout=5s)
  ```

  Le snapshot de lecture WAL de S2 est périmé : SQLite renvoie `SQLITE_BUSY_SNAPSHOT` sans attendre le `busy_timeout`. Le `version_id_col` n'est donc jamais atteint. Le chemin `immediate_transaction()` n'existe que pour une `Connection` brute ; `session_scope`/`get_db` émettent `BEGIN` (différé).
  Contrôle positif : `BEGIN IMMEDIATE` inter-processus sérialise correctement. Le second processus attend ~1 s puis réussit : `[('acquired', 0.0), ('released', 1.0), ('acquired', 1.03), ('released', 1.03)] exit codes 0 0`.
- **Impact** : le contrat API (`409 VERSION_CONFLICT`, `503 DATABASE_BUSY` réessayable) est irréalisable avec la dépendance fournie. Les conflits se traduiront en `500 INTERNAL_ERROR`. ADR-019(a) exige `BEGIN IMMEDIATE` pour les écritures.
- **Correction proposée** : fournir une session d'écriture (`write_session_scope` / dépendance dédiée) qui pose `_begin_immediate` sur la connexion de la session. Par exemple, `Session(bind=conn)` après `immediate_transaction`, ou l'événement `after_begin` de la session avec un drapeau. Mapper `OperationalError(SQLITE_BUSY*)` → `DATABASE_BUSY` (503) et `StaleDataError` → `VERSION_CONFLICT` (409). Tester deux sessions concurrentes.
- **Ticket porteur** : T05/T06, à lever avant T11 (commit d'import) et T16+ (revue).

#### MAJ-03 — Les logs d'accès uvicorn contournent la rédaction : chemins et query strings écrits en clair (T05, `core/logging.py:62-73`, `__main__.py`)

- **Preuve** : `uv run python -m app` avec des valeurs synthétiques dans l'URL. Les valeurs sont remplacées ci-dessous par des marqueurs, pour ne pas déclencher la garde IBAN du dépôt ; elles apparaissaient **en clair** dans le log.

  ```text
  INFO:     127.0.0.1:58356 - "GET /api/v1/health?iban=<IBAN-FR76-synthétique-27-car.>&carte=<carte-synthétique-16-chiffres-espacés>&q=LIBELLE%20PRIVE%20BOULANGERIE HTTP/1.1" 200 OK
  INFO:     127.0.0.1:58360 - "GET /api/v1/nope/<carte-synthétique-16-chiffres> HTTP/1.1" 404 Not Found
  ```

  `configure_logging()` n'installe le filtre que sur les handlers du logger racine. Or `uvicorn.access` et `uvicorn.error` ont leurs propres handlers (`propagate=False`) : ces lignes ne sont ni JSON ni rédigées.
- **Impact** : INV-11 (aucune donnée privée dans les logs). Les futurs endpoints de recherche, de filtres ou d'IDs, ainsi que toute URL erronée contenant un libellé ou un numéro, seront journalisés en clair.
- **Correction proposée** : passer un `log_config` à `uvicorn.run` qui route `uvicorn.*` via `JsonFormatter` + `RedactionFilter`. Alternative : `access_log=False` plus un middleware d'accès maison qui journalise méthode, route **template**, statut et durée, sans query string. Ajouter un test sur un vrai processus.
- **Ticket porteur** : T05 (puis T42).

#### MAJ-04 — Une erreur de validation des `Settings` affiche la fin de `LLM_API_KEY` sur stderr (T05, `core/config.py:54-61`)

- **Preuve** : `APP_HOST=0.0.0.0 LLM_API_KEY="sk-revuecanary123456789" uv run python -m app` :

  ```text
  Value error, APP_HOST doit être loopback (...) [type=value_error, input_value={'app_host': '0.0.0.0', '...k-revuecanary123456789'}, input_type=dict]
  ```

  Le `model_validator(mode="after")` lève une erreur dont `input_value` est le dict brut. Pydantic le tronque au milieu, mais en conserve la fin, ici la clé.
- **Impact** : interdiction n°3 (aucune clé dans les logs). Une mauvaise configuration banale (hôte, port) expose environ les 20 derniers caractères de la clé du fournisseur dans le terminal ou les journaux de service.
- **Correction proposée** : effectuer le contrôle loopback dans un `field_validator` sur `app_host` (avec `ValidationInfo` pour `allow_non_loopback`), ou attraper `ValidationError` dans `get_settings()`/`__main__` et réémettre un message sans `input_value` (`e.errors(include_input=False)`). Ajouter un test : clé canari absente de `str(exc)` pour chaque erreur de configuration.
- **Ticket porteur** : T05.

#### MAJ-05 — Divergence de contrat : enveloppe d'erreur et champs de `/health` (T05 ; documentation T02)

- **Preuve** : `docs/API.md:47` définit l'erreur normalisée à plat (`{"code":"VERSION_CONFLICT","message":…,"details":…}`). L'implémentation (`core/errors.py:41-50`) et `docs/FRONTEND_DEV.md:27` utilisent `{"error":{"code","message","details"}}`. Réponse réelle : `{"error":{"code":"NOT_FOUND","message":"Ressource introuvable.","details":{}}}`.
  Pour `/health`, `docs/API.md:74,77` ne liste que 4 champs. L'implémentation renvoie aussi `version`, `time_utc`, `database{status,revision}`, `llm{enabled}`, `offline_ready`. Ces champs sont documentés seulement dans `docs/BACKEND_DEV.md:14`, et `llm` n'y figure pas.
- **Impact** : l'interdiction n°6 exclut tout changement silencieux de signature d'API. Un agent qui suivrait `API.md` produirait des erreurs que le client frontend ne décode pas (repli sur « La requête a échoué. »). La révision Alembic exposée dans `health` n'est pas non plus prévue au contrat.
- **Correction proposée** : choisir une forme et l'acter par une ADR. L'enveloppe `error` est recommandée, puisque frontend et backend l'emploient déjà. Mettre à jour `API.md` (exemple + champs additionnels de health). Ajouter un test de contrat qui compare l'exemple d'`API.md` à la réponse.
- **Ticket porteur** : T05 + arbitrage orchestrateur (ADR).

#### MAJ-06 — Le schéma accepte des clés primaires `NULL` (plusieurs) et non-UUID ; l'ORM le reflète (`nullable=True`) (T06 + ADR ; schéma G0)

- **Preuve** : sur base migrée :

  ```text
  insert NULL pk #0 accepté
  insert NULL pk #1 accepté
  lignes à PK NULL: 2
  PK non-UUID acceptée
  ```

  Cause : `account_id TEXT PRIMARY KEY` sans `NOT NULL`. Une table SQLite rowid accepte alors `NULL` dans une PK non `INTEGER`, et ce cas se répète sur 23 tables (`docs/examples/schema.sql:9,27,93,…`). `models.py` déclare `primary_key=True, nullable=True`.
- **Impact** : ADR-013 (UUID opaques) n'est garanti que par la convention ORM. Une ligne à PK `NULL` n'est plus adressable par l'API ni référençable par FK (une FK `NULL` n'est pas contrôlée). C'est la catégorie d'intégrité « en base » qu'ADR-019 voulait fermer.
- **Correction proposée** : ADR, puis migration `0002` (sans réécrire `0001`) qui ajoute des triggers `BEFORE INSERT/UPDATE` refusant `NEW.<pk> IS NULL` ou un format non UUIDv4 (`length=36` + GLOB). La reconstruction des tables avec `NOT NULL` est une alternative. Ajouter le cas à `verify_schema.py`.
- **Ticket porteur** : T06 (propriétaire des migrations) après arbitrage.

#### MAJ-07 — La migration via la CLI Alembic standard contourne la sauvegarde pré-upgrade (T06, `migrations/env.py`)

- **Preuve** : base existante synthétique (`legacy`, 1 ligne), `DATABASE_URL=… DATA_DIR=… uv run alembic upgrade head` → `Running upgrade -> 0001_initial`. Aucun répertoire `backups/` n'est créé : le dossier ne contient que `legacy.sqlite`.
- **Impact** : OPS-02 et ADR-016 exigent une « sauvegarde vérifiée avant upgrade ». La commande Alembic, réflexe habituel et installée dans l'environnement, migre sans filet. Seul `python -m app.db.migrate upgrade` sauvegarde.
- **Correction proposée** : dans `env.py`, refuser `upgrade` si `config.attributes` ne porte pas un marqueur posé par `app.db.migrate`, ou y appeler la même routine de sauvegarde. Le documenter dans `DB.md`. Ajouter un test : `command.upgrade` sans marqueur sur une base non vide → refus.
- **Ticket porteur** : T06.

### MINEUR

| ID | Constat et preuve | Impact | Correction proposée | Ticket |
|---|---|---|---|---|
| m-01 | **Mobile (375 px)** : la barre latérale est translatée hors écran sans `visibility:hidden`/`inert`. Séquence Tab réelle : `Aller au contenu → Vue d'ensemble[HORS ÉCRAN] → … → Réglages[HORS ÉCRAN] → Ouvrir le menu`, soit 10 arrêts invisibles. `Échap` ne ferme pas le menu (`encore ouvert=true`) et le focus n'entre pas dans le menu ouvert. | WCAG 2.4.7 / 2.4.11 ; usage clavier mobile. | `visibility:hidden` ou `inert` quand le menu est fermé, fermeture sur Échap avec retour du focus, focus initial dans le menu. | T07 |
| m-02 | **Statut health invisible sur mobile** sans ouvrir le menu (`badgeVisibleSansMenu=false` à 375 px, dans les 5 états). Capture : en-tête limité à ☰, « Espace privé », thème. | Acceptation T07 : « health affiché ». | Reprendre un indicateur compact dans la topbar (< 760 px). | T07 |
| m-03 | **Contrastes thème clair** (axe-core, 1280 px) : `.breadcrumb > span:first-child` 2,42:1 ; `.note-kicker` 2,63:1 ; texte du pied de page 2,42:1 (fg `#98a29c`). Thème sombre : 0 violation. Aucun débordement horizontal à 375/768/1280. | WCAG 1.4.3 (AA ≥ 4,5:1). | Foncer `--ink-faint` en clair (≈ `#6b746f` ou plus). | T07 |
| m-04 | **Smoke E2E** : la page reçoit la réponse health par `page.route(...).fulfill(backendHealth)`. Le fetch réel et le proxy Vite `/api` → `127.0.0.1:9513` ne sont jamais exercés (le backend smoke tourne sur un port aléatoire). Hors CI, Chromium absent → `exit 0` « SKIPPED ». Le relecteur a vérifié la chaîne réelle à la main : migrate ×2, `python -m app`, `vite` → badge réel « Serveur local connecté », seul hôte contacté `127.0.0.1:<vite>`. | Le critère G1 « consomme health » n'est pas prouvé automatiquement ; `make check` peut être vert sans E2E. | Rendre la cible du proxy configurable (`BACKEND_URL`) et retirer `route.fulfill` dans un des deux tests ; afficher clairement le SKIP dans `make check`. | T08 |
| m-05 | **Faux négatifs des gardes** (sondes sur copies jetables). Les gardes ne détectent pas : `insert(t).prefix_with("OR REPLACE")`, `def f(c) -> float`, `c / 100`, `comptes.db`, `export.zip`, `releve.xlsx`, `releve.csv.txt`, `backend/tests/fixtures/private/vrai.csv` sans 8 chiffres consécutifs, IBAN non FR, numéro de carte dans un `.py`, `COMPTES_REAL_CSV` ou `data/…` ajoutés à `test_e2e_smoke.sh`, `cat csv_references/x.csv` ajouté à `ci.yml`. Ils détectent bien : `x.csv`, `X.CSV`, `data/releve.csv`, `.env.local`, `INSERT OR REPLACE` (sur une ou deux lignes, via `REPLACE INTO`), IBAN FR76. | Garde-fous partiels ; le filet de dernier recours reste `.gitignore`. | Liste d'extensions interdites (`.db .zip .xlsx .xls .ods .ofx .qif .qfx`) hors fixtures ; exclure `fixtures/private/` ; `prefix_with\(.*REPLACE` et `on_conflict_do_update` ; chercher `csv_references\|COMPTES_REAL_CSV\|\bdata/` dans tous les scripts CI et `ci.yml`. | T08 |
| m-06 | `COMPTES_ALLOW_DATA_LOSS_DOWNGRADE=1` coupe `foreign_keys` pour **toute** commande Alembic, y compris `upgrade` : mesuré `fk=0 rt=1` dans la migration. | Une future migration avec backfill s'exécuterait sans FK si la variable traîne dans l'environnement. | Ne couper les FK qu'en `downgrade` (`context.get_context().…` ou drapeau passé par `migrate`). | T06 |
| m-07 | Les permissions d'une base existante ne sont pas durcies : base créée en `0644` → `0644` après upgrade. Un `data/` existant en `0755` est conservé. Une nouvelle base est bien en `0600`, `backups/` en `0700`, sauvegarde et manifeste en `0600`. | INV-11 (droits locaux). | `chmod 0600` sur la base, `-wal`, `-shm` et `0700` sur `data/` à chaque `upgrade`, ou avertissement dans `check`. | T06 |
| m-08 | `/health` reste bloqué **5,21 s** sur une base verrouillée en `EXCLUSIVE` (journal rollback) et répond `storage_status=unavailable`, alors qu'il s'agit d'un verrou, pas d'une corruption. En WAL avec écrivain actif : 0,01 s, `ok`. | Latence de health, état trompeur. | `timeout` court (≤ 0,2 s) sur toutes les connexions `mode=ro` de `database_state`, et distinguer `busy` → `degraded`. | T05/T06 |
| m-09 | Chemins relatifs dépendants du CWD : `database_url=sqlite:///./data/db/comptes.sqlite`, `data_dir=data`, `env_file=.env`. Lancé depuis `backend/`, `python -m app` utilise `backend/data/…` et ignore le `.env` racine. Les sauvegardes vont dans `DATA_DIR/backups`, indépendamment de l'emplacement de `DATABASE_URL`. | Risque de base « fantôme » non migrée ; sauvegardes éloignées de la base. | Ancrer les chemins relatifs sur la racine du projet ou refuser les chemins relatifs ; sauvegarde par défaut à côté de la base. | T05 |
| m-10 | `uvicorn app.main:app --host 0.0.0.0` contourne la garde loopback (`LISTEN 0.0.0.0:9598` observé), car l'application ne vérifie pas l'hôte effectif. Un en-tête `Host: evil.example` reçoit `200` (rebinding DNS). | Exposition réseau par erreur de commande. | Contrôle au `lifespan` (lecture de `server.config.host` impossible : plutôt un middleware `TrustedHostMiddleware(["127.0.0.1","localhost"])`). Origin/CSRF déjà prévus en T42. | T05 → T42 |
| m-11 | Horodatages de précision mixte dans une même colonne : défaut SQLite `…:13.815Z` (ms) contre ORM `…:00.000000Z` (µs). L'ordre lexicographique est faux sous la milliseconde (`.815Z` > `.815000Z`, `.8151Z` < `.815Z`). Le CHECK `substr(x,-1)='Z'` accepte `'n importe quoi Z'`. | Tri et bornes de snapshot du curseur (ADR-014) sur `created_at_utc`. | Aligner `UTCDateTimeText` sur `%Y-%m-%dT%H:%M:%fZ` (3 décimales) ou l'inverse. Durcir le CHECK (`strftime(...) IS NOT NULL` + longueur) par migration. | T06 |
| m-12 | Le verrou optimiste n'est pas imposé en base : `session.execute(update(Account)...)` laisse `version=3` inchangée, de même qu'un `UPDATE` SQL brut. `merge()` et `bulk_update_mappings()` avec une version périmée lèvent bien `StaleDataError`. | Convention seulement ; une mise à jour en masse peut écraser sans conflit. | Trigger `BEFORE UPDATE … WHEN NEW.version <> OLD.version + 1` (migration future) ou garde lint sur `update(<modèle versionné>)`. | T06 |
| m-13 | `env.example` (HEAD) : `APP_PORT=8000`, alors que la valeur par défaut est `9513` et que le proxy Vite vise `9513`. Copier l'exemple casse le proxy. Une modification non commitée de ce fichier est en cours, non revue ici. | Démarrage local. | Aligner sur 9513. | T05 |
| m-14 | CI : actions épinglées par tag (`@v4`, `@v5`), pas par SHA ; `uv sync --all-groups` sans `--locked` (le lock est cohérent aujourd'hui : `uv lock --check` OK) ; `npx playwright install chromium` sans `--with-deps`. | Reproductibilité et chaîne d'approvisionnement ; risque d'échec E2E sur un runner nu (non vérifié, pas d'exécution GitHub). | Épingler par SHA, `uv sync --locked`, `playwright install --with-deps chromium`. | T08 |
| m-15 | Message d'échec de migration : « la sauvegarde vérifiée est conservée » même lorsqu'aucune sauvegarde n'a été faite (base neuve, mesuré `backups=0`). | Message trompeur pour l'utilisateur. | Adapter le message selon `backup is None`. | T06 |
| m-16 | `lib/filters` : accepte `period_start > period_end` et des `flow_kind` hors énumération (round-trip stable : `flow_kind=n_importe_quoi`) ; utilise `new Date("YYYY-MM-DDT00:00:00Z")` (ISO avec `Z`, déterministe, mais contraire à la règle « pas de `new Date(str)` »). `lib/money` et `lib/dates` sont conformes (voir §3.3). | Filtres invalides propagés à l'API. | Valider les énumérations et l'ordre des bornes ; réutiliser `parseIsoDate` de `lib/dates`. | T07 (→ T27) |
| m-17 | Hygiène des tests : `conftest.test_settings` et `test_config` (`get_settings()`) lisent le `.env` du développeur (pas de `_env_file=None`). `test_API_01_errors_are_normalized…` crée une application sur l'URL par défaut `./data/db/comptes.sqlite` (moteur paresseux, jamais ouvert : vérifié par `chmod 000 data` → 32 passed). La suite exige un dépôt Git. | Fragilité locale, risque futur de toucher la base réelle si le test évolue. | `_env_file=None` et `database_url` temporaire partout ; fixture `autouse` qui interdit `./data`. | T08 |

---

## 3. Vérifications positives (preuves)

### 3.1 Migrations (T06)

- **Parité stricte** avec `docs/examples/schema.sql` : octets du SQL embarqué identiques (`diff` vide). `sqlite_master` brut (non normalisé) : `ref-mig []`, `mig-ref [alembic_version, sqlite_autoindex_alembic_version_1]`. Ordre de création identique (`True`).
- **Deux `upgrade` successifs** : `rev 0001_initial backup None master identique True fichier identique True` (même SHA-256 du fichier).
- **Migration interrompue** (copie modifiée dans `/tmp` : instruction invalide au milieu du SQL, ou exception Python après le DDL) : `objets avant=absent après=0 rev=None` sur base neuve, `objets avant=1 après=1 rev=None backups=1` sur base existante. Le DDL transactionnel SQLite est atomique et la base reste inchangée.
- **PRAGMA sur la connexion Alembic** : mesurés dans la migration, `fk=1 rt=1 in_tx=True`. `env.py` utilise bien `create_sqlite_engine` et ne contourne pas la fabrique (exception : m-06).
- **Sauvegarde** : un échec réel (répertoire `backups` en `0500`) produit `BackupError: Sauvegarde pré-migration impossible; la migration est refusée`, avec un schéma inchangé et `rev: None`. Sauvegarde et manifeste en `0600`, `integrity_check` contrôlé deux fois, capture WAL testée.
- **Base corrompue** : `MigrationError: Impossible de lire la révision Alembic de la base`, octets inchangés.
- **Downgrade avec données** : refusé sans `COMPTES_ALLOW_DATA_LOSS_DOWNGRADE` et sans sauvegarde vérifiée (tests `test_T06_downgrade_*` verts).
- **UUID** : générés côté application (`uuid4_text`, test ORM-02), sous réserve de MAJ-06.

### 3.2 Sessions et health (T05)

- PRAGMA `foreign_keys`, `recursive_triggers`, `journal_mode=wal`, `busy_timeout=5000`, `synchronous=1` présents sur chaque connexion du pool (test SEC-06).
- `BEGIN IMMEDIATE` inter-processus : le second processus attend puis réussit (voir MAJ-02). Rollback sur exception vérifié.
- **Pas de fuite de connexions** après 100 requêtes `get_db` réussies et 100 requêtes en erreur : `Checked out connections: 0`.
- `/health` sans base : `not_initialized`, aucun fichier ni répertoire créé (`dir créé: False`), aucun socket (test API-01), clé absente de la réponse et des logs applicatifs. Erreurs 404/405/422/500 normalisées, sans écho d'entrée ni traceback.
- Le refus non loopback fonctionne via `python -m app` (sous réserve de MAJ-04 et m-10).

### 3.3 Frontend (T07)

- `formatCentsToEuro` : `0 → "0,00 €"`, `-1 → "−0,01 €"`, `±(2^53−1)` corrects ; `2^53`, `1.5`, `NaN` et `Infinity` → `RangeError` ; bigint `2^53+1 → "90 071 992 547 409,93 €"`.
- `parseEuroInputToCents` : rejette `1.234,56`, `1,234.56`, `12,345`, `,5`, `5,`, `1e3`, `--1`, les chiffres arabes et `1 23` ; accepte `1 234,56` (espace fine), `−0,01` et `+5`. Calcul en `BigInt`, sans flottant.
- `lib/dates` : `2026-02-29` est rejeté, `2024-02-29` accepté, `2026-13-01` rejeté. Aucun `new Date(str)`.
- Le round-trip URL des filtres est stable.
- Bundle `dist/` : aucune occurrence de `sk-…`, `LLM_*`, `api_key`, `Bearer`, `VITE_*`, `/home/…` ou `FR76…` ; aucune source map. `npm audit` : 0.
- Landmarks présents (`aside`, `nav[aria-label=Pages]`, `header`, `main#main-content[tabindex=-1]`, `footer`). Le lien d'évitement est le premier arrêt Tab et déplace le focus sur `#main-content`. Focus visible (`outline 3px`). Thème sombre : 0 violation axe. Les états health ok, degraded, unavailable, 500 et offline s'affichent correctement (libellés distincts).

### 3.4 CI (T08)

- `ci.yml` : `permissions: contents: read`, aucune référence à `csv_references/`, `data/` ou `COMPTES_REAL_CSV` ; `-m 'not opt_in_real_data'`. Le smoke E2E utilise des bases dans `mktemp -d` et un `DATA_DIR` temporaire, indépendants de `data/`.

---

## 4. Critères G1

| Critère (PLAN §F LOT 1, l. 217) | Statut | Preuve |
|---|---|---|
| Démarrage local sans LLM ni CSV | **OK** | `uv run python -m app` avec une base temporaire. Health : `"llm_enabled":false`, `"offline_ready":true`. Aucun CSV lu. |
| `health = OK` | **OK** | Après `migrate upgrade` : `{"status":"ok","api_version":"1","storage_status":"ok",…,"database":{"status":"ok","revision":"0001_initial"}}`. `migrate check` : avant `status=not_initialized` (exit 1), après `status=ok` (exit 0). |
| Le frontend sert une page et consomme health | **OK (preuve manuelle)** | Avec `vite` et le backend réel sur 9513, sans mock : badge « Serveur local connecté ». Smoke Playwright `2 passed`, mais réponse injectée (m-04). |
| Migrations ×2 sans modification indésirable | **OK** | `sqlite_master` identique et fichier identique (SHA-256) après le second upgrade ; second upgrade `backup=none`. |
| Suite verte sur environnement propre | **OK** | §1 : pytest 32/32, vitest 37/37, ruff, mypy, eslint, tsc, build, audit, contrats, confidentialité, E2E. |

### Verdict : **G1 PASS sous réserves**

Réserves à lever, avec leur ticket porteur :

1. **MAJ-01** (T05/T06) : commit de `get_db` après la réponse. **Condition d'entrée de tout ticket qui écrit via l'API** (T09+ en écriture, au plus tard avant T11).
2. **MAJ-02** (T05/T06) : session d'écriture en `BEGIN IMMEDIATE` et mapping `VERSION_CONFLICT` / `DATABASE_BUSY`. Même échéance.
3. **MAJ-03, MAJ-04** (T05) : logs uvicorn non rédigés et fuite partielle de la clé LLM. À corriger avant LOT 2, puisque les libellés bancaires arrivent en G2.
4. **MAJ-05** (T05 + ADR orchestrateur) : alignement de `API.md` sur l'enveloppe `error` et sur les champs de health.
5. **MAJ-06** (T06 + ADR) : PK `NULL` et non-UUID, migration `0002`.
6. **MAJ-07** (T06) : garde-fou de sauvegarde pour la CLI Alembic.
7. MINEUR m-01..m-17 : à planifier, aucun n'est bloquant. m-01, m-02 et m-03 (T07) et m-04 (T08) sont recommandés avant G7.

Propositions pour `docs/REGISTRE_RISQUES.md` (arbitrage orchestrateur) : MAJ-01 et MAJ-02 (intégrité et concurrence), MAJ-03 et MAJ-04 (vie privée et secrets), MAJ-06 (intégrité du schéma).

## 5. Reproduction

Les scripts d'expérience étaient dans `/tmp/revue_g1_exp/`, un répertoire éphémère. Leur logique est citée ci-dessus. Ils se lancent par `PYTHONPATH=<copie>/backend <copie>/.venv/bin/python <script>.py` depuis un répertoire sans `.env`. Les sondes de garde-fous utilisent une copie fraîche par sonde (`cp -a`, `git init`, `git add -A`, mutation, `git add -f -A`), suivie de `pytest backend/tests/unit/test_repo_guards.py` et `scripts/check_no_private_data.sh`. L'audit d'accessibilité a été fait avec Playwright Chromium et `axe-core` (fourni par `eslint-plugin-jsx-a11y`) sur `vite preview`, avec `page.route` pour simuler les cinq états health, aux viewports 375, 768 et 1280 et dans les schémas clair et sombre.
