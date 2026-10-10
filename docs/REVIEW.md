# Contrat T20 — journal et annulation

Le service `app.services.review` conserve les décisions de classification et
permet de les annuler sans modifier la transaction bancaire. Les routes REST
de revue restent dans T21 ; T20 expose des fonctions Python typées et des
erreurs `AppError` contractuelles.

## Journal

Chaque décision d'allocation conserve son acteur, son action, son horodatage
UTC, et les allocations complètes avant et après : flux, catégorie, centimes
signés, provenance et dimensions de revue. Les décisions et événements
d'audit sont append-only ; une annulation crée une nouvelle décision `undo`
et relie cette décision à la décision annulée. Les snapshots de règle écrits
après le correctif T20 ne contiennent que l'identifiant et la version de règle,
la longueur et l'empreinte SHA-256 du motif, ainsi que les métadonnées métier
nécessaires ; les motifs et raisons libres d'exception y sont aussi résumés par
longueur et empreinte, sans texte brut. Les anciennes traces T19 ne sont pas
réécrites ; elles peuvent encore contenir des motifs dans leur audit historique.

La migration 0004 ajoute une empreinte SHA-256 canonique pour chacun des deux
snapshots de décision écrits par l'application. Avant une restitution, le
service revérifie les empreintes, rapproche `before` de l'état `after` de la
décision auditée précédente lorsqu'il est contigu, puis vérifie que parts
restaurées et résidu couvrent le montant signé de l'opération. Les décisions
historiques gardent des empreintes nulles : elles ne sont annulables que si
`before` concorde avec la décision auditée précédente ; sans précédent
concordant, le service renvoie `UNDO_SNAPSHOT_INVALID` sans écriture. Les
snapshots d'une exécution de règle sont aussi rapprochés de ceux de sa décision
source ; une trace historique sans décision source est refusée sûrement.

Une application de règle retourne un `batch_id` UUIDv4. Le même ID regroupe
ses exécutions, la version approuvée de la règle, l'empreinte de la portée,
l'identifiant de la décision créée et la version de transaction après
application. Les nouveaux lots sont réversibles ensemble dans une seule
transaction SQLite. Une application interrompue peut être rejouée avec le
même `batch_id`; un ID déjà associé à une autre règle, version ou portée est
refusé. Une application de règle historique antérieure à T20 reçoit un lot
singleton lors de la migration, car aucune donnée fiable ne permet de
reconstituer son ancien regroupement.

## Contrats de service

- `undo_decision(engine, decision_id, *, expected_version, actor,
  confirm_human_override=False)` annule une décision manuelle de ventilation.
- `undo_rule_application(engine, batch_id, *, expected_versions, actor,
  confirm_human_override=False)` annule toutes les exécutions appliquées du
  lot atomiquement. `expected_versions` doit contenir la version courante de
  chaque transaction appliquée.
- Les deux méthodes renvoient le nouvel état d'allocation et l'ID de la
  décision d'annulation. Un rejeu après succès est sans effet et indique
  `idempotent=true`.
- L'annulation compare les allocations actives à l'instantané attendu et
  vérifie les versions sous `BEGIN IMMEDIATE`. Un état divergent renvoie une
  erreur typée sans écriture. Si une décision humaine postérieure est
  détectée, l'appel exige `confirm_human_override=True`; sans confirmation,
  aucune allocation n'est remplacée. Les liens de remboursement actifs
  bloquent toujours une annulation.
- Les anciennes allocations supersédées ou retirées ne sont jamais
  réactivées ni supprimées. L'annulation crée de nouvelles lignes avec les
  mêmes dimensions et les mêmes centimes entiers ; si l'état antérieur était
  non ventilé, les allocations courantes sont retirées par la décision
  d'annulation.
- Une décision `undo` ne peut pas être annulée à nouveau par ce service. Une
  correction ultérieure doit être enregistrée comme une nouvelle décision
  humaine.
- L'annulation en chaîne d'une décision antérieure n'est permise que si son
  `after` correspond à l'état courant et que sa version enregistrée est la
  version courante. Une annulation précédente avance la version : l'annulation
  de la décision plus ancienne échoue alors avec `UNDO_STATE_DIVERGED`, même si
  son contenu `after` a été restauré. Il faut enregistrer une nouvelle décision
  humaine pour corriger cet état ; l'ordre inverse n'est pas rejoué
  automatiquement.

## Erreurs contractuelles

| Code | HTTP | Signification |
|---|---:|---|
| `DECISION_NOT_FOUND` | 404 | Décision inconnue. |
| `RULE_BATCH_NOT_FOUND` | 404 | Lot de règle inconnu. |
| `RULE_BATCH_CONFLICT` | 409 | `batch_id` est déjà lié à une autre règle, version, acteur ou portée. |
| `RULE_BATCH_ALREADY_UNDONE` | 409 | Un lot annulé ne peut pas être appliqué de nouveau avec le même ID. |
| `VERSION_CONFLICT` | 409 | Version attendue obsolète. |
| `UNDO_STATE_DIVERGED` | 409 | L'état courant ne correspond plus à l'instantané annulable. |
| `UNDO_HUMAN_DECISION_CONFIRMATION_REQUIRED` | 409 | Une décision humaine postérieure exige une confirmation explicite. |
| `UNDO_REFUND_LINK_ACTIVE` | 409 | Un lien de remboursement actif référence la transaction. |
| `RULE_BATCH_NOT_REVERSIBLE` | 409 | Le lot ne contient aucune exécution appliquée. |
| `RULE_BATCH_UNDO_INCOMPLETE` | 409 | Le lot est partiellement annulé ou ses traces sont incohérentes. |
| `RULE_BATCH_UNDO_REQUIRED` | 409 | Une décision issue d'une règle doit être annulée par son lot. |
| `UNDO_ACTION_NOT_SUPPORTED` | 409 | Une décision `undo` ne peut pas être annulée à son tour. |
| `UNDO_SNAPSHOT_INVALID` | 500 | Un instantané stocké est incomplet ou invalide. |
| `REVIEW_INVALID` | 422 | Acteur, identifiant, confirmation ou version mal formés. |
