# Décisions d'architecture et de collaboration (ADR courts)

> Tenu par l'orchestrateur. Toute modification d'un invariant, d'une formule, d'un `flow_kind` ou de la taxonomie est une décision et doit être consignée ici. Format : ID, date, statut, contexte, décision, conséquences.

## ADR-001 — La racine du dépôt est la racine du projet

- **Date** : 2026-10-09 · **Statut** : acceptée
- **Contexte** : le plan (PLAN_ORCHESTRATION.md §D) décrit une arborescence cible `comptes-familiaux/` à la racine. Le dépôt réel est `mescompts`, qui contient déjà `.nimbalyst/plan_implementation_comptes_familiaux/` (spécifications).
- **Décision** : la racine du dépôt joue le rôle de `comptes-familiaux/` : `backend/`, `frontend/`, `config/`, `docs/`, `scripts/` et `data/` sont créés directement à la racine. Les spécifications restent dans `.nimbalyst/plan_implementation_comptes_familiaux/`, ne sont pas déplacées et font autorité.
- **Conséquences** : les chemins du plan s'entendent relativement à la racine du dépôt, sans préfixe `comptes-familiaux/`. `AGENTS.md` et `README.md` racine sont les points d'entrée ; les copies du dossier de spécification restent la référence d'origine.

## ADR-002 — [OBSOLÈTE, voir ADR-006] GPT-6 Luna supposé indisponible

- **Date** : 2026-10-09 · **Statut** : acceptée
- **Contexte** : le plan demande des sous-agents « gpt-luna » high/xhigh « si accessible ». L'outil Agent de l'environnement n'expose que `sonnet`, `opus`, `haiku` et `fable` (voir `docs/ETAT_IMPLEMENTATION.md`).
- **Décision** : `opus` pour les tickets `xhigh` et les composants financiers ; `sonnet` pour les tickets `high` ; `fable` pour les revues critiques indépendantes des composants sensibles. Le protocole de contrôle qualité du plan est inchangé.
- **Conséquences** : la qualité des sous-agents peut être moins homogène que prévu (risque R-11) ; elle est compensée par la revue indépendante systématique sur DATA/DOMAIN/ANALYTICS/AI/OPS et par des preuves d'exécution exigées pour chaque ticket.

## ADR-003 — Plage de données : 2026 seulement

- **Date** : 2026-10-09 · **Statut** : acceptée
- **Contexte** : l'utilisateur démarre ses comptes à 2026 ; il n'existe pas d'historique 2025 complet. Le CSV de 2025 contient deux sections (compte courant et compte espèces PEA).
- **Décision** : la base métier réelle ne contient que 2026. Le CSV 2025 n'est qu'une **fixture de compatibilité facultative** (test opt-in local, jamais importé par défaut ni commité). Le trou de 2025 n'est pas une anomalie (pas d'alerte « manque 2025 », cf. KPI-05).
- **Conséquences** : les comparaisons et couvertures sont calculées à partir de 2026 ; aucune reconstitution de 2025 ; les tests liés à 2025 sont marqués `opt_in_real_data` et ne tournent pas en CI.

## ADR-004 — Les commits passent par l'orchestrateur

- **Date** : 2026-10-09 · **Statut** : acceptée
- **Contexte** : plusieurs sous-agents travaillent sur le même dépôt ; l'historique doit rester cohérent, relu et sans donnée privée.
- **Décision** : les commits sont réalisés via l'outil de proposition de commit de l'orchestrateur. Les sous-agents **ne committent pas** : ils laissent les fichiers modifiés dans l'arbre de travail et les listent dans leur rapport.
- **Conséquences** : l'orchestrateur contrôle chaque diff (y compris `scripts/check_no_private_data.sh`) avant commit ; le champ « Commit » de `docs/ETAT_IMPLEMENTATION.md` est renseigné par lui. Les sous-agents n'exécutent ni `git commit`, ni `git push`, ni opération destructive sur l'historique.

## ADR-005 — Contrat des flux, catégories et provenance

- **Date** : 2026-10-09 · **Statut** : acceptée (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : PLAN §C distingue 14 `flow_kind` de la taxonomie, mais ne donne pas la table exhaustive des compatibilités ni le rattachement du remboursement à la dépense. La taxonomie expose seulement `origin` et `decision`; « heuristique » n’est pas une valeur de `origin`.
- **Décision proposée** : adopter la matrice exhaustive de `docs/DOMAIN_MODEL.md`. `income_benefits` est admissible en `income_recurring` ou `income_other` selon la cadence constatée ; `income_refund` est l’unique catégorie bancaire du flux `refund`, et un `transaction_link` validé attribue son effet net à la catégorie de dépense d’origine. Les propositions LLM, mémoire exacte et heuristique restent en revue ; aucune ne devient effective sans confirmation, sauf règle déterministe préalablement approuvée. « Heuristique » demeure `suggestion.method`; une transaction sans allocation confirmée garde `origin=unclassified`. La priorité est décision humaine confirmée > règle approuvée > mémoire exacte > heuristique > LLM.
- **Conséquences** : chaque ID source a au moins un flux admissible ; un remboursement non apparié ne change ni le revenu régulier ni la consommation ; les statuts lisibles dérivent des dimensions existantes et n’ajoutent pas une valeur de taxonomie. Les exemples money et refund matérialisent ces choix.

## ADR-006 — Correction de l’environnement de sous-agents

- **Date** : 2026-10-09 · **Statut** : acceptée (alignement sur `AGENTS.md`)
- **Contexte** : ADR-002 documentait l’absence de GPT-6 Luna et retenait `opus`/`sonnet`/`fable`. L’environnement et les consignes de contribution ont depuis changé ; `AGENTS.md` indique que les sous-sessions GPT-6 Luna sont disponibles en `high`/`xhigh`, avec revue complémentaire `claude-code:opus` pour les composants financiers.
- **Décision** : appliquer le choix de modèles mis à jour dans `AGENTS.md` ; ADR-002 reste l’historique de la décision antérieure et ne décrit plus les capacités courantes.
- **Conséquences** : le ticket T01 est produit en GPT-6 Luna `xhigh`; la revue indépendante financière reste recommandée/complémentaire. Aucun changement aux exigences de preuve, de revue ni aux invariants.

## ADR-007 — Arrondi exact des splits et des ratios

- **Date** : 2026-10-09 · **Statut** : acceptée (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : LLM-13 exige `−11,01 €` à 70/30 → `−7,71 € / −3,30 €`, sans définir la règle généralisée à trois parts. Les ratios du plan n’ont pas d’arrondi de sortie défini.
- **Décision proposée** : un split doit totaliser exactement `10000` basis points. Trier les parts par poids décroissant puis ID catégorie croissant ; arrondir chaque part sauf la dernière au centime le plus proche, demi loin de zéro ; affecter le reste signé à la dernière. Pour les ratios, utiliser les basis points entiers (`10000 × numérateur / dénominateur`) avec le même arrondi demi loin de zéro. `7000 + 2999` bps est rejeté sans allocation.
- **Conséquences** : les exemples exécutables vérifient les splits −10000 à 70/30, −1101 à 70/30, −10001 en trois parts et le rejet MONEY-02. Aucun calcul monétaire ne passe en flottant (INV-02).

## ADR-008 — Capacité, couverture temporelle et delta mensuel

- **Date** : 2026-10-09 · **Statut** : acceptée (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : PLAN §C3 laisse « other explicitly selected cash commitments », la validité de couverture du taux, la comparaison mensuelle et la preuve de mois complet à préciser. INV-10 interdit la comparaison mécanique d’un mois partiel à un mois plein.
- **Décision proposée** : aucun engagement n’est déduit par défaut de `operational_saving_capacity`; seules les sorties explicitement sélectionnées, disjointes des dépenses et allocations personnelles, sont déduites. Une échéance `loan_repayment` non scindée reste toujours visible et n’est déduite que si elle est sélectionnée, pour son montant total, avec avertissement capital/intérêts inconnus. Renvoyer la capacité `null` si la couverture du mois est partielle/inconnue ou s’il existe un résidu sur un flux pertinent ; taux `null` si capacité indisponible, revenu régulier nul/négatif ou mois non complet. Un delta n’est calculé que sur deux mois calendaires adjacents, complets, couverts et aux mêmes filtres ; autrement `null`. Le statut de mois repose sur les plages de couverture par compte, jamais sur l’absence de transactions un jour donné.
- **Conséquences** : les cumuls bancaires et de catégories confirmées restent consultables sur mois partiel avec avertissement ; ni prorata ni extrapolation silencieuse. Le golden S01–S12 conserve une capacité indicative de 435000 centimes lorsque les engagements supplémentaires ne sont pas sélectionnés.

## ADR-009 — Dénominateur et compensation de résidus non ventilés

- **Date** : 2026-10-09 · **Statut** : acceptée avec variante prudente (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : la formule du PLAN §C3 indique `abs(sum unclassified relevant amounts) / sum abs(all relevant amounts)` sans définir « relevant » ni la granularité du `sum`. Dans le cas de résidus de signes opposés, la somme signée peut se compenser et sous-estimer le montant non ventilé.
- **Décision proposée pour T01** : conserver littéralement la formule du plan, définir les montants pertinents comme `expense`, `income_recurring`, `income_other`, `allocation_personal`, `refund`, `cash_withdrawal_pending`, `intermediary_pending` et `unknown`, et définir chaque résidu transactionnel comme montant bancaire moins allocations confirmées. Le dénominateur est la somme des valeurs absolues transactionnelles pertinentes. Afficher en plus la couverture en nombre d’opérations et le résidu signé.
- **Point soumis à arbitrage** : préférerait-on un numérateur `Σ abs(résidu par transaction)` pour qu’un crédit et un débit non classés ne s’annulent jamais ? Cette variante modifierait la formule du plan ; elle n’est pas appliquée dans le contrat T01 avant arbitrage.
- **Conséquences** : le cas KPI-06 vérifie exactement 3000 bps sur 30 % de montant non ventilé. Les sorties en sens opposés peuvent toutefois réduire le ratio actuel ; l’indicateur de couverture en opérations doit être montré avec lui.
- **Arbitrage orchestrateur (ADR-009)** : le numérateur de `unclassified_amount_share` est `Σ abs(résidu par transaction)` (précision de la granularité du « sum » du plan, qui en conserve l'intention « part des montants non ventilés »), afin qu'un crédit et un débit non ventilés ne s'annulent jamais (INV-10, incertitude visible). Le dénominateur reste `Σ abs(montants transactionnels pertinents)`. Le résidu signé et la couverture en nombre d'opérations restent affichés. Mise en conformité de `docs/METRICS.md`, `docs/examples/metrics_examples.yaml` et `scripts/verify_metrics_examples.py` demandée à T01 (suivi).

## ADR-010 — Empreinte de compte et archive brute (chiffrement d'archive refusé en v1)

- **Date** : 2026-10-09 · **Statut** : acceptée avec amendement (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : les sections CSV portent un numéro bancaire, mais INV-11 interdit de le conserver ou de le journaliser en clair ; INV-01 requiert une provenance vérifiable et T12 archive les octets bruts.
- **Proposition initiale (partiellement REFUSÉE, voir arbitrage en fin d'ADR)** : calculer une empreinte de compte HMAC-SHA-256 avec clé aléatoire locale versionnée dans `app_settings`, sur le numéro canonique transitoire défini précisément dans `docs/IMPORT_CONTRACT.md`, sans exposer numéro ni empreinte dans les logs/API. Le mapping de toute empreinte inconnue est choisi explicitement par alias/rôle humain à l'aperçu et reste stable. Conserver les octets CSV exacts uniquement sous chiffrement authentifié local (AES-256-GCM, clé protégée hors du répertoire des archives) ; le hash d'idempotence porte sur les octets avant chiffrement.
- **Conséquences** : association stable sans numéro bancaire en clair dans la base ou l'archive ; la clé HMAC doit survivre aux sauvegardes/restaurations et toute rotation doit être auditée. Le chiffrement d'archive nécessite une primitive crypto maintenue et une procédure de récupération locale.
- **Arbitrage orchestrateur** : l'empreinte HMAC-SHA-256 du numéro de compte (clé locale dans `app_settings`, alias choisi par l'humain) est retenue. Le **chiffrement AES-256-GCM des archives est REFUSÉ pour la v1** : l'archive des octets bruts est conservée telle quelle sous `data/originals/` (ignoré par Git, permissions 0600, jamais exposée par l'API ni les logs), conformément au PLAN §D ; le SHA-256 d'idempotence porte sur ces octets. Motifs : usage local mono-utilisateur, risque de perte de clé qui rendrait les originaux (et la restauration T41) inutilisables, dépendance cryptographique supplémentaire. Le chiffrement d'archive pourra être ajouté ultérieurement par ADR. Le numéro bancaire n'est jamais écrit en base, en log ni dans une réponse d'API.

## ADR-011 — Clé d'identité prudente et casse préservée

- **Date** : 2026-10-09 · **Statut** : acceptée (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : l'export ne porte pas d'identifiant bancaire par opération ; une normalisation agressive peut fusionner deux opérations distinctes. IMP-08 exige que les différences de casse ne provoquent pas une fusion abusive.
- **Décision proposée** : la clé logique est `(account_id, booked_on, amount_cents signé, identity_label)` ; `identity_label` applique NFC et réduction des espaces Unicode seulement. La casse, les accents, chiffres et ponctuation sont conservés ; `raw_label` reste intact. L'unicité inclut un `occurrence_rank` entier positif ; le hash de tuple est auxiliaire, les colonnes portent l'unicité SQL.
- **Conséquences** : une variation de casse peut laisser deux identités et être signalée comme candidate ; elle n'est jamais auto-fusionnée. Les alias de payee restent un mécanisme de présentation/classification séparé.

## ADR-012 — Réconciliation par maximum de multiplicité (amendée par ADR-017/018)

- **Date** : 2026-10-09 · **Statut** : acceptée (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : les paiements sans ID unique peuvent être strictement identiques jusqu'à quatre fois ; une simple unicité de clé perd des occurrences, tandis que sommer les exports chevauchants crée des doublons.
- **Décision proposée** : par clé stricte, créer les rangs absents jusqu'à `max(n_existing, n_new)`. Chaque occurrence de chaque fichier distinct reçoit une `source_row`; le hash d'un fichier déjà commité crée une tentative `no_op`, sans transaction ni source_row nouvelle. Aucune absence dans un export ne supprime une transaction. Le maximum n'est autorisé que si les bornes de couverture prouvent des journées complètes et inclusives ; section tronquée ou explicitement partielle → groupe bloqué et soumis à résolution, car sans ID bancaire il est impossible de distinguer un doublon réexporté d'un nouveau paiement identique dans un extrait incomplet.
- **Conséquences** : `max` est commutatif pour A/B, respecte IMP-01..05/09 et rend l'ordre de lignes sans effet sur la cardinalité lorsque les fichiers sont complets. Les doublons stricts légitimes restent des transactions distinctes par rang ; la limite d'identifiabilité des extraits partiels est visible et n'est pas résolue par fusion probabiliste.
- **Arbitrage orchestrateur** : accepté tel quel. Précision : l'absence de plage déclarée ou une troncature structurelle bloque le commit **de la section concernée uniquement**, avec résolution humaine auditée ; les exports CA standard déclarent leur période (« entre le A et le B ») et sont traités comme des journées entières. Le maximum ne supprime jamais rien : un export partiel ne peut que sous-compter une identité (limite d'identifiabilité consignée dans le registre des risques).

## ADR-013 — Identifiants UUID opaques

- **Date** : 2026-10-09 · **Statut** : acceptée (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : les entités référencées dans l'API ont besoin d'identifiants stables ; un compteur exposerait l'ordre et dépendrait de la base physique, un numéro de compte ne peut en aucun cas servir de clé.
- **Décision proposée** : employer des UUIDv4 texte générés côté backend pour les comptes, transactions, imports, sources et décisions. Garder les dates `YYYY-MM-DD` comme colonnes de tri et les instants techniques en UTC `Z`.
- **Conséquences** : IDs opaques utilisables dans les liens d'audit/API et stables après backup/restore ; ils n'ordonnent pas les événements, donc toute pagination utilise une date métier puis l'UUID comme départage.

## ADR-014 — Pagination serveur par curseur signé

- **Date** : 2026-10-09 · **Statut** : acceptée (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : offset/limit devient instable lorsque des imports ajoutent des dates anciennes ou lorsque la file de revue change entre deux pages ; le plan demande tri stable par date et ID.
- **Décision proposée** : utiliser une pagination keyset opaque signée, bornée à 100 éléments et liée aux filtres. Transactions triées `(booked_on DESC, transaction_id DESC)` avec snapshot de création ; revue triée par priorité, date max puis clé stable et invalidée avec `CURSOR_STALE` si une décision change la file.
- **Conséquences** : pas de répétition/saut dû à un offset et résultats reproductibles pendant une navigation ; le client doit redémarrer une file de revue obsolète.

## ADR-015 — Idempotence des mutations et des imports

- **Date** : 2026-10-09 · **Statut** : acceptée avec amendement (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : le navigateur ou SQLite peuvent réessayer une requête après timeout ; un import identique doit rester traçable sans produire une seconde mutation.
- **Décision proposée** : chaque mutation HTTP porte `Idempotency-Key`; stocker clé, empreinte canonique de requête et réponse pour rejouer la même opération, et répondre `409` si le corps diffère. Réserver le hash complet aux imports committés via unicité partielle ; un nouvel essai au hash connu est un `no_op` audité, tandis qu'un hash rejeté peut être réessayé après correction du parseur.
- **Conséquences** : retry réseau sans double décision ou double import. Les réponses persistées n'incluent aucun secret ni libellé brut au-delà du besoin contractuel ; les entrées sont redigées et expirées selon une politique documentée.
- **Arbitrage orchestrateur** : `Idempotency-Key` est **obligatoire pour les POST de création/commit/application** (import commit, décisions, application de règle, backup) et optionnelle pour PUT/PATCH/DELETE déjà protégés par `version` optimiste. Les GET n'en portent pas.

## ADR-016 — Migrations Alembic monotones, propriétaire unique

- **Date** : 2026-10-09 · **Statut** : acceptée (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : plusieurs tickets travailleront sur SQLite ; modifier une révision déjà appliquée peut rendre une base locale irréparable.
- **Décision proposée** : un seul propriétaire par lot de migration, parents/révisions Alembic linéaires, sauvegarde SQLite cohérente et vérifiée avant upgrade, `upgrade` et `downgrade` raisonnables. Un downgrade n'est accepté que s'il préserve les données ; toute transformation irréversible s'arrête explicitement et demande une restauration sûre ou une migration corrective vers l'avant. Une migration appliquée n'est jamais réécrite.
- **Conséquences** : aucun patch concurrent sur un même historique de migration ; les changements de catégories, identité ou flux nécessitent backfill/version explicites et tests sur copie.

## ADR-017 — Atomicité de l'import : tout ou rien par fichier (remplace la précision « par section » d'ADR-012)

- **Date** : 2026-10-10 · **Statut** : acceptée (arbitrage orchestrateur, revues G0 Opus B-02 et Luna F-01)
- **Contexte** : ADR-012 bloquait « la section concernée uniquement », IMPORT_CONTRACT bloquait le fichier ou des clés de jour ; un commit partiel enregistrerait le hash du fichier et rendrait les lignes restantes irrécupérables (no_op).
- **Décision** : l'unité transactionnelle est **le fichier**. Tout élément non résolu (section sans plage déclarée, ligne hors période, clé de jour ambiguë, deux sections associées au même compte, section de compte inconnue non mappée, erreur structurelle ou monétaire) empêche le commit : le fichier reste `previewed` (ou `rejected` si irrécupérable) et n'enregistre aucune transaction, `source_row` ni hash « committed ». Les résolutions humaines (mapping de compte, plage, décision de multiplicité) se font **dans l'aperçu** ; puis un unique `BEGIN IMMEDIATE` commite l'ensemble. Statuts d'import : `previewed`, `committed`, `rejected`, `no_op` — aucun statut partiel. API : pas de résultat `partial`.
- **Conséquences** : INV-04 décidable ; un réimport après résolution n'est jamais un faux `no_op` ; l'ADR-012 est amendée en conséquence.

## ADR-018 — Complétude des journées, jour de téléchargement et lignes hors période

- **Date** : 2026-10-10 · **Statut** : acceptée (arbitrage orchestrateur, revue Opus B-01)
- **Contexte** : le maximum de multiplicité (ADR-012) suppose des journées complètes ; un export tiré le jour même, ou une ligne hors période déclarée, peut faire sous-compter un doublon légitime tout en affichant un mois « complet ».
- **Décision** : (1) la date du préambule `Téléchargement du JJ/MM/AAAA` (à défaut, la date de réception) est la **date de génération** ; toute journée ≥ date de génération est `provisional` : elle compte dans les cumuls bancaires mais n'est **jamais** « complète » ; `complete_through = min(fin de période, date de génération − 1 jour)`. Un mois n'est `complete` que si son dernier jour ≤ `complete_through` pour chaque compte sélectionné ; l'aperçu recommande de refaire l'export en chevauchant la journée provisoire. (2) Une ligne datée hors de la période déclarée de sa section est `OUT_OF_PERIOD` : **ambiguïté bloquante pour sa clé de jour** (ADR-017) — elle n'est jamais utilisée par le maximum automatique ; l'aperçu propose « lier à l'occurrence existante » ou « créer le rang supplémentaire » (décision humaine auditée). (3) Un mois dont un contrôle de solde d'en-tête est en `mismatch` passe à `month_completeness = unknown`. (4) Sur le CSV réel 2026 (profil local, agrégats seulement) les dates min/max coïncident avec la période annoncée : aucune ligne hors période observée.
- **Conséquences** : nouveaux tests IMP-11 (jour de génération provisoire), IMP-12 (ligne hors période), IMP-13 (même compte dans deux sections) ; mise à jour de la fixture `date_out_of_period_2025.csv` (comportement : bloquant pour sa clé de jour, pas simple avertissement).

## ADR-019 — Durcissement de l'intégrité en base (revue Opus M-01..M-06, m-01, m-03)

- **Date** : 2026-10-10 · **Statut** : acceptée (arbitrage orchestrateur)
- **Décision** : (a) chaque connexion SQLite pose `PRAGMA foreign_keys=ON` **et** `recursive_triggers=ON` (listener `connect`) ; `INSERT OR REPLACE`/`REPLACE INTO` sont interdits dans le code (garde lint/grep) ; test T06 qui vérifie les PRAGMA sur une connexion du pool ; le service d'import utilise `BEGIN IMMEDIATE` explicite (mode `isolation_level=None` ou hook `begin`) et lit le hash **sous le verrou**. (b) Triggers `source_rows` : le compte de la transaction cible = compte de la section ; l'import est `previewed` ou `committed` (jamais `no_op`/`rejected`) ; `UNIQUE(import_file_id, transaction_id)` tant qu'un compte n'apparaît qu'une fois par fichier (ADR-017/IMP-13). (c) Trigger `transactions` : `occurrence_rank = 1` ou rang−1 existant (contiguïté) ; `n_existing = max(occurrence_rank)` ; le service signale `count ≠ max` comme anomalie bloquante ; contrôle de fin de commit : toute transaction a ≥ 1 `source_row`, et par import/identité le nombre de sources = `n_new`. (d) `import_files`, `import_account_sections` immuables et non supprimables dès `committed` (statut, hash, archive, couverture, mapping) ; transitions autorisées `previewed → committed|rejected` ; un `no_op` référence (`duplicate_of`) un import `committed` de même hash. (e) `allocations` : signe de l'allocation = signe de la transaction ; Σ allocations confirmées de même signe et |Σ| ≤ |montant| (l'égalité exacte est vérifiée par le service en fin de décision) ; direction des catégories (`flow_savings_out` < 0, `flow_savings_in` > 0, `flow_loan_disbursement` > 0, `income_*` > 0…) ; allocations confirmées non modifiables/supprimables en place (versionnement `superseded_by` + audit). (f) `transaction_links` : Σ liens `confirmed` par source ≤ montant du crédit ; Σ par cible ≤ |montant de l'achat| ; source = crédit `refund`, cible = débit `expense`. (g) `identity_label` : ensemble de blancs = propriété Unicode `White_Space` ; caractères `Cf` (ZWSP, BOM interne…) = avertissement d'aperçu, jamais supprimés silencieusement ; CHECK `identity_label = trim(identity_label)` et pas de double espace. (h) Seuls des `int` Python stricts (`type(x) is int`) sont liés comme montants. (i) Montant nul : **rejeté** pour le format CA (cohérent avec la fixture `zero_amount.csv`).
- **Conséquences** : `verify_schema.py` couvre ces contraintes, IMP-06/IMP-07 (rollback, deux connexions) et DELETE/REPLACE.

## ADR-020 — Représentation du flux d'une transaction, épargne sur allocations, pertinence au résidu

- **Date** : 2026-10-10 · **Statut** : acceptée (arbitrage orchestrateur, revue Opus M-07, Luna F-02/F-03)
- **Décision** : `transactions` ne porte **pas** de `flow_kind` stocké. Le flux est porté par les allocations confirmées ; le flux d'une transaction est **dérivé** : `unknown` si aucune allocation confirmée, sinon le(s) `flow_kind` de ses allocations (multi-flux autorisé) ; le **résidu non ventilé** (signé) est toujours rapporté en `unknown`. Le filtre API `flow_kind=X` retient les transactions ayant une allocation confirmée de flux X ; `flow_kind=unknown` retient celles dont le résidu est ≠ 0. `savings_net_transferred` se calcule sur les **allocations confirmées** `transfer_savings` (pas sur le montant de la transaction) ; la pertinence de `unclassified_amount_share` est définie au **résidu** (tout résidu ≠ 0 est pertinent) : numérateur = Σ|résidu|, dénominateur = Σ|allocations confirmées pertinentes| + Σ|résidus| ; `operational_saving_capacity` vaut `null` si un résidu ≠ 0 existe sur la période/les comptes sélectionnés (ADR-008).
- **Conséquences** : exemples YAML avec transaction multi-flux et transaction partiellement ventilée sur flux non pertinent ; aucune double imputation d'une sortie scindée.

## ADR-021 — Imputation des remboursements liés

- **Date** : 2026-10-10 · **Statut** : acceptée (arbitrage orchestrateur, revue Opus M-08)
- **Décision** : l'effet d'un lien `refund_of` **confirmé** réduit la consommation de la catégorie de l'achat d'origine **à la date de l'avoir** (jamais de réécriture rétroactive d'un mois passé). Les deux jambes (avoir et achat) doivent appartenir au périmètre de comptes sélectionné ; sinon le lien est ignoré dans cette vue (avoir « non attribué » + avertissement). Les liens `proposed` ne comptent pas. Une consommation nette négative sur un mois/catégorie est permise et signalée (« remboursement d'un achat antérieur »). Le drilldown affiche le lien et l'achat d'origine. Le type de lien est `refund_of` partout (YAML, schéma, API).

## ADR-022 — Enveloppe personnelle vs dépenses du compte personnel importé

- **Date** : 2026-10-10 · **Statut** : acceptée (arbitrage orchestrateur, revue Opus M-09)
- **Décision** : paramètre de filtre `personal_mode ∈ {envelope, observed}`, défaut `envelope`. `envelope` : les dépenses des comptes de rôle `personal` sont exclues de `consumption_net` ; `allocation_personal` compte. `observed` : `allocation_personal` est exclu de la lecture « vie familiale », les dépenses observées des comptes personnels sont incluses, la jambe créditrice côté compte personnel est `transfer_owned`, et le reliquat non dépensé est signalé. Jamais les deux dans le même total (PLAN §C2).

## ADR-023 — Clé HMAC de compte, sauvegarde et idempotence (revue Luna F-06, F-11, F-04)

- **Date** : 2026-10-10 · **Statut** : acceptée (arbitrage orchestrateur)
- **Décision** : la clé HMAC est une valeur aléatoire locale dans `app_settings` (`is_secret=1`), jamais renvoyée par l'API ni journalisée ; elle est **incluse dans les sauvegardes** (qui sont des données privées au même niveau de confiance que la base : permissions 0600, exclues de Git) ; clé perdue ⇒ les comptes ne sont plus reconnus et exigent un **remappage explicite dans l'aperçu**. Les enregistrements d'idempotence sont conservés **7 jours** puis purgés (tâche au démarrage) ; les mutations créatrices restent rejouables pendant cette durée. Les faits d'un insight sont stockés dans une table `insight_facts` (un `metric_definition_id` validé contre le registre des 11 IDs de `METRICS.md` par fait) ; `insights.metric_definition_id` est supprimé. Le propriétaire G0 est le **rôle** du backlog (pas de nom nominatif).

## ADR-024 — Amendements de confirmation G0 : complétude par jour, contrôle de solde à la lecture, imputation multi-catégories

- **Date** : 2026-10-10 · **Statut** : acceptée (arbitrage orchestrateur, revue de confirmation Opus N-01, N-02, N-03, n-04..n-12)
- **Décision** :
  1. **Complétude par jour (amende ADR-018.1)** : un jour est *complet* s'il appartient à l'union des intervalles `[coverage_from, complete_through]` des sections commitées du compte (chaque section ayant son propre `complete_through = min(fin de période, date de génération − 1)`). Un mois est `complete` si **tous** ses jours sont complets pour chaque compte sélectionné ; un export suivant qui ne chevauche pas le jour provisoire laisse donc le mois `partial` (jamais `complete`). Fixture et exemple « export suivant non chevauchant » à ajouter. Repli `received_on_fallback` (préambule absent) : le dernier jour portant des opérations est traité comme provisoire.
  2. **Contrôle de solde (amende ADR-018.3)** : seuls des soldes d'en-tête **datés au plus tard à `complete_through`** (donc avant le jour de génération) sont comparables ; sinon `not_comparable`. Le contrôle est **calculé à la lecture** (ou dans une table append-only versionnée `balance_checks`) sur l'ensemble des soldes connus, **indépendamment de l'ordre d'import** ; `balance_check_status` n'est jamais une vérité figée sur `import_account_sections`. Un `mismatch` réel rend `month_completeness = unknown` pour la période concernée.
  3. **Avoir multi-catégories (amende ADR-021)** : le lien `refund_of` cible une **allocation** d'achat précise (`target_allocation_id`) ; à défaut, il est réparti au **prorata des allocations `expense`** de l'achat avec l'arrondi d'ADR-007 ; la somme des catégories égale toujours le total (KPI-03/KPI-07). Les plafonds portent sur le montant de l'allocation `refund` (source) et sur Σ des allocations `expense` (cible), non sur le montant brut de la transaction. Un lien `confirmed` n'est modifiable que par supersession versionnée et auditée.
  4. **Reportés aux tickets porteurs** : n-06 (pas de source si `rejected`) et n-07 (décisions d'aperçu immuables après commit) et n-08 (clé d'identité sur `source_rows` pour `link_existing`) → T06/T12/T13 ; n-09 (sort d'un aperçu `no_op`, archive) → T11/T12 ; n-11 (ordre de supersession d'allocation avec FK différée) → contrat de décision T17/T20 ; n-12 (résidu des comptes `personal` en mode `envelope`) → T24 : le résidu d'un compte personnel n'invalide la capacité familiale **que** si ce compte est sélectionné.
- **Conséquences** : mise en conformité de METRICS/DOMAIN_MODEL/IMPORT_CONTRACT/schema/API et de l'oracle avant T06 (demandée aux sessions T01/T02) ; vérification à G3 (T14) et G4 (T17).

## ADR-025 — Arbitrages de la revue G1 : enveloppe d'erreur, health, migration 0001 pré-publication, session d'écriture

- **Date** : 2026-10-10 · **Statut** : acceptée (arbitrage orchestrateur, revue G1 MAJ-01..MAJ-07)
- **Décision** :
  1. **Enveloppe d'erreur** (MAJ-05) : la forme officielle est `{"error": {"code", "message", "details"}}` (déjà implémentée côté backend et frontend). `docs/API.md` est amendé en conséquence (exemples inclus) ; un test de contrat compare les exemples d'`API.md` aux réponses réelles. `/health` : les 4 champs obligatoires (`status`, `api_version`, `storage_status`, `llm_enabled`) + champs additifs documentés dans `API.md` (`version`, `time_utc`, `database{status,revision}`, `offline_ready`).
  2. **Migration 0001 non publiée au moment de cette décision** (MAJ-06) : avant la première base réelle, `0001_initial` et `docs/examples/schema.sql` pouvaient être amendées en place pour imposer `NOT NULL` et le format UUIDv4 texte sur toutes les clés primaires (`CHECK length=36 AND GLOB`), la précision des horodatages UTC (`%Y-%m-%dT%H:%M:%fZ`) et le verrou optimiste (`BEFORE UPDATE … WHEN NEW.version <> OLD.version + 1` sur les tables versionnées). Ce régime a pris fin à la première base réelle ; ADR-031 fixe désormais la révision 0001 au commit `1066748` et impose une nouvelle révision pour tout changement ultérieur.
  3. **Session d'écriture** (MAJ-01/02) : le commit n'a jamais lieu après l'envoi de la réponse. Les écritures passent par une unité de travail explicite `with write_transaction(...)` dans le service/handler (commit **avant** de retourner), en `BEGIN IMMEDIATE` ; `sqlite3.OperationalError: database is locked` → `503 DATABASE_BUSY` (réessayable), `StaleDataError`/version périmée → `409 VERSION_CONFLICT`. `get_db` ne sert plus qu'aux lectures.
  4. **Sauvegarde obligatoire** (MAJ-07) : la CLI Alembic standard ne peut pas migrer une base non vide sans passer par la routine de sauvegarde ; `COMPTES_ALLOW_DATA_LOSS_DOWNGRADE` ne coupe les FK qu'en `downgrade` (m-06).
  5. **Secrets/logs** (MAJ-03/04) : les logs d'accès uvicorn sont désactivés ou rédigés (pas de chemins/query en clair) ; les erreurs de validation des `Settings` ne reproduisent jamais la valeur fournie.
- **Conséquences** : ces corrections sont une **condition d'entrée de T11** (premier endpoint d'écriture) ; `REVUE_G1.md` est la liste de référence (mineurs m-01..m-17 assignés à leurs tickets).

## ADR-026 — Arbitrages de la revue G2 : bornes, codes d'anomalie bloquants, identité jamais tronquée

- **Date** : 2026-10-10 · **Statut** : acceptée (arbitrage orchestrateur, revue G2 B-01, M-01..M-05, m-01..m-14)
- **Décision** :
  1. **Bornes monétaires (B-01)** : `|montant| ≤ 10^13 centimes` (100 milliards d'euros) pour toute opération, solde d'en-tête et total ; au-delà : anomalie **bloquante** `AMOUNT_OUT_OF_RANGE`. Le stockage d'aperçu sérialise avant d'écrire les octets, nettoie sur échec et purge aussi `.bin`/`.tmp` orphelins (m-02). Aucun entier hors 2^53 n'atteint jamais le frontend (INV-02).
  2. **Numéro de compte (M-02)** : une section `Compte …` sans numéro exploitable (ou un en-tête de colonnes sans préambule `Compte`) est **bloquante** : `MISSING_ACCOUNT_NUMBER`, non résoluble par simple mapping ; `nº` (U+00BA) est accepté comme variante de `n°` ; les bords du numéro n'acceptent que les 5 blancs du contrat (m-07).
  3. **Remappage (M-01)** : une section dont l'empreinte est `known` ne peut pas être remappée (409) ; un `account_id` existant dont l'empreinte est non nulle et différente de celle de la section est refusé ; deux `new_account` de même alias dans l'aperçu, ou un alias déjà utilisé par un compte existant, sont refusés (m-13) ; alias : au plus 7 chiffres cumulés (séparateurs retirés) et aucun motif IBAN (m-04).
  4. **Couverture (M-03)** : `PATCH coverage` n'est accepté que pour les sections signalées `SECTION_RANGE_REQUIRED` ; la couverture saisie doit contenir `[min_date, max_date]` de la section, sinon `OUT_OF_PERIOD` est recalculé pour les lignes concernées ; la période déclarée par la banque n'est jamais écrasée.
  5. **Identité (M-04)** : `identity_label` n'est **jamais tronqué** ; l'entrée est bornée en amont : libellé brut ≤ 10 000 caractères **et** forme NFC ≤ 20 000 caractères, sinon anomalie bloquante `LABEL_TOO_LONG`. `LABEL_TRUNCATED` ne peut plus se produire pour une identité.
  6. **Dates (m-06, m-14)** : bloquants : `GENERATION_DATE_INCONSISTENT` (date de génération < début de période, deux dates divergentes, ou `Téléchargement du` après le début des sections), `FUTURE_DATED_ROW` (booked_on > generated_on), `DATE_IMPLAUSIBLE` (booked_on < 2000-01-01, non résoluble). Les `PROVISIONAL_DAY` sont recalculés en fin de parse.
  7. **Lignes ignorées (m-05)** : après l'en-tête, toute ligne non conforme (y compris commençant par `Madame`, `Solde…`, `Téléchargement du`) est une candidate d'opération : `BAD_FIELD_COUNT`/`INVALID_DATE`, jamais ignorée en silence.
  8. **`link_existing` (M-05)** : exige `transaction_id` (UUID d'une transaction existante de même compte et de même identité stricte, vérifié en lecture à l'aperçu et re-vérifié au commit) ; sinon 422. Chaque décision d'aperçu garde un historique (acteur, horodatage, ancienne valeur). `docs/API.md` est aligné (champs documentés `transaction_id`, `section_index` si `logical_row_idx` est ambigu).
  9. **Hygiène (m-01, m-03, m-10, m-11)** : `can_commit` recalculé après retrait des rejets ; endpoints d'import synchrones (`def`, thread pool) avec contrôle de `Content-Length` en amont et plafond du nombre d'anomalies ; `repr=False` sur `raw_label`, `identity_label`, `search_label` ; `merchant_raw_hint` masqué s'il ressemble à un IBAN/numéro long avant exposition.
- **Conséquences** : nouveaux codes ajoutés à la table d'anomalies (docs/IMPORT_PARSER.md, docs/API.md) ; nouveaux tests IMP-15..IMP-19 ; ces corrections sont une condition de G2 et d'entrée de la validation G3.

## ADR-027 — Schéma des allocations : retrait auditable et liens de remboursement par allocation

- **Date** : 2026-10-10 · **Statut** : acceptée (arbitrage orchestrateur, retour T17)
- **Contexte** : T17 constate que (a) une allocation confirmée ne peut être que remplacée, jamais retirée de façon auditable (retour à « non ventilée » impossible, donc annulation T20 impossible) ; (b) `transaction_links` ne référence pas l'allocation source `refund`, donc un seul remboursement actif par crédit ; (c) l'index d'unicité des liens actifs inclut les liens rejetés non supersédés.
- **Décision** (règle de migration désormais fixée par ADR-031 : toutes les évolutions T17 de cette décision sont dans `0002_t17_refund_safety`) :
  1. **Retrait** : une allocation peut être *retirée* par une action humaine auditée : colonnes `retracted_at_utc` et `retracted_by_decision_id` (NULL = active) ; une allocation est **active** si `superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL` ; les triggers de signe/plafond/somme et toutes les lectures (métriques, résidu, liens) ne considèrent que les allocations actives ; une allocation retirée ou supersédée est immuable (aucune autre modification) ; le retrait ne peut être posé qu'avec une décision existante de la même transaction ; jamais de DELETE.
  2. **Liens par allocation** : `transaction_links` porte `source_allocation_id` (allocation `refund` active du crédit) et `target_allocation_id` ; plafonds : Σ liens `confirmed` par allocation source ≤ montant de cette allocation, Σ par allocation cible ≤ |montant de cette allocation| ; plusieurs remboursements actifs par crédit autorisés (une ventilation `refund` + `income_other` + plusieurs achats liés).
  3. **Unicité** : l'index d'unicité des liens actifs ne porte que sur les liens `proposed|confirmed` non supersédés (les `rejected` sont conservés comme historique et n'empêchent pas une nouvelle proposition).
- **Conséquences** : la migration `0002_t17_refund_safety` met à jour les objets SQL nécessaires sans modifier la migration gelée 0001 ; `docs/examples/schema.sql`, les modèles, `verify_schema.py` et les tests reflètent la tête 0001+0002 ; T17 adapte `clear_allocations` et les liens ; T18/T19/T24 ne lisent que les allocations actives.


## ADR-028 — Parts proratisées matérialisées et plafond combiné par allocation

- **Date** : 2026-10-10 · **Statut** : acceptée (arbitrage orchestrateur, revue T17 BLOCK B-01, correctif T17)
- **Contexte** : la revue T17 a prouvé qu'un lien de remboursement proratisé (`target_allocation_id` NULL) n'était pas compté dans le plafond d'une allocation cible : prorata de 10 000 puis lien ciblé de 8 000 sur une allocation de 10 000 conduisaient à −3 000 centimes nets sur cette catégorie.
- **Décision** :
  1. À la confirmation d'un lien proratisé, le service calcule les parts par allocation d'achat selon ADR-007 (centimes entiers, arrondi et résidu déterministes) et les **matérialise** dans `refund_link_allocation_parts`. Le service, les contrôles de capacité et la lecture analytique utilisent ce même vecteur.
  2. Consommation d'une allocation = Σ liens ciblés + Σ parts proratisées matérialisées ; le plafond combiné est contrôlé par le service ET par triggers SQL (allocation dépensée active, somme des parts, validité, plafond).
  3. Le calcul proportionnel n'est pas reproduit en SQL : le service est le seul producteur pris en charge de la répartition exacte ; le schéma garantit qu'une écriture directe ne peut jamais rendre négatif le net d'une allocation.
  4. Le retrait auditable d'allocation (ADR-027.1) est refusé tant qu'un lien proposé/confirmé la référence en source ou en cible.
- **Conséquences** : migration `0002_t17_refund_safety` matérialise les parts et leurs gardes SQL sans modifier 0001 ; IDs de tests MONEY-11..14, KPI-03/KPI-07 dans `docs/MATRICE_TESTS_ADDENDUM.md`.

## ADR-030 — Durcissement G3 : archives orphelines, OUT_OF_PERIOD, `not_started`, alias

- **Date** : 2026-10-10 · **Statut** : acceptée (arbitrage orchestrateur, revue G3 MAJ-01, MAJ-02, m-01..m-11)
- **Décision** :
  1. **Archives orphelines (MAJ-02)** : sur toute exception avant le `COMMIT` confirmé, l'archive est supprimée si aucun `import_files` commité ne la référence ; inventaire + purge auditée au démarrage des `originals/*.bin` non référencés de plus de 24 h ; deux liens `link_existing` vers la même cible ⇒ `can_commit=false`. Amende ADR-010 / IMPORT_COMMIT.
  2. **OUT_OF_PERIOD (MAJ-01)** : l'aperçu expose, pour chaque ligne concernée, les transactions candidates de même identité stricte (`transaction_id`, rang, date d'import, sans libellé supplémentaire) ; l'UI propose « Lier à l'opération existante » par sélection (défaut s'il existe un candidat) ; « Créer une occurrence » crée **toujours** une nouvelle opération (rang n+1) et exige confirmation quand l'identité existe déjà.
  3. **Complétude (m-04)** : `period.completeness` accepte la valeur additive `not_started` (mois antérieur à la première couverture, non alertable, ADR-003).
  4. **Comptes (m-02)** : unicité d'alias (casefold) et absence d'empreinte revérifiées au commit (`ACCOUNT_ALIAS_CONFLICT`, `ACCOUNT_FINGERPRINT_CONFLICT`) ; l'index unique `lower(alias)` en base est reporté après T17 (propriétaire de la migration).
  5. **Entiers (m-03)** : aucun total hors 2^53 n'atteint le frontend ; `errorElement` de route obligatoire.
  6. **Solde d'en-tête (m-05)** : confirmé sur le profil réel (« Solde au » = date de téléchargement) ; ADR-024.2 rend le contrôle inerte ; correctif `T14b` (delta entre deux exports consécutifs) à planifier.

## ADR-031 — Gel de 0001 (première base réelle) et migrations additives

- **Date** : 2026-10-10 · **Statut** : acceptée (arbitrage orchestrateur ; amende ADR-025.2 et ADR-027)
- **Contexte** : l'utilisateur a importé de vraies données via le frontend ; `data/db/comptes.sqlite` est à la révision `0001_initial` avec exactement le schéma de `0001_initial.sql` du commit `1066748` (126 objets identiques, vérifié par comparaison de `sqlite_master`, sans lire de données). Les amendements en place de `0001` effectués depuis (T17 : parts proratisées, retrait, triggers) divergent donc de cette base.
- **Décision** :
  1. `0001_initial.sql` et `versions/0001_initial.py` sont **gelés** à leur contenu de `1066748` ; un test de gel (SHA-256 épinglés) échoue à toute modification.
  2. Tout changement de schéma est une **nouvelle révision** (`0002`, `0003`…) sûre sur base non vide : reconstruction de tables par create/copy/drop/rename, réinstallation de tous les triggers, `integrity_check` + `foreign_key_check`, sauvegarde automatique vérifiée avant migration (ADR-025.4), downgrade conforme à la politique de perte de données explicite.
  3. `docs/examples/schema.sql` reste la référence du schéma de **tête** ; la parité migrée ↔ `schema.sql` ↔ modèles est vérifiée par `verify_schema.py` et les tests.
  4. Les tests n'ouvrent jamais `data/` : bases temporaires uniquement.
- **Conséquences** : delta T17 → migration `0002` ; `rule_executions.batch_id/rule_version` (proposition T19) → `0003` (T20) ; l'index unique `lower(alias)` (ADR-030.4) → migration dédiée.

## Proposition d'ADR — Répartition prorata bornée (numéro à attribuer)

- **Statut** : proposition T17, numérotation et arbitrage réservés à l'orchestrateur.
- **Contexte** : le prorata de remboursement hérité d'ADR-007 arrondissait les
  catégories et affectait le résidu à la dernière. Après des remboursements
  antérieurs, cette méthode pouvait dépasser la capacité restante d'une
  catégorie, même lorsque la capacité totale suffisait.
- **Décision proposée** :
  1. Pour les liens `refund_of` proratisés, agréger les poids par catégorie à
     partir des montants absolus des allocations `expense` actives. La capacité
     d'une catégorie est la capacité active restante après les liens confirmés
     précédents, ciblés ou proratisés.
  2. Répartir en centimes entiers par la méthode des plus grands restes. Si une
     part calculée dépasse sa capacité, plafonner cette catégorie et redistribuer
     le résidu au prorata des poids initiaux entre les catégories encore
     disponibles. Départager les restes égaux par `category_id` croissant.
  3. Pour une catégorie ventilée sur plusieurs allocations, distribuer sa part
     par `allocation_id` croissant sans dépasser la capacité restante de chacune.
     Matérialiser uniquement les parts strictement positives; leur somme égale
     toujours le montant positif du lien.
  4. Si le montant dépasse la somme des capacités restantes, refuser atomiquement
     la confirmation avec `REFUND_PRORATA_INFEASIBLE`; ne pas tronquer le lien,
     réduire son montant ni écrire des parts partielles.
  5. Le calcul reste entier, déterministe et sans `float`. Cette règle concerne
     le prorata des remboursements (`prorate_link`) ; le split des allocations
     par points de base (`split_by_basis_points`) conserve la règle ADR-007.
- **Conséquences proposées** : les parts de chaque lien sont figées à la
  confirmation, les liens suivants utilisent les capacités restantes, et les
  oracles MONEY-15/MONEY-20 vérifient la conservation, les plafonds, l'ordre
  déterministe et le refus conservateur. Cette proposition accompagne la
  migration `0002_t17_refund_safety` sans modifier la migration 0001 gelée.
