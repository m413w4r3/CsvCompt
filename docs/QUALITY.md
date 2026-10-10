# Qualité des périodes et contrôle des soldes

Les calculs de qualité sont en lecture seule. Ils utilisent les sections
commitées, les dates des transactions et les centimes entiers stockés en base.
Ils ne changent ni une opération ni un solde source.

## Couverture

Pour chaque compte, une section committée apporte l’intervalle inclusif
`[coverage_from, complete_through]`. La borne est recalculée depuis les données
de section :

- avec une date de téléchargement, `complete_through` vaut le minimum entre la
  fin déclarée et la veille de la génération ;
- sans date de préambule, la date de la dernière opération de la section sert de
  jour provisoire, et `complete_through` vaut au plus la veille de cette date ;
- sans borne fiable ou sans opération pour le repli, aucun jour complet n’est
  affirmé.

`covered_days` forme l’union des intervalles complets. L’ordre des sections et
leur répétition ne changent pas cette union. Ajouter une section ne retire
aucun jour déjà couvert. `provisional_days` désigne les jours déclarés après la
borne complète ; leurs opérations restent comptées dans `operation_count` et
sont aussi comptées dans `provisional_operation_count` tant qu’aucune section
ne les couvre complètement.

`gaps` renvoie les plages non couvertes situées strictement entre deux
intervalles complets attestés. Les jours avant le premier intervalle ne sont
pas des trous. Dans `coverage_summary`, une période entièrement antérieure à
la première section connue a le statut `not_started`, `missing_days=[]` et
zéro jour complet : l’absence de 2025 avant un premier import 2026 n’est donc
pas une alerte. Pour un mois calendaire avant le démarrage, l’enveloppe API
garde `period.completeness="partial"` (cette métrique n’a que trois états),
mais `coverage.status="not_started"` et `missing_days=[]` portent l’absence
d’alerte. Les jours manquants demandés après le début des données sont renvoyés
en plages inclusives.

Pour plusieurs comptes, une date ne compte dans `complete_day_count` que si
elle est complète pour chacun d’eux. Le statut mensuel est `complete` seulement
si tous les jours du mois sont couverts pour chaque compte. Le mois courant
reste `partial` tant qu’il est le mois de la date `as_of`, même si une source
prétend déjà couvrir tout le mois. Un mois antérieur incomplet est `partial` ;
un mois sans preuve suffisante ou touché par un mismatch comparable est
`unknown`. Le résumé de couverture expose séparément `not_started` pour garder
visible la différence entre une période antérieure au démarrage et un trou.

## Contrôle de solde

Tous les soldes d’en-tête des sections commitées sont triés par
`balance_as_of`, sans tenir compte de leur ordre d’import. Un solde est
comparable si `balance_as_of <= complete_through`. Pour deux dates comparables
successives `D0 < D1`, il faut une couverture continue de `(D0, D1]`. Le
recalcul exact est :

```text
solde(D0) + somme(amount_cents pour D0 < booked_on <= D1)
```

Chaque ligne de `transactions` est prise une fois, même si plusieurs imports
ont ajouté des lignes de provenance vers cette opération. Le résultat expose
les deux soldes sources, le solde recalculé et
`delta_cents = solde(D1) - solde_recalculé`.

- `match` : delta nul ;
- `mismatch` : delta non nul ; le mois touché devient `unknown` ;
- `not_comparable` : les données ne permettent pas une comparaison sûre.

Les raisons `not_comparable` sont des codes lisibles par le client :

| Code | Cause |
|---|---|
| `balance_after_complete_through` | Solde daté le jour de génération ou après le dernier jour complet. |
| `coverage_bound_unknown` | La section ne prouve pas de borne complète. |
| `coverage_discontinuous` | Il existe au moins un jour non couvert entre les deux soldes. |
| `single_comparable_balance` | Un seul solde comparable est disponible. |
| `incomplete_header_balance` | La date ou le montant du solde d’en-tête manque. |
| `conflicting_balances_same_day` | Plusieurs sections donnent des montants différents à la même date. |

Un `not_comparable` ne rend jamais le mois `unknown` à lui seul. Les contrôles
sont recalculés à chaque lecture ; aucun statut de solde n’est enregistré sur
`import_account_sections`. La réponse inclut `input_snapshot_sha256` (SHA-256
d’un encodage canonique des sections et transactions), `version=1` et un ID
dérivé du résultat. Les curseurs de l’API sont signés et invalidés si ce
snapshot change.

## Exemples synthétiques

1. Une section `[2026-01-01, 2026-01-31]`, générée le 1er février, couvre les
   31 jours de janvier. Le mois précédent est `complete`.
2. Une section du 1er au 9 octobre générée le 9 octobre couvre complètement
   les jours 1 à 8. Si l’export suivant commence le 10, le 9 reste manquant. Si
   l’export suivant commence le 9 et a une génération le 10, le jour 9 devient
   complet. Dans les deux cas, octobre entier reste `partial` tant que les
   jours après la génération ne sont pas complets.
3. Solde au 31 décembre : `10 000`; opérations du 1er au 31 janvier : `+600`
   et `+400`; solde au 31 janvier : `11 000`. Le résultat est `match`.
   Un en-tête de `11 001` produit `mismatch`, un delta de `+1` centime et un
   mois `unknown`.
4. Un solde daté le jour de génération est `not_comparable`. Aucun faux
   mismatch n’est créé à partir d’un solde intrajournalier.

## Limites

La couverture repose sur les périodes annoncées par les sections importées ;
elle ne peut pas prouver qu’un export bancaire est lui-même exhaustif. Les
transactions ne sont jamais corrigées automatiquement pour faire coïncider un
solde. Un conflit de soldes le même jour reste non comparable jusqu’à une
résolution humaine hors de ce calcul.

Conformément à ADR-024.2, le recalcul à la lecture est la seule vérité et la
table `balance_checks` n’est pas utilisée pour l’instant. Les statuts ne sont
pas persistés. Pour une éventuelle mémoïsation append-only ultérieure, le schéma
actuel ne comporte pas de champ `reason` ni de champs dédiés aux deux soldes
source (`start_balance_cents`, `end_balance_cents`; les montants restent
récupérables depuis les sections référencées). Ses références de section début
et fin sont obligatoires : il ne peut donc pas représenter un contrôle fondé
sur un seul solde d’en-tête. Aucun changement de schéma n’est inclus ici.
