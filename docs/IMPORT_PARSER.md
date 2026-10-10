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

`ParsedExport` contient l'encodage détecté (`utf-8-sig`, `utf-8` ou `cp1252`), `generated_on`, `generation_date_source` (`preamble` ou `missing`), les sections dans l'ordre source et toutes les anomalies. Chaque `ParsedSection` utilise un ordinal commençant à 1, les noms persistés `section_index`, `coverage_from`, `coverage_through`, `complete_through`, `balance_as_of` et `header_balance_cents`, et les opérations acceptées. Une section `Compte` sans numéro canonique exploitable, ou un en-tête sans préambule de compte, est bloquant. `ParsedRow.logical_row_idx` est l'ordre d'apparition par section ; `line_start`/`line_end` sont les lignes physiques du CSV et correspondent à `physical_line_start`/`physical_line_end` en base. Son `raw_label` est le champ décodé exact, sans normalisation T10. `repr(ParsedRow)` masque le libellé.

`amount_cents` est un `int` strict : les débits sont négatifs et les crédits positifs. Toute opération, solde d'en-tête et total agrégé est borné par `|cents| ≤ 10^13`. Un dépassement produit `AMOUNT_OUT_OF_RANGE` bloquant. Une ligne invalide n'est pas publiée dans `rows`; une ligne `OUT_OF_PERIOD` reste visible avec son anomalie bloquante afin que le service demande une résolution auditée. Aucun fichier bloqué ne doit être commité partiellement (INV-04).

Le numéro de compte existe uniquement en mémoire dans `CanonicalAccountNumber`. `str`, `repr` et le formatage de ce type masquent sa valeur. L'empreinte est calculée par `bank_fingerprint` avec le préfixe de domaine `credit-agricole\0`; le parseur ne persiste ni le numéro ni l'empreinte.

Le décodeur préfère le BOM UTF-8, puis UTF-8 strict, puis CP1252 strict. Les octets CP1252 indéfinis déclenchent `INVALID_ENCODING`. Les fins de ligne CRLF, LF et CR délimitent les mêmes enregistrements ; à l'intérieur d'un champ cité, leurs caractères décodés sont préservés tels quels. La taille maximale par défaut est 20 MB ; la longueur de champ maximale par défaut est 10 000 caractères et sa borne de sécurité est 100 000. Ces limites sont paramétrables dans ces bornes. Au-dessus, le parseur retourne `FILE_TOO_LARGE` ou `FIELD_TOO_LONG`. Un libellé est aussi borné à 10 000 caractères bruts et 20 000 caractères après NFC ; un dépassement produit `LABEL_TOO_LONG` bloquant. Aucun `identity_label` n'est tronqué. Un libellé de plus de 5 000 caractères sous ces bornes demeure conservé avec l'avertissement `LONG_LABEL`.

`complete_through` vaut le minimum entre la fin de période et la veille de `generated_on`. Sans date de génération, le dernier jour portant une opération est provisoire et la borne est au plus la veille de cette date ; une section sans opération n'affirme aucune journée complète (IMP-11). Une opération le jour de génération reçoit `PROVISIONAL_DAY`; une opération postérieure déclenche `FUTURE_DATED_ROW` bloquant. Une date antérieure au 01/01/2000 déclenche `DATE_IMPLAUSIBLE` bloquant. Une date de génération antérieure à la période, divergente d'une autre ligne de génération, ou placée après le début des sections déclenche `GENERATION_DATE_INCONSISTENT`. Après l'en-tête, toute ligne non vide qui n'est pas un nouvel en-tête ou une nouvelle section structurée est traitée comme une ligne candidate et reçoit une anomalie en cas de non-conformité. Les lignes hors période et les deux sections portant le même numéro canonique sont bloquantes. Une fin de période au moins 31 jours après la date maximale des opérations déclenche `COVERAGE_TAIL_GAP` avertissant ; ce seuil est un signal de contrôle, pas une preuve de troncature.

## Codes d'anomalie

Les messages sont rédigés depuis un catalogue fixe et ne contiennent jamais de numéro de compte, de champ CSV ou de libellé. `section_ordinal`, `logical_row_idx`, `line_no` et `field` donnent le contexte non privé.

| Code | Niveau | Signification |
|---|---|---|
| `AMOUNT_OUT_OF_RANGE` | bloquant | Opération, solde ou total supérieur à `10^13` centimes en valeur absolue. |
| `FILE_TOO_LARGE`, `FIELD_TOO_LONG` | bloquant | Limite configurée dépassée. |
| `INVALID_ENCODING`, `PARSE_ERROR`, `UNCLOSED_QUOTE` | bloquant | Octets, CSV ou guillemets impossibles à interpréter. |
| `EMPTY_FILE`, `MISSING_HEADER`, `MISSING_PERIOD` | bloquant | Export incomplet ; `MISSING_PERIOD` reste résoluble dans l'aperçu. |
| `MISSING_ACCOUNT_NUMBER`, `INVALID_ACCOUNT_NUMBER`, `DUPLICATE_ACCOUNT_SECTION` | bloquant | Numéro de section absent/inexploitable ou compte répété. |
| `BAD_FIELD_COUNT`, `INVALID_DATE` | bloquant | Structure ou date invalide. |
| `DATE_IMPLAUSIBLE`, `FUTURE_DATED_ROW`, `GENERATION_DATE_INCONSISTENT` | bloquant | Date hors de la borne plausible ou génération incohérente. |
| `LABEL_TOO_LONG` | bloquant | Libellé brut supérieur à 10 000 caractères ou NFC supérieur à 20 000. |
| `NON_NUMERIC_AMOUNT`, `BOTH_AMOUNTS`, `NEGATIVE_AMOUNT`, `ZERO_AMOUNT` | bloquant | Montant absent, nul ou non conforme au contrat CA. |
| `OUT_OF_PERIOD` | bloquant | L'opération requiert une résolution de multiplicité avant commit. |
| `PROVISIONAL_DAY` | avertissement | La journée de génération reste dans les sommes, mais n'est pas complète. |
| `COVERAGE_TAIL_GAP` | avertissement | La date maximale des opérations est distante d'au moins 31 jours de `period_to`. |
| `DUPLICATE_HEADER`, `INTERLEAVED_BLANK_LINES`, `LONG_LABEL` | avertissement | Structure récupérable ou libellé long conservé. |

Correspondance des codes parser/API définie par `MATRICE_TESTS_ADDENDUM.md` : `MISSING_PERIOD` devient `SECTION_RANGE_REQUIRED`; `DUPLICATE_ACCOUNT_SECTION`, `PROVISIONAL_DAY` et `OUT_OF_PERIOD` gardent leur code; `ZERO_AMOUNT` est exposé sous `VALIDATION_ERROR` avec le détail `ZERO_AMOUNT`. Les nouveaux codes bloquants doivent rester bloquants dans le service d'aperçu.

## Tests et ajout d'un cas

Les tests unitaires sont dans `backend/tests/unit/importers/`. Le test `CSV-04` relit les 34 fixtures synthétiques et vérifie les anomalies, comptes d'opérations, sommes en centimes, dates et soldes contre `backend/tests/fixtures/manifest.yaml`. Le test réel `CSV-06` est isolé dans `backend/tests/integration/test_real_csv_opt_in.py` ; sans `COMPTES_REAL_CSV`, il est explicitement ignoré.

Le manifeste synthétique considère `ca_2026_multi_sections.csv` accepté, mais sa seconde section « Compte espèce PEA » ne contient aucun numéro exploitable. ADR-026.2 prévaut : le parseur la signale `MISSING_ACCOUNT_NUMBER` sans modifier la fixture.

Pour un nouveau format ou une anomalie, ajouter d'abord un test synthétique isolé dans `backend/tests/unit/importers/`, vérifier le code et le niveau attendus, puis actualiser le corpus et son manifeste avec le propriétaire T03. Ne pas ajouter de vrai CSV aux tests. Les cas de `identity_label` et de normalisation sont du périmètre T10.
