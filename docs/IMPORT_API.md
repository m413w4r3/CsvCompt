# API d'aperçu des imports (T11)

L'aperçu est temporaire et privé. Il ne crée aucune ligne dans `import_files`,
`import_account_sections`, `transactions`, `source_rows` ou `accounts`. La seule
écriture SQLite permise est la clé HMAC locale dans `app_settings`, créée une
fois sous `BEGIN IMMEDIATE`. `reconciliation` vaut toujours `null`.

## Cycle de vie

1. `POST /api/v1/imports/preview` accepte un seul champ multipart `file`. Les
types `text/csv`, `application/vnd.ms-excel`, `application/octet-stream` et
`text/plain` sont acceptés si le contenu est du texte. La taille maximale par
défaut est 20 Mio. Un contrôle `Content-Length` rejette les corps manifestement
trop grands avant leur spool multipart, puis la taille des octets du fichier est
vérifiée précisément. Le SHA-256 porte sur les octets reçus avant décodage.
2. Le nom client n'est ni chemin, ni donnée d'aperçu, ni journalisé. Les lignes
d'exemple sont omises : aucun libellé, numéro de compte ou HMAC n'est renvoyé.
3. L'aperçu contient les totaux exacts en centimes, dates, sections, mapping
HMAC (`known`, `unknown`, `ambiguous`), anomalies et `can_commit`. Les détails
d'anomalie stockés sont plafonnés (1 000 par catégorie par défaut) ;
`anomaly_counts` et `anomaly_summary` conservent les agrégats et le nombre de
détails omis. Tout rejet bloquant reste bloquant même si son détail dépasse le
plafond.
4. Les octets et l'état sont stockés temporairement sous `data/import-previews`
(ou `preview_dir`) dans un répertoire `0700`, avec des fichiers `0600` et des
noms UUID opaques. Le TTL par défaut est de 1 800 secondes ; le quota cumulé par
défaut est de 100 Mio. Les expirés et fichiers temporaires orphelins sont
purgés au démarrage et à chaque accès. Ce stockage n'est jamais une archive.
5. `GET /api/v1/imports/preview/{preview_id}` renvoie l'état courant.
`DELETE /api/v1/imports/preview/{preview_id}` supprime les données immédiatement
et répond 204. Un aperçu expiré, supprimé ou absent répond `410
IMPORT_PREVIEW_EXPIRED`.
6. Les PATCH exigent `expected_version`. Un conflit donne `409
VERSION_CONFLICT`; une mutation réussie incrémente `version` et recalcule les
rejets restants et `can_commit`.
7. La date `generated_on` de repli est la date locale du système (`date.today()`)
avec `generation_date_source: "received_on_fallback"`. La date du client ou du
fuseau Paris n'est pas substituée.

## Résolutions humaines

- `PATCH /api/v1/imports/{preview_id}/sections/{section_index}/account-mapping`
  accepte soit un `account_id` existant, soit
  `new_account: {alias, account_role}`. Une section `known` ne peut pas être
  remappée. Une empreinte préexistante différente, une empreinte déjà associée
  à un nouveau compte, un alias existant ou un alias répété dans l'aperçu est
  refusé. Un alias a au plus 80 caractères, 7 chiffres cumulés après retrait
  des séparateurs, aucun motif IBAN et aucune balise `<...>`. Le compte projeté
  reste une donnée d'aperçu ; sa création est hors T11.
- Une section sans numéro canonique donne `MISSING_ACCOUNT_NUMBER`, reste
  bloquante et ne peut pas être résolue par mapping.
- `PATCH /api/v1/imports/{preview_id}/sections/{section_index}/coverage`
  n'est valide que pour `SECTION_RANGE_REQUIRED`. La couverture saisie doit
  contenir `[min_date, max_date]`; elle ne remplace jamais la période déclarée
  par la banque. `complete_through` suit ADR-018 : jour de génération moins un,
  borné à la fin de couverture.
- `PATCH /api/v1/imports/{preview_id}/rows/{logical_row_idx}/multiplicity`
  accepte `action: "link_existing"` avec `transaction_id` obligatoire ou
  `action: "create_occurrence"`. `link_existing` vérifie dès l'aperçu que la
  transaction existe et que compte, date, montant entier signé et identité
  stricte sont égaux ; T12 revérifie la cible avant l'archive puis sous
  `BEGIN IMMEDIATE` pendant le commit.
  `section_index` est nécessaire quand l'index logique identifie plusieurs
  lignes. Une décision est mémorisée avec acteur, horodatage et ancienne valeur.
  Le lien n'est pas réconcilié avant T13.

`can_commit` est faux si une anomalie bloquante demeure, si une section ayant un
numéro reconnu attend son mapping, si une couverture est requise, si une ligne
`OUT_OF_PERIOD` n'a pas de décision, si des détails de décision ont été omis au
plafond, ou si deux sections aboutissent au même compte. Les rejets non
résolus restent visibles. Le hash déjà commité est indiqué uniquement par
`already_imported: true` et `import_file_id`.

## Erreurs normalisées

Toutes les erreurs suivent `{"error":{"code":"…","message":"…","details":{}}}`.

| HTTP | Code | Cause |
|---:|---|---|
| 400 | `VALIDATION_ERROR` | formulaire ou taille déclarée invalide |
| 409 | `VERSION_CONFLICT` | `expected_version` périmé |
| 409 | `ACCOUNT_MAPPING_LOCKED` | tentative de remapper une section `known` |
| 409 / 422 | `ACCOUNT_FINGERPRINT_CONFLICT` | empreinte déjà liée ou incompatible avec le compte |
| 409 | `ACCOUNT_ALIAS_CONFLICT` | alias existant ou répété dans l'aperçu |
| 409 | `MULTIPLICITY_AMBIGUOUS` | index logique présent dans plusieurs sections sans `section_index` |
| 410 | `IMPORT_PREVIEW_EXPIRED` | aperçu absent, supprimé ou TTL écoulé |
| 413 | `FILE_TOO_LARGE` | corps déclaré manifestement trop grand ou fichier au-dessus du plafond |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | type non autorisé ou contenu binaire manifeste |
| 422 | `CSV_INVALID` | CSV mal formé |
| 422 | `VALIDATION_ERROR` | mapping, plage, alias, ou cible `link_existing` invalide |
| 200 | `MISSING_ACCOUNT_NUMBER` | rejet bloquant dans `rejections`, sans mapping possible |
| 200 | `AMOUNT_OUT_OF_RANGE` | rejet bloquant dans `rejections`, hors borne ADR-026 |
| 503 | `DATABASE_NOT_READY` | base absente, en retard ou indisponible ; aucune création/migration |

Les réponses d'erreur ne reflètent jamais les valeurs source. La clé HMAC ne
sort jamais du backend. Une clé absente est créée une seule fois au premier
usage valide ; un endpoint d'aperçu avec une base non prête répond 503 sans
créer ni migrer la base.
