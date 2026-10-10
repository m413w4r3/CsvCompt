# API de lecture des opérations (T15)

## Liste

`GET /api/v1/transactions` renvoie une page de lignes brutes, un total et un
résumé calculé sur l'ensemble du filtre. Les filtres acceptés sont les
`account_ids` répétables, `period_start`, `period_end`, `direction` (`debit` ou
`credit`), `amount_min_cents` et `amount_max_cents` (valeurs absolues), `q`,
`import_file_id`, `classification` (`all` ou `unclassified`), `flow_kind`,
`cursor` et `limit` (50 par défaut, 100 maximum). Les bornes de date sont
inclusives. Les bornes de montant vont de zéro à `2^53 - 1` centimes; cette
limite garde également les filtres signés dans une représentation exacte côté
client. Une recherche de plus de 200 caractères, un enum inconnu, une plage
inversée ou une limite invalide reçoit l'enveloppe d'erreur contractuelle.

L'ordre est toujours `booked_on DESC, transaction_id DESC`. Le curseur HMAC lie
les filtres canoniques, la dernière paire, un identifiant opaque de snapshot et
le `snapshot_at` UTC. Le snapshot est une lecture SQLite WAL épinglée à la
visibilité observée lors de la première page; une écriture non committée est
invisible sur toutes ses pages, même si elle est committée entre deux appels.
Le snapshot reste en mémoire au plus cinq minutes et jusqu'à 32 navigations
inachevées par processus; un redémarrage ou son expiration répond
`CURSOR_STALE` et impose de relancer la liste. La valeur de recherche `q` n'est
jamais sérialisée en clair dans le curseur. Un curseur mal formé ou modifié
répond `CURSOR_INVALID` (422).

```json
{
  "items": [{
    "id": "550e8400-e29b-41d4-a716-446655440020",
    "account_id": "550e8400-e29b-41d4-a716-446655440012",
    "account_alias": "Compte synthétique",
    "booked_on": "2026-03-12",
    "amount_cents": -1234,
    "raw_label_preview": "LIBELLE SYNTHETIQUE",
    "occurrence_rank": 1,
    "import_count": 2,
    "classification_state": "unclassified",
    "residual_cents": -1234
  }],
  "next_cursor": null,
  "total_count": 1,
  "summary": {
    "count": 1,
    "debit_total_cents": -1234,
    "credit_total_cents": 0,
    "net_cents": -1234,
    "total_out_of_range": false
  },
  "snapshot_at": "2026-10-10T12:00:00.000Z"
}
```

`summary` et `total_count` couvrent tout le filtre et non la page. Le total est
exact, jamais estimé. Les sommes sont des additions d'entiers en centimes. Si
un total dépasse `2^53 - 1`, les totaux monétaires du résumé valent `null` et
`total_out_of_range` vaut `true`; aucun entier monétaire non sûr n'atteint le
client. La même règle s'applique aux champs de centimes des lignes, détails,
résidus, allocations et contrôles de solde : la valeur non sûre devient `null`
et l'objet concerné porte l'indicateur. La prévisualisation
du libellé est coupée à 200 caractères; le serveur ne renvoie ni identité,
empreinte, numéro de compte, chemin d'archive ni libellé dans les erreurs.

`q` est borné à 200 caractères. Le serveur normalise d'abord la requête et
chaque libellé avec `search_label` : casefold, décomposition Unicode NFKD,
suppression des marques d'accent, puis ponctuations, caractères de contrôle et
espaces ramenés à un séparateur espace unique, avec suppression des espaces
aux extrémités. La recherche est une sous-chaîne de cette forme canonique.
Ainsi `%`, `_` et les guillemets n'ont pas de sens spécial de motif SQL et ne
restent pas littéraux : ils sont traités comme des séparateurs (`operation%`
est équivalent à `operation`). Une requête composée uniquement de séparateurs
se normalise en chaîne vide et n'ajoute donc aucun filtre `q`. Les lettres
Unicode sont recherchées après casefold et suppression des marques d'accent.
Les libellés recherchés ne devraient pas être copiés dans l'URL.

`flow_kind` est calculé à partir des allocations actives; `unknown` sélectionne
un résidu signé non nul. Sans ventilation, le résidu est le montant entier et
l'état reste `unclassified`. `classification=unclassified` sélectionne les
opérations sans allocation active.

## Détail

`GET /api/v1/transactions/{transaction_id}` renvoie le libellé brut complet,
`identity_label`, le rang d'occurrence, l'état, le résidu, les allocations
actives et les sources `{import_file_id, imported_at, section, logical_row_idx}`.
Une opération inconnue répond `404 NOT_FOUND`. Le client doit traiter toutes
les chaînes comme du texte, sans interprétation HTML.

## Index et coût

La lecture utilise l'index existant `ix_transactions_account_booked` pour le
parcours du compte et l'ordre date, ainsi que `ix_source_rows_transaction` et
`ix_allocations_active_transaction` pour les provenances et ventilations.
`total_count` et le résumé sont exacts; SQLite les calcule en agrégats sur le
filtre complet avant de charger uniquement la page demandée. Le filtre `q`
normalise les libellés à la lecture parce que le schéma v1 n'a pas de colonne de
recherche dédiée; il doit donc examiner les lignes du filtre. Le plan de requête
principal est couvert par un test `EXPLAIN QUERY PLAN` sur le chemin compte/date.
Une mesure de latence sur 100 000 opérations reste à produire.

Les erreurs ont la forme `{"error":{"code":"…","message":"…","details":{}}}`.
Les routes sont en lecture seule et ne créent pas de clé de pagination.
