# Aperçu API des imports (T11)

L'aperçu prépare un fichier CA pour une décision humaine sans créer de ligne
`import_files`, `import_account_sections`, `transactions`, `source_rows` ou
`accounts`. La seule écriture possible pendant la première requête est la clé
locale HMAC dans `app_settings`, sous transaction `BEGIN IMMEDIATE`.

## Cycle de vie

1. `POST /api/v1/imports/preview` reçoit un champ multipart `file`. Le SHA-256
   porte sur les octets exacts avant décodage. Les types `text/csv`,
   `application/vnd.ms-excel`, `application/octet-stream` et `text/plain` sont
   acceptés si leur contenu est du texte. Le nom transmis par le client n'est
   jamais utilisé.
2. Le parseur et le normaliseur établissent les totaux en centimes, dates,
   anomalies et mappings HMAC connus. Les exemples de lignes sont omis ; aucun
   libellé bancaire n'est exposé dans l'aperçu ou ses erreurs.
3. Les octets bruts et l'état résolu sont gardés dans `data/import-previews`
   (ou `preview_dir`), répertoire `0700`, fichiers `0600`, avec noms UUID
   opaques. Le TTL par défaut est de 1 800 secondes. Les expirés sont purgés au
   démarrage et à chaque accès ; la taille cumulée par défaut est bornée à
   100 Mio. Le stockage est temporaire et n'est jamais une archive de commit.
4. `GET /api/v1/imports/preview/{preview_id}` lit l'état. `DELETE` supprime
   immédiatement aperçu et octets (204). Un aperçu absent, supprimé ou expiré
   répond `410 IMPORT_PREVIEW_EXPIRED`.
5. Les trois PATCH acceptent `expected_version`, augmentent `version` en cas
   de succès et recalculent `can_commit`. Une version obsolète donne
   `409 VERSION_CONFLICT`. La résolution d'un nouveau compte ne le crée pas.
6. Aucun endpoint de commit ni de réconciliation n'est fourni par T11.

## Résolutions

- `PATCH /api/v1/imports/{preview_id}/sections/{section_index}/account-mapping`
  accepte un `account_id` existant ou `new_account` avec alias et rôle. L'alias
  est borné à 80 caractères et refuse les suites de 8 chiffres ou plus.
- `PATCH /api/v1/imports/{preview_id}/sections/{section_index}/coverage` accepte
  `coverage_from` et `coverage_through`, puis calcule `complete_through` selon
  le jour de génération provisoire (ADR-018).
- `PATCH /api/v1/imports/{preview_id}/rows/{logical_row_idx}/multiplicity`
  stocke `link_existing` ou `create_occurrence` pour une ligne `OUT_OF_PERIOD`.
  Cette décision reste en aperçu et n'est pas appliquée avant T13. Le corps
  accepte aussi `section_index` lorsque le même index logique existe dans
  plusieurs sections ; sans cette précision, l'API répond
  `409 MULTIPLICITY_AMBIGUOUS` et ne lève aucun rejet.

`reconciliation` vaut toujours `null`. Les libellés sont omis entièrement,
y compris les lignes synthétiques : les seuls diagnostics de rejet sont des
codes, emplacement logique et messages prédéfinis sans extrait source.

## Erreurs normalisées

Toutes les erreurs suivent `{"error":{"code":"…","message":"…","details":{}}}`.

| HTTP | Code | Cause |
|---:|---|---|
| 413 | `FILE_TOO_LARGE` | Taille d'upload supérieure au plafond |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | Type non autorisé ou contenu binaire manifeste |
| 422 | `CSV_INVALID` / `VALIDATION_ERROR` | Fichier ou montant invalide |
| 409 | `VERSION_CONFLICT` | `expected_version` périmé |
| 410 | `IMPORT_PREVIEW_EXPIRED` | Aperçu supprimé ou TTL écoulé |
| 503 | `DATABASE_NOT_READY` | Base absente, en retard ou indisponible ; aucune création/migration |

La clé HMAC n'est jamais renvoyée ni journalisée. Le numéro de compte n'est
conservé que dans les octets temporaires protégés et en mémoire lors du parse ;
seule son empreinte HMAC sert à retrouver un mapping connu.
