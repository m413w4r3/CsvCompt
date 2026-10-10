# Revue indépendante T19

Date : 2026-10-11 · Base relue : commit `9e680befd5255748bd554eac176cb2046e489418` · Périmètre : G4, T19

## Verdict : BLOCK

Le comportement de lot partiellement appliqué est cohérent au niveau de chaque opération et rejouable. Dans le commit relu, son acceptation métier reste toutefois une proposition ADR-029, sans arbitrage accepté. Le gate ne peut pas être déclaré PASS tant que la politique de lot n’est pas explicitement acceptée ou remplacée par une exigence d’atomicité du lot.

## Constats classés

### B-01 — MAJEUR, blocage décisionnel : politique de lot non arbitrée

`apply_rule()` valide chaque opération dans sa propre transaction SQLite et y écrit allocation, décision/audit et trace. Une panne de trace annule l’opération courante; celles déjà validées restent présentes. `docs/RULES.md` décrit ce fonctionnement et le rejeu, mais l’intitule « proposition ADR-029 »; aucune ADR-029 acceptée n’existe dans le registre des décisions.

Preuve synthétique exécutée : injection d’une panne de trace sur la deuxième opération d’un lot de deux. Après l’échec, la première allocation et sa trace étaient présentes, la seconde opération était non ventilée et sans trace. Au rejeu, la première a été sautée et la seconde appliquée; le lot avait exactement deux traces, sans allocation orpheline. L’état observé est cohérent et reprenable, mais il n’offre ni rollback atomique du lot ni identifiant de lot. L’arbre de travail a reçu pendant cette revue une modification non incluse dans le commit, ADR-034 T20 sur l’identifiant de lot et l’annulation versionnée; elle ne tranche pas explicitement si le commit partiel par opération est la politique acceptée pour T19. Je ne l’ai pas modifiée.

Référence : [docs/RULES.md:117](/home/nill/Desktop/compte/mescompts/docs/RULES.md:117), [engine.py:840](/home/nill/Desktop/compte/mescompts/backend/app/classification/engine.py:840), [DECISIONS.md:288](/home/nill/Desktop/compte/mescompts/docs/DECISIONS.md:288).

### M-01 — MOYEN : les cas de concurrence et de décision humaine restent peu protégés par des tests persistants

Les cas RULE-01..03 obligatoires sont présents et passent. La suite ne contient pas de test dédié au rejet humain, au split manuel, à l’ordre d’insertion/aux permutations d’IDs, ni à l’échec tardif d’un lot. Je les ai vérifiés par exécutions synthétiques ponctuelles décrites ci-dessous; elles ne protègent pas les changements futurs.

La course réelle sur SQLite est sérialisée par `BEGIN IMMEDIATE`. Si la règle obtient le verrou en premier, l’écriture humaine basée sur l’ancienne version reçoit `VersionConflictError` (409); un rejeu avec la version courante remplace ensuite la règle par la décision manuelle. Si l’humain valide en premier, la version précontrôlée de la règle devient périmée et sa mutation est refusée. Aucune décision humaine validée n’est écrasée; l’interface doit toutefois permettre de rafraîchir/réessayer après le 409.

Référence : [allocations.py:489](/home/nill/Desktop/compte/mescompts/backend/app/services/allocations.py:489), [engine.py:832](/home/nill/Desktop/compte/mescompts/backend/app/classification/engine.py:832).

## Complément détecté pendant T20

### B-02 — MAJEUR : le journal de création de règle peut contenir le motif littéral

`rule_store` inclut le `label_pattern` dans la condition sérialisée et écrit
cette condition dans `audit_events.after_json` lors de `rule.created`. Un motif
peut contenir un fragment copié d'un libellé bancaire. Le test synthétique du
relecteur a confirmé la présence du texte du motif dans l'événement. Ce constat
ne vient pas d'une exécution sur des données bancaires réelles. Le service T20
n'ajoute pas ce motif à ses instantanés ou événements d'annulation; le risque
préexistant demeure dans les événements T19. Je ne modifie pas `rule_store`
dans le ticket T20.

### B-03 — MAJEUR : l'ID de lot généré à l'intérieur de l'appel est perdu sur exception

`apply_rule()` accepte un `batch_id` omis et en génère un avant la boucle, mais
ne le retourne qu'après que toutes les opérations ont réussi. Comme les
opérations sont commitées séparément, une erreur tardive peut laisser un lot
partiellement appliqué sans exposer cet ID à l'appelant. Un appelant qui ne
l'avait pas fourni ne peut alors ni rejouer ce lot ni l'annuler via T20. Le
contrat documentaire demande désormais un ID stable fourni par l'appelant
lorsqu'il doit pouvoir rejouer un appel en erreur; le comportement par défaut
reste pour compatibilité avec T19. Un contrat d'API de lot consultable ou un
arbitrage sur l'API reste à faire hors de ce patch.

## Relecture indépendante des corrections T20

Date : 2026-10-11 · **Verdict : PASS statique.** Le relecteur confirme que la
validation enum/catégorie/flux, le test de snapshot corrompu sans écriture,
ainsi que les mutations de cohérence de lot, index uniques et triggers
append-only répondent aux constats T20 précédents. Aucun nouvel écart majeur
T20 n'a été relevé. Cette contre-relecture est statique; les preuves
d'exécution T20 sont rapportées séparément par l'implémentation.

## Vérifications adversariales

- **Simulation sans écriture : PASS.** `test_RULE_03_dry_run_previews_140_rows_without_database_writes` compare le contenu de toutes les tables applicatives avant/après, y compris `audit_events` et `rule_executions`; les snapshots sont égaux. `session_scope()` active aussi `PRAGMA query_only=ON`. La simulation de ratio vérifie de nouveau l’égalité des snapshots.
- **Idempotence et atomicité par opération : PASS.** Le test RULE-13 rejoue la règle sans changement de base et vérifie les traces. Un échec de trace restaure le snapshot complet. L’échec au milieu d’un lot puis le rejeu ont aussi été reproduits synthétiquement (voir B-01).
- **Priorité humaine : PASS sur les chemins vérifiés.** RULE-01 conserve l’allocation manuelle et couvre l’allocation vidée après une application antérieure de règle. Une décision synthétique `reject` sans allocation a été protégée et sautée; un split humain `−900/−1500` a conservé ses deux parts, son total `−2400` et sa version après tentative d’application de règle. Les écritures concurrentes suivent le résultat décrit en M-01.
- **Priorités, égalités, conflits : PASS.** Priorité numérique croissante, tri explicite par `(priority, rule_id)`, conflit si les actions de rang minimal diffèrent, équivalence si leurs signatures sont identiques. RULE-11 vérifie conflits, gagnant de priorité et départage par ID. Une vérification synthétique a permuté l’ordre d’entrée et les IDs : les conflits sont restés des conflits et les actions équivalentes ont gardé le même résultat.
- **Règle périmée : PASS.** Le test désactive la règle après simulation; l’application lève `VersionConflictError` et n’écrit pas de trace de saut manuel. Chaque transaction d’écriture revalide version et approbation sous verrou.
- **Montants : PASS.** Les montants sont des entiers stricts; ratios en points de base entiers totalisant 10 000; parts calculées par arithmétique entière et dernière part au reste. Le test de ratio donne exactement `−7141 + −3060 = −10001` centimes. Une entrée `float` est rejetée. Aucun calcul métier en `float` ou `Decimal` n’est utilisé par le moteur et le magasin de règles.
- **Injection / ReDoS : PASS.** Le motif est limité à 128 caractères et 4 jokers; ses fragments sont échappés avec `re.escape` et recherchés comme texte littéral. Le motif `*(a+)+*` n’exécute pas une regex fournie par l’utilisateur; une recherche sur 20 000 caractères est bornée par le test à moins d’une seconde. Le moteur ne traite pas le libellé comme instruction ou code.
- **Matrice RULE-* pour T19 : aucun ID manquant.** RULE-01, RULE-02 et RULE-03, références du backlog T19, ont des tests nommés correspondants et passent. RULE-04 relève de T20; RULE-05 et RULE-06 des tickets de revue/UI suivants, pas du périmètre T19.
- **Tests existants : aucun affaiblissement observé.** Le diff du commit ajoute 159 lignes de tests. L’assertion de conflit existante devient plus stricte (égalité avec le tuple trié); aucune assertion n’est supprimée, aucun test désactivé ou marqueur de skip ajouté. `git diff --check` passe.

## Commandes et résultats réellement exécutés

Dans `/tmp/revue_t19`, copie issue du commit et excluant `.git`, `.venv`, `node_modules`, `csv_references/`, `data/`, `tmp.zip` et `.env*` :

- `env UV_PROJECT_ENVIRONMENT=/tmp/revue_t19_env uv run pytest -q backend/tests -m 'not opt_in_real_data'` — **426 passed, 2 skipped, 4 deselected**, 122.58 s; seuls tests synthétiques, aucune variable `COMPTES_REAL_CSV` définie.
- `env UV_PROJECT_ENVIRONMENT=/tmp/revue_t19_env uv run ruff check backend` — **All checks passed**.
- `env UV_PROJECT_ENVIRONMENT=/tmp/revue_t19_env uv run ruff format --check backend` — **116 files already formatted**.
- `make verify-contracts` — **code retour 0** sur une copie de vérification ne contenant que les fichiers sûrs; vérification schéma, métriques, fixtures, profil réel opt-in et plan réussie.
- `scripts/check_no_private_data.sh --with-untracked` — **OK, 308 fichiers inspectés** sur cette copie sûre.
- `git diff --check 9e680be^ 9e680be` — **code retour 0**.

## Retour selon AGENTS.md

- **Ticket :** T19.
- **Objectif livré :** revue indépendante de la logique, des invariants, des risques et des preuves demandées.
- **Fichiers changés :** `docs/revues/REVUE_T19.md` seulement.
- **Interfaces modifiées :** aucune.
- **Tests ajoutés :** aucun par la relecture; tests existants et vérifications ponctuelles exécutés.
- **Invariant vérifié :** simulation sans écriture; décisions humaines validées protégées; allocations en centimes exacts; allocation et trace atomiques par opération; rejeu sans doublon.
- **Limites / risques résiduels :** acceptation de la politique de lot à arbitrer (B-01); pérenniser les tests de rejet, split, course et échec tardif (M-01). Le rejeu humain après 409 dépend de la gestion UI.
- **Vie privée :** aucun CSV privé, `data/`, `tmp.zip` ou `.env*` lu; contrôle `--with-untracked` vert sur copie sans données privées.
- **Relecture recommandée :** arbitrage de l’orchestrateur sur ADR-029 avant validation du gate G4.
