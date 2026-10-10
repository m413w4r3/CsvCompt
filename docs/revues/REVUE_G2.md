# Revue G2 — relecture critique adversariale indépendante (T09, T10, T11)

- **Relecteur** : session `claude-code:opus` indépendante (n'a écrit aucun livrable T09/T10/T11).
- **Date** : 2026-10-10.
- **Base relue** : `HEAD = cbb30dd` (« feat: T11 API d'aperçu d'import sans écriture métier »), extraite par `git archive HEAD` dans `/tmp/revue_g2` (copie propre, `git init` local pour les gardes, `uv sync`). L'arbre de travail du dépôt réel n'a été ni lu ni modifié ; seul ce fichier y a été créé.
- **Données** : uniquement des fixtures synthétiques et des CSV fabriqués par les scripts (numéro de compte fictif `98765432101`, libellés `X`, `Y`, `LIBELLE_SECRET_ZQX`). `csv_references/` et `data/` n'ont jamais été ouverts.
- **Scripts jetables** (hors dépôt) : `/tmp/revue_g2_scripts/p1_parser.py`, `p1b_trunc.py`, `p2_norm.py`, `p3_api.py`, `p4_extra.py`, `p5_orphan.py`, `p6_trunc_api.py`, `p7_loop.py` ; sorties dans `p1.out` et `p3.out`. Exécution : `/tmp/revue_g2/.venv/bin/python -I <script>`.

## Verdict

**BLOCK.** 1 BLOQUANT, 5 MAJEUR, 14 MINEUR.

Les critères littéraux de G2 sont presque tous tenus. Les 34 fixtures passent, la conversion est exacte au centime, l'aperçu ne modifie aucune table métier (contenu comparé, pas seulement les comptes) et le parseur ne lève jamais d'exception sur 15 548 mutants. Deux points empêchent pourtant le passage du gate :

1. Un CSV hostile fait **planter l'API** (500) et laisse les **octets bancaires bruts hors TTL**. Le critère G2 « CSV hostile sans plantage » n'est donc pas tenu (B-01).
2. `can_commit`, livrable central de T11 que consommera T12, devient **vrai à tort** dans cinq scénarios qui contredisent le contrat d'import : remappage d'une empreinte connue, section sans numéro, contournement d'`OUT_OF_PERIOD` par la couverture, collision d'identité, `link_existing` sans cible (M-01 à M-05).

**Levée proposée** : corriger B-01 et M-01 à M-04, chacun avec un test de non-régression nommé, et faire arbitrer M-05 par l'orchestrateur (ADR, puisque API.md et IMPORT_API.md divergent). Les MINEUR peuvent rester des réserves suivies dans `docs/REGISTRE_RISQUES.md`. Après ces corrections, un « PASS sous réserves » est atteignable sans refonte.

## Commandes standard (copie propre)

```text
$ uv run pytest -q -m "not opt_in_real_data"
133 passed, 2 deselected, 1 warning in 6.29s
$ uv run pytest -q -m opt_in_real_data
2 skipped, 133 deselected            # COMPTES_REAL_CSV absent : skip explicite (attendu)
$ uv run ruff check backend          -> All checks passed!
$ uv run ruff format --check backend -> 52 files already formatted
$ uv run mypy backend/app            -> Success: no issues found in 34 source files
$ make verify-contracts              -> exit=0 (PASS 29 oracles…, triggers SEC-06/ADR-019/ADR-025 OK, validate_plan OK)
$ ./scripts/check_no_private_data.sh -> exit=0 « aucune donnée privée détectée (206 fichier(s)) »
```

## Constats

### BLOQUANT

#### B-01 — Montants non bornés : 500, octets bruts orphelins hors TTL, montants > int64 commitables
- **Preuve** (`p1_parser.py`, `p3_api.py`, `p4_extra.py`, `p5_orphan.py`) :
  ```text
  parse_french_amount("9"*20+",00")  -> 9999999999999999999900   (> 2^63)
  parse_french_amount("92233720368547758,08") -> 9223372036854775808 (= 2^63)
  preview débit 23 chiffres  -> 200, debit_total_cents=-9999999999999999999900, can_commit=True
  preview débit 4 290 chiffres -> 200
  preview débit 4 299 chiffres -> 500 INTERNAL_ERROR
  après le 500 : .bin: 1  .json: 0   (octets bruts écrits, aucun JSON d'aperçu)
  ```
  Cause : `amounts.py` n'impose aucune borne. Avec 4 299 chiffres d'euros, `int` est accepté (limite Python de 4 300 chiffres), puis `×100` dépasse cette limite et `json.dumps` lève `ValueError` dans `ImportPreviewStore.create`. Or `create` écrit `<id>.bin` **avant** de sérialiser le JSON. `purge_expired` ne parcourt que `*.json` : le `.bin` (octets CSV complets, 0600) n'est donc jamais purgé et reste compté dans le quota.
- **Impact** : le critère G2 « sans plantage » n'est pas tenu. Les octets bancaires bruts restent conservés indéfiniment, contrairement à IMPORT_CONTRACT (« Les aperçus temporaires sont supprimés à expiration ») et à INV-11. Un montant hors int64 avec `can_commit=true` provoquera au commit T12 une `OverflowError` SQLite. Au-delà de 2^53, le frontend JavaScript perd des centimes (INV-02).
- **Correction proposée** : (1) borner les montants dans `parse_french_amount` (par exemple `|cents| ≤ 10^13`, ou au minimum ≤ 2^53−1) avec un code bloquant (`NON_NUMERIC_AMOUNT` ou nouveau `AMOUNT_OUT_OF_RANGE` sous ADR) ; appliquer la même borne au solde d'en-tête et aux totaux. (2) Dans `ImportPreviewStore.create`, sérialiser le JSON avant toute écriture et supprimer le `.bin` en cas d'échec (`try/except` → `unlink`). (3) Faire purger aussi les `.bin`/`.tmp` orphelins (voir m-02). Tests : montant à 4 299 chiffres → 200 avec rejet bloquant ; montant > 2^53 → bloquant ; aucun `.bin` sans `.json` après erreur.
- **Ticket porteur** : T09 (borne), T11 (ordre d'écriture et nettoyage).

### MAJEUR

#### M-01 — Une section à empreinte connue peut être remappée vers un autre compte ou un nouveau compte
- **Preuve** (`p3_api.py`, section « Remappage d'une section CONNUE ») :
  ```text
  statut initial: known True can_commit True
  -> vers autre compte existant: 200 True can_commit True
  -> vers new_account (empreinte déjà liée à A): 200 can_commit True
  ```
- **Impact** : IMPORT_CONTRACT §5 dit qu'une empreinte égale retrouve le même `account_id` et que le mapping validé reste stable. Ici, les opérations du compte A peuvent être versées dans le compte B (INV-05, mélange de comptes). Le cas `new_account` créerait au commit un second compte portant la même empreinte, alors que `accounts.bank_fingerprint` est UNIQUE : erreur SQL au commit ou contournement silencieux.
- **Correction proposée** : dans `set_mapping`, refuser (409 ou 422) tout remappage d'une section dont `account_resolution == "known"`, sauf procédure explicite et auditée hors T11. Refuser aussi un `account_id` existant dont `bank_fingerprint` est non nul et différent de l'empreinte de la section. Tests dédiés.
- **Ticket porteur** : T11.

#### M-02 — Section sans numéro de compte reconnu acceptée sans anomalie, puis commitable
- **Preuve** (`p1_parser.py` §5, `p3_api.py`) :
  ```text
  Compte sans n°: {'n': 1, 'codes': [], 'blocking': False} [None]
  acct 'Compte n\xba 0001;' (U+00BA « nº ») -> None []            # aucun code
  API « sans n° »        rejections: []  -> après mapping new_account: can_commit True
  API « nº U+00BA »      rejections: []  -> après mapping: can_commit True
  API « sans préambule » ['SECTION_RANGE_REQUIRED'] -> après mapping+couverture: can_commit True
  ```
- **Impact** : le compte serait créé sans `bank_fingerprint`. Les imports suivants du même compte ne pourront jamais être rattachés par empreinte : nouveaux comptes et doublons inter-imports (INV-03, INV-05, IMPORT_CONTRACT §2 et §5). Une simple variante typographique du préambule (`nº`, `N°` mal encodé) suffit à déclencher le cas.
- **Correction proposée** : en T09, lorsqu'une section `Compte …` n'a pas de `n°` exploitable, ou qu'un en-tête de colonnes arrive sans préambule `Compte`, émettre `INVALID_ACCOUNT_NUMBER` (ou un nouveau code `MISSING_ACCOUNT_NUMBER` sous ADR), bloquant et non résoluble par simple mapping. Envisager d'accepter `nº` (U+00BA) comme variante de `n°`. En T11, faire de `canonical is None` un blocage de `can_commit`.
- **Ticket porteur** : T09 et T11.

#### M-03 — Le PATCH `coverage` contourne `OUT_OF_PERIOD` et écrase la période déclarée par la banque
- **Preuve** (`p3_api.py`) :
  ```text
  section sans période, lignes 10/02/2026 et 15/09/2026, couverture saisie 2026-03-01..2026-03-31
    -> rejections [] can_commit True complete_through 2026-03-31
  section AVEC période déclarée 01/01/2026..28/02/2026
    PATCH coverage 2025-01-01..2026-09-30 -> 200, complete_through 2026-09-30, can_commit True
  ```
  Cause : `OUT_OF_PERIOD` n'est calculé qu'au parse, contre la période lue, et `set_coverage` ne recalcule pas les lignes hors couverture. `set_coverage` accepte en outre n'importe quelle section, y compris celles qui ne sont pas dans `_period_missing_sections`.
- **Impact** : IMP-12 et ADR-018 exigent qu'une ligne hors des bornes de sa section bloque le commit jusqu'à une décision auditée ; cette exigence est contournée. La couverture et la complétude deviennent falsifiables : 2025 affirmé « complet », contrairement à ADR-003, et une journée de mars déclarée complète sans aucune donnée (INV-09). Les KPI de complétude et le contrôle de solde (G3/G6) en héritent.
- **Correction proposée** : n'accepter le PATCH `coverage` que pour les sections signalées `SECTION_RANGE_REQUIRED` (409 ou 422 sinon). Après saisie, recalculer `OUT_OF_PERIOD` pour toutes les lignes de la section contre la couverture saisie et alimenter `_out_of_period_rows` et `rejections`. Exiger au minimum `coverage_from ≤ min_date` et `max_date ≤ coverage_through`, ou une décision par ligne.
- **Ticket porteur** : T11.

#### M-04 — Collision silencieuse d'`identity_label` par troncature après expansion NFC (INV-03)
- **Preuve** (`p2_norm.py`, `p6_trunc_api.py`) :
  ```text
  NFC len U+FB2C: 3
  len raw 9001 | identité égale ? True | warnings ('LABEL_TRUNCATED',)
  parser (limite par défaut 10 000): 1 [('LONG_LABEL', 'warning')]      # pour chacun des deux libellés
  raw distincts: True identity_label égales: True
  API: warnings [('LABEL_TRUNCATED','warning'), ('LONG_LABEL','warning')] rejections [] can_commit True operation_count 2
  ```
  Deux libellés bruts distincts de 9 001 caractères, sous la limite du parseur, produisent la même `identity_label`, car la forme NFC (×3) est tronquée à `MAX_LABEL_CHARS = 20 000`.
- **Impact** : au commit, les deux opérations distinctes seraient comptées comme une même identité, avec multiplicité `max` et non somme. C'est une fusion silencieuse, interdite par IMPORT_CONTRACT et ADR-011. IMPORT_NORMALIZATION.md prévient que « le code appelant doit traiter cet avertissement avant toute persistance d'identité », mais T11 le traite comme un simple avertissement.
- **Correction proposée** : en T10, ne jamais tronquer `identity_label`. Borner l'entrée en amont : la limite du parseur doit garantir que `NFC(raw) ≤ MAX_LABEL_CHARS` (vérifier la longueur après NFC, ou refuser le champ). Si une troncature reste possible, rendre `LABEL_TRUNCATED` **bloquant** en T11 et T12. Test avec U+FB2C (expansion ×3) et une différence en fin de libellé.
- **Ticket porteur** : T10 (règle) et T11 (blocage) ; à régler avant T12.

#### M-05 — `link_existing` accepté sans cible ; divergence API.md ↔ IMPORT_API.md et code
- **Preuve** (`p3_api.py`) :
  ```text
  link_existing (base sans aucune transaction) -> can_commit True []
  avec transaction_id (champ documenté dans API.md) -> 422 VALIDATION_ERROR
  re-décision même ligne -> 200   (aucune trace de l'ancienne décision, ni acteur)
  ```
  API.md § Imports documente `{expected_version, action, transaction_id?}`, avec une décision « enregistrée avec acteur/audit », et précise que `link_existing` exige une identité égale. IMPORT_API.md et le code documentent `section_index` mais ni `transaction_id` ni acteur ; `MultiplicityRequest` (`extra="forbid"`) rejette `transaction_id`.
- **Impact** : `can_commit=true` repose sur une décision `link_existing` qui ne désigne aucun rang ni aucune transaction et peut viser un ensemble vide. T13 devra la réinterpréter, avec un risque de lien arbitraire (INV-08, IMP-12). C'est aussi un changement de signature d'API non arbitré, en contradiction avec la règle « aucun changement silencieux » d'AGENTS.md.
- **Correction proposée** : ADR d'arbitrage. Soit ajouter `transaction_id` (ou `occurrence_rank`) obligatoire pour `link_existing` et vérifier en T11 qu'une identité égale existe, sinon 422. Soit retirer `transaction_id` d'API.md et documenter que `link_existing` ne rend pas `can_commit` vrai tant que T13 n'a pas résolu la cible. Conserver un historique des décisions (acteur, horodatage) dans l'aperçu.
- **Ticket porteur** : T11, T13, ARCH (ADR).

### MINEUR

| ID | Constat et preuve | Correction proposée | Ticket |
|---|---|---|---|
| m-01 | `can_commit` en retard d'une étape : après correction d'un `DUPLICATE_ACCOUNT_SECTION` (section 2 remappée vers un autre compte), on obtient `rejections []`, `can_commit False`, de même au `GET`. Il faut un PATCH identique supplémentaire pour obtenir `can_commit True` (`p4_extra.py`). Cause : `pending_blocking` est calculé avant le retrait du rejet `section=None` dans `_recalculate`. Le défaut est sans danger (il bloque au lieu d'autoriser) mais trompeur pour l'utilisateur. | Retirer ou recalculer les rejets avant de calculer `pending_blocking`. | T11 |
| m-02 | Les orphelins `<uuid>.bin` et `.<uuid>.tmp` ne sont jamais purgés (purge limitée à `*.json`) et comptent dans le quota. Avec un orphelin, le statut devient `507 PREVIEW_STORAGE_FULL` de façon permanente (`p3_api.py`, « TTL / purge »). | Purger aussi les `.bin` sans `.json` et les `.tmp` plus vieux que le TTL. | T11 |
| m-03 | La boucle d'événements est bloquée : `async def create_preview` exécute le parse et la base de façon synchrone. Pendant l'aperçu d'un fichier de 10,1 Mo, `GET /api/v1/health` répond en **12,2 s** (`p7_loop.py`, uvicorn réel). Mesures du parse : 200 000 lignes (13,4 Mo) en 30,8 s ; 20 Mo de lignes vides en 34,9 s ; 300 000 lignes hors période : 600 000 anomalies, pic de 139 Mo. Le corps multipart est reçu et spoolé intégralement sur disque avant le contrôle de taille (Starlette 1.7, pas de `max_file_size`) : 200 Mio → 413 seulement après réception. | Utiliser `def` (threadpool) ou `run_in_threadpool` ; contrôler `Content-Length` en amont ; plafonner le nombre d'anomalies (agrégation par code). | T11 (T42 pour la limite de corps) |
| m-04 | Alias `new_account` : le filtre `\d{8,}` est contourné par des espaces ou des tirets. Sont acceptés `1234 5678 9012 3456`, un IBAN fictif écrit par groupes de 4, `1234-5678-9012`, ainsi que `<script>alert(1)</script>` (`p3_api.py`). | Compter les chiffres après retrait des séparateurs (≥ 8 → refus) et détecter un motif IBAN ; garantir l'échappement côté frontend. | T11 (INV-11) |
| m-05 | Des lignes de la zone de données sont **ignorées en silence** si leur premier champ commence par `Madame`, `Monsieur`, `Solde initial`, `Solde précédent`… ou `Téléchargement du`. Exemple : `Madame Dupont;"X";1,00;;` donne `codes []`, `n=1`. La ligne `Téléchargement du` située après l'en-tête peut même fixer `generated_on`. Cela contredit l'intention de `test_CSV_04_unknown_rows_after_header_are_never_silently_ignored`. | Après `header_seen`, traiter ces lignes comme candidates (`BAD_FIELD_COUNT` ou `INVALID_DATE`), ou au moins émettre un avertissement. | T09 |
| m-06 | `generated_on` incohérent sans anomalie. Une date de génération antérieure à `period_from` donne `complete_through=2025-12-14` pour une couverture commençant le `2026-01-01`. Une opération postérieure à `generated_on` ne reçoit qu'un `PROVISIONAL_DAY`. Un second `Téléchargement du` différent est ignoré. Un `Téléchargement du` placé après les sections fixe `complete_through` sans aucun `PROVISIONAL_DAY` par ligne (`p1_parser.py` §4). | Bloquer (ou au minimum avertir) si `generated_on < period_from`, si `booked_on > generated_on` ou si deux dates divergent ; recalculer les `PROVISIONAL_DAY` en fin de parse. | T09 |
| m-07 | Bords du numéro de compte : la regex du parseur (`\s*(.*?)\s*$`) retire tout blanc Unicode, si bien que `Compte n° 　0001` est accepté (`0001`). IMPORT_CONTRACT n'autorise que 5 blancs (SPACE, TAB, NBSP, NNBSP, THIN SPACE). La canonicalisation elle-même (Nd, zéros initiaux, tiret encadré, préfixe `credit-agricole\0`, HMAC) est conforme ; vérification indépendante faite. | Capturer le texte brut après `n°` sans `\s*` et laisser `canonicalize_account_number` gérer les bords. | T09 |
| m-08 | Divergences documentaires. (a) La réponse expose des champs absents d'API.md : `can_commit`, `already_imported`, `import_file_id` (UUID d'un import déjà commité de même hash), `account_mapping`, `period_from/to`, `min/max_date`, `net_cents`, `anomaly_counts`, `detail_code`. (b) IMPORT_PARSER annonce que `FILE_TOO_LARGE` « conserve son code », alors que `_rejection_code` le traduit en `CSV_INVALID` (inatteignable via l'API). (c) Aucun élément `ACCOUNT_MAPPING_REQUIRED` n'est exposé : seul `can_commit=false` signale le besoin de mapping. (d) La date de repli `generated_on` est la date **UTC** et non la date locale prévue par le contrat. (e) Deux champs `file` sont acceptés (200) sans règle documentée. | Aligner API.md et IMPORT_API.md (ADR si le contrat change) ; exposer les résolutions en attente. | T11, docs |
| m-09 | Couverture des tests : `test_CSV_05_…` compare des **comptages**, sur base vide et sans PATCH. Aucun test ne couvre B-01 ni M-01 à M-05. La présente revue a prouvé CSV-05 sur le **contenu** : dump trié de toutes les tables et du schéma, avant et après 34 aperçus, PATCH mapping, multiplicité et 5 DELETE, avec 2 comptes préexistants et la clé HMAC déjà créée ; résultat : `tables dont le contenu a changé: []`. | Remplacer le test CSV-05 par une comparaison de contenu incluant les PATCH ; ajouter les tests de non-régression des constats. | T11 / QA |
| m-10 | Le `repr` par défaut de `ParsedRow`, `ParsedExport` et `NormalizedLabel` contient `raw_label`. Aucune fuite constatée aujourd'hui (aucun log ; `logs contiennent libellé/numéro: False False`), mais un futur `logger.debug(row)` fuirait. | `field(repr=False)` sur `raw_label`, `identity_label` et `search_label`. | T09 / T10 |
| m-11 | `parse_ca_label` : `merchant_raw_hint` peut contenir un IBAN ou un long numéro (chaîne fictive de forme IBAN renvoyée telle quelle par `p2_norm.py`). | Masquer avant toute persistance ou exposition (T14+). | T10 |
| m-12 | Une troncature à une frontière d'enregistrement est indétectable : sur les fixtures valides, 936 offsets de troncature donnent un préfixe exact sans anomalie, et 0 cas de contenu altéré (`p1b_trunc.py`). C'est une limite du format (pas de ligne de fin) et non un import partiel au sens strict. | Avertir lorsque `max_date` est nettement antérieure à `period_to`, et s'appuyer sur le contrôle de solde (G3/G6). | T09 / T16 |
| m-13 | Deux sections mappées vers deux `new_account` de même alias : `can_commit True`, ce qui créera des comptes homonymes. | Refuser les alias dupliqués dans un même aperçu, et contre les comptes existants. | T11 |
| m-14 | Des dates extrêmes valides (`01/01/0001`, `31/12/9999`) ne produisent qu'un `OUT_OF_PERIOD` résoluble par `create_occurrence` : une opération datée de l'an 1 peut donc être commitée. | Borne de plausibilité bloquante non résoluble (par exemple `< 2000` ou `> generated_on + 1 j`). | T09 |

## Points attaqués et trouvés conformes

- **Robustesse du parseur** : 15 548 mutants testés (troncature à chaque offset, `"` ajouté, NUL et CR insérés à chaque offset, 3 000 mutations aléatoires, 500 blobs aléatoires) ; résultat `exceptions=0`. Ligne unique de 19 Mo → `FIELD_TOO_LONG` en 1,9 s ; guillemet ouvert sur 19 Mo → `UNCLOSED_QUOTE` ; 10^6 lignes (67 Mo) → `FILE_TOO_LARGE`.
- **Grammaire des montants** : `0,01`→1, `0,00`→`ZERO_AMOUNT`, `1 000 000 000,00` exact, débit `-5,00`→`NEGATIVE_AMOUNT`. Rejets : `5,001`, `5,`, `,50`, `5,5`, chiffres arabo-indiens et pleine chasse, `1.000,00`, séparateurs mixtes, U+3000 en groupement, U+200B. NBSP, NNBSP et THIN SPACE sont acceptés. Aucun `float`.
- **Dates** : `29/02/2024` est valide ; `29/02/2026`, `31/04`, `00/00`, ISO, `1/2/2026`, années sur 2 chiffres et chiffres non ASCII donnent `INVALID_DATE` ; une période inversée est bloquante.
- **Sections** : deux sections du même compte → `DUPLICATE_ACCOUNT_SECTION`, bloquant non résoluble. Section sans période → `SECTION_RANGE_REQUIRED`. Un en-tête ou un `Compte … n°` à l'intérieur d'un champ cité reste du libellé. Un double en-tête produit un avertissement.
- **`raw_label` bit-exact (INV-01)** : NBSP et espaces de bord, CRLF, CR seul et LF dans un champ cité, combinants non NFC, `""`, BOM et ZWSP internes, TAB sont tous préservés. Un BOM en milieu de fichier donne `INVALID_DATE` (bloquant).
- **`complete_through` (ADR-018)** : `min(period_to, generated_on − 1)` ; repli `max(booked_on) − 1` avec `PROVISIONAL_DAY` ; une section vide donne `null`.
- **Normalisation** : `identity_label` est idempotent, conserve casse et accents, signale les `Cf` (ZWSP, BOM) sans les supprimer, et respecte la liste White_Space. `search_label` n'est utilisé que par `near_duplicate_candidates`. Les signaux CARREFOUR BANQUE (intermédiaire) se distinguent de CARREFOUR MARKET (aucun signal) ; VINTED et AMAZON sont signalés ambigus. Aucun champ de catégorie ou de `flow_kind`.
- **Aperçu** : contenu des tables inchangé (cf. m-09) ; seule écriture possible, la clé HMAC dans `app_settings`, conformément à IMPORT_API.md. Répertoire `0700`, fichiers `0600`, noms UUID ; nom de fichier client jamais utilisé (`../../etc/passwd` absent du stockage). TTL : 410 et fichiers supprimés après 1 801 s. Concurrence : 16 PATCH sur la même version donnent 1×200 et 15×409 ; DELETE et PATCH concurrents donnent 204 puis 410 sans fichier restant ; 12 POST concurrents passent en 200. `expected_version` périmé → 409. Types de contenu : `image/png`, `application/json` et `multipart/form-data` → 415 ; UTF-16 (NUL) → 415. Le champ `transaction_id` est rejeté (cf. M-05).
- **Vie privée** : ni libellé, ni numéro, ni HMAC dans les réponses ; aucun libellé ni numéro dans le JSON stocké ; logs vides pendant toute la campagne, y compris sur le 500 (seul un `correlation_id` est renvoyé). L'empreinte HMAC est stockée dans le JSON temporaire en 0600, ce qu'IMPORT_API.md admet.

## Critères G2

| Critère (PLAN §F, gate G2) | Statut | Preuve |
|---|---|---|
| Les fixtures passent | ✅ | `test_CSV_04_manifest_recounts_all_34_synthetic_fixtures`, `test_non_adversarial_fixtures_match_manifest_aggregates` ; 133 tests passés |
| Conversion exacte à 1 centime | ✅ avec réserve B-01 | Grammaire stricte et tests aller-retour ; mais aucune borne haute (int64, 2^53, 4 300 chiffres) |
| CSV hostile ou imparfait sans plantage ni import partiel | ❌ | Parseur : 0 exception sur 15 548 mutants. **API : 500 sur un montant de 4 299 chiffres** et `.bin` orphelin (B-01) ; `can_commit` vrai à tort (M-01 à M-05) |
| Vrai fichier 2026 (1 024 écritures, dates et limites) | ⚪ non rejoué | Test opt-in correctement ignoré sans `COMPTES_REAL_CSV` ; l'exécution locale par l'orchestrateur n'a pas été vérifiée par ce relecteur (accès interdit) |
| L'aperçu ne modifie pas le nombre de transactions | ✅ (renforcé) | Dump complet du contenu de toutes les tables inchangé (m-09) |
| Couverture MATRICE CSV-01 à CSV-05, IMP-11 à IMP-13 | ✅ partielle | CSV-01 à CSV-04 et IMP-11 à IMP-13 testés au niveau aperçu ; CSV-05 par comptage seulement ; IMP-11 à IMP-13 restent à compléter au commit (G3) |

## Propositions pour les documents de suivi (arbitrage de l'orchestrateur)

- `REGISTRE_RISQUES.md` : ajouter B-01 et M-01 à M-05 comme préalables à T12, ainsi que m-03 (disponibilité) et m-12 (troncature indétectable).
- `DECISIONS.md` : ADR sur `link_existing` et `transaction_id` (M-05) ; ADR sur la borne des montants et un éventuel code `AMOUNT_OUT_OF_RANGE` (B-01) ; ADR sur une section sans numéro (M-02).
