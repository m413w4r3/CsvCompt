# Contrat des métriques financières

**Version :** 1 · **Source :** PLAN §C3, §D ; `DOMAIN_MODEL.md`. Les identifiants ci-dessous sont des contrats stables : un changement de formule ou de périmètre exige un nouvel arbitrage et une version d’ID.

## Conventions de calcul

* Argent en centimes entiers signés : débit négatif, crédit positif. Aucune conversion monétaire via `float`; la présentation en euros est faite après calcul. Zéro connu et absence de donnée sont distincts.
* Seules les allocations `decision=confirmed` sont effectives. Une transaction partiellement ventilée a un résidu signé `amount_cents − somme des allocations confirmées`. Les suggestions ne sont jamais additionnées.
* Toutes les métriques utilisent le même filtre explicite de dates, comptes, catégories/flux et mode. Les transferts internes n’entrent pas dans la consommation. Le brut bancaire conserve les écritures sélectionnées.
* Une réponse donne le montant brut en `raw_cents` (integer ou `null`) et l’état de couverture. Les ratios sont des **basis points entiers** (1 bp = 0,01 point de pourcentage), arrondis au plus proche, demi vers l’extérieur/loin de zéro. Formule pour numérateur signé `n` et dénominateur positif `d` : `sign(n) × floor((2 × abs(n) × scale + d) / (2 × d))`. Aucun `float` ni `Decimal` flottant n’est requis.
* Pour les ratios de montants, un montant est « pertinent » s’il appartient à `expense`, `income_recurring`, `income_other`, `allocation_personal`, `refund`, `cash_withdrawal_pending`, `intermediary_pending` ou `unknown`. Les flux épargne/propre, prêt, capital et régularisation connus ne sont pas un dénominateur de qualité de consommation. Cette liste de qualité est fixe dans la v1 ; le choix de compte reste explicite.

## Définitions canoniques

| ID stable | Formule exacte en centimes | Signes, périmètre et dénominateur | Validité, cas limites et affichage |
|---|---|---|---|
| `bank_net_flow.v1` | `Σ transaction.amount_cents` | Tous les `flow_kind` et toutes les transactions sélectionnées ; négatif = sortie, positif = entrée. Égal à crédits − débits absolus (KPI-01). | `null` si la sélection/date n’a pas de couverture transactionnelle connue ; `0` si couverture connue et aucune écriture. Inclut prêt, capital, espèces, remboursement non apparié et régularisation. |
| `regular_income.v1` | `Σ allocation.amount_cents` où `flow_kind=income_recurring`, catégorie admissible et allocation confirmée | Crédits positifs uniquement ; exclut `income_other`, `income_refund` et `loan_disbursement`. Pas de dénominateur. | `null` si couverture transactionnelle inconnue ; zéro valide pour une période couverte sans revenu régulier. Un prêt de 50 000 € contribue zéro (MONEY-04/KPI-02). Un résidu non classé est signalé à côté de la somme. |
| `consumption_net.v1` | `−(Σ allocations confirmées `expense` + Σ montants positifs des liens `refund` validés)` | Une dépense négative augmente le coût ; le remboursement apparié positif le réduit. Un lien partiel ne porte que sur son montant. Le résidu non apparié du crédit est exclu. | `null` si couverture transactionnelle inconnue ; somme des parties confirmées sinon, avec `coverage`/`incomplete` si résidu pertinent existe. Le brut bancaire ne change pas. Exemple achat −100,00 € + remboursement apparié +20,00 € = 80,00 € nets (MONEY-03). |
| `personal_allowances.v1` | `−Σ allocation.amount_cents` où `flow_kind=allocation_personal`, confirmé | Les allocations personnelles étant négatives, résultat positif. Comptes du foyer explicitement sélectionnés ; la mesure est une enveloppe, non consommation finale. | `null` si couverture inconnue ; zéro si aucune enveloppe dans une période couverte. Si le compte personnel destinataire est inclus, exclure la jambe d’enveloppe déjà représentée par ses dépenses observées. |
| `savings_net_transferred.v1` | `−Σ transaction.amount_cents` pour les jambes `transfer_savings` du compte courant sélectionné liées à une épargne identifiée | Débit de transfert −20000 et retour +5000 donnent +15000. Une paire importée sur les deux comptes n’est comptée qu’une fois, côté courant (MONEY-06). | `null` si couverture du compte courant inconnue ; zéro si période couverte sans transfert épargne identifié. Transferts entre comptes propres non identifiés comme épargne exclus. |
| `operational_saving_capacity.v1` | `regular_income − consumption_net − personal_allowances − selected_commitments_cents` | Les quatre termes sont des montants positifs hors le dernier : engagements sélectionnés = valeur absolue des sorties confirmées choisies, disjointes de consommation et enveloppes. Par défaut `selected_commitments_cents=0`. Ce n’est pas un changement de solde bancaire. | Valeur calculable si couverture et classification des flux pertinents sont complètes ; sinon `null` pour éviter de présenter une capacité gonflée comme disponible. Une échéance `loan_repayment` non scindée reste visible dans trésorerie et engagements, mais n’est soustraite de cette capacité que si choisie explicitement ; elle est alors déduite entière et signalée « capital/intérêts inconnus ». Espèces/intermédiaires en attente restent affichés, non déduits par défaut. S01–S12 donnent 435000 sans autre engagement sélectionné. |
| `operational_saving_rate.v1` | `round_half_away_from_zero(10000 × operational_saving_capacity / regular_income)` bps | Numérateur et dénominateur en centimes ; dénominateur > 0. Convertir en pourcentage d’affichage par `bps / 100`, sans float de calcul. Taux négatif possible ; aucun plafond artificiel. | `null` si revenu régulier ≤ 0, couverture temporelle incomplète/inconnue, flux pertinent non ventilé, ou capacité indisponible. Le mois en cours n’a pas de taux mensuel comparable avant couverture complète ; les composants bruts restent affichables. |
| `unclassified_amount_share.v1` | `round_half_away_from_zero(10000 × Σ abs(residual_signed_cents_par_transaction) / Σ abs(transaction.amount_cents))` pour transactions pertinentes | Pour chaque transaction `t`, `residual_t = amount_t − Σ allocations confirmées_t`. Le numérateur est `Σ abs(residual_t)` : aucun débit et crédit non ventilés ne peuvent se compenser. Le dénominateur est `Σ abs(amount_t)` sur les mêmes transactions pertinentes. Exposer aussi séparément `Σ residual_t` (résidu signé total) et les compteurs de couverture en opérations. | `null` si dénominateur nul ou couverture temporelle inconnue. Sinon ratio en bps, montant absolu non ventilé et résidu signé total en centimes ; la couverture en nombre d’opérations est fournie par `operation_coverage.v1`. KPI-06 donne `3000` bps ; l’exemple de signes opposés donne `10000` bps même si le résidu signé total est −1000 cts. |
| `operation_coverage.v1` | `fully_classified_operation_count / selected_operation_count` (en bps selon l’arrondi commun) | `fully_classified` signifie somme des allocations confirmées égale au montant bancaire ; une suggestion seule ne compte pas. `raw_count` fournit numérateur et dénominateur ; `raw_cents=null`. | Ratio `null` si zéro opération ou sélection non couverte ; compteurs zéro exacts si période couverte et sans opération. Inclure les opérations non consommatoires : cet indicateur mesure la classification des opérations sélectionnées, pas la part de dépenses. |
| `monthly_delta.v1` | Valeur brute de la métrique sélectionnée sur le mois courant − même valeur brute sur le mois précédent complet | Même `metric_definition_id` de base, mêmes comptes et filtres ; périodes calendaires adjacentes complètes et couvertes. L’unité suit la métrique de base : centimes pour les sommes, bps pour un ratio, compte entier pour un compte. Statut catégoriel sans unité additive → `null`. | `null` pour mois partiel/courant, trou de couverture, filtre différent, mois précédent absent ou métrique de base indisponible. Pas de comparaison d’un mois partiel à un mois plein. Le client peut afficher « non disponible : mois partiel » (INV-10). |
| `month_completeness.v1` | Valeur d’état `complete`, `partial` ou `unknown` ; aucune somme | `complete` uniquement si chaque compte sélectionné a une preuve de couverture du premier au dernier jour du mois. `partial` si une borne manquante ou si le mois calendaire courant n’est pas terminé. `unknown` si les éléments de couverture ne permettent pas de trancher. | Aucun jour sans transaction ne prouve un trou ; il faut les périodes de relevé/import. Octobre 2026 courant est `partial` (INV-10). `raw_cents=null`; exposer l’état et les bornes observées. |

### Traitements analytiques imposés

* **Remboursement apparié** : la jambe bancaire reste crédit positif et `income_refund`; le lien contribue positivement à la somme entre parenthèses de `consumption_net`, donc réduit le coût net. Un remboursement non apparié n’est ni revenu récurrent ni diminution de consommation.
* **`loan_repayment` non scindé** : ne jamais le masquer. Il reste un débit dans `bank_net_flow`, compte dans `operation_coverage` s’il est classé comme tel, est présenté dans la série d’engagements, et n’est pas une dépense de consommation. Sans échéancier il ne renseigne pas le coût total du crédit (INV-07).
* **`cash_withdrawal_pending` / `intermediary_pending`** : débit visible dans la trésorerie, bucket explicite d’attente, non imputé automatiquement à une dépense. Signaler le montant comme incertitude ; il n’est déduit de la capacité qu’après sélection explicite comme engagement.
* **Mois courant** : les valeurs brutes cumulées peuvent être restituées avec `period.completeness=partial`. Les moyennes/deltas de mois complets ne l’utilisent pas ; `monthly_delta.v1` et le taux mensuel sont `null` jusqu’à une période complète comparable. Aucun prorata ni extrapolation implicite.
* **Multi-comptes** : chaque opération n’est agrégée que si son `account_id` figure dans la liste de comptes du résultat. Les dimensions de compte, rôle et éventuelle paire de transfert sont conservées dans les drilldowns (INV-05).

## Forme d’une réponse analytique

Chaque résultat contient les champs du protocole API PLAN §D. Les montants et ratios sont bruts ; la mise en forme française est une couche de présentation.

```json
{
  "period": {
    "start": "2026-01-01",
    "end": "2026-01-31",
    "kind": "calendar_month",
    "completeness": "complete",
    "as_of": "2026-02-01"
  },
  "accounts": ["family-current"],
  "coverage": {
    "status": "complete",
    "covered_from": "2026-01-01",
    "covered_through": "2026-01-31",
    "operation_count": 12,
    "fully_classified_count": 12
  },
  "filters": {
    "flow_kinds": null,
    "category_ids": null,
    "mode": "family_life"
  },
  "metric_definition_id": "consumption_net.v1",
  "raw_cents": 90000,
  "unit": "EUR-cent"
}
```

Pour une mesure en basis points, ajouter `raw_basis_points`; pour un compte d’opérations, ajouter `raw_count`. `raw_cents` vaut alors `null`, jamais un montant flottant détourné. Une indisponibilité est codée `raw_cents: null` ou le champ typé correspondant à `null`, avec un statut/raison exploitable ; elle n’est jamais transformée en zéro. Un résultat doit permettre le drilldown des opérations utilisées et exposer formule, dates, comptes, exclusions et couverture (INV-09).

Pour `unclassified_amount_share.v1`, la réponse expose `raw_basis_points`, `unclassified_absolute_residual_cents = Σ abs(residual_t)` et `unclassified_signed_residual_cents = Σ residual_t`. Le champ `coverage` expose au minimum `operation_count` et `fully_classified_count` ; le taux de couverture est `operation_coverage.v1`. Le résidu signé est informatif et ne sert pas au numérateur du ratio.

## Traçabilité des oracles

Le fichier `docs/examples/metrics_examples.yaml` contient les opérations/allocations fictives et les oracles en centimes. `scripts/verify_metrics_examples.py` recalcule ces oracles indépendamment du futur moteur et échoue au premier écart. Les cas S01–S12 reprennent les oracles de MATRICE_TESTS ; les nombres sont comparés exactement à l’entier près.
