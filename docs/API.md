# Contrats REST `/api/v1`

**Version :** 1 · **Ticket :** T02 · **Gate visé :** G0 · **Références :** PLAN §D/§F, API-01, ADR-015/022/023/024, `DOMAIN_MODEL.md`, `METRICS.md`, `PROTOCOLE_LLM_TOLERANT.md`.

Les contrats HTTP de l'application sont des JSON structurés et validés. L'interdiction de JSON strict concerne uniquement la sortie du LLM. Les noms des champs sont en `snake_case`; les dates sont ISO `YYYY-MM-DD`; les instants sont UTC terminés par `Z`. Toute quantité `*_cents`, `*_count`, `*_bps`, `version`, taille et latence est un entier JSON. Aucun montant en euros, ratio ou pourcentage n'est envoyé sous forme flottante. Une donnée inconnue vaut `null`, jamais un zéro inventé.

## Conventions communes

### Filtres partagés

Les filtres répétés `account_ids`, `category_ids` et `flow_kind` acceptent plusieurs valeurs et sont sérialisés sous une forme canonique stable dans l'URL. Les filtres partagés sont :

| Paramètre | Type | Sémantique |
|---|---|---|
| `period_start`, `period_end` | date | Bornes inclusives sur `booked_on`; requis par les endpoints analytiques. |
| `account_ids` | liste d'UUID | Périmètre explicite des agrégats; requis et non vide pour l'analytique. Aucun mélange implicite de comptes. |
| `category_ids` | liste d'ID stables | Filtre catégories ; la valeur absente signifie toutes les catégories compatibles du périmètre. |
| `flow_kind` | enum répétable | Flux dérivé des allocations confirmées ; `unknown` sélectionne les transactions dont le résidu signé est non nul. Il n'existe aucune colonne `transactions.flow_kind`. |
| `status` | enum/liste | État fonctionnel de transaction, revue, import, règle ou insight selon l'endpoint. |
| `mode` | enum | `treasury` ou `family_life` ; lecture trésorerie brute ou vie familiale conforme à `DOMAIN_MODEL.md`. |
| `personal_mode` | enum | `envelope` (défaut) ou `observed`; règle l'inclusion des allocations et dépenses de comptes `personal`, conformément à ADR-022. |

`flow_kind` n'est jamais stocké sur `transactions`. Il est dérivé des allocations confirmées : sans allocation, le flux entier est `unknown`; après ventilation partielle, les flux alloués restent filtrables et le résidu signé non nul est aussi `unknown`. `flow_kind=unknown` sélectionne donc les transactions dont `amount_cents - Σ allocations confirmées` est non nul. Une transaction multi-flux peut correspondre à plusieurs valeurs. En `mode=family_life`, `personal_mode=envelope` (défaut) exclut les dépenses des comptes de rôle `personal` de `consumption_net` et compte les allocations personnelles ; `personal_mode=observed` exclut ces allocations de la lecture familiale et inclut les dépenses observées des comptes personnels, avec les transferts reçus classés `transfer_owned`. Les deux composantes ne s'additionnent jamais dans un même total.

Le front synchronise ces filtres dans l'URL sur les pages Transactions, Overview, Expenses, Income, Family, Merchants, Budgets et Review. Les valeurs `period_start`, `period_end`, IDs, enums, `mode` et `personal_mode` sont copiables/rejouables. Les libellés bancaires recherchés ne sont pas mis dans l'URL par défaut. Le défaut d'interface est l'année 2026 ; l'absence de couverture avant le premier import n'est pas une alerte de trou (ADR-003).

### Pagination et cohérence

Les listes serveur utilisent une pagination par curseur opaque `cursor` et `limit` (défaut 50, maximum 100), pas un offset. Transactions : ordre déterministe `booked_on DESC, transaction_id DESC`. Le curseur signé lie la dernière paire, les filtres canoniques et une borne de snapshot `created_at_utc`; une importation concurrente n'insère donc pas de lignes au milieu d'une navigation existante. File de revue : curseur signé lié au filtre et à la révision de file, avec ordre `priority DESC, latest_booked_on DESC, review_group_id ASC`; si une décision a changé la file, l'API renvoie `CURSOR_STALE` et l'interface repart de la première page. `next_cursor=null` termine la liste.

Forme commune :

```json
{"items": [], "page": {"limit": 50, "next_cursor": null, "snapshot_at": "2026-10-09T10:00:00Z"}}
```

### Mutations, erreurs et sécurité

- Toutes les mutations exigent `Origin` local autorisé et jeton CSRF valide. `Idempotency-Key` UUID est obligatoire uniquement pour les POST qui créent ou appliquent une écriture persistante : `POST /imports/{preview_id}/commit`, `POST /rules`, `POST /rules/{rule_id}/apply`, `POST /review/decisions`, `POST /review/decisions/{decision_id}/undo`, `POST /review/llm-suggestions`, `POST /budgets`, `POST /insights/generate`, `POST /backups` et `POST /backups/restore/commit`. Les POST d'aperçu, de dry-run ou de test de connexion n'en exigent pas. Toute réutilisation d'une clé avec la même méthode/chemin/corps canonique rejoue le statut et la réponse enregistrés ; une même clé avec un corps différent renvoie `409 IDEMPOTENCY_CONFLICT`. Les réponses mémorisées ne contiennent ni clé secrète ni corps bancaire brut non nécessaire.
- `Idempotency-Key` est facultative pour `PUT`, `PATCH` et `DELETE`; ces méthodes exigent toujours `expected_version` et le contrôle optimiste reste obligatoire. Si une clé est fournie, elle suit la même règle de rejeu/conflit. Les `GET` ne demandent, n'enregistrent ni ne traitent jamais de clé d'idempotence.
- Toute ressource modifiable expose `version`. La requête de mutation envoie `expected_version`; une version obsolète répond `409 VERSION_CONFLICT` avec `details.current_version`.
- Les entrées `idempotency_records` sont conservées 7 jours glissants, avec `expires_at_utc`; une purge au démarrage supprime celles expirées. Les réponses créatrices ne sont rejouables qu'au cours de cette fenêtre (ADR-023).
- Le serveur écoute `127.0.0.1` uniquement. `GET /api/v1/health` n'appelle ni banque ni fournisseur LLM et fonctionne sans réseau externe ni clé configurée.
- Erreur normalisée sous l'enveloppe `{ "error": { "code", "message", "details" } }`, sans secret, chemin disque, numéro de compte ou libellé réel dans les détails techniques :

<!-- contract-example:error -->
```json
{"error":{"code":"VERSION_CONFLICT","message":"Cette ressource a changé.","details":{"current_version":3}}}
```

Codes contractuels : `VALIDATION_ERROR` (422), `NOT_FOUND` (404), `INVALID_HOST` (400), `UNSUPPORTED_MEDIA_TYPE` (415), `FILE_TOO_LARGE` (413), `CSV_INVALID` (422), `ACCOUNT_MAPPING_REQUIRED` (409), `SECTION_RANGE_REQUIRED` (422), `DUPLICATE_ACCOUNT_SECTION` (422), `OUT_OF_PERIOD` (409 tant que non résolu), `MULTIPLICITY_AMBIGUOUS` (409), `IMPORT_PREVIEW_EXPIRED` (410), `VERSION_CONFLICT` (409), `IDEMPOTENCY_CONFLICT` (409), `CURSOR_INVALID` (400), `CURSOR_STALE` (409), `CSRF_ORIGIN_REJECTED` (403), `DATABASE_BUSY` (503 réessayable), `RULE_DRY_RUN_STALE` (409), `CONFLICTING_DECISION` (409), `LLM_DISABLED` (409), `LLM_CONNECTION_FAILED` (502), `BACKUP_INVALID` (422), `RESTORE_PREVIEW_EXPIRED` (410), `INSUFFICIENT_COVERAGE` (422), `INTERNAL_ERROR` (500). `PROVISIONAL_DAY` est un code d'avertissement, pas un échec de requête. `details` contient uniquement champs, IDs opaques, codes de ligne et versions utiles.

### Enveloppe analytique

Chaque réponse analytique a `period`, `accounts`, `coverage`, `filters`, `metric_definition_id`, `raw_cents` et `unit`. Les listes/séries ajoutent `items` ou `series`; une mesure non monétaire emploie `raw_basis_points` ou `raw_count` et met `raw_cents:null`. Les détails de chaque métrique comprennent leur propre `metric_definition_id`. Les formules, règles de nullabilité, arrondis en basis points et unités suivent sans divergence `METRICS.md`. Drilldown conserve les mêmes filtres et donne les IDs exacts des opérations utilisées. `coverage` calcule les jours complets comme l'union des intervalles de section `[coverage_from, complete_through]`, avec `complete_day_count`, `missing_days` ou les plages de jours manquants et `status:"complete"|"partial"|"unknown"`. Les jours `provisional` restent dans les sommes brutes, mais ne valident jamais une couverture complète ; un contrôle de solde `mismatch` rend `month_completeness=unknown`. Une réponse métier d'import n'emploie jamais `partial`.

```json
{
  "period":{"start":"2026-01-01","end":"2026-01-31","kind":"calendar_month","completeness":"complete","as_of":"2026-02-01"},
  "accounts":["550e8400-e29b-41d4-a716-446655440001"],
  "coverage":{"status":"complete","covered_from":"2026-01-01","covered_through":"2026-01-31","operation_count":2,"fully_classified_count":2},
  "filters":{"category_ids":null,"flow_kinds":null,"mode":"treasury","personal_mode":"envelope"},
  "metric_definition_id":"bank_net_flow.v1",
  "raw_cents":-1200,
  "unit":"EUR-cent"
}
```

## Endpoints

### Santé

| Méthode et chemin | Requête | Réponse |
|---|---|---|
| `GET /api/v1/health` | aucune | Champs obligatoires `status`, `api_version`, `storage_status`, `llm_enabled`; peut aussi renvoyer `version`, `time_utc`, `database:{status,revision}`, `offline_ready`. 200 hors ligne. |
| `GET /api/v1/security/csrf` | bootstrap same-origin du client local | établit le cookie de session `SameSite=Strict; HttpOnly` et renvoie un jeton anti-CSRF à conserver en mémoire pour les mutations. |

Exemple : `GET /api/v1/health` → 200. `storage_status` vaut `ok` si la base est migrée à la révision courante, `degraded` si elle est absente, non initialisée ou en retard, et `unavailable` si elle est illisible ou corrompue. Aucun appel réseau externe ni création de base.

<!-- contract-example:health -->
```json
{"status":"ok","api_version":"1","storage_status":"ok","llm_enabled":false,"version":"0.1.0","time_utc":"2026-10-10T12:00:00Z","database":{"status":"ok","revision":"0001_initial"},"offline_ready":true}
```

Le bind est loopback uniquement et les en-têtes `Host` non autorisés reçoivent l'erreur normalisée `INVALID_HOST` (400). Le lancement direct d'Uvicorn avec `--host 0.0.0.0` est déconseillé : la garde d'hôte demeure active, mais le serveur ne doit pas être exposé sur un réseau.

Exemple bootstrap : `GET /api/v1/security/csrf` → `200 {"csrf_token":"<jeton-aléatoire>"}` + cookie local `SameSite=Strict; HttpOnly`; le jeton n'est pas écrit dans `localStorage`.

### Imports

| Méthode et chemin | Requête / réponse |
|---|---|
| `POST /api/v1/imports/preview` | Un unique champ `multipart/form-data file`; crée un aperçu temporaire sans ligne métier SQLite, clé d'idempotence non requise. Réponse `preview_id`, `version`, `status:"previewed"`, `encoding`, `file_sha256` des octets reçus, `generated_on`, `generation_date_source`, `sections[]`, `warnings[]`, `rejections[]`, `anomaly_counts`, `already_imported`, `import_file_id`, `can_commit`, `expires_at_utc`, `reconciliation:null`. Les aperçus sont privés et expirent. Les numéros, empreintes et libellés ne sont jamais renvoyés. |
| `GET /api/v1/imports/preview/{preview_id}` | Renvoie l'état courant et sa `version`; un aperçu expiré ou supprimé répond `410 IMPORT_PREVIEW_EXPIRED`. |
| `DELETE /api/v1/imports/preview/{preview_id}` | Supprime immédiatement l'aperçu privé et ses octets (204). |
| `PATCH /api/v1/imports/{preview_id}/sections/{section_index}/account-mapping` | Résolution versionnée `{expected_version, account_id}` ou `{expected_version, new_account:{alias,account_role}}`; une section `known` est verrouillée. Le nouveau compte et son alias sont seulement mémorisés, jamais créés ici. |
| `PATCH /api/v1/imports/{preview_id}/sections/{section_index}/coverage` | `{expected_version, coverage_from, coverage_through}` uniquement pour `SECTION_RANGE_REQUIRED`; la plage contient toutes les dates d'opérations et ne remplace pas la période déclarée. |
| `PATCH /api/v1/imports/{preview_id}/rows/{logical_row_idx}/multiplicity` | `{expected_version, action:"link_existing"|"create_occurrence", transaction_id?, section_index?}`. `link_existing` exige `transaction_id` d'une transaction existante du même compte et de même identité stricte; le contrôle est refait au commit. `section_index` est obligatoire si l'index logique est ambigu. Les décisions gardent acteur, horodatage et ancienne valeur dans l'aperçu. |
| `POST /api/v1/imports/{preview_id}/commit` | `Idempotency-Key` obligatoire ; `{expected_version}` après résolution de toutes les sections/lignes. Commit atomique du fichier entier. Réponse uniquement `committed` ou `no_op`, avec compteurs et plages de couverture ; aucun état `partial`. Une occurrence `no_op` est un nouveau registre final, conserve hash/taille/référence au commit d'origine, supprime le preview temporaire et répond `archive_retained:false` (`archive_relpath` reste `NULL`). |
| `GET /api/v1/imports` | filtres `status`, `period_start`, `period_end`, curseur ; liste `ImportSummary[]` paginée. |
| `GET /api/v1/imports/{import_file_id}` | détail statut, hash, alias de sections, bornes, soldes source, références aux `balance_checks`, compteurs, candidats ambigus et diagnostic rédigé. Jamais d'archive ou de numéro. |
| `GET /api/v1/imports/{import_file_id}/rejections` | curseur ; `logical_row_idx`, `code`, `field`, `message` non privé. Aucun dump du libellé. |
| `GET /api/v1/accounts/{account_id}/balance-checks` | `period_start`, `period_end`, curseur ; résultats `match`, `mismatch`, `not_comparable`, date et références aux sections, cents de recalcul/delta, `input_snapshot_sha256`, `version`. Les valeurs sont calculées à partir de toutes les sections commitées par date de solde, non dans l'ordre d'import ; seules les dates `balance_as_of <= complete_through` sont comparables. |

Types principaux :

```text
ImportSectionPreview = {section_index: integer, account_mapping: {status: "known"|"unknown"|"ambiguous",
  account_id: UUID|null, alias: string|null, new_account?: {alias: string, account_role: string}}, period_from: date|null, period_to: date|null,
  coverage_from: date|null, coverage_through: date|null, complete_through: date|null,
  balance_as_of: date|null, header_balance_cents: integer|null, min_date: date|null, max_date: date|null,
  account_resolution: "known"|"unknown"|"ambiguous", account_id: UUID|null, account_alias: string|null,
  operation_count: integer, debit_total_cents: integer, credit_total_cents: integer, net_cents: integer,
  provisional_days: date[], anomaly_counts: {code: integer}}
ImportPreview = {preview_id: UUIDv4, version: integer, status: "previewed", encoding: string,
  file_sha256: string, generated_on: date, generation_date_source: string, sections: ImportSectionPreview[],
  warnings: PreviewFinding[], rejections: PreviewFinding[], anomaly_counts: {code: integer}, anomaly_summary: object,
  already_imported: boolean,
  import_file_id: UUID|null, can_commit: boolean, expires_at_utc: datetime, reconciliation: null}
PreviewFinding = {code: string, detail_code?: string|null, severity: string, section: integer|null,
  logical_row_idx: integer|null, field: string|null, message: string}
ImportSummary = {import_file_id: UUID, file_sha256: string, status: "previewed"|"committed"|"rejected"|"no_op",
  created_at_utc: datetime, generated_on: date, generation_date_source: "header_download"|"received_on_fallback",
  sections: integer, operation_count: integer,
  created_transaction_count: integer, source_row_count: integer}
ImportResolution = {resolution_id: UUID, section_index: integer|null, logical_row_idx: integer|null,
  code: "ACCOUNT_MAPPING_REQUIRED"|"SECTION_RANGE_REQUIRED"|"DUPLICATE_ACCOUNT_SECTION"|"OUT_OF_PERIOD"|"MULTIPLICITY_AMBIGUOUS",
  status: "pending"|"resolved", resolution_action: string|null, resolved_by: string|null}
BalanceCheck = {balance_check_id: UUID, account_id: UUID, status: "match"|"mismatch"|"not_comparable",
  start_balance_as_of: date|null, end_balance_as_of: date|null, recalculated_balance_cents: integer|null,
  delta_cents: integer|null, input_snapshot_sha256: string, version: integer}
```

`can_commit` est faux tant qu'une anomalie bloquante, un mapping requis, une plage requise, une ligne `OUT_OF_PERIOD` ou un compte de section dupliqué n'est pas résolu. Une section sans numéro canonique émet `MISSING_ACCOUNT_NUMBER` et ne peut pas être résolue par un mapping. Une ligne `OUT_OF_PERIOD` n'entre jamais automatiquement dans la multiplicité. Elle doit recevoir une décision auditée; `link_existing` identifie la transaction par `transaction_id`, et une ambiguïté de ligne exige `section_index`. Un fichier associant deux sections au même compte reste bloqué avec `DUPLICATE_ACCOUNT_SECTION`. `ZERO_AMOUNT`, `AMOUNT_OUT_OF_RANGE`, `MISSING_ACCOUNT_NUMBER`, `GENERATION_DATE_INCONSISTENT`, `FUTURE_DATED_ROW`, `DATE_IMPLAUSIBLE` et `LABEL_TOO_LONG` sont bloquants. `ACCOUNT_MAPPING_REQUIRED` figure dans `rejections` tant qu'un mapping manque. `FILE_TOO_LARGE` est une réponse 413 avant l'analyse. Les statuts d'import ne comprennent jamais `partial`.

Exemples synthétiques :

```http
POST /api/v1/imports/preview
Origin: http://127.0.0.1:5173
X-CSRF-Token: <jeton local>
Content-Type: multipart/form-data; boundary=...
```

```json
{"preview_id":"550e8400-e29b-41d4-a716-446655440010","version":1,"status":"previewed","encoding":"utf-8","file_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","generated_on":"2026-03-31","generation_date_source":"header_download","operation_count":2,"anomaly_counts":{},"anomaly_summary":{"warning_count":0,"rejection_count":0,"stored_warning_count":0,"stored_rejection_count":0,"omitted_warning_count":0,"omitted_rejection_count":0},"sections":[{"section_index":1,"account_mapping":{"status":"unknown","account_id":null,"alias":null},"account_resolution":"unknown","account_id":null,"account_alias":null,"period_from":"2026-03-01","period_to":"2026-03-31","coverage_from":"2026-03-01","coverage_through":"2026-03-31","complete_through":"2026-03-30","operation_count":2,"debit_total_cents":-2500,"credit_total_cents":5000,"net_cents":2500,"balance_as_of":"2026-03-30","header_balance_cents":120000,"min_date":"2026-03-30","max_date":"2026-03-31","provisional_days":["2026-03-31"],"anomaly_counts":{}}],"warnings":[],"rejections":[{"code":"ACCOUNT_MAPPING_REQUIRED","severity":"blocking","section":1,"logical_row_idx":null,"field":"account_mapping","message":"Associez cette section à un compte avant de continuer."}],"can_commit":false,"already_imported":false,"import_file_id":null,"expires_at_utc":"2026-10-10T10:15:00Z","reconciliation":null}
```

Erreurs principales de l'aperçu : `DATABASE_NOT_READY` (503), `FILE_TOO_LARGE`
(413), `UNSUPPORTED_MEDIA_TYPE` (415), `VERSION_CONFLICT` (409),
`ACCOUNT_MAPPING_LOCKED` / `ACCOUNT_ALIAS_CONFLICT` (409),
`ACCOUNT_FINGERPRINT_CONFLICT` (409 ou 422), `MULTIPLICITY_AMBIGUOUS` (409), `IMPORT_PREVIEW_EXPIRED` (410),
`CSV_INVALID` / `VALIDATION_ERROR` (422). `MISSING_ACCOUNT_NUMBER`,
`AMOUNT_OUT_OF_RANGE`, `GENERATION_DATE_INCONSISTENT`, `FUTURE_DATED_ROW`,
`DATE_IMPLAUSIBLE` et `LABEL_TOO_LONG` sont des codes de rejet bloquant dans
`rejections` d'une réponse 200.

Commit synthétique :

Si le SHA-256 est déjà commité, le preview se termine en tentative finale `no_op`, sans octets archivés : `POST /api/v1/imports/<preview_id>/commit` → `200 {"import_file_id":"<UUID_TENTATIVE>","status":"no_op","duplicate_of_import_file_id":"<UUID_COMMIT>","file_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","created_transaction_count":0,"new_source_row_count":0,"archive_retained":false}`.

```http
POST /api/v1/imports/550e8400-e29b-41d4-a716-446655440010/commit
Idempotency-Key: 22222222-2222-4222-8222-222222222222
Content-Type: application/json
```

```json
{"expected_version":1,"mappings":[{"section_index":1,"new_account":{"alias":"Compte fictif","account_role":"current"}}]}
```

```json
{"import_file_id":"550e8400-e29b-41d4-a716-446655440011","status":"committed","no_op":false,"created_transaction_count":2,"new_source_row_count":2,"linked_source_row_count":0,"generation_date_source":"header_download","sections":[{"section_index":1,"account_id":"550e8400-e29b-41d4-a716-446655440012","coverage_from":"2026-03-01","coverage_through":"2026-03-31","complete_through":"2026-03-30","balance_as_of":"2026-03-30","header_balance_cents":120000}]}
```

Liste et rejets : `GET /api/v1/imports?status=committed` → `{"items":[{"import_file_id":"<UUID>","status":"committed","created_at_utc":"2026-10-09T10:00:00Z","sections":1,"operation_count":2,"created_transaction_count":2,"source_row_count":2}],"page":{"limit":50,"next_cursor":null}}`. `GET /api/v1/imports/<UUID>/rejections` → `{"items":[{"logical_row_idx":4,"code":"INVALID_AMOUNT","field":"debit_euros","message":"Montant non interprétable."}],"page":{"limit":50,"next_cursor":null}}`.

Contrôle de solde : `GET /api/v1/accounts/<UUID>/balance-checks?period_start=2026-03-01&period_end=2026-04-30` → `{"items":[{"balance_check_id":"<UUID>","account_id":"<UUID>","status":"match","start_balance_as_of":"2026-03-15","end_balance_as_of":"2026-04-15","recalculated_balance_cents":9000,"delta_cents":0,"input_snapshot_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","version":1}],"page":{"limit":50,"next_cursor":null}}`. Un solde daté après son `complete_through` répond `not_comparable` ; `import_account_sections` ne porte aucun statut de contrôle figé.

### Comptes

| Méthode et chemin | Contrat |
|---|---|
| `GET /api/v1/accounts` | liste `account_id`, alias, rôle, actif, version, couverture agrégée ; aucune empreinte HMAC exposée. |
| `GET /api/v1/accounts/{account_id}` | même forme, plus intervalles de couverture connus et historique non sensible. |
| `PATCH /api/v1/accounts/{account_id}` | `{expected_version, alias?, account_role?, is_active?}` ; mutation auditée. Pas de changement de fingerprint/transactions. |

Exemple : `GET /api/v1/accounts` → `{"items":[{"account_id":"550e8400-e29b-41d4-a716-446655440012","alias":"Compte fictif","account_role":"current","is_active":true,"version":1,"coverage":[{"from":"2026-03-01","through":"2026-03-31"}]}],"page":{"limit":50,"next_cursor":null}}`.

Exemple de mutation : `PATCH /api/v1/accounts/<UUID>` avec `{"expected_version":1,"alias":"Courant de démonstration","account_role":"current"}` → `200 {"account_id":"<UUID>","alias":"Courant de démonstration","account_role":"current","version":2}`. Un alias déjà pris ou une ancienne version ne change rien et renvoie l'erreur normalisée.

### Transactions

| Méthode et chemin | Contrat |
|---|---|
| `GET /api/v1/transactions` | filtres communs, `cursor`, `limit`, `sort=booked_on_desc`; items `transaction_id`, `account_id`, alias, `booked_on`, `amount_cents`, `raw_label`, payee éventuel, état dérivé, version. |
| `GET /api/v1/transactions/{transaction_id}` | mêmes données, allocations confirmées, suggestions séparées, résidu, `source_rows[]` (import ID, section et ligne logique), liens et dernière décision. |

Exemple : `GET /api/v1/transactions?period_start=2026-03-01&period_end=2026-03-31&account_ids=550e8400-e29b-41d4-a716-446655440012&limit=50` → `200 {"items":[{"transaction_id":"550e8400-e29b-41d4-a716-446655440020","account_id":"550e8400-e29b-41d4-a716-446655440012","booked_on":"2026-03-12","amount_cents":-1234,"raw_label":"LIBELLE SYNTHETIQUE","status":"unclassified","version":1,"source_row_count":1}],"page":{"limit":50,"next_cursor":null,"snapshot_at":"2026-10-09T10:00:00Z"}}`.

### Catégories

| Méthode et chemin | Contrat |
|---|---|
| `GET /api/v1/categories` | arbre actif/inactif avec `category_id`, parent, libellé, version taxonomie, IDs stables, `allowed_flow_kinds[]`, `is_active`; filtre `taxonomy_version`. |
| `GET /api/v1/categories/{category_id}` | détail d'un ID stable et ses flux admissibles. |

Les catégories sont gérées par taxonomie versionnée/migration ; aucun endpoint ne renomme ou ne supprime un ID silencieusement. Exemple synthétique : `{"category_id":"groceries_organic","parent_category_id":"groceries","label":"Bio","taxonomy_version":1,"allowed_flow_kinds":["expense"],"is_active":true}`.

### Règles

| Méthode et chemin | Contrat |
|---|---|
| `GET /api/v1/rules` | liste paginée par `is_active`, `is_approved`, `scope_kind`. |
| `POST /api/v1/rules` | `Idempotency-Key` obligatoire ; crée inactive/non approuvée par défaut : `{name, scope_kind, scope, flow_kind, category_id, effect, priority}`. |
| `GET /api/v1/rules/{rule_id}` | détail, version et approbateur. |
| `PATCH /api/v1/rules/{rule_id}` | modification avec `expected_version`; toute modification désactive l'ancienne portée et est auditée. |
| `DELETE /api/v1/rules/{rule_id}` | désactivation logique versionnée ; aucune suppression d'historique. |
| `POST /api/v1/rules/{rule_id}/dry-run` | filtres/périmètre ; **aucune écriture métier** ; renvoie `dry_run_id`, `matched_count`, sommes en centimes, exemples et versions. |
| `POST /api/v1/rules/{rule_id}/apply` | `Idempotency-Key` obligatoire ; `{dry_run_id, expected_rule_version, expected_preview_version}` ; applique uniquement après aperçu explicite, jamais sur décisions humaines déjà confirmées. Résultat avec compte créé/ignoré et audit. |

Forme de scope v1 : `{"payee_id":"<UUID>","exact_label":"...","transaction_ids":[],"exclude_transaction_ids":[]}` ; les clés pertinentes dépendent de `scope_kind`. Exemple dry-run : `POST /api/v1/rules/<id>/dry-run` → `{"dry_run_id":"<UUID>","matched_count":2,"sum_amount_cents":-3000,"examples":[{"transaction_id":"<UUID>","booked_on":"2026-03-12","amount_cents":-1000}],"database_changed":false}`.

Exemples d'écriture : `POST /api/v1/rules` avec `{"name":"Règle synthétique","scope_kind":"exact_payee","scope":{"payee_id":"<UUID>"},"flow_kind":"expense","category_id":"groceries_organic","effect":{"beneficiary":"household"},"priority":50}` → `201 {"rule_id":"<UUID>","is_approved":false,"is_active":false,"version":1}`. Après aperçu et approbation explicite, `POST /api/v1/rules/<id>/apply` avec `{"dry_run_id":"<UUID>","expected_rule_version":2,"expected_preview_version":1}` → `200 {"applied_count":2,"skipped_manual_count":1,"audit_event_id":"<UUID>","version":3}`.

### Revue humaine

| Méthode et chemin | Contrat |
|---|---|
| `GET /api/v1/review/queue` | filtres communs, états `unclassified`, `suggested`, `conflict`, `status`; groupes par payee/contexte, impact en cents, compteurs et curseur stable. |
| `GET /api/v1/review/queue/{review_group_id}` | opérations et suggestions du groupe, sans appliquer de suggestion. |
| `POST /api/v1/review/decisions` | `Idempotency-Key` obligatoire ; `{transaction_id, expected_version, action, allocations?, suggestion_id?, scope}` où scope vaut `this_transaction`, `selected_transactions` ou `future_rule`; les centimes du split doivent sommer exactement au montant. `future_rule` crée une règle distincte non approuvée tant que l'utilisateur ne l'a pas confirmée. |
| `POST /api/v1/review/decisions/{decision_id}/undo` | `Idempotency-Key` obligatoire ; `{expected_version, reason}` ; annule l'effet analytique avec audit avant/après, sans modifier source transaction ni CSV. |
| `POST /api/v1/review/llm-suggestions/preview` | `{transaction_ids[]}` ; aucun appel sortant. Renvoie exactement les champs et libellés caviardés qui seraient transmis, les redactions et des IDs temporaires non réversibles. |
| `POST /api/v1/review/llm-suggestions` | `Idempotency-Key` obligatoire ; `{preview_id, expected_version, confirm_outbound:true}` ; exige opt-in LLM, nouvelle confirmation utilisateur et clé backend configurée. Le modèle ne reçoit qu'un petit lot ; réponse textuelle tolérante devient suggestions en revue seulement. |

Exemple de file : `GET /api/v1/review/queue?period_start=2026-03-01&period_end=2026-03-31&account_ids=<UUID>&mode=family_life` → `{"items":[{"review_group_id":"<UUID>","priority":"high","transaction_count":1,"amount_cents":-1234,"latest_booked_on":"2026-03-12","status":"unclassified"}],"page":{"limit":50,"next_cursor":null,"snapshot_at":"2026-10-09T10:00:00Z"}}`.

Exemple de décision exacte :

```json
{"transaction_id":"550e8400-e29b-41d4-a716-446655440020","expected_version":1,"action":"confirm","scope":"this_transaction","allocations":[{"category_id":"groceries_organic","flow_kind":"expense","amount_cents":-1234,"beneficiary":"household","cadence":"occasional","flexibility":"variable_essential","acquisition":"unknown"}]}
```

Réponse : `{"decision_id":"<UUID>","transaction_id":"<UUID>","status":"confirmed","allocated_cents":-1234,"residual_cents":0,"version":2,"audit_event_id":"<UUID>"}`. Une allocation en somme inexacte ou dans un `flow_kind` non admissible répond 422 sans écriture.

Annulation : `POST /api/v1/review/decisions/<decision_id>/undo` avec `{"expected_version":2,"reason":"Correction demandée"}` → `200 {"decision_id":"<UUID>","undone_decision_id":"<UUID>","transaction_id":"<UUID>","status":"unclassified","version":3,"audit_event_id":"<UUID>"}`. La ligne bancaire source demeure inchangée.

Exemple d'aperçu LLM sans appel sortant : `POST /api/v1/review/llm-suggestions/preview` avec `{"transaction_ids":["<UUID>"]}` → `200 {"preview_id":"<UUID>","version":1,"outbound_operations":[{"temporary_id":"A01","booked_on":"2026-03-12","amount_cents":-1234,"label":"LIBELLE SYNTHETIQUE"}],"redactions":[],"network_call_made":false,"expires_at_utc":"2026-10-09T10:15:00Z"}`. Après revue du texte exact et confirmation explicite : `POST /api/v1/review/llm-suggestions` avec `{"preview_id":"<UUID>","expected_version":1,"confirm_outbound":true}` → `202 {"llm_run_id":"<UUID>","status":"partial","suggestion_count":1,"applied_allocation_count":0}`. La sortie modèle est analysée en texte tolérant et seule une suggestion est écrite.

### Analytique

Tous les chemins suivants acceptent les filtres communs et renvoient l'enveloppe analytique décrite ci-dessus. `drilldown` exige `metric_definition_id` et réutilise les paramètres exacts du résultat d'origine.

| Méthode et chemin | Charge utile spécifique |
|---|---|
| `GET /api/v1/analytics/overview` | enveloppe primaire `bank_net_flow.v1`; métriques KPI individuelles et URL de drilldown, toutes avec IDs canoniques de `METRICS.md`. |
| `GET /api/v1/analytics/cashflow` | `bank_net_flow.v1`; sommes `by_flow_kind[]`, crédits, débits, inconnus. Le total est la somme bancaire signée. |
| `GET /api/v1/analytics/spending-hierarchy` | `consumption_net.v1`; arbre de dépenses confirmées, liens `refund_of` confirmés appliqués à la date de l'avoir, résidus à part. |
| `GET /api/v1/analytics/series` | `metric_definition_id` demandé parmi ceux de `METRICS.md`; bucket `day`, `week` ou `month`, chaque point garde `raw_cents`/état de couverture. |
| `GET /api/v1/analytics/merchants` | groupement par payee de `consumption_net.v1`; opération_count, cents et drilldown reprennent le même périmètre. |
| `GET /api/v1/analytics/family` | enveloppe primaire `consumption_net.v1`; consommation, `personal_allowances.v1`, enfants/garde et remboursements liés présentés avec leurs IDs canoniques. |
| `GET /api/v1/analytics/savings` | `savings_net_transferred.v1`; jambes identifiées côté compte courant, paires et engagements explicitement choisis. |
| `GET /api/v1/analytics/data-quality` | `unclassified_amount_share.v1`, `operation_coverage.v1`, résidu absolu et signé, mois/couverture. |
| `GET /api/v1/analytics/drilldown` | `metric_definition_id` canonique, mêmes filtres, curseur ; transactions exactes utilisées et somme recalculée. |

Exemple Overview : `GET /api/v1/analytics/overview?period_start=2026-03-01&period_end=2026-03-31&account_ids=<UUID>&mode=family_life&personal_mode=envelope&flow_kind=unknown` → réponse standard avec `metric_definition_id:"bank_net_flow.v1"`, `raw_cents:-1200` et `metrics:[{"metric_definition_id":"bank_net_flow.v1","raw_cents":-1200,"unit":"EUR-cent"},{"metric_definition_id":"regular_income.v1","raw_cents":5000,"unit":"EUR-cent"}]`. Le filtre `unknown` cible le résidu non nul ; aucun ID de métrique financière n'est inventé pour le regroupement de cartes.

Exemple Drilldown sur consommation : `GET /api/v1/analytics/drilldown?metric_definition_id=consumption_net.v1&period_start=2026-03-01&period_end=2026-03-31&account_ids=<UUID>&mode=family_life&personal_mode=envelope` → `{"period":{"start":"2026-03-01","end":"2026-03-31","kind":"custom","completeness":"complete","as_of":"2026-10-10"},"accounts":["<UUID>"],"coverage":{"status":"complete","operation_count":2,"fully_classified_count":2},"filters":{"mode":"family_life","personal_mode":"envelope","category_ids":["groceries_organic"],"flow_kinds":["expense","refund"]},"metric_definition_id":"consumption_net.v1","raw_cents":1200,"unit":"EUR-cent","items":[{"transaction_id":"<UUID_ACHAT>","booked_on":"2026-03-12","amount_cents":-1234,"metric_contribution_cents":1234,"transaction_links":[]},{"transaction_id":"<UUID_AVOIR>","booked_on":"2026-04-02","amount_cents":34,"metric_contribution_cents":-34,"transaction_links":[{"transaction_link_id":"<UUID_LIEN>","link_kind":"refund_of","target_transaction_id":"<UUID_ACHAT>","target_allocation_id":"<UUID_ALLOCATION_ACHAT>","amount_cents":34,"status":"confirmed","attribution_date":"2026-04-02"}]}],"page":{"limit":50,"next_cursor":null}}`. Le crédit réduit la catégorie de l'achat à la date de l'avoir, même entre deux mois. Le lien ne contribue que si les deux comptes source et cible sont dans `account_ids`; sinon l'avoir reste « non attribué » avec avertissement. Le brut bancaire est inchangé et le drilldown montre les deux transactions et le lien (ADR-021/024, INV-09).

Autres réponses analytiques synthétiques (elles reprennent toutes la même enveloppe `period/accounts/coverage/filters/metric_definition_id/raw_cents/unit`) :

- `GET /analytics/cashflow` → `metric_definition_id:"bank_net_flow.v1"`, `raw_cents:-1200`, `by_flow_kind:[{"flow_kind":"expense","raw_cents":-1234},{"flow_kind":"refund","raw_cents":34}]`.
- `GET /analytics/spending-hierarchy` → `metric_definition_id:"consumption_net.v1"`, `raw_cents:1200`, `items:[{"category_id":"groceries_organic","raw_cents":1200,"transaction_ids":["<UUID_ACHAT>","<UUID_AVOIR>"]}]`; les deux IDs sont l'achat et l'avoir lié montrés dans le drilldown.
- `GET /analytics/series?metric_definition_id=bank_net_flow.v1&bucket=month` → `metric_definition_id:"bank_net_flow.v1"`, `raw_cents:null` pour l'enveloppe de série, `series:[{"period_start":"2026-03-01","period_end":"2026-03-31","raw_cents":-1200,"completeness":"complete"}]`.
- `GET /analytics/merchants` → `metric_definition_id:"consumption_net.v1"`, `raw_cents:1200`, `items:[{"payee_id":"<UUID>","operation_count":1,"amount_cents":1200}]`.
- `GET /analytics/family` → `metric_definition_id:"consumption_net.v1"`, `raw_cents:1200`, `metrics:[{"metric_definition_id":"consumption_net.v1","raw_cents":1200},{"metric_definition_id":"personal_allowances.v1","raw_cents":0}]`, `linked_refunds_cents:34`.
- `GET /analytics/savings` → `metric_definition_id:"savings_net_transferred.v1"`, `raw_cents:15000`, `unit:"EUR-cent"`.
- `GET /analytics/data-quality` → `metric_definition_id:"unclassified_amount_share.v1"`, `raw_cents:null`, `raw_basis_points:3000`, `unclassified_absolute_residual_cents:3000`, `unclassified_signed_residual_cents:-1000`, `operation_count:10`, `fully_classified_count:7`.

### Budgets

| Méthode et chemin | Contrat |
|---|---|
| `GET /api/v1/budgets` | filtre `period_start`, `period_end`, `account_ids`, `category_ids`, `status`; liste paginée et scope compte explicite. |
| `POST /api/v1/budgets` | `Idempotency-Key` obligatoire ; crée `{label, account_ids[], category_id, flow_kind, period_start, period_end, amount_cents}` ; au moins un compte explicite, centimes positifs ou nuls, catégorie/flux admissible. |
| `GET /api/v1/budgets/{budget_id}` | détail de l'enveloppe, IDs de comptes, version et dates. |
| `PATCH /api/v1/budgets/{budget_id}` | mises à jour versionnées `expected_version`; auditées. |
| `DELETE /api/v1/budgets/{budget_id}` | désactivation logique versionnée, pas de suppression d'historique. |
| `GET /api/v1/budgets/{budget_id}/progress` | réel vs enveloppe, couverture, formule canonique selon `flow_kind`, sans prorata ni extrapolation des périodes incomplètes. |

Les périmètres compte sont persistés dans `budget_accounts` ; la création exige une liste non vide afin qu'un budget ne mélange pas de nouveaux comptes silencieusement. Dépense utilise `consumption_net.v1`; allocation personnelle utilise `personal_allowances.v1`. Exemple : `POST /api/v1/budgets` avec `{"label":"Courses synthétiques","account_ids":["<UUID>"],"category_id":"groceries","flow_kind":"expense","period_start":"2026-03-01","period_end":"2026-03-31","amount_cents":50000}` → `201 {"budget_id":"<UUID>","account_ids":["<UUID>"],"version":1,"is_active":true}`. `GET /api/v1/budgets/<UUID>/progress` → `{"budget_id":"<UUID>","metric_definition_id":"consumption_net.v1","budget_amount_cents":50000,"actual_cents":12000,"remaining_cents":38000,"coverage":{"status":"complete"},"period":{"start":"2026-03-01","end":"2026-03-31"}}`.

### Insights

| Méthode et chemin | Contrat |
|---|---|
| `GET /api/v1/insights` | liste des faits/synthèses conservés, états, période, metric IDs et coverage. |
| `GET /api/v1/insights/{insight_id}` | détail, références de métriques/drilldowns et limites. |
| `POST /api/v1/insights/generate` | `Idempotency-Key` obligatoire ; génération explicitement demandée avec filtres ; LLM utilisé seulement si opt-in déjà actif ; réponse lie chaque fait à `metric_definition_id`, période et drilldown, enregistré dans `insight_facts`. |
| `PATCH /api/v1/insights/{insight_id}` | `{expected_version, status:"ignored"|"active"|"archived"}` ; auditée. |

Les textes générés restent du texte, jamais du HTML interprété ; un nombre non relié à un fait serveur n'est pas exposé comme constat vérifié. Réponse de génération : `{insight_id, status, generated_by, facts:[{insight_fact_id, metric_definition_id, period, value_kind, raw_cents, raw_basis_points, raw_count, state_value, transaction_ids}], narrative, coverage, version}`. Chaque `metric_definition_id` est validé contre les 11 IDs canoniques de `METRICS.md`; `insights` ne porte pas de métrique unique. Aucun événement LLM ne modifie une allocation.

Exemple : `POST /api/v1/insights/generate` avec `{"period_start":"2026-03-01","period_end":"2026-03-31","account_ids":["<UUID>"],"mode":"family_life"}` → `201 {"insight_id":"<UUID>","status":"active","generated_by":"deterministic","facts":[{"metric_definition_id":"consumption_net.v1","raw_cents":1200,"transaction_ids":["<UUID>"]}],"narrative":"La consommation confirmée de la période est de 1 200 centimes.","coverage":{"status":"complete"},"version":1}`. `PATCH /api/v1/insights/<UUID>` avec `{"expected_version":1,"status":"ignored"}` → `200 {"insight_id":"<UUID>","status":"ignored","version":2}`.

### Paramètres et LLM

| Méthode et chemin | Contrat |
|---|---|
| `GET /api/v1/settings` | configuration sans secrets : `llm_enabled`, endpoint/model, `api_key_configured:boolean`, consentement/versions, préférences de confidentialité. |
| `PATCH /api/v1/settings` | `{expected_version, llm_enabled, base_url?, model?, consent_confirmed?, redaction_options?}` ; opt-in explicite et audit. `llm_enabled=true` exige consentement explicite et clé disponible côté backend. |
| `POST /api/v1/settings/llm/test-connection` | corps `{confirm_outbound_request:true}` ; un appel sortant unique au fournisseur ; réponse connectée/erreur redigée, latence en entier ; la clé n'est jamais renvoyée. |

Clé du fournisseur : variable backend/environnement local ignoré, jamais dans le frontend, corps de réponse, logs ou `llm_runs`. Les données réellement transmises sont prévisualisables avant suggestion conformément à INV-11/LLM-07/08.

Exemple `GET /api/v1/settings` : `{"version":1,"llm_enabled":false,"provider":"openai_compatible","base_url":null,"model":null,"api_key_configured":false,"consent_at_utc":null}`.

Exemple d'opt-in et de test : `PATCH /api/v1/settings` avec `{"expected_version":1,"llm_enabled":true,"base_url":"https://provider.example/v1","model":"model-synthetic","consent_confirmed":true}` → `200 {"version":2,"llm_enabled":true,"api_key_configured":true,"consent_at_utc":"2026-10-09T10:00:00Z"}`. `POST /api/v1/settings/llm/test-connection` avec `{"confirm_outbound_request":true}` → `200 {"status":"connected","latency_ms":120,"provider":"openai_compatible"}` ; aucune clé n'est présente dans la réponse.

### Sauvegardes et restauration

| Méthode et chemin | Contrat |
|---|---|
| `GET /api/v1/backups` | manifeste paginé : version de schéma, date UTC, hash, taille, originaux inclus, état ; aucun chemin local. |
| `POST /api/v1/backups` | `Idempotency-Key` obligatoire ; `{include_originals:boolean}` ; API SQLite de backup compatible WAL, manifeste vérifié et hashé. Chaque backup inclut obligatoirement la clé HMAC locale dans son périmètre protégé (`includes_hmac_key:true`), préserve les empreintes de compte et est stocké en mode `0600`, ignoré par Git. |
| `GET /api/v1/backups/{backup_manifest_id}` | état détaillé du manifeste et résultat `integrity_check`. |
| `POST /api/v1/backups/restore/preview` | upload de backup ; restaure/vérifie dans DB temporaire distincte, renvoie versions, counts synthétiques, compatibilité et avertissements ; l'actuelle ne change pas. |
| `POST /api/v1/backups/restore/commit` | `Idempotency-Key` obligatoire ; `{restore_preview_id, expected_database_version}` ; après aperçu et sauvegarde pré-restore, swap atomique/audité. La restauration vérifie que la clé HMAC incluse est présente ; si elle est perdue hors backup, la reconnaissance exige un remappage explicite dans l'aperçu d'import. |

Exemple : `POST /api/v1/backups` avec `{"include_originals":true}` → `201 {"backup_manifest_id":"<UUID>","schema_version":"1","archive_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","file_size_bytes":4096,"includes_originals":true,"includes_hmac_key":true,"permissions_octal":"0600","status":"verified","integrity_check":"ok"}`. Un restore non valide répond `BACKUP_INVALID` sans toucher à la base active.

Restauration : `POST /api/v1/backups/restore/preview` avec une archive synthétique vérifiable → `200 {"restore_preview_id":"<UUID>","compatible":true,"schema_version":"1","integrity_check":"ok","account_count":1,"transaction_count":2,"requires_backup_of_current":true}`. `POST /api/v1/backups/restore/commit` avec `{"restore_preview_id":"<UUID>","expected_database_version":4}` → `200 {"status":"restored","backup_manifest_id":"<UUID>","database_version":5,"audit_event_id":"<UUID>"}`. Le commit n'est possible qu'après aperçu, validation et sauvegarde de la base active.

## Blocages par lot / gates

| Lot | Ce qui est bloquant avant de déclarer le lot utilisable |
|---|---|
| LOT 0 / G0 | Contrats et oracles approuvés ; IDs `flow_kind`/catégories stables ; schéma chargeable ; multiplicité et invariants d'import non ambigus ; fixtures synthétiques ; décisions G0 arbitrées. |
| LOT 1 / G1 | DB fraîche migrée ; `health` hors ligne ; erreurs API cohérentes ; boucle `up` vérifiée ; UI consomme health. |
| LOT 2 / G2 | Décodage CP1252/UTF-8, en-têtes multi-sections et montants exacts ; aperçu sans transaction ; numéro masqué ; aucune entrée CSV malformée ne passe silencieusement. |
| LOT 3 / G3 | Hash/idempotence, `BEGIN IMMEDIATE`, rangs, provenance par occurrence, ordre inversé, troisième doublon, rollback, concurrence, couverture et soldes prouvés ; aucune suppression automatique. |
| LOT 4 / G4 | Taxonomie versionnée importée ; FK flow/catégorie et sommes exactes ; priorité humaine ; simulation sans effet ; audit/annulation. |
| LOT 5 / G5 | File et actions de revue persistent ; concurrence optimiste ; une correction n'altère que la sélection ; split exact et annulation accessibles sans SQL. |
| LOT 6 / G6 | Une seule définition par métrique ; banque = somme des transactions pour tout filtre ; catégories/drilldowns concordent ; null et partialité corrects. |
| LOT 7 / G7 | Filtres URL cohérents ; chaque graphique mène à un drilldown égal au total ; aucun mock en mode réel ; dates/couverture visibles. |
| LOT 8 / G8 | IA désactivée par défaut ; appel sortant seulement après consentement ; sortie libre tolérée mais suggestions seules ; aucun texte hostile exécuté ni clé loguée. |
| LOT 9 / G9 | Prévisions excluent mois incomplets et exposent hypothèses ; aides/allocations sans double comptage ; budget ne modifie pas les transactions. |
| LOT 10 / G10 | Toute affirmation chiffrée est liée à une métrique et un drilldown serveur ; limites ou manque de données sont visibles. |
| LOT 11 / G11 | Backup WAL cohérent, restauration isolée vérifiée, binding loopback/CSRF, aucune fuite, cycle mensuel E2E et reprise sans changement des décisions. |

Ces contrats sont des propositions T02. Toute modification ultérieure de signature, nullabilité, unité ou sémantique de flux doit être accompagnée d'un arbitrage ADR et d'une évolution versionnée.
