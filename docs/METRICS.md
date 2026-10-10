# Contrat des métriques financières

**Version :** 1 · **Source :** PLAN §C3, §D ; `DOMAIN_MODEL.md`. Les identifiants ci-dessous sont des contrats stables : un changement de formule ou de périmètre exige un nouvel arbitrage et une version d’ID.

## Conventions de calcul

* Argent en centimes entiers signés : débit négatif, crédit positif. Aucune conversion monétaire via `float`; la présentation en euros est faite après calcul. Zéro connu et absence de donnée sont distincts.
* Seules les allocations `decision=confirmed` sont effectives. Une transaction partiellement ventilée a un résidu signé `amount_cents − somme des allocations confirmées`. Le flux est dérivé des allocations confirmées ; le résidu non nul est toujours `unknown` et pertinent pour la part non ventilée. Les suggestions ne sont jamais additionnées (ADR-020).
* Toutes les métriques utilisent le même filtre explicite de dates, comptes, catégories/flux et `personal_mode`. Le mode personnel vaut `envelope` par défaut ou `observed`; ils ne sont jamais additionnés dans le même total (ADR-022). Les transferts internes n’entrent pas dans la consommation. Le brut bancaire conserve chaque écriture sélectionnée une fois.
* Une réponse donne le montant brut en `raw_cents` (integer ou `null`) et l’état de couverture. Les ratios sont des **basis points entiers** (1 bp = 0,01 point de pourcentage), arrondis au plus proche, demi vers l’extérieur/loin de zéro. Formule pour numérateur signé `n` et dénominateur positif `d` : `sign(n) × floor((2 × abs(n) × scale + d) / (2 × d))`. Aucun `float` ni `Decimal` flottant n’est requis.
* Pour `unclassified_amount_share.v1`, chaque résidu non nul est pertinent sans égard au flux connu des autres allocations de la transaction. Le dénominateur contient seulement les allocations confirmées pertinentes (`expense`, `income_recurring`, `income_other`, `allocation_personal`, `refund`, `cash_withdrawal_pending`, `intermediary_pending`) et tous les résidus absolus. Les flux épargne/propre, prêt, capital et régularisation confirmés n’y entrent pas.
* Les transactions CA de montant nul sont rejetées à l’import (ADR-019.i). Un montant observé nul ne peut donc pas devenir une transaction comptable.

## Définitions canoniques

| ID stable | Formule exacte en centimes | Signes, périmètre et dénominateur | Validité, cas limites et affichage |
|---|---|---|---|
| `bank_net_flow.v1` | `Σ transaction.amount_cents` | Tous les `flow_kind` et toutes les transactions sélectionnées ; négatif = sortie, positif = entrée. Égal à crédits − débits absolus (KPI-01). | `null` si la sélection/date n’a pas de couverture transactionnelle connue ; `0` si couverture connue et aucune écriture. Inclut prêt, capital, espèces, remboursement non apparié et régularisation. |
| `regular_income.v1` | `Σ allocation.amount_cents` où `flow_kind=income_recurring`, catégorie admissible et allocation confirmée | Crédits positifs uniquement ; exclut `income_other`, `income_refund` et `loan_disbursement`. Pas de dénominateur. | `null` si couverture transactionnelle inconnue ; zéro valide pour une période couverte sans revenu régulier. Un prêt de 50 000 € contribue zéro (MONEY-04/KPI-02). Un résidu non classé est signalé à côté de la somme. |
| `consumption_net.v1` | En mode `envelope` : `−Σ allocations confirmées expense sur comptes non-personnels − Σ liens refund_of confirmés attribuables à ces achats à la date du crédit`. En mode `observed` : même formule, avec dépenses de tous les rôles de compte, sans déduire les `allocation_personal`. Un lien est imputé à une allocation cible précise ou, sans `target_allocation_id`, réparti au prorata des allocations expense avec l’arrondi ADR-007. | Dépense négative augmente le coût ; seul le montant du lien confirmé réduit la catégorie source. Le résidu non apparié du crédit est exclu. Les plafonds cumulés sont les allocations `refund` à la source et les allocations `expense` à la cible. Les liens `proposed` et ceux dont les deux comptes ne sont pas sélectionnés sont ignorés. | `null` si couverture de transaction inconnue. Autrement restituer le signé : un résultat négatif est permis et signalé comme remboursement d’un achat antérieur (ADR-021/024). Un remboursement réduit le mois de la date de l’avoir, jamais le mois d’achat. En mode `envelope`, les dépenses des comptes `personal` sont exclues ; en mode `observed`, elles sont incluses (ADR-022). La ventilation par catégorie doit sommer exactement à cette mesure. |
| `personal_allowances.v1` | En mode `envelope` : `−Σ allocation.amount_cents` où `flow_kind=allocation_personal`, confirmée et portée par un compte non-personnel sélectionné. En mode `observed` : `0`. | Les allocations personnelles sont négatives, le résultat d’enveloppe est positif. La mesure n’est pas une dépense observée. | `null` si couverture inconnue ; zéro valide si aucun mouvement d’enveloppe ou si mode `observed`. En mode `observed`, signaler le reliquat signé `−Σ allocation_personal.amount_cents − Σ(−expense.amount_cents sur comptes personal)` ; jamais additionner enveloppe et dépenses (ADR-022). |
| `savings_net_transferred.v1` | `−Σ allocation.amount_cents` pour les allocations confirmées `transfer_savings` des comptes de rôle `current` sélectionnés. | Les allocations débitrices sont négatives, les retours positifs ; −20000 et +5000 donnent +15000. Les jambes d’un compte d’épargne ne sont pas recomptées, même si plusieurs comptes sont sélectionnés. La transaction peut avoir plusieurs flux et seul le montant réellement alloué à `transfer_savings` entre dans la formule (ADR-020, MONEY-06). | `null` si couverture du compte courant inconnue ; zéro si aucune allocation confirmée de ce type. Une allocation partielle est mesurée à sa valeur allouée ; son résidu `unknown` reste visible et rend la capacité opérationnelle `null`. |
| `operational_saving_capacity.v1` | `regular_income − consumption_net − personal_allowances − selected_commitments_cents` | Les engagements sélectionnés = valeur absolue des sorties confirmées choisies, disjointes de consommation et enveloppes. Par défaut `selected_commitments_cents=0`. Ce n’est pas un changement de solde bancaire. En mode `observed`, les dépenses personnelles sont dans `consumption_net` et `personal_allowances=0`; en mode `envelope`, c’est l’inverse. | `null` si période/calendrier non complet ou inconnu, ou si un résidu non nul existe dans les comptes et dates **sélectionnés** (ADR-020/024). Le résidu d’un compte personnel exclu du périmètre en mode `envelope` ne bloque donc pas cette capacité (n-12). Une échéance `loan_repayment` non scindée reste visible dans trésorerie et la série des échéances ; elle n’est soustraite que si choisie explicitement comme engagement, entière, avec signalement « capital/intérêts inconnus ». Espèces en attente et intermédiaires (dont Carrefour Banque) restent affichés, non déduits par défaut. S01–S12 donnent 435000 sans autre engagement sélectionné. |
| `operational_saving_rate.v1` | `round_half_away_from_zero(10000 × operational_saving_capacity / regular_income)` bps | Numérateur et dénominateur en centimes ; dénominateur > 0. Convertir en pourcentage d’affichage par `bps / 100`, sans float de calcul. Taux négatif possible ; aucun plafond artificiel. | `null` si revenu régulier ≤ 0, couverture temporelle incomplète/inconnue, flux pertinent non ventilé, ou capacité indisponible. Le mois en cours n’a pas de taux mensuel comparable avant couverture complète ; les composants bruts restent affichables. |
| `unclassified_amount_share.v1` | `round_half_away_from_zero(10000 × Σ_t abs(residual_t) / (Σ_a abs(a.amount_cents) + Σ_t abs(residual_t)))`, où `a` est une allocation confirmée de flux pertinent et `t` une transaction avec résidu non nul. | `residual_t = transaction.amount_cents − Σ allocations confirmées_t`. Tout résidu non nul est pertinent sous `unknown`, même si l’allocation connue de sa transaction est un flux non pertinent. Le dénominateur est la somme des valeurs absolues des allocations confirmées pertinentes (`expense`, `income_recurring`, `income_other`, `allocation_personal`, `refund`, `cash_withdrawal_pending`, `intermediary_pending`) et de tous les résidus. Ne jamais prendre `abs(Σ résidus)` : chaque opération reste séparée. Exposer aussi `Σ residual_t` signé, `Σ abs(residual_t)`, et les compteurs d’opérations. | `null` si dénominateur nul ou couverture transactionnelle inconnue ; sinon bps entiers. KPI-06 = `3000` bps (3000 non ventilés / 10000 pertinents). Résidus −3000 et +2000 : ratio `10000` bps, résidu signé `−1000`, résidu absolu `5000`. Un résidu de flux connu non pertinent reste au numérateur/dénominateur et rend la capacité `null` (ADR-020). |
| `operation_coverage.v1` | `fully_classified_operation_count / selected_operation_count` (en bps selon l’arrondi commun) | `fully_classified` signifie somme des allocations confirmées égale au montant bancaire ; une suggestion seule ne compte pas. `raw_count` fournit numérateur et dénominateur ; `raw_cents=null`. | Ratio `null` si zéro opération ou sélection non couverte ; compteurs zéro exacts si période couverte et sans opération. Inclure les opérations non consommatoires : cet indicateur mesure la classification des opérations sélectionnées, pas la part de dépenses. |
| `monthly_delta.v1` | Valeur brute de la métrique sélectionnée sur le mois courant − même valeur brute sur le mois précédent complet | Même `metric_definition_id` de base, mêmes comptes et filtres ; périodes calendaires adjacentes complètes et couvertes. L’unité suit la métrique de base : centimes pour les sommes, bps pour un ratio, compte entier pour un compte. Statut catégoriel sans unité additive → `null`. | `null` pour mois partiel/courant, trou de couverture, filtre différent, mois précédent absent ou métrique de base indisponible. Pas de comparaison d’un mois partiel à un mois plein. Le client peut afficher « non disponible : mois partiel » (INV-10). |
| `month_completeness.v1` | État `complete`, `partial`, `unknown` ou `not_started`. Pour chaque section committée, intervalle complet `[covered_from, min(covered_through, generated_on − 1 jour)]`; le compte est complet seulement sur l'union journalière de ces intervalles. | Les périodes mensuelles des comptes ne sont pas amalgamées avant ce contrôle : chaque jour doit être couvert pour chaque compte sélectionné. À défaut de préambule, `received_on_fallback` rend provisoire le dernier jour portant une opération de la section. | `complete` si chaque jour du mois est dans l'union par compte. `partial` si un jour manque après le début de couverture, notamment après un export suivant non chevauchant le jour provisoire. `not_started` si le mois précède toute couverture connue : état neutre, sans alerte de trou (ADR-003). `unknown` si la preuve des sections est insuffisante ou si le contrôle de solde calculé à la lecture révèle un vrai mismatch. Un solde daté après `complete_through` est ignoré ; sans paire comparable, le contrôle vaut `not_comparable`, pas mismatch. `raw_cents=null`; exposer les sections, dates de génération/repli et jours provisoires (ADR-018/024, INV-10). |

### Traitements analytiques imposés

* **Remboursement apparié** : la jambe bancaire reste crédit positif et `income_refund`; le lien `refund_of` confirmé réduit la catégorie source dans le mois de l’avoir, uniquement si les deux comptes sont inclus. La cible est une allocation précise ou la répartition au prorata ADR-007 ; le total des catégories égale la mesure globale. Le plafond exclut toute jambe `income_other` du crédit et toute partie non `expense` de l’achat. `proposed` ne compte pas. L’achat reste immuable. Un remboursement non apparié ou hors périmètre est non attribué, ni revenu récurrent ni diminution de consommation ; une consommation négative est valide et signalée (ADR-021/024).
* **`loan_repayment` non scindé** : ne jamais le masquer. Il reste un débit dans `bank_net_flow`, compte dans `operation_coverage` s’il est classé comme tel, est présenté dans la série d’engagements, et n’est pas une dépense de consommation. Sans échéancier il ne renseigne pas le coût total du crédit (INV-07).
* **`cash_withdrawal_pending` / `intermediary_pending`** : débit visible dans la trésorerie, bucket explicite d’attente, non imputé automatiquement à une dépense. Signaler le montant comme incertitude ; il n’est déduit de la capacité qu’après sélection explicite comme engagement.
* **Mois courant** : les valeurs brutes cumulées peuvent être restituées avec `period.completeness=partial`. Une écriture sur une journée non couverte par l’union des intervalles complets reste provisoire et comptée dans le cumul brut. `monthly_delta.v1` et la capacité/taux mensuels sont `null` jusqu’à une période complète comparable. Le contrôle de solde est recalculé à la lecture : il ne rend `month_completeness=unknown` que pour un mismatch comparable ; un solde de date provisoire donne `not_comparable` (ADR-018/024, INV-10).
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
    "as_of": "2026-02-01",
    "generated_on": "2026-02-02"
  },
  "accounts": ["family-current"],
  "coverage": {
    "status": "complete",
    "complete_intervals_by_account": {
      "family-current": [{"from": "2026-01-01", "through": "2026-01-31"}]
    },
    "sections": [{
      "section_id": "section-synthetic-01",
      "account_id": "family-current",
      "covered_from": "2026-01-01",
      "covered_through": "2026-01-31",
      "generated_on": "2026-02-01",
      "complete_through": "2026-01-31",
      "status": "committed"
    }],
    "balance_check_by_account": {"family-current": "not_comparable"},
    "provisional_operation_count": 0,
    "operation_count": 12,
    "fully_classified_count": 12
  },
  "filters": {
    "flow_kinds": null,
    "category_ids": null,
    "mode": "family_life",
    "personal_mode": "envelope"
  },
  "metric_definition_id": "consumption_net.v1",
  "raw_cents": 90000,
  "unit": "EUR-cent"
}
```

Pour une mesure en basis points, ajouter `raw_basis_points`; pour un compte d’opérations, ajouter `raw_count`. `raw_cents` vaut alors `null`, jamais un montant flottant détourné. `period.generated_on` date la génération de la réponse ; pour un mois, exposer séparément chaque section source avec sa date de génération et son `complete_through`, l’union des intervalles complets par compte, le résultat du contrôle de solde par compte et le nombre d’opérations provisoires. Ne pas réduire plusieurs sections d’un compte à une seule date. Une indisponibilité est codée `raw_cents: null` ou le champ typé correspondant à `null`, avec un statut/raison exploitable ; elle n’est jamais transformée en zéro. Un résultat doit permettre le drilldown des opérations utilisées et exposer formule, dates, comptes, exclusions et couverture (INV-09).

Pour `unclassified_amount_share.v1`, la réponse expose `raw_basis_points`, `unclassified_absolute_residual_cents = Σ abs(residual_t)` et `unclassified_signed_residual_cents = Σ residual_t`. Le champ `coverage` expose au minimum `operation_count` et `fully_classified_count` ; le taux de couverture est `operation_coverage.v1`. Le résidu signé est informatif et ne sert pas au numérateur du ratio.

## Traçabilité des oracles

Le fichier `docs/examples/metrics_examples.yaml` contient les opérations/allocations fictives et les oracles en centimes. `scripts/verify_metrics_examples.py` recalcule ces oracles indépendamment du futur moteur et échoue au premier écart. Les cas S01–S12 reprennent les oracles de MATRICE_TESTS ; les nombres sont comparés exactement à l’entier près.
