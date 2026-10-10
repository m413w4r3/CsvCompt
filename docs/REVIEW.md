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
et relie cette décision à la décision annulée. Les instantanés et événements
créés par T20 ne copient ni le libellé bancaire, ni le libellé normalisé, ni le
motif de règle, ni un extrait de transaction. Les événements de création de
règle hérités de T19 peuvent encore conserver un motif littéral dans leur
`before_json`/`after_json`; ce risque préexistant est consigné dans
`docs/revues/REVUE_T19.md` et reste à traiter dans le périmètre approprié.

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
