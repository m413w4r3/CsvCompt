# Allocations et remboursements

Ce document décrit les services de domaine T17. Ils ne créent aucune route HTTP ;
les endpoints de revue relèvent de T21. Les transactions bancaires, leurs dates,
leurs montants et leurs libellés source ne sont jamais réécrits.

## Montants et splits exacts

Tout montant est un entier strict de centimes : débit négatif, crédit positif.
Les conversions en `float` sont interdites.

`app.domain.money_split.split_by_basis_points(amount_cents, parts)` reçoit des
paires `(category_id, basis_points)`. Les poids sont strictement positifs et leur
somme vaut exactement `10000`. Les catégories répétées sont fusionnées. Le calcul
traite les poids du plus grand au plus petit puis par ID croissant ; chaque part
sauf la dernière est arrondie au centime le plus proche, demi loin de zéro, et la
dernière reçoit le reste signé. Une part nulle ou de signe contraire rejette tout
le split.

```python
split_by_basis_points(-10_000, [("groceries", 7_000), ("restaurants", 3_000)])
# {"groceries": -7_000, "restaurants": -3_000}
```

`split_by_amounts(amount_cents, parts)` valide une ventilation signée exacte et
fusionne les catégories répétées. `prorate_link(amount_cents, allocations)` reçoit
un avoir positif et des paires `(category_id, expense_cents)` négatives ; il
agrège les poids par catégorie et retourne une attribution positive qui somme
exactement au lien. Une catégorie peut recevoir zéro centime pour un avoir très
petit.

Vecteurs ADR-007 : `-1101` à `7000/3000` donne `-771/-330` ; `-10001` à
`5000/3000/2000` donne `-5001/-3000/-2000`. `7000/2999` est rejeté avant
toute écriture.

## Allocations confirmées

`app.services.allocations.allocate(engine, transaction_id, parts, *, mode, origin,
actor, expected_version)` écrit sous `write_transaction()` (`BEGIN IMMEDIATE`).
Chaque `AllocationPart` contient `flow_kind`, `category_id`, `amount_cents` et
les dimensions facultatives `beneficiary`, `cadence`, `flexibility` et
`acquisition` (valeur par défaut `unknown`). La matrice T16, la direction de la
catégorie, le signe bancaire et les dimensions sont vérifiés avant l'insertion.
`unknown` est un flux dérivé du résidu ; il n'est jamais une allocation.

- `mode="complete"` exige que la somme signée égale le montant bancaire au
  centime près.
- `mode="partial"` laisse un résidu signé visible et refuse toute
  sur-allocation. `get_allocation_state()` renvoie `unclassified`, `partial` ou
  `complete`, les allocations confirmées, le résidu et les flux dérivés. Tout
  résidu non nul ajoute `unknown`.
- `origin` vaut `manual`, `rule`, `history` ou `llm`. Une règle ne peut classer
  qu'une transaction sans allocation ni décision antérieure. Les origines
  `history` et `llm` ne remplacent pas une allocation effective. Un appel
  `manual` constitue la confirmation humaine explicite d'une proposition.
- Un appel identique ne crée ni ligne ni audit et ne change pas la version.
  Chaque écriture effective vérifie `expected_version` puis avance la version
  optimiste de la transaction. Une version périmée lève
  `VersionConflictError`.
- Une allocation manuelle active résiste à toute demande `rule`, `history` ou
  `llm`, qui lève `HUMAN_DECISION_PROTECTED`. Une correction manuelle crée de
  nouvelles lignes, une `classification_decision` avec avant/après et un
  `audit_event`. L'ancienne allocation reste historisée.

Le remplacement respecte n-11 de la revue G0 : l'ancienne allocation est
marquée `superseded_by_allocation_id` **avant** l'insertion des nouvelles lignes.
Les deux références différées sont validées au commit. Cette séquence libère le
plafond d'allocation du trigger avant que les nouvelles lignes ne soient insérées.
Les données bancaires (`account_id`, date, montant, libellés et identité) restent
inchangées ; seule la version optimiste de la ligne transaction avance pour
servir de jeton de concurrence à la revue.

Les erreurs de validation sont des `AppError` avec des codes stables, notamment
`ALLOCATION_INVALID`, `ALLOCATION_CATEGORY_FLOW_INVALID`,
`ALLOCATION_SIGN_INVALID`, `ALLOCATION_TOTAL_MISMATCH`,
`ALLOCATION_TOTAL_EXCEEDED`, `HUMAN_DECISION_PROTECTED` et
`RULE_REQUIRES_UNCLASSIFIED_TRANSACTION`.

## Remboursements `refund_of`

`create_refund_link()` crée une proposition. La source doit être un crédit avec
une allocation confirmée `refund` / `income_refund` ; la cible doit être un débit
avec une ou plusieurs allocations confirmées `expense`. Les deux transactions
doivent appartenir au même compte. Un `target_allocation_id` cible une allocation
expense active précise ; sans cible, `prorate_link()` distribue l'avoir entre les
catégories expense, par poids absolu et avec l'arrondi ADR-007.

`confirm_refund_link()` revalide les deux jambes et les plafonds sous le verrou
d'écriture. `reject_refund_link()` rejette une proposition ; les liens rejetés,
proposés ou supersédés ne contribuent pas. `replace_refund_link()` remplace un
lien confirmé actif par supersession, version et audit avant/après. Les plafonds
sont contrôlés par le service et les triggers : liens confirmés actifs par source
≤ montant de l'allocation refund, liens par achat ≤ somme absolue de ses
allocations expense, et liens ciblant une allocation ≤ cette allocation.

`net_effect_by_category(engine, purchase_transaction_id, account_ids=...)`
retourne les coûts de catégorie positifs après remboursements confirmés. Si un
périmètre explicite est fourni, les deux jambes doivent y figurer ; sinon l'avoir
reste non attribué dans ce résultat et son montant est signalé dans
`ignored_out_of_scope_refunds_cents`. Le montant et le flux bancaire des deux
transactions ne changent jamais. Exemple : achat `-10000`, avoir confirmé `+2000`
sur la même catégorie → consommation nette `8000`; flux bancaire de la paire
`-8000`.

Les erreurs de lien comprennent `REFUND_LINK_DIRECTION_INVALID`,
`REFUND_SCOPE_MISMATCH`, `REFUND_SOURCE_CAP_EXCEEDED`,
`REFUND_TARGET_CAP_EXCEEDED`, `REFUND_TARGET_ALLOCATION_CAP_EXCEEDED` et
`REFUND_LINK_CONFIRMED_PROTECTED`.

## Limites du schéma v1 à arbitrer

Le schéma n'est pas modifié par T17. Les limites suivantes demandent un arbitrage
et, pour une prise en charge complète, une évolution du schéma :

1. `transaction_links` possède `source_transaction_id` mais aucune colonne
   `source_allocation_id`. Les triggers
   `trg_refund_links_source_cap_insert/update` plafonnent donc le total par
   transaction source. Pour conserver l'identité de chaque allocation source,
   le service accepte au plus une allocation `refund` active par crédit ; les
   crédits ventilés entre `refund` et `income_other` restent pris en charge.
   Plusieurs allocations `refund` distinctes sur le même crédit sont refusées
   avec `REFUND_SOURCE_ALLOCATION_UNSUPPORTED_SCHEMA`.
2. `allocations` exige, par CHECK et trigger, que toute allocation supersédée
   pointe vers une nouvelle allocation non nulle. Il n'existe ni statut annulé
   ni représentation d'une suppression auditable. `clear_allocations()` renvoie
   donc `ALLOCATION_CLEAR_UNSUPPORTED_SCHEMA` si des lignes actives existent ;
   aucune ligne n'est supprimée, mise à zéro ou artificiellement remplacée.
   Une évolution sûre doit ajouter un état d'annulation compatible avec
   l'historique et les métriques.

Le schéma impose aussi qu'un lien confirmé ne soit remplacé que par un autre
lien confirmé (`trg_confirmed_transaction_links_supersede_only` et
`trg_transaction_link_supersession_replacement`). Une proposition peut être
rejetée ; un lien déjà confirmé ne peut pas être retiré à vide ni rejeté en place.
Sa correction passe par un autre lien confirmé versionné.

L'index partiel `uq_transaction_links_active` porte sur les lignes dont
`superseded_by_transaction_link_id IS NULL`, quel que soit leur statut. Un lien
rejeté ne peut pas être supersédé par les triggers ci-dessus ; le même couple
source/cible/type/cible-allocation ne peut donc pas recevoir une nouvelle
proposition. Le service renvoie `REFUND_LINK_ALREADY_EXISTS` au lieu de
contourner l'index.
