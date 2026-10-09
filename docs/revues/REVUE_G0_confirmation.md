# Revue G0 de confirmation : levée du blocage finances/import

- **Relecteur** : `claude-code:opus`, le même relecteur adversarial indépendant que pour `REVUE_G0_opus.md`. Il n'a écrit aucune des corrections.
- **Date** : 2026-10-10.
- **Mode** : lecture seule. Seul ce rapport a été créé dans le dépôt. Les scripts jetables sont dans `/tmp/revue_g0_confirm/` (`common.py`, `t_import.py`, `t_constats.py`, `t_m01.py`, `t_metrics.py`). Les données sont 100 % synthétiques ; `csv_references/` et `data/` n'ont pas été ouverts.
- **Version relue** : `797001c`, avec en plus `env.example` modifié hors du périmètre de la revue. Documents relus : `IMPORT_CONTRACT.md`, `schema.sql`, `verify_schema.py`, `METRICS.md`, `DOMAIN_MODEL.md`, `metrics_examples.yaml`, `verify_metrics_examples.py`, `API.md`, ADR-012 et ADR-017 à ADR-023, `manifest.yaml` et les nouvelles fixtures, `MATRICE_TESTS_ADDENDUM.md`.
- **Mini-service** : `/tmp/revue_g0_confirm/common.py` applique `IMPORT_CONTRACT.md` à la lettre sur `schema.sql`. Il persiste l'aperçu en `previewed` et gère les résolutions `OUT_OF_PERIOD`. Le commit se fait sous `BEGIN IMMEDIATE` en mode autocommit : le hash est relu sous le verrou, puis `n_existing = max(rang)` avec contrôle `count = max`. Il crée ensuite les rangs `n_existing+1..max(n_existing, n_new)`, une `source_row` par occurrence, et enfin la transition `previewed → committed`.

## Verdict

**G0 PASS sous réserves**

Les deux bloquants d'origine ont été traités au niveau qui les rendait bloquants :

- **B-02** est entièrement résolu.
- **B-01** est résolu pour les lignes hors période et pour l'export chevauchant. Il n'est que partiellement résolu pour la règle mensuelle de complétude (réserve R1).

Le contrat d'import est **implémentable tel qu'écrit**. Le scénario A/B/A/B/A, l'ordre inverse B/A, la concurrence, le `no_op`, les rangs contigus et les résolutions hors période passent tous sur le schéma réel, sans qu'aucun trigger bloque un import légitime.

Restent trois MAJEURS. Aucun ne bloque G0, mais **R1 et R2 doivent être corrigés dans les documents et dans l'oracle avant le démarrage de T14, et vérifiés au gate G3**.

| Réserve | Constat | Ticket porteur | Échéance |
|---|---|---|---|
| R1 | N-01 : règle `month_completeness` (jour provisoire intermédiaire) | **T14** (+ oracle `verify_metrics_examples.py`, T24/T26) | avant T14, vérifiée à G3 |
| R2 | N-02 : contrôle de solde sur un solde du jour de génération ; statut figé sur une section immuable | **T14** | avant T14, vérifiée à G3 |
| R3 | N-03 : avoir lié à un achat ventilé sur plusieurs catégories | **T17** (+ T24) | avant T17, vérifiée à G4 |

Les dix mineurs (n-04 à n-13) sont listés plus bas, chacun avec son ticket porteur.

## Vérificateurs du dépôt (exécutés)

```text
$ python3 -I scripts/verify_schema.py                       -> PASS verify_schema (exit 0)
$ python3 -I scripts/verify_metrics_examples.py             -> PASS: 22 oracles métriques, 8 splits et 2 cas de priorité vérifiés en centimes entiers. (exit 0)
$ python3 -I backend/tests/fixtures/verify_fixtures.py      -> OK : 34 fixtures vérifiées, manifest concordant, confidentialité vérifiée. (exit 0)
$ python3 -I scripts/tests_real_csv_profile_selftest.py     -> Ran 4 tests ... OK (exit 0)
$ ./scripts/check_no_private_data.sh --with-untracked       -> OK : aucune donnée privée détectée (72 fichier(s) inspecté(s)). (exit 0)
```

## 1. Statut des 17 constats

**Bilan : 16 RÉSOLUS, 1 PARTIEL, 0 NON RÉSOLU.**

| Constat | Statut | Preuve rejouée (sortie réelle) |
|---|---|---|
| **B-01** | **PARTIEL** | **Cas 2, ligne hors période : résolu.** `commit B non résolu : committed_import_has_unresolved_rejection \| tx 1`, puis `après décision 'create_occurrence' : committed \| tx 2 (vérité 2)`.<br>**Cas 1, jour provisoire : résolu si l'export suivant chevauche** (`X = 2`). **Non résolu si l'export suivant commence le lendemain** : `tx X = 1 (vérité 2)`, et `derive_month_completeness(...) : complete`. Voir N-01. |
| **B-02** | RÉSOLU | Fichier à 2 sections dont S2 n'a pas de plage : `committed_import_section_unresolved \| tx 0 \| statut previewed`. Après résolution dans l'aperçu : `committed \| tx 2`. Le réimport des mêmes octets donne `no_op` sans perte. Aucun statut `partial` (schéma, API, ADR-017). |
| **M-01** | RÉSOLU (défense procédurale) | PRAGMA actifs : `REPLACE source_row … REJETÉ (source_row_immutable)`. Avec `recursive_triggers=OFF`, le REPLACE est encore `ACCEPTÉ` : c'est le comportement de SQLite, et le schéma ne peut pas l'empêcher. Le hook `connect`, le lint anti-`REPLACE` et le test SEC-06 sont désormais contractuels (ADR-019.a, `IMPORT_CONTRACT.md:129`). `verify_schema` : `SEC-06 chaque connexion vérifiée avec les deux PRAGMA`. |
| **M-02** | RÉSOLU | Rang 3 sans les rangs 1 et 2 : `occurrence_rank_not_contiguous`. Source du compte A vers une transaction du compte B : `source_row_account_mismatch`. Même fichier lié deux fois à T1 : violation de `UNIQUE … import_file_id, transaction_id`. Transaction sans source au commit : `transaction_without_source_row`. Source rattachée à un import `rejected` : `source_row_import_status_invalid` (verify_schema). |
| **M-03** | RÉSOLU | `committed->previewed`, réécriture du sha, `committed->rejected` : tous `committed_import_immutable`. Modification ou suppression d'une section : `committed_import_section_immutable`. Suppression d'un import : `import_history_immutable`. `no_op` pointant vers un import `rejected` : `no_op_must_reference_committed_same_hash`. |
| **M-04** | RÉSOLU | Le contrat définit `n_existing = max(rang)`, vérifie `count = max` et interdit de combler un trou. Le trigger renvoie `occurrence_rank_not_contiguous`. |
| **M-05** | RÉSOLU | Deux sections sur le même compte : violation de `UNIQUE … import_file_id, account_id`. La fixture `same_account_two_sections.csv` attend `rejected`. |
| **M-06** | RÉSOLU | Sur-allocation : `allocation_total_exceeds_transaction`. UPDATE en place : `allocation_update_requires_audited_supersession`. DELETE : `allocation_delete_forbidden`. Le signe et la direction par catégorie sont couverts par verify_schema. Voir n-11. |
| **M-07** | RÉSOLU | P4 : `capacity None`, `share 1667 bps`, `résidu abs 80000`, `savings 20000`. P8 : `savings 20000 + consommation 5000 = 25000` pour 25 000 réellement sortis. |
| **M-08** | RÉSOLU (nouveaux constats N-03, n-04, n-05) | P5 : `consumption_net -2000`, signalé (ADR-021). P6 : `['cur'] 10000`, `['perso'] 0`, `['cur','perso'] 8000` ; le lien n'est pris en compte que si les deux comptes sont dans le périmètre. Plafonds par source et par cible : verify_schema MONEY-10. |
| **M-09** | RÉSOLU | P7 : en `envelope`, `consumption 0`, `allowances 25000`, `capacity 375000` ; en `observed`, `consumption 18000`, `allowances 0`, `capacity 382000`. Jamais les deux à la fois. |
| **m-01** | RÉSOLU | `'12.00'` inséré en SQL reste `ACCEPTÉ -> (12,'integer')`, ce qu'on ne peut pas empêcher dans SQLite. La garde est `type(x) is int` (ADR-019.h), testée par verify_schema (float intégral, texte et bool rejetés). |
| **m-02** | RÉSOLU | `DOMAIN_MODEL.md:112-124` : rejet atomique d'une part nulle ou de signe opposé, fusion des doublons, ordre canonique. Exemples YAML `split_rejected_*` et `split_category_order_*`. |
| **m-03** | RÉSOLU | Propriété Unicode `White_Space`, avertissement `Cf`. Les CHECK `trim` et double espace rejettent `' X'` et `'A  B'`. Le hash d'identité reste auxiliaire, non contrôlable en SQL, ce qui est assumé. |
| **m-04** | RÉSOLU | verify_schema : hash lu sous `BEGIN IMMEDIATE` (IMP-07, deux connexions), IMP-06 rollback, DELETE/REPLACE, blocage par couverture et par `OUT_OF_PERIOD`. |
| **m-05** | RÉSOLU | `ZERO_AMOUNT` est rejeté (contrat l. 38, ADR-019.i, CHECK `amount_cents <> 0`). |
| **m-06** | RÉSOLU | `category_net_cents` et `pair_bank_flow_cents` sont assertés (S11 : 48000 / −48000 ; MONEY-03 : 8000 / −8000). `refund_of` est employé partout ; `purchase_refund` n'apparaît plus que dans l'ancien rapport. |

## 2. Implémentabilité du contrat (mini-service à la lettre)

```text
A/B/A/B/A : [('committed', 3), ('committed', 6), ('no_op', 6), ('no_op', 6), ('no_op', 6)]
signatures A..B == B/A inversé : True | tx 6 | sources 9
N-concurrence (2 fichiers distincts, 2 connexions WAL) : ['committed', 'committed'] | tx X = 3 (attendu 3)
```

Aucun trigger ne bloque un import légitime :

- l'ordre `previewed → sections → transactions (rangs croissants) → source_rows → audit → committed` est accepté dans une même transaction ;
- les rangs restent contigus après réimport ;
- la résolution hors période se commite dans les deux cas, `link_existing` comme `create_occurrence` ;
- le garde de fin de commit (nombre de sources = `parsed_operation_count`, aucune transaction orpheline) est compatible avec ces résolutions.

**Contraintes implicites à documenter (T12/T13)** :

- les rangs doivent être insérés dans l'ordre croissant, faute de quoi le trigger de contiguïté échoue ;
- une supersession d'allocation doit faire d'abord l'UPDATE de l'ancienne, puis l'INSERT de la nouvelle (n-11) ;
- la tentative `no_op` est forcément une **nouvelle** ligne, car la transition `previewed→no_op` est interdite (n-09).

## 3. Nouveaux constats

### MAJEURS

#### N-01 (reste de B-01) — Un mois est déclaré `complete` alors qu'un jour intermédiaire n'est que provisoire (R1, T14)

**Références** : `METRICS.md:28`, `DOMAIN_MODEL.md:32`, `IMPORT_CONTRACT.md:78`, ADR-018(1), `verify_metrics_examples.py:276-312`.

La règle mensuelle ne contrôle que deux choses : la continuité des fenêtres **déclarées**, et le fait que le dernier jour du mois soit ≤ `complete_through` « du compte ». Elle n'exige pas que chaque jour soit complet.

Scénario :
- l'export A couvre du 01/10 au 09/10 et est téléchargé le 09/10, donc `complete_through` = 08/10 ;
- l'export B' couvre du 10/10 au 31/10 et est téléchargé le 01/11.

```text
sections : [('2026-10-01','2026-10-09','2026-10-08'), ('2026-10-10','2026-10-31','2026-10-31')] | tx X = 1 (vérité 2)
verify_metrics.derive_month_completeness(gen=2026-11-01, fenêtres déclarées) : complete
lecture stricte (chaque jour ∈ ∪[from, complete_through]) : jours manquants = ['2026-10-09']
```

ADR-018 dit pourtant qu'un jour provisoire n'est « jamais complet ». La perte n'est détectée que si un contrôle de solde est disponible et calculé (`mismatch → unknown`). Sans soldes comparables, on retombe sur le défaut d'origine de B-01 : un mois affiché `complete` avec une opération manquante.

**Correction** : un jour est complet s'il appartient à l'union des intervalles `[coverage_from, complete_through]` des sections commitées du compte. Un mois est `complete` si tous ses jours sont complets pour chaque compte sélectionné. Il faut aussi :
- corriger `derive_month_completeness`, qui ne connaît aujourd'hui qu'un seul `generated_on` par compte ;
- ajouter un exemple YAML et une fixture « export suivant non chevauchant ».

#### N-02 — Contrôle de solde calculé sur un solde intrajournalier ; résultat figé et dépendant de l'ordre d'import (R2, T14)

**Références** : `IMPORT_CONTRACT.md:111`, `schema.sql:76` et `:830-848`, ADR-018(3), ADR-019.d.

Le solde d'en-tête est daté du jour de génération (fixtures : `Solde au 09/10/2026` dans un fichier téléchargé le 09/10). Il est donc pris en cours de journée. Or le recalcul exclut D0 (`D0 < booked_on ≤ D1`).

Dans le flux **recommandé**, celui de l'export chevauchant, on obtient :

```text
données correctes : X = 2 | recalcul contractuel 100000 + -500 = 99500 vs 98377 -> FAUX mismatch ⇒ month_completeness=unknown
correction ultérieure du statut de solde stocké : REJETÉ (committed_import_section_immutable)
```

Selon l'oracle `ADR_018_balance_mismatch_unknown`, `unknown` rend `null` **toutes** les métriques du mois. Ce statut est stocké sur une section immuable : le mois reste donc inutilisable définitivement.

Le statut dépend en plus de l'ordre d'import. Si B (novembre) est importé avant A (octobre), le « dernier solde connu » au commit de A est postérieur à D1. L'intervalle est alors vide, et le contrôle de B ne pourra jamais être recalculé.

**Correction** :
- n'utiliser comme D0 ou D1 que des soldes datés au plus tard à `complete_through`, c'est-à-dire avant le jour de génération ; sinon le résultat est `not_comparable` ;
- calculer le contrôle **à la lecture**, ou dans une table append-only versionnée `balance_checks`, sur l'ensemble des soldes connus, sans dépendre de l'ordre d'import ;
- ne pas porter `balance_check_status` comme vérité figée sur `import_account_sections`.

#### N-03 — Avoir lié à un achat ventilé sur plusieurs catégories : imputation non définie et oracle incohérent (R3, T17 + T24)

ADR-021 et `DOMAIN_MODEL.md:82` parlent de « la catégorie d'origine », au singulier. L'oracle impute le lien entier à **chaque** catégorie de dépense de l'achat :

```text
consumption_net=8000 ; groceries=4000 ; restaurants=2000 ; somme catégories=6000 (≠ total : avoir imputé deux fois)
```

KPI-03 et KPI-07 (drilldown égal au total) sont alors violés.

**Correction** : au choix, lier à une allocation précise (`target_allocation_id`), ou répartir le lien au prorata des allocations `expense` avec l'arrondi d'ADR-007. Ajouter un exemple YAML et un test MONEY-10.

### MINEURS

| ID | Constat (preuve) | Ticket |
|---|---|---|
| n-04 | Le plafond `refund_of` porte sur le **montant du crédit**, pas sur l'allocation `refund`. Un crédit de 5000 ventilé 2000 `refund` + 3000 `income_other` accepte un lien de 5000 (`ACCEPTÉ`) : 3000 sont utilisés deux fois. Même problème côté cible : le plafond est \|achat\| et non Σ des allocations `expense`. | T17 |
| n-05 | Un lien `confirmed` reste modifiable par UPDATE, montant comme statut `confirmed→rejected` (`ACCEPTÉ`, sans version ni audit). Seul le DELETE est interdit. | T17/T20 |
| n-06 | La transition `previewed→rejected` est acceptée alors que des `source_rows` et des transactions existent : `transactions publiées rattachées à un import rejected : 1 [('rejected',)]`. Ajouter au trigger de transition : aucune source si `rejected`. | T12 |
| n-07 | Les décisions d'aperçu (`import_rejections` résolues, `OUT_OF_PERIOD`) restent modifiables ou supprimables après le commit (`ACCEPTÉ` pour les deux). La preuve de la décision humaine dépend alors du seul `audit_event`. | T12 |
| n-08 | `link_existing` vers une transaction d'une **autre identité** est accepté par le schéma (`-11,23 X` prouvé par `-50,00 AUTRE`). `source_rows` ne porte aucune clé d'identité. Seule la validation de service (API l. 88) protège ; ajouter `identity_key_sha256` sur `source_rows`, ou un trigger d'égalité. | T13 |
| n-09 | `no_op` : l'aperçu reste `previewed` orphelin (3 cas dans A/B/A/B/A), et son sort (`rejected` ? purge ?) n'est pas spécifié. Le CHECK `archive_relpath` impose `data/originals/<no_op_id>.bin` : le contrat ne dit pas s'il faut une copie des octets ou un chemin vers un fichier inexistant. | T11/T12 |
| n-10 | Repli `received_on_fallback` : la date de réception est toujours postérieure ou égale à la génération réelle. Un jour réellement provisoire peut donc être déclaré complet. T09 doit établir, en agrégat local, si le préambule `Téléchargement du` est toujours présent. Sinon, en repli, traiter comme provisoire le dernier jour qui porte des opérations. | T09/T14 |
| n-11 | Supersession d'allocation : l'ordre naturel « INSERT de la nouvelle, puis UPDATE de l'ancienne » échoue (`allocation_total_exceeds_transaction`). Seul l'ordre inverse, avec FK différée, passe. C'est à documenter dans le contrat de décision. | T17/T20 |
| n-12 | En mode `envelope`, une opération non classée du compte personnel rend la capacité familiale `null` (`capacity None`, résidu 700). Le résidu des comptes `personal` compte même quand leurs dépenses sont exclues. À arbitrer. | T24 |
| n-13 | Traçabilité :<br>• aucun ID de matrice pour ADR-019.d (immutabilité et transitions d'import), ADR-018(3) (`mismatch→unknown`), ADR-019.c (sources = `n_new` par identité, seul le total est garanti en SQL) et ADR-019.h ;<br>• les codes d'anomalie des fixtures (`MISSING_PERIOD`, `DUPLICATE_ACCOUNT_SECTION`, `PROVISIONAL_DAY`) n'ont pas de correspondance documentée avec les codes API (`SECTION_RANGE_REQUIRED`…) ;<br>• le paragraphe d'arbitrage d'ADR-012 (« section concernée uniquement », « journées entières ») reste lisible sans être barré. | T00/T02 (doc), T11 |

## 4. Cohérence croisée

| Paire | État |
|---|---|
| IMPORT_CONTRACT ↔ schema.sql | Cohérents : statuts, garde de commit, hash sous verrou, contiguïté, IMP-13, `OUT_OF_PERIOD`, `complete_through` vérifié par trigger. Écarts : n-06 à n-09. |
| IMPORT_CONTRACT ↔ API.md | Cohérents : commit `committed\|no_op`, PATCH de mapping, de couverture et de multiplicité, pas de `partial`. Écart : n-13 (codes). |
| ADR-017..023 ↔ documents | Cohérents. Écarts : ADR-018(1) contredit la règle mensuelle (N-01) ; ADR-018(3) combiné à ADR-019.d fige un statut faux (N-02). |
| Fixtures ↔ contrat | Cohérents : `date_out_of_period_2025` et `out_of_period_line` en `previewed` ; `same_account_two_sections` en `rejected` ; `provisional_day_A/B/B_overlap` (IMP-11). **Manque** : la fixture de N-01 (export suivant qui commence le lendemain, sans ligne du jour provisoire). |
| MATRICE_TESTS_ADDENDUM | IMP-11..14, MONEY-10, KPI-08 et SEC-06 couvrent les garde-fous principaux. Lacunes : n-13. |

## 5. Propositions pour les documents de suivi (arbitrage par l'orchestrateur)

- **DECISIONS** :
  - amender ADR-018 : complétude par jour (N-01), et soldes comparables seulement s'ils sont datés au plus tard à `complete_through`, avec calcul indépendant de l'ordre (N-02) ;
  - amender ADR-021 : imputation d'un achat multi-catégories (N-03) et plafond sur l'allocation `refund` (n-04).
- **REGISTRE_RISQUES** :
  - « faux `mismatch` de solde au jour de génération » (N-02) ;
  - « repli `received_on` » (n-10).
- **MATRICE** :
  - IMP-15 : export suivant non chevauchant, le mois reste `partial` ;
  - IMP-16 : immutabilité et transitions d'import, `rejected` sans source ;
  - KPI-09 : contrôle de solde indépendant de l'ordre ;
  - MONEY-11 : avoir sur un achat multi-catégories.
