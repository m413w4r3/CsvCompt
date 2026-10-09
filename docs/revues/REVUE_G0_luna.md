# Revue critique indépendante G0 — Comptes familiaux

**Relecteur :** QA indépendant · GPT-6 Luna xhigh · **Date :** 2026-10-10  
**Périmètre :** tickets T00–T04, contrats et validateurs demandés.  
**Verdict : G0 BLOCK** — l’unité d’atomicité de l’import est contradictoire entre décisions, contrat, schéma et API. G0 exige que les invariants d’import et la multiplicité soient non ambigus avant la suite. Les autres constats majeurs doivent être arbitrés avant la migration et l’implémentation concernées.

## Méthode et limites

Lecture de [AGENTS.md](/home/nill/Desktop/compte/mescompts/AGENTS.md), du plan, du backlog, de la matrice, du protocole LLM et de la taxonomie source, puis des contrats/domaines, ADR, exemples SQL/YAML, fixtures et scripts listés dans la demande. Aucun contenu de `csv_references/` ni de `data/` n’a été lu. Les contrôles qui inspectent des CSV n’ont lu que `backend/tests/fixtures/`, validé comme synthétique. Aucun fichier existant n’a été modifié; seuls les deux rapports demandés sont créés.

Les vérificateurs des livrables G0 ne sont pas une preuve que l’application est implémentée. La matrice marque initialement ses cas « À réaliser »; les tests applicatifs et le parseur bancaire ne sont pas livrés par T00–T04.

## Résumé des vérifications croisées

| Sujet | Résultat |
|---|---|
| `flow_kind` | Les 14 valeurs de [DOMAIN_MODEL.md](/home/nill/Desktop/compte/mescompts/docs/DOMAIN_MODEL.md:30) correspondent à l’API. Le schéma autorise les 13 flux ayant une catégorie; `unknown` est sans catégorie, comme prévu. Le schéma ne porte toutefois pas de `flow_kind` sur `transactions`, ce qui rend le filtre des opérations non classées ambigu (F-02). |
| Catégories | Recompte indépendant : 87 IDs uniques dans `categories.v1.yaml`; la matrice de `DOMAIN_MODEL.md` couvre les 87 sans manque ni extra. Les CHECK SQL autorisent les mêmes 13 flux classables. La table de couples catégorie/flux n’est pas peuplée dans le schéma de référence et n’est pas comparée par `verify_schema.py`; son chargement/parité doit être garanti par T16 (F-07). |
| Métriques | Les 11 IDs de `METRICS.md` sont canoniques. Les références trouvées dans `API.md` et `metrics_examples.yaml` sont des sous-ensembles exacts, sans ID inconnu. Les exemples donnent `bank_net_flow.v1`; le schéma n’impose aucun registre/contrôle d’ID et le modèle `insights` ne s’aligne pas clairement sur les faits multiples de l’API (F-04). |
| Formules et oracles | Les conventions d’entiers, remboursement lié, capacité, incertitude et arrondis correspondent entre contrat, matrice et exemples. Les trois oracles indépendants ont passé après correction d’une erreur dans mon premier brouillon de script, décrite plus bas. |
| ADR-005..009, 011..016 | Les contrats d’allocations, priorités, arrondis, couverture, résidus, identité prudente, multiplicité, UUID, curseurs, idempotence et migrations correspondent globalement à leurs ADR. L’exception principale est ADR-012 sur les sections partielles (F-01). |
| ADR-010 / chiffrement | **Aucun chiffrement des archives n’est exigé par les contrats actifs** : stockage brut bit à bit, permissions 0600, pas de chiffrement applicatif dans `IMPORT_CONTRACT.md`, `schema.sql` et `ARCHITECTURE.md`. `DECISIONS.md` conserve le texte de l’ancienne proposition AES-256-GCM, mais la refuse explicitement ensuite; seule sa présentation historique est à clarifier (F-08). La protection des sauvegardes et de la clé HMAC reste distincte et non spécifiée (F-11). |
| Périmètre et moteur | Pas de connexion bancaire, cloud ni fonction v1 hors plan repérée. LLM facultatif, désactivé sans opt-in, texte souple et suggestions uniquement; les métriques sont calculées par le backend déterministe. Clé fournisseur réservée au backend. |

**Nombre de constats : 1 BLOQUANT · 5 MAJEURS · 6 MINEURS.**

### Fixtures

Les fichiers et le manifeste s’accordent sur les 28 chemins. Recompte indépendant par `csv.reader` et conversion entière en centimes, sur trois fixtures choisies :

| Fixture | Sections | Résultat du recompte indépendant |
|---|---:|---|
| `ca_2026_simple.csv` | 1 | concorde avec le manifeste : nombre d’opérations, débits/crédits, dates extrêmes |
| `ca_2026_multi_sections.csv` | 2 | concorde avec le manifeste pour les deux sections |
| `overlap_A.csv` | 1 | concorde avec le manifeste : nombre d’opérations, débits/crédits, dates extrêmes |

Contrôle indépendant de confidentialité : 0 fichier de fixture ne contient le motif d’un IBAN français ou une suite de 8 chiffres ou plus. `verify_fixtures.py` passe aussi sur les 28 fixtures et confirme manifeste, octets, anomalies et confidentialité.

Couverture réelle des entrées :

- `CSV-01` : CP1252 avec accents et variante UTF-8/NBSP présentes.
- `CSV-02` : champ CSV cité contenant `;` et retour à la ligne effectivement présent dans `ca_2026_simple.csv`.
- `CSV-03` : deux sections et deux en-têtes présents dans `ca_2026_multi_sections.csv`.
- `CSV-04` : cas adversariaux présents, dont date/montants/champs invalides, guillemet non fermé, fichier vide et en-tête manquant.
- `CSV-05` : les fichiers donnent des entrées d’aperçu, mais ne prouvent pas l’absence d’écriture. `docs/FIXTURES.md` le dit explicitement; ce comportement reste à tester par T11.
- `IMP-01..05`, `IMP-08`, `IMP-09` : A/B/C fournissent réimport, recouvrement et delta, doublons légitimes, troisième occurrence, ordre inversé, variation de casse et ligne ancienne. Le vérificateur SQL démontre plusieurs cas sur un modèle simulé; il n’exécute pas encore le futur service d’import.

Contrôle indépendant des recouvrements : A et B ont des identités communes et des nouveautés; A contient une multiplicité ≥2; B contient une identité passant de deux à trois occurrences; C égale l’union par maximum de multiplicité (34 lignes); une variation de casse à date/montant identiques existe entre A et B; B contient une nouvelle ligne plus ancienne que la date maximale de A.

Sortie brute compacte du recompte indépendant et du contrôle de confidentialité :

```text
fixtures_manifest_file_set_equal=True; csv_files=28; manifest_entries=28
private_pattern_files=0
independent_manifest_recount ca_2026_simple.csv: sections=1 expected=1 match=True
independent_manifest_recount ca_2026_multi_sections.csv: sections=2 expected=2 match=True
independent_manifest_recount overlap_A.csv: sections=1 expected=1 match=True
IMP02 overlap_and_delta=(True, True)
IMP03 duplicate_in_A=True
IMP04 third_identical_occurrence_in_B=True
IMP05 C_matches_max_multiplicity=True; C_rows=34; expected_rows=34
IMP08 case_variant_same_date_amount_present=True
IMP09 older_new_record_in_B=True; B_max_date=2026-05-30
```

## Constats

### F-01 — BLOQUANT — la granularité de l’atomicité de l’import se contredit

**Preuve.** [DECISIONS.md:91](/home/nill/Desktop/compte/mescompts/docs/DECISIONS.md:91) dit qu’une plage absente ou une troncature bloque « la section concernée uniquement ». À l’inverse, [IMPORT_CONTRACT.md:82](/home/nill/Desktop/compte/mescompts/docs/IMPORT_CONTRACT.md:82) fait bloquer tout le fichier sur une erreur structurelle ou monétaire, et [IMPORT_CONTRACT.md:94](/home/nill/Desktop/compte/mescompts/docs/IMPORT_CONTRACT.md:94) décrit un commit unique `BEGIN IMMEDIATE` couvrant sections et écritures. Le schéma [schema.sql:37](/home/nill/Desktop/compte/mescompts/docs/examples/schema.sql:37) n’a qu’un statut au niveau `import_files`; `import_account_sections` n’a pas d’état de commit/rejet par section ([schema.sql:56](/home/nill/Desktop/compte/mescompts/docs/examples/schema.sql:56)). L’API ne définit pas de résultat `partial` ([API.md:81](/home/nill/Desktop/compte/mescompts/docs/API.md:81), [API.md:94](/home/nill/Desktop/compte/mescompts/docs/API.md:94)).

**Correction proposée.** Arbitrer avant G1/T06/T09–T13 l’unité transactionnelle. Pour respecter l’atomicité fichier et garder le modèle existant, une section ambiguë devrait rejeter le fichier entier, avec aucune transaction ni `source_row` publiée. Si le commit des sections saines est souhaité, il faut au contraire un état partiel explicite, un résultat d’API section par section et un oracle de test dédié; cette option change le sens attendu d’INV-04 et doit être approuvée formellement.

### F-02 — MAJEUR — `flow_kind` d’une opération non classée n’a pas de représentation contractuelle

**Preuve.** Le domaine définit un flux pour chaque opération et inclut `unknown` ([DOMAIN_MODEL.md:30](/home/nill/Desktop/compte/mescompts/docs/DOMAIN_MODEL.md:30), [DOMAIN_MODEL.md:34](/home/nill/Desktop/compte/mescompts/docs/DOMAIN_MODEL.md:34)); l’API annonce un filtre sur les 14 flux ([API.md:18](/home/nill/Desktop/compte/mescompts/docs/API.md:18)). Or `transactions` ne possède pas de colonne `flow_kind` ([schema.sql:153](/home/nill/Desktop/compte/mescompts/docs/examples/schema.sql:153)); seules les allocations confirmées et les suggestions portent ce champ ([schema.sql:212](/home/nill/Desktop/compte/mescompts/docs/examples/schema.sql:212), [schema.sql:248](/home/nill/Desktop/compte/mescompts/docs/examples/schema.sql:248)). Une transaction sans allocation ni suggestion ne peut donc pas être filtrée sans règle de dérivation implicite.

**Correction proposée.** Décider si le flux courant est une propriété mutable de transaction ou s’il est dérivé. Si dérivé, définir précisément `unknown` en l’absence de décision, les transactions partiellement ventilées et l’unicité du flux entre allocations; aligner filtre API, agrégats et drilldown. Faire porter l’arbitrage à T02/T06 puis le test à T17/T24.

### F-03 — MAJEUR — le signe de `transfer_savings` dépend de la catégorie mais le schéma ne le valide pas

**Preuve.** Le domaine impose `flow_savings_out` négatif et `flow_savings_in` positif ([DOMAIN_MODEL.md:39](/home/nill/Desktop/compte/mescompts/docs/DOMAIN_MODEL.md:39), [DOMAIN_MODEL.md:60](/home/nill/Desktop/compte/mescompts/docs/DOMAIN_MODEL.md:60)). La contrainte d’allocation SQL accepte pourtant les deux signes pour `transfer_savings` ([schema.sql:238](/home/nill/Desktop/compte/mescompts/docs/examples/schema.sql:238), [schema.sql:243](/home/nill/Desktop/compte/mescompts/docs/examples/schema.sql:243)). Le plan demande des contraintes de signes en base ([PLAN_ORCHESTRATION.md:167](/home/nill/Desktop/compte/mescompts/.nimbalyst/plan_implementation_comptes_familiaux/PLAN_ORCHESTRATION.md:167)).

**Correction proposée.** Ajouter une validation SQL (trigger) liée à la catégorie ou une forme relationnelle qui exprime la direction, puis tester les deux combinaisons erronées. Une validation de service seule devrait être explicitement arbitrée comme exception à la contrainte DB du plan.

### F-04 — MAJEUR — la table `insights` ne représente pas clairement les faits métriques de l’API

**Preuve.** L’API expose `facts[]`, et chaque fait porte son propre `metric_definition_id` ([API.md:250](/home/nill/Desktop/compte/mescompts/docs/API.md:250), [API.md:253](/home/nill/Desktop/compte/mescompts/docs/API.md:253)). Le schéma conserve une seule colonne `insights.metric_definition_id` plus `facts_json` ([schema.sql:417](/home/nill/Desktop/compte/mescompts/docs/examples/schema.sql:417), [schema.sql:420](/home/nill/Desktop/compte/mescompts/docs/examples/schema.sql:420)); aucune relation ou contrainte n’explique si cette colonne est principale, unique ou redondante. Elle n’est pas non plus liée au registre des 11 IDs canoniques.

**Correction proposée.** Soit définir un insight mono-métrique et restreindre `facts[]`, soit ajouter une table `insight_facts` avec un ID canonique par fait et les références nécessaires au drilldown. T06/T38 doivent partager cette décision.

### F-05 — MAJEUR — le test CSV réel opt-in est encore un squelette

**Preuve.** [TEST_DATA_PRIVATE.md:21](/home/nill/Desktop/compte/mescompts/docs/TEST_DATA_PRIVATE.md:21) qualifie le test pytest de squelette; sa fonction appelle `parse_csv`, explicitement à remplacer par l’interface T09 ([TEST_DATA_PRIVATE.md:31](/home/nill/Desktop/compte/mescompts/docs/TEST_DATA_PRIVATE.md:31), [TEST_DATA_PRIVATE.md:36](/home/nill/Desktop/compte/mescompts/docs/TEST_DATA_PRIVATE.md:36)). Le seul self-test exécuté utilise des fixtures synthétiques ([tests_real_csv_profile_selftest.py:19](/home/nill/Desktop/compte/mescompts/scripts/tests_real_csv_profile_selftest.py:19), [tests_real_csv_profile_selftest.py:75](/home/nill/Desktop/compte/mescompts/scripts/tests_real_csv_profile_selftest.py:75)); il ne teste pas `parse_csv` ni un test marqué `opt_in_real_data`.

**Correction proposée.** Ne pas déclarer le sous-critère CSV-06 implémenté avant T09. À T09, ajouter le vrai test marqué `opt_in_real_data`, avec skip explicite sans `COMPTES_REAL_CSV`, assertions limitées aux agrégats convenus et preuve locale sans sortie de libellé. Le self-test synthétique actuel est utile mais ne remplace pas ce test.

### F-06 — MAJEUR — stratégie de conservation/restauration de la clé HMAC non définie

**Preuve.** La clé HMAC est dite secrète et stockée dans `app_settings` ([IMPORT_CONTRACT.md:12](/home/nill/Desktop/compte/mescompts/docs/IMPORT_CONTRACT.md:12)). En SQL, `setting_value` est du texte et `is_secret` n’est qu’un booléen ([schema.sql:366](/home/nill/Desktop/compte/mescompts/docs/examples/schema.sql:366), [schema.sql:369](/home/nill/Desktop/compte/mescompts/docs/examples/schema.sql:369)). Le contrat demande que la sauvegarde/restauration préserve cette clé sous « chiffrement/protection du backup » ([IMPORT_CONTRACT.md:16](/home/nill/Desktop/compte/mescompts/docs/IMPORT_CONTRACT.md:16)), tandis que l’API de backup ne dit pas si elle l’embarque ni comment elle la protège ([API.md:275](/home/nill/Desktop/compte/mescompts/docs/API.md:275), [API.md:278](/home/nill/Desktop/compte/mescompts/docs/API.md:278)).

**Correction proposée.** Consigner avant T06/T41 une politique précise : stockage local de la valeur secrète, inclusion ou non dans un backup, protection du backup et voie de remappage si la clé est perdue. Ne pas confondre cette question avec le chiffrement des archives CSV, explicitement refusé pour v1.

### F-07 — MINEUR — le jeu de couples catégorie/flux est attendu mais absent du schéma exécutable

**Preuve.** `category_flow_kinds` impose une FK composite, mais `schema.sql` ne contient que la définition de table ([schema.sql:140](/home/nill/Desktop/compte/mescompts/docs/examples/schema.sql:140)); aucune ligne de taxonomie n’est insérée. `verify_metrics_examples.py` dérive son mapping depuis le YAML source, mais `verify_schema.py` ne compare pas ce mapping aux lignes réellement chargées dans la base de test.

**Correction proposée.** Faire de T16 le propriétaire explicite du seed versionné à partir de `categories.v1.yaml` et ajouter un test de parité qui vérifie les 87 IDs et toutes les paires admises de DOMAIN_MODEL contre les lignes DB. L’absence de seed ne bloque pas une migration vide G1, mais la parité est exigée avant G4.

### F-08 — MINEUR — ADR-010 laisse une proposition crypto historique visuellement active

**Preuve.** L’en-tête dit encore « proposée pour arbitrage G0 » et les lignes [DECISIONS.md:74](/home/nill/Desktop/compte/mescompts/docs/DECISIONS.md:74)–[DECISIONS.md:75](/home/nill/Desktop/compte/mescompts/docs/DECISIONS.md:75) parlent d’archives chiffrées; [DECISIONS.md:76](/home/nill/Desktop/compte/mescompts/docs/DECISIONS.md:76) refuse explicitement AES-256-GCM pour v1. Les contrats d’import et d’architecture actifs disent bien archive brute non chiffrée.

**Correction proposée.** Marquer les deux paragraphes AES comme proposition historique refusée et retirer le qualificatif « proposée pour arbitrage » de l’en-tête. Aucun changement de stockage n’est demandé par ce constat.

### F-09 — MINEUR — quelques invariants n’ont qu’une couverture partielle par leurs IDs de matrice

**Preuve.** Pour INV-01, `IMP-02` vérifie la provenance, mais ne vérifie pas l’immutabilité source. Le contrôle actuel d’UPDATE est dans un vérificateur de schéma, sans ID de matrice. Pour INV-08, `RULE-01` et `FULL-01` ne détaillent pas chacun les effets combinés d’un import, d’un nouveau prompt, d’une règle et d’un recalcul. Pour INV-09, `KPI-07` compare le total du drilldown, sans oracle exhaustif des exclusions et formules par endpoint.

**Correction proposée.** Étendre les scénarios existants sans relâcher leurs oracles : assertion des champs bruts/provenances dans `IMP-02`; décision humaine inchangée après re-import et recalcul dans `FULL-01`; même formule, filtres et exclusions dans `KPI-07`. Le critère binaire G0 reste couvert, mais la matrice ne décrit pas encore toutes ces preuves.

### F-10 — MINEUR — ligne du filtre `mode` mal formée dans le tableau API

**Preuve.** Le tableau API a trois colonnes, mais sa ligne `mode` fournit quatre cellules ([API.md:20](/home/nill/Desktop/compte/mescompts/docs/API.md:20)). `treasury` et `family_life` peuvent être interprétés comme deux colonnes distinctes plutôt qu’un enum.

**Correction proposée.** Écrire explicitement `mode | enum | treasury ou family_life; lecture trésorerie ou vie familiale`.

### F-11 — MINEUR — politique de rétention des clés d’idempotence absente

**Preuve.** ADR-015 annonce l’expiration des entrées selon une politique documentée ([DECISIONS.md:112](/home/nill/Desktop/compte/mescompts/docs/DECISIONS.md:112)); API et table `idempotency_records` ne définissent ni durée, ni suppression, ni borne de croissance ([API.md:36](/home/nill/Desktop/compte/mescompts/docs/API.md:36), [schema.sql:448](/home/nill/Desktop/compte/mescompts/docs/examples/schema.sql:448)).

**Correction proposée.** Documenter une durée de rejeu et une purge sûre, ou amender ADR-015 pour dire que les clés persistent. Garder le rejeu des mutations créatrices sûr pendant toute la durée retenue.

### F-12 — MINEUR — la matrice donne des rôles suggérés, pas des propriétaires attribués

**Preuve.** Le backlog s’annonce « compléter statut + propriétaire sur le vrai dépôt » ([backlog.yaml:1](/home/nill/Desktop/compte/mescompts/.nimbalyst/plan_implementation_comptes_familiaux/backlog.yaml:1)) et les tickets G0 renseignent `suggested_role` ([backlog.yaml:59](/home/nill/Desktop/compte/mescompts/.nimbalyst/plan_implementation_comptes_familiaux/backlog.yaml:59)), sans champ d’owner affecté. La table de [TRACABILITE_INVARIANTS.md](/home/nill/Desktop/compte/mescompts/docs/TRACABILITE_INVARIANTS.md) satisfait le critère si un rôle compte comme propriétaire, pas si une personne doit être attribuée.

**Correction proposée.** Confirmer que le propriétaire G0 est un rôle (ARCH/DATA/QA/etc.) ou renseigner l’owner nominatif prévu par le backlog. Aucun nom n’a été inventé par cette revue.

## Faisabilité avant G1

À arbitrer ou consigner explicitement avant de figer les modèles/migrations :

1. **F-01 :** atomité de l’import par fichier ou par section; statuts et réponse API correspondants.
2. **F-02 :** stockage ou dérivation du `flow_kind` courant, en particulier `unknown`, flux partiels et filtres.
3. **F-03 :** vérification du signe des catégories directionnelles au niveau DB ou exception explicitement justifiée.
4. **F-04 :** cardinalité entre insight, faits et `metric_definition_id`; validation contre les définitions canoniques.
5. **F-06 :** stockage et cycle de vie de la clé HMAC lors de backup/restore.
6. **T06/T16 :** chargement versionné des 87 catégories et de toutes les paires catégorie/flux; test de parité domaine–DB avant G4.
7. **T12/T13 :** respecter les limites d’import partielles décidées en F-01 tout en conservant maximum de multiplicité, rangs contigus, source par occurrence et ordre inversé.

## Exécution des vérificateurs demandés

Les sorties ci-dessous sont les sorties brutes. Aucune donnée réelle n’a été utilisée.

### `python3 -I scripts/verify_metrics_examples.py`

```text
PASS: 13 oracles métriques, 4 splits et 2 cas de priorité vérifiés en centimes entiers.
```

### `python3 -I scripts/verify_schema.py`

```text
OK FK-01 compte inexistant rejeté
OK IMP-03 occurrence rank 1 en double rejeté
OK IMP-03 même identité avec ranks 1 et 2 acceptée
OK INV-01 UPDATE du libellé source transaction rejeté
OK INV-01 UPDATE de source_rows rejeté
OK INV-02 montant SQLite REAL refusé
OK INV-11 chemin relatif data/originals, mode raw et permissions 0600
OK INV-11 chemin d'archive hors du répertoire autorisé rejeté
OK INV-11 mode de stockage autre que raw rejeté en v1
OK INV-11 permissions d'archive différentes de 0600 rejetées
OK IMP-01 hash de fichier commité en doublon rejeté
OK FK rule_executions.audit_event_id orphelin rejeté
OK IMP-01..05 A/B/A/B/A => transactions [3, 6, 6, 6, 6], ranks 1..3
OK IMP-05 B puis A et ordre de lignes inversé => même signature de transactions
OK IMP-09 une opération plus ancienne dans B est créée malgré la date max déjà importée
OK INV-01 chaque occurrence de A+B garde sa source_rows (9 sources → 6 transactions)
PASS verify_schema
```

### `python3 -I backend/tests/fixtures/verify_fixtures.py`

```text
OK : 28 fixtures vérifiées, manifest concordant, confidentialité vérifiée.
```

### `python3 -I scripts/tests_real_csv_profile_selftest.py`

```text
test_CSV06_profile_aggregates_match_fixture_oracles (__main__.RealCSVProfileSelfTest.test_CSV06_profile_aggregates_match_fixture_oracles) ... ok
test_SEC04_errors_do_not_disclose_input_rows (__main__.RealCSVProfileSelfTest.test_SEC04_errors_do_not_disclose_input_rows) ... ok
test_SEC04_no_fixture_label_is_printed (__main__.RealCSVProfileSelfTest.test_SEC04_no_fixture_label_is_printed) ... ok
test_opt_in_skip_when_environment_unset (__main__.RealCSVProfileSelfTest.test_opt_in_skip_when_environment_unset) ... ok

----------------------------------------------------------------------
Ran 4 tests in 0.799s

OK
```

### `python3 -I .nimbalyst/plan_implementation_comptes_familiaux/scripts/validate_plan.py`

```text
OK dossier : 46 tickets, 12 gates, 87 IDs de catégories, 81 tests référencés.
```

### `./scripts/check_no_private_data.sh --with-untracked`

Commande relancée après la création des deux livrables :

```text
OK : aucune donnée privée détectée (65 fichier(s) inspecté(s)).
```

## Oracles numériques contrôlés indépendamment

Un mini-script temporaire créé sous `/tmp`, sans import du vérificateur projet et supprimé après exécution, a recompté les valeurs à partir d’entiers. La première version du script avait omis restaurant et garde, et a échoué sur l’assertion de capacité (`477000` au lieu de `435000`); le script de contrôle a été corrigé avant le résultat ci-dessous. Il s’agissait d’une erreur dans mon oracle jetable, pas d’un écart des documents.

```text
flux bancaire: -4416561 centimes
capacité opérationnelle: 435000 centimes
split -1101 centimes à 70/30: -771 / -330 centimes; somme=-1101 centimes
```

Le résultat indépendant concorde avec les oracles de la matrice : **−44 165,61 €**, **4 350,00 €**, puis **−11,01 € → −7,71 € / −3,30 €**.

## Conformité aux invariants non négociables

- **Centimes exacts :** contrats et vérificateurs utilisent des entiers; vérification du REAL refusé, des splits et des oracles passée.
- **Import idempotent et doublons légitimes :** contrat par maximum de multiplicité et tests de schéma synthétiques exécutés; la résolution F-01 reste bloquante.
- **Pas de continuité 2025/2026 :** démarrage 2026 et 2025 non démarré sont cohérents entre ADR-003, couvertures et KPI-05.
- **Flux séparés :** concordance métier/API; F-02 et F-03 signalent les écarts de représentation/contrainte SQL.
- **Priorité humaine et mémoire :** domaine/protocole placent l’humain avant règle, historique, heuristique et LLM; mémoire exacte suggère et nécessite acceptation.
- **Vie privée :** aucun fichier privé lu; les 28 fixtures passent leurs garde-fous et le contrôle Git passe. La clé HMAC n’est jamais destinée au frontend ou aux réponses; sa protection des backups reste à préciser (F-06).
- **Sans LLM / sans JSON strict :** mode désactivé et texte souple sont explicites; erreurs partielles passent en revue, sans écriture automatique.
- **Métriques déterministes :** formules backend avec IDs canoniques; le LLM ne fournit que du texte lié à des faits.

## Verdict final

**G0 BLOCK (F-01).** Les vérificateurs exécutables sont verts, les 87 IDs de catégories sont couverts par la matrice et chaque invariant est relié à un test prévu et à un rôle suggéré. Toutefois, les décisions sur l’import sectionnel et le contrat d’import en une transaction s’opposent; or le G0 décrit lui-même multiplicité et invariants d’import non ambigus comme condition de sortie. Ne pas figer les modèles/migrations d’import avant cet arbitrage. Les constats majeurs F-02 à F-06 restent à fermer avant les tickets concernés.
