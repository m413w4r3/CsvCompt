# Revue critique indépendante G0 : composants financiers et import

- **Relecteur** : `claude-code:opus`, revue adversariale indépendante. Le relecteur n'a écrit aucun des livrables relus.
- **Date** : 2026-10-10.
- **Mode** : lecture seule. Aucun fichier du dépôt n'a été modifié, à part ce rapport. Les scripts jetables sont dans `/tmp/revue_g0/`, hors du dépôt. `csv_references/` et `data/` n'ont pas été ouverts.
- **Périmètre** : `docs/IMPORT_CONTRACT.md`, `docs/examples/schema.sql`, `scripts/verify_schema.py`, `docs/METRICS.md`, `docs/DOMAIN_MODEL.md`, `docs/examples/metrics_examples.yaml` et `scripts/verify_metrics_examples.py`. Ils sont confrontés à PLAN §B/§C/§D, à MATRICE_TESTS (IMP, MONEY, KPI, golden S01–S12) et aux ADR-005 à ADR-016.
- **Environnement** : Python 3, SQLite 3.45.3 (CLI et module `sqlite3`).

## Verdict

**finances/import : BLOCK**

Le blocage porte sur le **contrat d'import**. Le volet **métriques** obtiendrait seul « PASS sous réserves » : tous les oracles du plan sont exacts, mais il reste quatre ambiguïtés MAJEURES.

Pour lever le blocage, il suffit d'amender des documents et le schéma de référence. Aucun code applicatif n'est concerné. Deux décisions de l'orchestrateur sont nécessaires avant T09, T12 et T13 :

1. **B-01** : l'hypothèse de « journées complètes » qui justifie le maximum de multiplicité n'est garantie ni aux bornes réelles d'un export ni pour les lignes hors période. Le résultat est une **perte silencieuse d'opérations réelles**, alors que le mois est affiché `complete`.
2. **B-02** : l'ADR-012 (arbitrage) et `IMPORT_CONTRACT.md` donnent trois granularités de blocage contradictoires. Combinées à l'idempotence par hash de fichier, elles rendent des lignes **définitivement non importables**.

Constats : **2 BLOQUANTS, 9 MAJEURS, 6 MINEURS**.

### Ce qui est validé (preuves positives)

| Point | Preuve | Résultat |
|---|---|---|
| Oracles golden du plan | Recalcul indépendant (`/tmp/revue_g0/t_metrics.py`, P1) | Flux bancaire −4 416 561 = crédits − débits. Revenus réguliers 550 000, consommation 90 000, enveloppes 25 000, épargne 15 000, capacité 435 000, taux 87000/11 = 7909,09 → **7909** bps. Tous conformes. |
| Vérificateurs du dépôt | `python3 scripts/verify_metrics_examples.py` ; `python3 scripts/verify_schema.py` | `PASS: 13 oracles métriques, 4 splits et 2 cas de priorité…` ; `PASS verify_schema` (16 OK) |
| Arrondi « demi loin de zéro » | Comparaison exhaustive avec `fractions.Fraction`, n ∈ [−3000, 3000] et d ∈ [1, 400] | **0 écart** |
| Splits de l'ADR-007 | P3 | −1101 à 70/30 → −771/−330 ; −10001 à 50/30/20 → −5001/−3000/−2000 ; 7000+2999 rejeté |
| Maximum de multiplicité sous hypothèse de journées complètes | Propriété aléatoire : 3000 scénarios, 0 à 4 doublons par jour, 1 à 5 exports chevauchants, permutations et réimports (`t_multiplicity.py`, M1) | **0 écart** avec la vérité : commutatif, idempotent, sans sur-comptage |
| Signes opposés et casse | Clé `(compte, date, montant signé, identity_label)`, collation BINARY | −1123 et +1123 restent distincts ; les casses différentes aussi (IMP-08) |
| Concurrence | Deux connexions sur une base WAL dans `/tmp` (R3) | Le second `BEGIN IMMEDIATE` reçoit `database is locked`. En mode DEFERRED, les deux lisent n=0, puis l'écriture de c2 reçoit `database is locked`. Résultat : **1** transaction, aucun doublon. |
| Index | `EXPLAIN QUERY PLAN` | La recherche de rang utilise l'index couvrant UNIQUE. Hash commité : index partiel. Allocations, liens et `(account_id, booked_on)` sont indexés. |
| ADR-009 | Exemple `unclassified_opposite_signs_no_cancellation` | Numérateur Σ\|résidu\| = 5000, ratio 10000 bps, résidu signé −1000 : conforme à l'arbitrage |

---

## BLOQUANTS

### B-01 — Le maximum de multiplicité perd silencieusement des opérations aux bornes d'export et sur les lignes hors période

**Références** : `IMPORT_CONTRACT.md:78` ; `DECISIONS.md:91` (ADR-012 : « les exports CA standard … sont traités comme des journées entières ») ; `backend/tests/fixtures/manifest.yaml` (`date_out_of_period_2025.csv` → `OUT_OF_PERIOD`, `expected_behavior = avertissement`).

Le contrat admet que `max(n_existing, n_new)` n'est juste que si chaque journée couverte est complète. Il **décrète** pourtant cette complétude à partir de la phrase de période, sans jamais la vérifier. Deux scénarios réalistes la violent.

**Contre-exemple 1 : dernier jour d'un export généré le jour même.** Le corpus réel couvre 2026-01-01 → 2026-10-09, et le plan est daté du 2026-10-09 : ce cas est donc probable. L'export A, tiré le 09/10 à midi, contient 1 paiement X ce jour-là. Un second X identique est comptabilisé l'après-midi. L'export B est demandé « depuis le dernier import », soit du 10/10 au 31/10.

**Contre-exemple 2 : ligne datée hors de la période déclarée.** Elle passe en simple avertissement, donc elle est commitée. Cela arrive par exemple avec un paiement carte comptabilisé tardivement sous sa date d'opération. A couvre du 01/03 au 15/03 avec X daté 14/03 (n=1). B couvre du 16/03 au 31/03 et contient un second X daté 14/03, hors période (n=1).

```text
$ python3 -I /tmp/revue_g0/t_multiplicity.py
== M2 sous-comptage : dernier jour d'un export généré le jour même ==
vérité=2, base= 1 ; couverture déclarée 01/10->31/10 sans trou => mois 'complete'
== M3 sous-comptage : ligne hors période déclarée (OUT_OF_PERIOD = avertissement dans manifest) ==
vérité=2, base= 1 (max(1,1)=1 : un paiement réel perdu, aucune alerte de multiplicité)
```

Simulation utilisée : `commit(db, rows)`, soit `db[k] = max(db[k], Counter(rows)[k])`. C'est exactement la règle normative de `IMPORT_CONTRACT.md:59`.

**Impact.** INV-03 et INV-10 sont violés. Une opération réelle manque, alors que la couverture est continue et que le mois est marqué `complete` (ADR-008). Le contrôle de solde (T14) peut révéler l'écart, mais il est non bloquant et n'est pas relié au statut de complétude du mois. On atteint exactement ce que le plan veut éviter : un chiffre faux présenté comme sûr. Le risque R-14 ne couvre que les sections *sans plage* ou tronquées, pas ces deux cas.

**Corrections proposées (sans application) :**

1. Toute journée égale ou postérieure à la date de génération de l'export est marquée `partial`. Cette date vient du préambule si CA la fournit, sinon de la date de réception. Un import ultérieur doit **chevaucher** cette journée pour la compléter. L'aperçu le recommande explicitement : « recommencer l'export au dernier jour importé ».
2. Une ligne hors période déclarée devient une anomalie **bloquante pour sa clé de jour**. Elle reste avertissement pour l'affichage, mais elle est exclue du maximum automatique tant qu'une décision humaine auditée ne l'a pas tranchée. Le manifest et la règle du contrat doivent être alignés.
3. `month_completeness` doit passer à `unknown` lorsque le contrôle de solde d'un intervalle est `mismatch`.
4. T04 et T09 doivent établir, sur le vrai CSV (local, agrégats seulement), si la colonne `Date` est la date d'opération ou de comptabilisation. Ils doivent aussi compter les lignes datées hors de la période annoncée. Il faut consigner un ADR et ajouter ces cas à la matrice : IMP-11 pour la borne de génération, IMP-12 pour la ligne hors période.

### B-02 — Granularité de blocage contradictoire et idempotence par hash : des lignes deviennent irrécupérables

**Références** :
- `IMPORT_CONTRACT.md:78` : bloquer « les clés de jour concernées » ;
- `IMPORT_CONTRACT.md:82` : une erreur structurelle « bloque le commit de tout le fichier » ;
- `DECISIONS.md:91` (arbitrage ADR-012) : bloquer le commit « de la section concernée uniquement » ;
- `IMPORT_CONTRACT.md:88` : hash déjà commité → `no_op`, 0 `source_row` ;
- `schema.sql:37` : `status` sans état partiel.

**Contre-exemple (raisonnement sur le contrat).** Un fichier F contient deux sections. S2 n'a pas de plage, donc l'arbitrage ADR-012 n'en bloque que S2. S1 est commitée et F passe au statut `committed` avec son hash. Une fois la plage de S2 résolue, l'utilisateur réimporte F, et la règle `IMPORT_CONTRACT.md:88` en fait un `no_op` sans ligne. **Les opérations de S2 ne peuvent plus jamais être importées depuis ces octets.** Avec la règle « clés de jour ambiguës », c'est pire : un même jour peut être à moitié importé.

Le schéma ne peut pas représenter un commit partiel. Aucun test (IMP-06) ne précise laquelle des trois règles fait foi.

**Impact.** INV-04 (atomicité) devient indécidable. Une perte de données reste invisible derrière un `no_op` « réussi ». Ce point bloque G3, puisque IMP-01 et IMP-06 sont des tests bloquants.

**Correction proposée.** Une seule règle : **tout ou rien par fichier**. Un fichier qui contient une section ou une clé de jour non résolue n'est pas commité ; il reste `previewed`. La résolution humaine (plage, décision de multiplicité) a lieu **dans l'aperçu**, puis un unique `BEGIN IMMEDIATE` commite l'ensemble.

Si l'orchestrateur tient au commit partiel, il faut au minimum :
- un statut `partially_committed`, exclu de l'index unique des hashes ;
- une table des sections ou clés reportées ;
- un chemin de reprise qui ne passe pas par le `no_op`.

L'arbitrage ADR-012 doit être amendé dans un sens ou dans l'autre.

---

## MAJEURS

### M-01 — L'immutabilité dépend de `PRAGMA recursive_triggers` et `foreign_keys` sur chaque connexion

**Références** : `schema.sql:3-4` ; `ARCHITECTURE.md:30` exige seulement `foreign_keys=ON` ; `verify_schema.py:29-35` ne vérifie jamais `recursive_triggers`.

SQLite ne déclenche les triggers `BEFORE DELETE` lors d'un `INSERT OR REPLACE` que si `recursive_triggers=ON`. Ce PRAGMA est local à chaque connexion. Un pool SQLAlchemy ou une connexion Alembic qui ne le pose pas contourne `trg_source_rows_no_delete`.

```text
$ sqlite3 :memory: ".read docs/examples/schema.sql" "<seed: T1 + S1(I1,1,1)->T1>" "PRAGMA recursive_triggers=OFF;" \
   "INSERT T2…" "INSERT OR REPLACE INTO source_rows(…) VALUES('S9','I1',1,1,…,'T2');" "SELECT … FROM source_rows;"
source_rows=S9->T2                       # S1 supprimée sans erreur ; T1 n'a plus de provenance
$ python3 -I /tmp/revue_g0/t_replace_concurrency.py
== R1 PRAGMA oubliés (fk=OFF, recursive_triggers=OFF) ==
transactions: 1 | t1 encore là ? 0 | source_rows orphelines: 1    # transaction source remplacée
== R2 fk=ON mais recursive_triggers=OFF : REPLACE sur source_rows ==
s1 supprimée silencieusement ? True | t1 sans provenance : True
```

**Impact.** INV-01 et INV-08 tombent sur une simple erreur de configuration de connexion ou un `merge()` ORM.

**Correction.**
- Documenter dans ARCHITECTURE et IMPORT_CONTRACT qu'**il faut poser les deux PRAGMA à chaque `connect`**, par exemple avec un listener `connect` SQLAlchemy.
- Ajouter à `verify_schema.py` et aux tests T06 un test qui vérifie les deux PRAGMA sur une connexion du pool et qui contrôle que `INSERT OR REPLACE` est rejeté.
- Bannir `OR REPLACE` des dépôts de code (lint/grep).

Note pour T12 : avec le module `sqlite3` en mode par défaut, un `BEGIN IMMEDIATE` émis après un DML implicite échoue (`cannot start a transaction within a transaction`, R3). Le service doit utiliser `isolation_level=None` ou un hook `begin` qui émet `BEGIN IMMEDIATE`.

### M-02 — L'intégrité de la provenance n'est pas garantie par le schéma (dernière barrière de la multiplicité)

**Références** : `schema.sql:153-188`. PLAN §D demande une « UNIQUE contrôlée pour identité d'occurrence par réconciliation ».

```text
### R-B (sqlite3 CLI, schéma de référence + seed)
rang3_sans_1_2=1                 # rang 3 accepté sans rangs 1 et 2
tx_sans_source=1                 # transaction sans aucune source_row (INV-01)
source_section_A_vers_tx_B=1     # source d'une section du compte A -> transaction du compte B (INV-05)
T1_lie_2_fois_meme_import=2      # deux occurrences d'un même fichier liées au même rang
# t_schema.py S3 : [ACCEPTÉ] source_row rattachée à un import no_op / rejected ;
#                  [ACCEPTÉ] section committée sans account_id + source_row
```

**Impact.** Un défaut du service de réconciliation passerait inaperçu. Si le service relie les deux occurrences d'un fichier au rang 1, le rang 2 n'a pas de source, ou la multiplicité est perdue. Les mêmes failles permettent de mélanger les comptes et de violer la règle « no_op = 0 source_row » (`IMPORT_CONTRACT.md:88`).

**Correction : triggers `BEFORE INSERT ON source_rows`.**
- (a) Le `transactions.account_id` doit être égal au `account_id` de la section, non NULL.
- (b) `import_files.status` doit valoir `previewed` ou `committed`, jamais `no_op` ni `rejected`.
- (c) Ajouter `UNIQUE(import_file_id, transaction_id)`, valable tant qu'un même compte n'apparaît qu'une fois par fichier (voir M-05).

**Correction : trigger `BEFORE INSERT ON transactions`.** Exiger `occurrence_rank = 1` ou l'existence du rang `occurrence_rank − 1` pour la même identité, afin de garantir la contiguïté.

**Contrôle de cohérence de fin de commit**, dans le service et dans un test : toute transaction a au moins une `source_row`, et pour chaque import et chaque identité, le nombre de sources est égal à `n_new`.

### M-03 — Les métadonnées d'un import commité restent modifiables (couverture, hash, mapping)

**Références** : `schema.sql:24-75` et `schema.sql:480-489`.

```text
### R-C : UPDATE status->'previewed' ; UPDATE section (A->B, coverage_through) ; UPDATE status->'committed', file_sha256
section=B 2026-01-01..2026-12-31     # contournement de trg_committed_section_mapping_immutable
sha=00007                            # hash commité réécrit
# t_schema.py S4 : [ACCEPTÉ] DELETE section committée sans source_rows -> (0,)
#                  [ACCEPTÉ] committed->rejected puis ré-commit du même hash -> 2 imports
#                  [ACCEPTÉ] no_op dont duplicate_of pointe un import rejected / autre hash
```

**Impact.** La couverture, et donc `month_completeness`, le contrôle de solde et l'idempotence IMP-01, peuvent être falsifiés par une simple mise à jour applicative. Le trigger de mapping se contourne en deux requêtes.

**Correction.**
- Rendre `import_files` immuable une fois `status='committed'` : interdire tout UPDATE de `status`, `file_sha256`, `archive_*` et `file_size_bytes`, ainsi que le DELETE.
- Rendre `import_account_sections` immuable (toutes ses colonnes, plus le DELETE) dès que l'import est commité.
- Restreindre les transitions de statut à `previewed→committed|rejected`.
- Contrôler qu'un `no_op` a un `duplicate_of` qui pointe vers un import `committed` de même `file_sha256`.

### M-04 — « nombre/rang maximal » est ambigu et la contiguïté des rangs n'est pas garantie : trois comportements divergents

**Références** : `IMPORT_CONTRACT.md:56` ; `verify_schema.py:323-330`.

```text
== M5 rangs non contigus en base {1,3} puis fichier n_new=3 ==
lecture 'nombre'  : n_existing= 2 -> crée rangs [3] -> rang 3 en conflit UNIQUE => import entier en échec
lecture 'rang max': n_existing= 3 -> crée rien -> rang 2 jamais créé, 2 transactions pour vérité 3
verify_schema.run_export : boucle 1..max et comble le trou -> crée rang 2 (3e interprétation)
```

**Correction.** Définir `n_existing = max(occurrence_rank)` et imposer la contiguïté en SQL (voir M-02). Au moment du commit, signaler toute incohérence `count ≠ max` comme une anomalie bloquante.

### M-05 — Sur-comptage quand un même compte apparaît dans deux sections d'un même fichier

**Référence** : `IMPORT_CONTRACT.md:57`, où `n_new` est compté sur « le fichier courant ».

```text
== M4 sur-comptage : même compte présent dans 2 sections d'un même fichier ==
vérité=1, base= 2 (n_new compté sur le fichier entier, pas par section)
```

**Impact.** Un doublon est créé alors que le hash du fichier est neuf. INV-03 est violé.

**Correction.** Rejeter, comme anomalie structurelle, un fichier qui contient deux sections associées au même `account_id`. À défaut, calculer `n_new` comme le **maximum par section**, avec des périodes disjointes exigées. Ajouter une fixture adversariale.

### M-06 — Le schéma laisse les allocations violer INV-06 et INV-08 (somme, signe, mutation)

**Références** : `schema.sql:212-244`. PLAN §D impose des « contraintes de signes/centimes ».

```text
### R-E
T1 montant=-1123 somme_alloc=-2246                      # sur-allocation (consommation doublée)
TC credit=+5000 alloc=expense:-5000,transfer_savings:5000  # dépense négative sur un crédit ; flow_savings_out positif
update/delete allocation confirmée: OK                    # ni version, ni audit, ni trigger
```

**Impact.** `consumption_net` peut doubler. Un crédit peut porter une dépense. Une décision humaine confirmée peut être effacée sans trace. Le vérificateur des métriques contrôle ces règles (`verify_metrics_examples.py:180-182`), mais pas le schéma.

**Correction : triggers sur `allocations`.**
- `sign(allocation) = sign(transaction)`.
- `|Σ allocations| ≤ |amount|` et même signe. L'égalité exacte est vérifiée en fin de décision par le service ; à défaut, un trigger différé simulé par un contrôle de cohérence avant COMMIT.
- Signe par catégorie : `flow_savings_out < 0`, `flow_savings_in > 0`.
- UPDATE/DELETE interdits hors du chemin de décision. Il vaut mieux une allocation versionnée avec un `superseded_by` qu'une mise à jour en place.

### M-07 — Les métriques reposent sur un `flow_kind` « de transaction » absent du schéma : le résidu des transactions mixtes ou partielles est masqué

**Références** :
- `METRICS.md:21`, où l'épargne se calcule sur « −Σ transaction.amount_cents des jambes transfer_savings » ;
- `METRICS.md:11` et `METRICS.md:24`, qui parlent de « transactions pertinentes » ;
- `verify_metrics_examples.py:172`, qui impose un seul flux par transaction ;
- `verify_metrics_examples.py:309-314` et `:324`.

Le schéma ne porte `flow_kind` que sur les allocations. Prenons un virement de −100 000 dont seuls −20 000 sont confirmés en `transfer_savings` :

```text
== P4 résidu sur une transaction de flux 'non pertinent' ==
{'savings_net_transferred_cents': 100000, 'unclassified_amount_share_bps': 0, 'unclassified_absolute_residual_cents': 0,
 'operational_saving_capacity_cents': 300000, ...} | résidu réel: {'sal': 0, 'vir': -80000}
== P8 split épargne/dépense ==
formule METRICS (−Σ transaction.amount) = 20000 ; consommation = 5000 ; total sorties imputées = 25000 pour 20000 réellement sortis
```

**Impact.** Il y a trois effets :
- 800 € non ventilés sont **invisibles** : 0 bps, et la capacité est calculée au lieu de valoir `null`, ce qui contredit INV-10 et ADR-008 ;
- l'épargne compte 1 000 € au lieu des 200 € confirmés ;
- dans le cas d'un split, la même sortie est comptée deux fois.

**Correction.**
- Calculer `savings_net_transferred` sur les **allocations** confirmées `transfer_savings`, et non sur le montant de la transaction.
- Définir la pertinence au niveau du **résidu**. Tout résidu non nul est pertinent, puisque son flux est `unknown`. Le dénominateur vaut Σ|allocations pertinentes| + Σ|résidus|.
- Ajouter un exemple YAML avec une transaction multi-flux et une transaction partiellement ventilée sur un flux non pertinent.

### M-08 — Rattachement des liens de remboursement non défini en période et en compte ; le schéma n'impose aucun plafond

**Références** : `METRICS.md:19` ; `DOMAIN_MODEL.md:78` ; `verify_metrics_examples.py:302-305` (le lien compte si l'**avoir** est dans la sélection) ; `schema.sql:334-346`.

```text
== P5 remboursement de février lié à un achat de janvier (requête février seule) ==
{'consumption_net_cents': -2000, 'operational_saving_capacity_cents': 302000}   # consommation négative
== P6 avoir sur compte B lié à un achat du compte A ==
['cur'] consumption_net = 10000 ; ['perso'] consumption_net = -2000 ; ['cur','perso'] = 8000
### R-F
lien confirmé=999999 (avoir 2000, achat 1123, comptes B->A)                     # accepté par le schéma
# t_schema.py S7 : même avoir lié 2× (Σ 4000 > crédit 2000) accepté
```

**Impact.** La consommation par mois ou par compte devient négative ou incohérente. KPI-03 (deux vues, mêmes sommes) et KPI-07 (drilldown = total) ne sont pas garantis. Un sur-appariement réduit artificiellement la consommation et gonfle la capacité.

**Correction.** Décider par ADR de la date et du compte d'imputation du lien. La date de l'avoir est acceptable si elle est **documentée** et accompagnée d'un drilldown. On peut aussi exiger que l'achat et l'avoir soient dans le même périmètre de comptes, sinon le lien est ignoré avec un avertissement.

Dans le schéma :
- Σ liens `confirmed` par source ≤ montant de l'avoir ;
- Σ par cible ≤ |achat| ;
- la source a une allocation `refund` et est un crédit, la cible est un débit `expense`.

Seuls les liens `status='confirmed'` comptent. Le YAML n'a pas de statut de lien : il faut ajouter un exemple `proposed` qui ne compte pas.

### M-09 — Enveloppe personnelle et dépenses du compte personnel comptées deux fois

**Références** : PLAN §C2 (« jamais leur addition à de nouvelles opérations du même compte ») ; `METRICS.md:20` (« exclure la jambe… », sans algorithme) ; `DOMAIN_MODEL.md:26`.

```text
== P7 enveloppe personnelle + dépenses observées du compte personnel ==
{'consumption_net_cents': 18000, 'personal_allowances_cents': 25000, 'operational_saving_capacity_cents': 257000}
-> 180 € déduits deux fois (enveloppe 250 + dépenses 180)
```

**Impact.** La capacité est sous-estimée et la vue « coût de vie + allocations personnelles » est fausse dès que le compte personnel est importé, ce qui est un cas annoncé par le plan. Il n'existe aucun oracle.

**Correction.** Introduire un paramètre explicite `personal_mode ∈ {envelope, observed}` dans `filters`.
- En mode `envelope`, les dépenses des comptes de rôle `personal` sont exclues de `consumption_net`.
- En mode `observed`, `allocation_personal` est exclu, la jambe créditrice est `transfer_owned`, et le reliquat non dépensé est signalé.

Ajouter l'exemple YAML correspondant et un test KPI-03.

---

## MINEURS

### m-01 — L'affinité INTEGER accepte un texte décimal et un float entier

`schema.sql:157` :

```text
### R-D : INSERT … amount_cents='12.00'
amount_cents=12 type=integer          # 12,00 € stocké 12 centimes, CHECK typeof satisfait
# t_schema.py S5 : 1123.0 -> (1123,'integer') accepté ; '1,5' et 12.5 rejetés
```

Le contrôle `typeof` ne protège pas d'une erreur d'unité (× 100) ni d'un float de valeur entière. **Correction** : n'accepter dans le parseur et l'ORM que des `int` Python, avec un `type(x) is int` strict avant de lier la valeur. Ajouter un test.

### m-02 — Cas dégénérés de l'arrondi des splits (ADR-007)

```text
-1 50/50 : [('a', -1), ('b', 0)]                       -> part nulle (CHECK amount_cents<>0 la rejette)
-3 17x5+15 : [... ('e', -1), ('f', 2)]                 -> dernière part POSITIVE sur un débit
ordre de saisie 30/70 : [('groceries', -771), ('restaurants', -330)]  (sortie dans l'ordre trié)
```

**Correction.** Préciser qu'un split produisant une part nulle ou de signe opposé est rejeté atomiquement, ou adopter la méthode du plus fort reste. Préciser que le résultat est rendu **par catégorie**, sans dépendre de l'ordre. Définir le départage quand deux parts ont la même catégorie.

### m-03 — `identity_label` : « blancs Unicode » imprécis

```text
U+001C isspace=True  catégorie Cc (non White_Space Unicode) ; U+200B/U+FEFF/U+180E : non blancs (Cf)
ZWSP collapse -> 'PAIEMENT​X' != 'PAIEMENT X'
```

Le schéma accepte aussi un `identity_label` non normalisé (espaces de bord, NFD) et un `identity_key_sha256` incohérent (`t_schema.py`, S8). **Correction** :
- fixer l'ensemble exact des blancs, par exemple la propriété Unicode `White_Space` ;
- traiter les caractères `Cf` (ZWSP, BOM interne) comme une anomalie d'aperçu ;
- ajouter les CHECK `identity_label = trim(identity_label)` et `instr(identity_label,'  ')=0`.

### m-04 — `verify_schema.py` ne couvre pas ce qu'il revendique

- Le hash est lu **hors** de `BEGIN IMMEDIATE` (`verify_schema.py:282-287`). En course, le second import obtient une `IntegrityError` et non le `no_op` promis par `IMPORT_CONTRACT.md:96`.
- Aucun test ne couvre IMP-06 (rollback), IMP-07 (deux connexions), DELETE de transactions ou de sources, `OR REPLACE`, ni le blocage par couverture : la section est toujours `2026-01-01..2026-12-31` (`verify_schema.py:113-118`).

**Correction** : lire le hash sous le verrou et ajouter ces cas.

### m-05 — Montant nul

`IMPORT_CONTRACT.md:38` (« peut être conservé si le parseur la juge valide ») contredit `manifest.yaml` (`ZERO_AMOUNT` → rejeté). **Correction** : trancher ; le rejet pour CA est cohérent avec la fixture.

### m-06 — Oracles manquants ou noms divergents

- L'effet « courses nettes 480 » de S11, c'est-à-dire l'imputation à la catégorie d'origine, n'est pas asserté.
- Le « flux −80 € sur la paire » de MONEY-03 non plus.
- Le type de lien diffère : `purchase_refund` dans le YAML, `refund_of` dans le schéma et l'API.

**Correction** : ajouter `category_net_cents` et `pair_bank_flow_cents`, puis aligner le vocabulaire.

---

## Conformité aux invariants et aux ADR

| Invariant / ADR | État | Constat |
|---|---|---|
| INV-01 source vérifiable | ⚠ | M-01, M-02 (transaction sans source, `REPLACE`) |
| INV-02 centimes exacts | ✅ / ⚠ | Formules et arrondis exacts ; m-01 |
| INV-03 non-duplication | ❌ | B-01 (sous-comptage), M-05 (sur-comptage) |
| INV-04 atomicité | ❌ | B-02 (granularité contradictoire) |
| INV-05 multi-comptes | ⚠ | M-02 (source A → transaction B), M-08 (liens inter-comptes) |
| INV-06 somme des allocations | ⚠ | M-06 (non garantie en SQL), M-07 |
| INV-07 flux distincts | ✅ | Prêt, capital, espèces et intermédiaire correctement isolés (S09, S10, S12, MONEY-04/05) |
| INV-08 priorité de l'humain | ⚠ | M-06 (allocations confirmées mutables sans trace) |
| INV-09 justification | ⚠ | M-08 (drilldown du lien inter-période) |
| INV-10 incertitude visible | ❌ | M-07 (résidu masqué), B-01 (mois `complete` à tort) |
| INV-11 confidentialité | ✅ | HMAC, archive `0600`, aucun numéro en base (non approfondi ici) |
| INV-12/13 | hors périmètre de cette revue | — |
| INV-14 réversibilité | ⚠ | M-03 (métadonnées d'import modifiables sans audit) |
| ADR-007 | ✅ / ⚠ | Exemples exacts ; m-02 |
| ADR-008 | ⚠ | Capacité non `null` malgré un résidu (M-07) |
| ADR-009 | ✅ | Conforme dans METRICS, le YAML et le vérificateur |
| ADR-010 | ✅ | L'arbitrage « pas de chiffrement v1 » est reflété dans le contrat |
| ADR-011 | ✅ | Clé prudente ; m-03 |
| ADR-012 | ❌ | B-01 (hypothèse non garantie), B-02 (contradiction avec le contrat) |

## Reproduction

1. Recréer les scripts jetables à partir des extraits ci-dessus. Ils sont éphémères dans `/tmp/revue_g0/` : `common.py`, `t_schema.py`, `t_replace_concurrency.py`, `t_multiplicity.py` et `t_metrics.py`.
2. Les reproductions `sqlite3` R-A à R-F sont autonomes. Il suffit de charger `.read docs/examples/schema.sql`, puis d'insérer le seed minimal : comptes `A` et `B`, import `I1` commité, section 1 sur le compte A, transaction `T1` (−1123) et `S1 → T1`.
3. Commandes exécutées pendant la revue :
   - `python3 scripts/verify_metrics_examples.py` → exit 0
   - `python3 scripts/verify_schema.py` → exit 0
   - `python3 -I /tmp/revue_g0/t_schema.py`
   - `python3 -I /tmp/revue_g0/t_replace_concurrency.py`
   - `python3 -I /tmp/revue_g0/t_multiplicity.py`
   - `python3 -I /tmp/revue_g0/t_metrics.py`
   - les reproductions `sqlite3` R-A à R-F
   - `EXPLAIN QUERY PLAN` sur les 5 requêtes clés

## Propositions pour les documents de suivi (arbitrage par l'orchestrateur)

- **REGISTRE_RISQUES** :
  - élargir R-14 aux bornes d'export générées le jour même et aux lignes hors période (B-01) ;
  - ajouter un risque « PRAGMA par connexion / contournement par REPLACE » (M-01) ;
  - ajouter un risque « double comptage enveloppe / compte personnel » (M-09).
- **DECISIONS** :
  - amender l'arbitrage ADR-012 (B-02) ;
  - créer un nouvel ADR « imputation des liens de remboursement » (M-08) ;
  - créer un nouvel ADR « pertinence au niveau du résidu, épargne sur les allocations » (M-07) ;
  - créer un nouvel ADR « `personal_mode` » (M-09).
- **MATRICE** : IMP-11 (borne de génération), IMP-12 (ligne hors période), IMP-13 (même compte dans deux sections), MONEY-10 (lien inter-mois ou inter-comptes), KPI-08 (compte personnel importé).
