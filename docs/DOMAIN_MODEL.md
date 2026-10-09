# Contrat du domaine financier

**Version :** 1 · **Périmètre :** comptes familiaux v1 · **Références :** PLAN §B, §C, §F ; MATRICE MONEY-01..09 et KPI-01..06 ; LLM-13.

Ce document fixe les termes et les conventions utilisés par les allocations et les lectures analytiques. Les montants bancaires et les allocations sont des centimes entiers signés : débit négatif, crédit positif (INV-02). Les catégories sont les identifiants de `categories.v1.yaml`, jamais des libellés libres.

## Glossaire

| Terme | Définition contractuelle |
|---|---|
| **Transaction** | Mouvement réellement inscrit au relevé : compte, date comptable, montant signé en centimes, libellé bancaire brut et provenance d’import. La donnée brute est immuable (INV-01). |
| **Allocation** | Part signée d’une transaction affectée à un `flow_kind` et à une catégorie admissible, avec les dimensions de classification et sa provenance. Une transaction peut porter plusieurs flux confirmés. La somme signée des allocations confirmées ne dépasse pas le montant bancaire ; elle lui est égale quand la transaction est entièrement ventilée (INV-06, ADR-020). |
| **Suggestion** | Proposition non appliquée, conservée avec sa méthode, son motif et son état de revue. Elle n’entre jamais dans les sommes confirmées. |
| **Règle** | Correspondance explicite, limitée par une portée et des exceptions, approuvée par une personne. Une règle modifiée ne réécrit pas les décisions déjà prises. |
| **Décision** | Confirmation, correction ou rejet explicite et audité d’une proposition ou allocation. Une décision humaine confirmée est protégée (INV-08). |
| **Marchand / payee** | Contrepartie normalisée, distincte du libellé bancaire brut. Une ressemblance de libellés ne prouve pas une même destination (INV-13). |
| **Compte** | Identifiant local stable, alias affichable, rôle (courant, épargne, personnel, PEA, etc.) et périodes couvertes. Un numéro bancaire n’est ni sa clé ni une valeur à journaliser. |
| **Import** | Tentative tracée de lecture d’un fichier et de ses sections. Une réimportation peut ajouter des `source_rows` à une transaction existante sans changer transaction, allocations ou décisions (INV-03, INV-08). |
| **source_row** | Occurrence de ligne brute, liée à un import et à une transaction si elle a été reconnue. Plusieurs sources peuvent pointer vers la même transaction. |

## Les deux lectures et le périmètre des comptes

Toute requête porte une période et une liste explicite d’identifiants de comptes. Aucun agrégat ne mélange silencieusement compte courant, épargne, PEA ou comptes personnels (INV-05).

* **Trésorerie** : somme des montants bancaires des transactions des comptes sélectionnés. Une transaction sélectionnée est comptée une seule fois, même si elle a plusieurs allocations/flux confirmés ou un résidu ; les prêts, remboursements, transferts, espèces en attente et régularisations restent visibles. Si deux jambes d’un transfert interne sont sélectionnées, elles figurent toutes deux dans la trésorerie et s’annulent seulement dans la somme signée si leurs montants concordent.
* **Vie familiale** : consommation nette d’achats et de remboursements `refund_of` confirmés, avec le filtre `personal_mode` (`envelope` par défaut ou `observed`). En mode `envelope`, les dépenses des comptes personnels sont exclues et les allocations personnelles du compte commun sont comptées. En mode `observed`, les dépenses des comptes personnels sont incluses et les allocations personnelles exclues ; le reliquat non dépensé est signalé. Ces deux modes ne sont jamais additionnés dans un même total (ADR-022). Revenus, épargne, dettes, capital, espèces et intermédiaires restent des séries distinctes ou des montants en attente, pas de la consommation.

Le choix de comptes s’applique à chaque série (INV-05). Les transactions ne stockent pas de `flow_kind` : le flux est dérivé de leurs allocations confirmées. Chaque allocation confirmée expose son flux ; un résidu non nul apparaît toujours comme `unknown`. La sélection `flow_kind=X` retient les transactions qui ont une allocation confirmée de `X` ; `flow_kind=unknown` retient les transactions au résidu non nul (ADR-020). `savings_net_transferred` somme les allocations confirmées `transfer_savings` du compte courant sélectionné et ne compte donc pas deux fois une paire bancaire ; le flux bancaire brut conserve toutes les jambes sélectionnées.

### Complétude et jour de génération

La génération est datée par le préambule du relevé ou, s’il manque, par la date de réception. Pour chaque compte, `complete_through = min(fin de la période, generated_on − 1 jour)`. Une journée à partir de `generated_on` est `provisional` : ses transactions restent dans les cumuls observés, mais cette journée ne prouve jamais la complétude. Un mois n’est `complete` que si la période couvre le mois calendaire entier, si les fenêtres de relevé/import couvrent sans trou le premier au dernier jour, et si le dernier jour est ≤ `complete_through` pour chaque compte sélectionné. Un contrôle de solde d’en-tête en `mismatch` donne `month_completeness=unknown`, même si les dates des lignes semblent couvrir le mois (ADR-018, INV-10).

## Les 14 types de flux (`flow_kind`)

Le signe décrit le mouvement bancaire et les allocations effectives, non l’effet analytique dérivé d’un lien de remboursement. Le format CA rejette un montant bancaire nul (ADR-019.i). `unknown` est aussi le flux dérivé d’une transaction sans allocation confirmée, ou de son résidu signé non nul ; il ne constitue pas une catégorie à allouer (ADR-020).

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
| `refund` | Crédit de remboursement d’achat ; positif, alloué à `income_refund`. | Inclus, positif. | Réduit la consommation de la catégorie d’origine seulement à hauteur d’un lien `refund_of` confirmé, à la date du crédit ; sinon reste non attribué (ADR-021). |
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

La transaction de remboursement garde un montant bancaire positif et l’allocation positive `refund` / `income_refund`. Un `transaction_link` de type exact `refund_of` relie ce crédit à l’achat et aux allocations d’achat d’origine, avec un montant positif en centimes. Seuls les liens `confirmed` comptent ; `proposed` reste une suggestion. Le montant confirmé ne dépasse ni le crédit reçu ni le solde remboursable de l’achat. L’effet analytique réduit la consommation de la catégorie d’origine à la date du crédit ; il ne réécrit ni l’achat ni le mois antérieur. Les deux transactions doivent appartenir au périmètre de comptes sélectionné ; sinon le lien est ignoré dans cette vue et l’avoir est non attribué avec avertissement. Une consommation mensuelle ou catégorielle négative est valide et signalée comme remboursement d’un achat antérieur (ADR-021, INV-01, INV-06, INV-07).

## États, provenance et décisions

Les dimensions stables de la taxonomie sont orthogonales : `origin ∈ {manual, rule, history, llm, unclassified}` et `decision ∈ {confirmed, review, unknown}`. Un libellé d’état UI est dérivé de ces valeurs et de l’existence d’allocations/suggestions ; il ne remplace pas ces dimensions. `heuristic` n’est pas une valeur d’`origin` : une suggestion heuristique porte `origin=unclassified`, `decision=review` et un champ de méthode `heuristic`.

| État lisible | Représentation contractuelle |
|---|---|
| `unclassified` | Aucun montant confirmé pour tout ou partie du mouvement ; résidu `amount_cents − somme des allocations confirmées`, avec `origin=unclassified`, `decision=unknown` s’il n’y a aucune proposition, ou `decision=review` si une revue est ouverte. |
| `suggested` | Une proposition non appliquée existe ; `decision=review`. Le montant proposé n’est pas une allocation effective et n’entre pas dans les KPI confirmés. |
| `history` / `history_suggested` | Ventilation historique proposée : `origin=history`, `decision=review`. Une correspondance exacte seule ne s’applique pas automatiquement. |
| `llm_suggested` | Suggestion issue du modèle : `origin=llm`, `decision=review`. Elle ne devient jamais règle ni allocation automatiquement. |
| `heuristic_suggested` | Suggestion à méthode `heuristic`, `origin=unclassified`, `decision=review`. |
| `manual_confirmed` | Affectation choisie/corrigée par la personne : `origin=manual`, `decision=confirmed`. |
| `rule_confirmed` | Affectation produite par une règle déterministe approuvée, seulement sur une transaction non déjà décidée : `origin=rule`, `decision=confirmed`. |
| `history_confirmed` | Une proposition de mémoire exacte explicitement acceptée : `origin=history`, `decision=confirmed`, avec l’événement d’acceptation audité. |
| `llm_confirmed` | Proposition LLM acceptée explicitement, sans changement : `origin=llm`, `decision=confirmed`, avec le valideur humain en audit. Si la personne modifie la catégorie, l’origine devient `manual`. |

Le **montant non ventilé** ou **résidu** d’une transaction est `transaction.amount_cents − somme(allocations confirmées signées)`. Il est signé ; une transaction est entièrement ventilée si et seulement si ce résidu vaut zéro. Le flux du résidu est toujours `unknown`, même si les allocations confirmées sont toutes d’un autre flux. Si aucune allocation n’est confirmée, le mouvement entier est donc `unknown`. Le numérateur de la part non ventilée additionne les valeurs absolues des résidus par transaction ; un débit et un crédit opposés ne se compensent jamais. La capacité opérationnelle vaut `null` si un résidu non nul subsiste dans la période/comptes sélectionnés (ADR-020). Une suggestion, même chiffrée et équilibrée, ne diminue jamais ce résidu avant confirmation (INV-06, INV-10). Une transaction à plusieurs allocations confirmées doit respecter le signe, la compatibilité de chacune et `Σ allocations + résidu = montant bancaire`.

### Priorité des sources

Ordre de résolution pour un montant qui n’a pas encore de décision confirmée : **décision humaine confirmée > règle déterministe approuvée > mémoire exacte > heuristique > suggestion LLM**. La priorité est un ordre de confiance/proposition, pas une permission d’écrasement. Une règle approuvée peut classer automatiquement une opération encore vierge. Une mémoire exacte, une heuristique et un LLM alimentent la revue ; elles n’appliquent rien seules. Une proposition de rang supérieur bloque les propositions inférieures contradictoires.

« **Jamais écrasé** » (INV-08) signifie qu’un import/re-import, prompt, nouvelle règle, mémoire, heuristique ou recalcul ne peut ni remplacer, supprimer, rescinder ni modifier une allocation confirmée existante. Une personne peut demander une modification explicite ; elle crée alors une nouvelle version/décision et un événement d’audit avec avant/après. Une règle nouvelle ne s’applique rétroactivement qu’après aperçu explicite, confirmation et chemin réversible (INV-14). Les champs bancaires source restent immuables dans tous les cas (INV-01).

## Contraintes de signe et fractionnement

Le format CA rejette les transactions de montant nul (ADR-019.i). Chaque allocation confirmée est non nulle et conserve le signe de la transaction : débit → allocations négatives ; crédit → allocations positives. Les catégories directionnelles imposent aussi leur signe : `flow_savings_out` et `flow_loan_principal` sont négatives, `flow_savings_in`, `flow_loan_disbursement` et les `income_*` positives. Un `refund` est le seul cas où un lien autorise un effet analytique de compensation sur une dépense antérieure ; il ne change pas le signe positif de la transaction ni de son allocation bancaire `income_refund` (INV-02, ADR-019.i).

Un split en pourcentage exige des entiers de base 100 (centièmes de pourcent, total exactement `10000` basis points, aucun poids nul/négatif) et un montant source non nul. Les entrées répétées pour une même catégorie sont fusionnées en additionnant leurs poids, avant le calcul. Les catégories fusionnées sont triées par poids décroissant puis `category_id` croissant ; l’ordre de saisie n’influe donc ni les centimes ni leur rendu par catégorie. Chaque part sauf la dernière est calculée sur le montant source, arrondie au centime le plus proche avec les demis loin de zéro ; la dernière catégorie reçoit le reste signé exact. Le split entier est rejeté atomiquement si une part calculée vaut zéro ou a un signe contraire à la transaction. Pour 2 parts, la plus grande reçoit l’arrondi et la dernière le reste. Si un pourcentage manque ou si la somme diffère de 10000, aucun split applicable n’est créé (LLM-13, MONEY-02, m-02). Toute allocation finale est ensuite confirmée et sa somme est revalidée.

Exemples exacts (les nombres sont des centimes) :

| Montant | Parts données | Calcul et résultat signé |
|---:|---|---|
| `-10000` | `7000 / 3000` bps | `-7000 / -3000` |
| `-1101` | `7000 / 3000` bps | `-771 / -330` (−11,01 € → −7,71 € / −3,30 € ; LLM-13) |
| `-10001` | `5000 / 3000 / 2000` bps | `-5001 / -3000 / -2000` |

`7000 / 2999` bps totalise `9999` et est rejeté ; aucun centime ne peut être comblé par arrondi (MONEY-02). Les débits et crédits fractionnés gardent le signe de la transaction.

Cas de rejet après arrondi : `-1` centime à `7000 / 3000` produit une part nulle ; le split entier est rejeté. `-2` centimes en quatre parts `2500 / 2500 / 2500 / 2500` produit provisoirement `-1 / -1 / -1 / +1` avec la règle du reste ; la dernière part a le mauvais signe et le split entier est rejeté (m-02).

## Invariants couverts

INV-02 centimes signés exacts ; INV-05 compte/périmètre explicite ; INV-06 allocation sans résidu ; INV-07 flux distincts ; INV-08 décision humaine protégée ; INV-09 chiffres explicables ; INV-10 incertitude et non-ventilé visibles ; INV-13 aucune inférence catégorielle vague. Les exemples chiffrés exécutables associés sont dans `docs/examples/metrics_examples.yaml`.
