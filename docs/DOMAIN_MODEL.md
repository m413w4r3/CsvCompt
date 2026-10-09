# Contrat du domaine financier

**Version :** 1 · **Périmètre :** comptes familiaux v1 · **Références :** PLAN §B, §C, §F ; MATRICE MONEY-01..09 et KPI-01..06 ; LLM-13.

Ce document fixe les termes et les conventions utilisés par les allocations et les lectures analytiques. Les montants bancaires et les allocations sont des centimes entiers signés : débit négatif, crédit positif (INV-02). Les catégories sont les identifiants de `categories.v1.yaml`, jamais des libellés libres.

## Glossaire

| Terme | Définition contractuelle |
|---|---|
| **Transaction** | Mouvement réellement inscrit au relevé : compte, date comptable, montant signé en centimes, libellé bancaire brut et provenance d’import. La donnée brute est immuable (INV-01). |
| **Allocation** | Part signée d’une transaction affectée à un `flow_kind` et à une catégorie admissible, avec les dimensions de classification et sa provenance. La somme des allocations confirmées d’une transaction entièrement classée est exactement le montant bancaire signé (INV-06). |
| **Suggestion** | Proposition non appliquée, conservée avec sa méthode, son motif et son état de revue. Elle n’entre jamais dans les sommes confirmées. |
| **Règle** | Correspondance explicite, limitée par une portée et des exceptions, approuvée par une personne. Une règle modifiée ne réécrit pas les décisions déjà prises. |
| **Décision** | Confirmation, correction ou rejet explicite et audité d’une proposition ou allocation. Une décision humaine confirmée est protégée (INV-08). |
| **Marchand / payee** | Contrepartie normalisée, distincte du libellé bancaire brut. Une ressemblance de libellés ne prouve pas une même destination (INV-13). |
| **Compte** | Identifiant local stable, alias affichable, rôle (courant, épargne, personnel, PEA, etc.) et périodes couvertes. Un numéro bancaire n’est ni sa clé ni une valeur à journaliser. |
| **Import** | Tentative tracée de lecture d’un fichier et de ses sections. Une réimportation peut ajouter des `source_rows` à une transaction existante sans changer transaction, allocations ou décisions (INV-03, INV-08). |
| **source_row** | Occurrence de ligne brute, liée à un import et à une transaction si elle a été reconnue. Plusieurs sources peuvent pointer vers la même transaction. |

## Les deux lectures et le périmètre des comptes

Toute requête porte une période et une liste explicite d’identifiants de comptes. Aucun agrégat ne mélange silencieusement compte courant, épargne, PEA ou comptes personnels (INV-05).

* **Trésorerie** : somme des montants bancaires des transactions des comptes sélectionnés. Chaque transaction sélectionnée est comptée une fois, quel que soit son `flow_kind`; les prêts, remboursements, transferts, espèces en attente et régularisations restent visibles. Si deux jambes d’un transfert interne sont sélectionnées, elles figurent toutes deux dans la trésorerie et s’annulent seulement dans la somme signée si leurs montants concordent.
* **Vie familiale** : dépenses de consommation nettes des remboursements appariés, plus éventuellement les allocations personnelles pour mesurer l’enveloppe sortie du compte commun. Les allocations personnelles ne sont pas une consommation finale observée. Si les opérations du compte personnel sont aussi importées, on choisit soit l’enveloppe, soit les dépenses observées sur ce compte pour la même période ; on ne compte jamais les deux. Revenus, épargne, dettes, capital, espèces et intermédiaires restent des séries distinctes ou des montants en attente, pas de la consommation.

Le choix de comptes s’applique à chaque série. Le calcul `savings_net_transferred` requiert le compte courant source sélectionné et un compte d’épargne identifié ; les jambes appariées ne sont comptées qu’une fois dans cette mesure. Le flux bancaire brut conserve toutes les jambes sélectionnées.

## Les 14 types de flux (`flow_kind`)

Le signe décrit le mouvement bancaire et les allocations effectives, non l’effet analytique dérivé d’un lien de remboursement. Un montant nul peut rester `unknown` ou `reconciliation`, mais ne constitue pas un flux positif/négatif confirmé.

| `flow_kind` | Définition et signe bancaire attendu | Trésorerie | Vie familiale |
|---|---|---|---|
| `expense` | Achat ou charge de consommation ; négatif. | Inclus, négatif. | Consommation ; contribue au net. |
| `income_recurring` | Salaire ou revenu régulier confirmé ; positif. | Inclus, positif. | Revenu régulier séparé de la consommation. |
| `income_other` | Ressource exceptionnelle/autre non récurrente ; positif. | Inclus, positif. | Revenu distinct ; jamais revenu régulier. |
| `transfer_savings` | Virement depuis le courant vers épargne : négatif ; retour : positif. Le compte de destination/source doit être identifié. | Inclus par jambe de compte. | Ni consommation ni revenu ; mesure d’épargne dédiée. |
| `transfer_owned` | Transfert entre comptes appartenant au foyer ; signe selon la jambe. | Inclus par compte sélectionné. | Ni consommation ni revenu ; reste traçable. |
| `allocation_personal` | Enveloppe du compte commun vers un compte personnel ; négatif. | Inclus, négatif. | Exclus de la consommation ; inclus en option comme allocation personnelle, sans double compte du compte destinataire. |
| `loan_disbursement` | Déblocage d’emprunt ; positif. | Inclus, positif. | Ni revenu régulier ni consommation. |
| `loan_repayment` | Échéance/remboursement de prêt ; négatif. Si l’échéance n’est pas documentée, ne pas inventer la part capital/intérêts. | Inclus, négatif, même non ventilé. | Pas une consommation courante ; affiché comme engagement/dette et exclu du coût total du crédit tant que non scindé. |
| `capital_event` | Dépense d’investissement/travaux exceptionnels v1 ; négatif. Une ressource de vente n’est pas forcée dans ce flux. | Inclus, négatif. | Hors consommation courante ; série exceptionnelle séparée. |
| `cash_withdrawal_pending` | Retrait dont la destination n’est pas documentée ; négatif. | Inclus, négatif. | En attente ; aucune présomption « ménage », aucune consommation imputée. |
| `refund` | Crédit de remboursement d’achat ; positif. | Inclus, positif. | Réduit la consommation seulement à hauteur d’un lien validé à l’achat ; sinon reste non attribué. |
| `intermediary_pending` | Débit envers un intermédiaire dont le détail d’achat manque ; négatif. | Inclus, négatif. | En attente, sans catégorie de consommation supposée. |
| `reconciliation` | Correction/régularisation bancaire ; signe selon le relevé. | Inclus dans le brut bancaire. | Exclu des revenus et de la consommation ; visible séparément. |
| `unknown` | Flux non déterminé ; signe quelconque du relevé. | Inclus dans le brut bancaire. | Hors calcul de consommation jusqu’à revue ; montant non classé signalé. |

### Compatibilité exhaustive `flow_kind` ↔ identifiants de catégories

Une catégorie par allocation doit être dans la cellule de son flux. Une liste vide signifie qu’aucune allocation catégorisée n’est autorisée : la transaction reste sans classification et visible en revue. Les catégories parentes comme leurs enfants sont des IDs admissibles. Cette matrice couvre tous les IDs de `categories.v1.yaml`.

| `flow_kind` | IDs de catégories admissibles |
|---|---|
| `expense` | `groceries`, `groceries_supermarket`, `groceries_organic`, `groceries_frozen`, `groceries_bakery`, `restaurants`, `restaurants_dining`, `restaurants_delivery`, `restaurants_fastfood`, `housing`, `housing_energy`, `housing_water`, `housing_copro`, `housing_tax`, `housing_insurance`, `housing_maintenance`, `home`, `home_furniture`, `home_appliances`, `home_diy`, `home_supplies`, `children`, `children_nanny`, `children_daycare`, `children_baby`, `children_school`, `children_activities`, `clothing`, `clothing_children`, `clothing_adults`, `clothing_footwear`, `clothing_unspecified`, `health`, `health_visits`, `health_pharmacy`, `health_optical`, `health_other`, `transport`, `transport_fuel`, `transport_tolls`, `transport_public`, `transport_vehicle`, `transport_taxis`, `household_services`, `household_cleaning`, `household_help`, `household_oneoff`, `subscriptions`, `subscriptions_telecom`, `subscriptions_streaming`, `subscriptions_apps`, `subscriptions_other`, `leisure`, `leisure_sports`, `leisure_culture`, `leisure_travel`, `leisure_family`, `pets`, `pets_vet`, `pets_food`, `pets_care`, `admin`, `admin_bank`, `admin_insurance`, `admin_tax`, `admin_other`, `gifts`, `gifts_present`, `gifts_donation` |
| `income_recurring` | `income_salary_a`, `income_salary_b`, `income_benefits`, `income_regular_other` |
| `income_other` | `income_benefits`, `income_extraordinary` |
| `transfer_savings` | `flow_savings_out`, `flow_savings_in` |
| `transfer_owned` | `flow_owned_transfer` |
| `allocation_personal` | `flow_personal_a`, `flow_personal_b` |
| `loan_disbursement` | `flow_loan_disbursement` |
| `loan_repayment` | `flow_loan_principal`, `flow_loan_payment_unknown_split` |
| `capital_event` | `flow_capital_improvement` |
| `cash_withdrawal_pending` | `flow_cash_unallocated` |
| `refund` | `income_refund` |
| `intermediary_pending` | `flow_intermediary_unresolved` |
| `reconciliation` | `flow_reconciliation` |
| `unknown` | aucune |

`income_benefits` peut être récurrent (`income_recurring`) ou non récurrent (`income_other`) selon la cadence vérifiée. Les salaires ne sont admissibles que comme `income_recurring`; `income_extraordinary` ne peut être que `income_other`. Les dimensions transversales `cadence`, `beneficiary`, `flexibility`, `acquisition`, `decision` et `origin` ne changent pas le `flow_kind`.

Un paiement de prêt non ventilé reste entièrement `loan_repayment` / `flow_loan_payment_unknown_split`. Le principal documenté peut être `flow_loan_principal`. La taxonomie n’a pas d’ID d’intérêt de prêt distinct : un intérêt isolé ne doit pas être forcé dans `admin_other` ni déclaré consommation ; son traitement détaillé attend un arbitrage taxonomique explicite.

## Remboursement et lien d’achat

La transaction de remboursement garde un montant bancaire positif et l’allocation positive `refund` / `income_refund`. Un `transaction_link` validé relie ce crédit à la transaction et aux allocations d’achat d’origine, avec un montant de lien positif en centimes. Un lien partiel réduit la dépense d’autant ; le total apparié ne peut dépasser ni le crédit reçu ni le solde remboursable de l’achat. L’effet analytique est une réduction de consommation dans la catégorie d’origine ; il ne réécrit ni l’achat ni le montant bancaire du remboursement. La partie non appariée reste en `income_refund`, non attribuée : elle n’augmente pas `regular_income` et ne réduit pas `consumption_net` (INV-01, INV-06, INV-07).

## États, provenance et décisions

Les dimensions stables de la taxonomie sont orthogonales : `origin ∈ {manual, rule, history, llm, unclassified}` et `decision ∈ {confirmed, review, unknown}`. Un libellé d’état UI est dérivé de ces valeurs et de l’existence d’allocations/suggestions ; il ne remplace pas ces dimensions.

| État lisible | Représentation contractuelle |
|---|---|
| `unclassified` | Aucune allocation confirmée pour tout ou partie du montant ; `origin=unclassified`, `decision=unknown` sans proposition, ou `decision=review` avec une revue ouverte. |
| `suggested` | Une proposition non appliquée existe ; `decision=review`. Le montant proposé n’est pas une allocation effective et n’entre pas dans les KPI confirmés. |
| `llm_suggested` | Suggestion issue du modèle : `origin=llm`, `decision=review`. Elle ne devient jamais règle ni allocation automatiquement. |
| `heuristic_suggested` | Suggestion à méthode `heuristic`, `decision=review`; faute de valeur `heuristic` dans `origin`, l’allocation effective reste `origin=unclassified` jusqu’à décision. |
| `history_suggested` | Proposition provenant d’une ventilation confirmée antérieure : `origin=history`, `decision=review`. Une correspondance exacte seule ne s’applique pas automatiquement. |
| `manual_confirmed` | Affectation choisie/corrigée par la personne : `origin=manual`, `decision=confirmed`. |
| `rule_confirmed` | Affectation produite par une règle déterministe approuvée, seulement sur une transaction non déjà décidée : `origin=rule`, `decision=confirmed`. |
| `history_confirmed` | Une proposition de mémoire exacte explicitement acceptée : `origin=history`, `decision=confirmed`, avec l’événement d’acceptation audité. |
| `llm_confirmed` | Proposition LLM acceptée explicitement, sans changement : `origin=llm`, `decision=confirmed`, avec le valideur humain en audit. Si la personne modifie la catégorie, l’origine devient `manual`. |

Le **montant non ventilé** d’une transaction est `transaction.amount_cents − somme(allocations confirmées signées)`. Il est signé ; une transaction est entièrement ventilée si et seulement si ce résidu vaut zéro. Une suggestion, même chiffrée et équilibrée, ne diminue jamais ce résidu avant confirmation (INV-06, INV-10). Une transaction à plusieurs allocations confirmées doit aussi respecter la compatibilité de chacune et l’égalité exacte des sommes.

### Priorité des sources

Ordre de résolution pour un montant qui n’a pas encore de décision confirmée : **décision humaine confirmée > règle déterministe approuvée > mémoire exacte > heuristique > suggestion LLM**. La priorité est un ordre de confiance/proposition, pas une permission d’écrasement. Une règle approuvée peut classer automatiquement une opération encore vierge. Une mémoire exacte, une heuristique et un LLM alimentent la revue ; elles n’appliquent rien seules. Une proposition de rang supérieur bloque les propositions inférieures contradictoires.

« **Jamais écrasé** » (INV-08) signifie qu’un import/re-import, prompt, nouvelle règle, mémoire, heuristique ou recalcul ne peut ni remplacer, supprimer, rescinder ni modifier une allocation confirmée existante. Une personne peut demander une modification explicite ; elle crée alors une nouvelle version/décision et un événement d’audit avec avant/après. Une règle nouvelle ne s’applique rétroactivement qu’après aperçu explicite, confirmation et chemin réversible (INV-14). Les champs bancaires source restent immuables dans tous les cas (INV-01).

## Contraintes de signe et fractionnement

Chaque allocation confirmée conserve le signe de la transaction : débit → allocations négatives ; crédit → allocations positives. Un `refund` est le seul cas où un lien autorise un effet de compensation sur une dépense antérieure ; il ne change pas le signe de la transaction ni de son allocation bancaire `income_refund`.

Un split en pourcentage exige des entiers de base 100 (centièmes de pourcent, total exactement `10000` basis points, aucun poids nul/négatif). On trie les parts par pourcentage décroissant, puis par `category_id` croissant pour lever les égalités. Chaque part sauf la dernière est calculée sur le montant total, arrondi au centime le plus proche avec les demis loin de zéro ; la dernière reçoit le reste signé exact. Pour 2 parts, la plus grande est donc arrondie et la plus petite reçoit le reste. Si un pourcentage manque ou si la somme diffère de 10000, aucun split applicable n’est créé (LLM-13, MONEY-02). Toute allocation finale est ensuite confirmée et sa somme est revalidée.

Exemples exacts (les nombres sont des centimes) :

| Montant | Parts données | Calcul et résultat signé |
|---:|---|---|
| `-10000` | `7000 / 3000` bps | `-7000 / -3000` |
| `-1101` | `7000 / 3000` bps | `-771 / -330` (−11,01 € → −7,71 € / −3,30 € ; LLM-13) |
| `-10001` | `5000 / 3000 / 2000` bps | `-5001 / -3000 / -2000` |

`7000 / 2999` bps totalise `9999` et est rejeté ; aucun centime ne peut être comblé par arrondi (MONEY-02). Les débits et crédits fractionnés gardent le signe de la transaction.

## Invariants couverts

INV-02 centimes signés exacts ; INV-05 compte/périmètre explicite ; INV-06 allocation sans résidu ; INV-07 flux distincts ; INV-08 décision humaine protégée ; INV-09 chiffres explicables ; INV-10 incertitude et non-ventilé visibles ; INV-13 aucune inférence catégorielle vague. Les exemples chiffrés exécutables associés sont dans `docs/examples/metrics_examples.yaml`.
