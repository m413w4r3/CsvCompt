# Parseur Crédit Agricole (T09)

`app.importers.credit_agricole.parse_export(data: bytes, *, max_bytes=20_000_000, max_field_chars=10_000)` est une fonction pure : elle ne lit ni fichier, ni base, ni réseau, n'écrit aucun journal et ne convertit jamais un montant en flottant. Une entrée trop grande ou illisible produit une anomalie structurée. Toute erreur imprévue est ramenée à `PARSE_ERROR` sans texte source.

```python
from app.importers.credit_agricole import parse_export

parsed = parse_export(upload_bytes)
if parsed.has_blocking_anomalies:
    # Le service d'import refusera le commit de tout le fichier.
    ...
```

## Résultat

`ParsedExport` contient l'encodage détecté (`utf-8-sig`, `utf-8` ou `cp1252`), `generated_on`, `generation_date_source` (`preamble` ou `missing`), les sections dans l'ordre source et toutes les anomalies. Chaque `ParsedSection` utilise un ordinal commençant à 1, les noms persistés `section_index`, `coverage_from`, `coverage_through`, `complete_through`, `balance_as_of` et `header_balance_cents`, et les opérations acceptées. `ParsedRow.logical_row_idx` est l'ordre d'apparition par section ; `line_start`/`line_end` sont les lignes physiques du CSV et correspondent à `physical_line_start`/`physical_line_end` en base. Son `raw_label` est le champ décodé exact, sans normalisation T10.

`amount_cents` est un `int` strict : les débits sont négatifs et les crédits positifs. Une ligne invalide n'est pas publiée dans `rows`; une ligne `OUT_OF_PERIOD` reste visible avec son anomalie bloquante afin que le service demande une résolution auditée. Aucun fichier bloqué ne doit être commité partiellement (INV-04).

Le numéro de compte existe uniquement en mémoire dans `CanonicalAccountNumber`. `str`, `repr` et le formatage de ce type masquent sa valeur. L'empreinte est calculée par `bank_fingerprint` avec le préfixe de domaine `credit-agricole\0`; le parseur ne persiste ni le numéro ni l'empreinte.

Le décodeur préfère le BOM UTF-8, puis UTF-8 strict, puis CP1252 strict. Les octets CP1252 indéfinis déclenchent `INVALID_ENCODING`. Les fins de ligne CRLF, LF et CR délimitent les mêmes enregistrements ; à l'intérieur d'un champ cité, leurs caractères décodés sont préservés tels quels. La taille maximale par défaut est 20 MB ; la longueur de champ maximale par défaut est 10 000 caractères et sa borne de sécurité est 100 000. Ces limites sont paramétrables dans ces bornes. Au-dessus, le parseur retourne `FILE_TOO_LARGE` ou `FIELD_TOO_LONG`. Un libellé de plus de 5 000 caractères sous la limite demeure conservé avec l'avertissement `LONG_LABEL`.

`complete_through` vaut le minimum entre la fin de période et la veille de `generated_on`. Sans date de génération, le dernier jour portant une opération est provisoire et la borne est au plus la veille de cette date ; une section sans opération n'affirme aucune journée complète (IMP-11). Toute opération datée le jour de génération ou après reçoit `PROVISIONAL_DAY`. Les lignes hors période et les deux sections portant le même numéro canonique sont bloquantes.

## Codes d'anomalie

Les messages sont rédigés depuis un catalogue fixe et ne contiennent jamais de numéro de compte, de champ CSV ou de libellé. `section_ordinal`, `logical_row_idx`, `line_no` et `field` donnent le contexte non privé.

| Code | Niveau | Signification |
|---|---|---|
| `FILE_TOO_LARGE`, `FIELD_TOO_LONG` | bloquant | Limite configurée dépassée. |
| `INVALID_ENCODING`, `PARSE_ERROR`, `UNCLOSED_QUOTE` | bloquant | Octets, CSV ou guillemets impossibles à interpréter. |
| `EMPTY_FILE`, `MISSING_HEADER`, `MISSING_PERIOD` | bloquant | Export incomplet ; `MISSING_PERIOD` reste résoluble dans l'aperçu. |
| `INVALID_ACCOUNT_NUMBER`, `DUPLICATE_ACCOUNT_SECTION` | bloquant | Identité de section invalide ou répétée. |
| `BAD_FIELD_COUNT`, `INVALID_DATE` | bloquant | Structure ou date invalide. |
| `NON_NUMERIC_AMOUNT`, `BOTH_AMOUNTS`, `NEGATIVE_AMOUNT`, `ZERO_AMOUNT` | bloquant | Montant absent, nul ou non conforme au contrat CA. |
| `OUT_OF_PERIOD` | bloquant | L'opération requiert une résolution de multiplicité avant commit. |
| `PROVISIONAL_DAY` | avertissement | La journée reste dans les sommes, mais n'est pas complète. |
| `DUPLICATE_HEADER`, `INTERLEAVED_BLANK_LINES`, `LONG_LABEL` | avertissement | Structure récupérable ou libellé long conservé. |

Correspondance des codes parser/API définie par `MATRICE_TESTS_ADDENDUM.md` : `MISSING_PERIOD` devient `SECTION_RANGE_REQUIRED`; `DUPLICATE_ACCOUNT_SECTION`, `PROVISIONAL_DAY` et `OUT_OF_PERIOD` gardent leur code; `ZERO_AMOUNT` est exposé sous `VALIDATION_ERROR` avec le détail `ZERO_AMOUNT`. Les autres erreurs structurelles de fichier relèvent de `CSV_INVALID`; `FILE_TOO_LARGE` conserve son code contractuel.

## Tests et ajout d'un cas

Les tests unitaires sont dans `backend/tests/unit/importers/`. Le test `CSV-04` relit les 34 fixtures synthétiques et vérifie les anomalies, comptes d'opérations, sommes en centimes, dates et soldes contre `backend/tests/fixtures/manifest.yaml`. Le test réel `CSV-06` est isolé dans `backend/tests/integration/test_real_csv_opt_in.py` ; sans `COMPTES_REAL_CSV`, il est explicitement ignoré.

Pour un nouveau format ou une anomalie, ajouter d'abord un test synthétique isolé dans `backend/tests/unit/importers/`, vérifier le code et le niveau attendus, puis actualiser le corpus et son manifeste avec le propriétaire T03. Ne pas ajouter de vrai CSV aux tests. Les cas de `identity_label` et de normalisation sont du périmètre T10.
