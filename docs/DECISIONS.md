# Décisions d'architecture et de collaboration (ADR courts)

> Tenu par l'orchestrateur. Toute modification d'un invariant, d'une formule, d'un `flow_kind` ou de la taxonomie est une décision et doit être consignée ici. Format : ID, date, statut, contexte, décision, conséquences.

## ADR-001 — La racine du dépôt est la racine du projet

- **Date** : 2026-10-09 · **Statut** : acceptée
- **Contexte** : le plan (PLAN_ORCHESTRATION.md §D) décrit une arborescence cible `comptes-familiaux/` à la racine. Le dépôt réel est `mescompts`, qui contient déjà `.nimbalyst/plan_implementation_comptes_familiaux/` (spécifications).
- **Décision** : la racine du dépôt joue le rôle de `comptes-familiaux/` : `backend/`, `frontend/`, `config/`, `docs/`, `scripts/` et `data/` sont créés directement à la racine. Les spécifications restent dans `.nimbalyst/plan_implementation_comptes_familiaux/`, ne sont pas déplacées et font autorité.
- **Conséquences** : les chemins du plan s'entendent relativement à la racine du dépôt, sans préfixe `comptes-familiaux/`. `AGENTS.md` et `README.md` racine sont les points d'entrée ; les copies du dossier de spécification restent la référence d'origine.

## ADR-002 — GPT-6 Luna indisponible : sous-agents opus / sonnet / fable

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

## ADR-010 — Empreinte de compte et archive brute protégée (proposée pour arbitrage G0)

- **Date** : 2026-10-09 · **Statut** : acceptée avec amendement (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : les sections CSV portent un numéro bancaire, mais INV-11 interdit de le conserver ou de le journaliser en clair ; INV-01 requiert une provenance vérifiable et T12 archive les octets bruts.
- **Décision proposée** : calculer une empreinte de compte HMAC-SHA-256 avec clé aléatoire locale versionnée dans `app_settings`, sur le numéro canonique transitoire défini précisément dans `docs/IMPORT_CONTRACT.md`, sans exposer numéro ni empreinte dans les logs/API. Le mapping de toute empreinte inconnue est choisi explicitement par alias/rôle humain à l'aperçu et reste stable. Conserver les octets CSV exacts uniquement sous chiffrement authentifié local (AES-256-GCM, clé protégée hors du répertoire des archives) ; le hash d'idempotence porte sur les octets avant chiffrement.
- **Conséquences** : association stable sans numéro bancaire en clair dans la base ou l'archive ; la clé HMAC doit survivre aux sauvegardes/restaurations et toute rotation doit être auditée. Le chiffrement d'archive nécessite une primitive crypto maintenue et une procédure de récupération locale.
- **Arbitrage orchestrateur** : l'empreinte HMAC-SHA-256 du numéro de compte (clé locale dans `app_settings`, alias choisi par l'humain) est retenue. Le **chiffrement AES-256-GCM des archives est REFUSÉ pour la v1** : l'archive des octets bruts est conservée telle quelle sous `data/originals/` (ignoré par Git, permissions 0600, jamais exposée par l'API ni les logs), conformément au PLAN §D ; le SHA-256 d'idempotence porte sur ces octets. Motifs : usage local mono-utilisateur, risque de perte de clé qui rendrait les originaux (et la restauration T41) inutilisables, dépendance cryptographique supplémentaire. Le chiffrement d'archive pourra être ajouté ultérieurement par ADR. Le numéro bancaire n'est jamais écrit en base, en log ni dans une réponse d'API.

## ADR-011 — Clé d'identité prudente et casse préservée (proposée pour arbitrage G0)

- **Date** : 2026-10-09 · **Statut** : acceptée (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : l'export ne porte pas d'identifiant bancaire par opération ; une normalisation agressive peut fusionner deux opérations distinctes. IMP-08 exige que les différences de casse ne provoquent pas une fusion abusive.
- **Décision proposée** : la clé logique est `(account_id, booked_on, amount_cents signé, identity_label)` ; `identity_label` applique NFC et réduction des espaces Unicode seulement. La casse, les accents, chiffres et ponctuation sont conservés ; `raw_label` reste intact. L'unicité inclut un `occurrence_rank` entier positif ; le hash de tuple est auxiliaire, les colonnes portent l'unicité SQL.
- **Conséquences** : une variation de casse peut laisser deux identités et être signalée comme candidate ; elle n'est jamais auto-fusionnée. Les alias de payee restent un mécanisme de présentation/classification séparé.

## ADR-012 — Réconciliation par maximum de multiplicité (proposée pour arbitrage G0)

- **Date** : 2026-10-09 · **Statut** : acceptée (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : les paiements sans ID unique peuvent être strictement identiques jusqu'à quatre fois ; une simple unicité de clé perd des occurrences, tandis que sommer les exports chevauchants crée des doublons.
- **Décision proposée** : par clé stricte, créer les rangs absents jusqu'à `max(n_existing, n_new)`. Chaque occurrence de chaque fichier distinct reçoit une `source_row`; le hash d'un fichier déjà commité crée une tentative `no_op`, sans transaction ni source_row nouvelle. Aucune absence dans un export ne supprime une transaction. Le maximum n'est autorisé que si les bornes de couverture prouvent des journées complètes et inclusives ; section tronquée ou explicitement partielle → groupe bloqué et soumis à résolution, car sans ID bancaire il est impossible de distinguer un doublon réexporté d'un nouveau paiement identique dans un extrait incomplet.
- **Conséquences** : `max` est commutatif pour A/B, respecte IMP-01..05/09 et rend l'ordre de lignes sans effet sur la cardinalité lorsque les fichiers sont complets. Les doublons stricts légitimes restent des transactions distinctes par rang ; la limite d'identifiabilité des extraits partiels est visible et n'est pas résolue par fusion probabiliste.
- **Arbitrage orchestrateur** : accepté tel quel. Précision : l'absence de plage déclarée ou une troncature structurelle bloque le commit **de la section concernée uniquement**, avec résolution humaine auditée ; les exports CA standard déclarent leur période (« entre le A et le B ») et sont traités comme des journées entières. Le maximum ne supprime jamais rien : un export partiel ne peut que sous-compter une identité (limite d'identifiabilité consignée dans le registre des risques).

## ADR-013 — Identifiants UUID opaques (proposée pour arbitrage G0)

- **Date** : 2026-10-09 · **Statut** : acceptée (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : les entités référencées dans l'API ont besoin d'identifiants stables ; un compteur exposerait l'ordre et dépendrait de la base physique, un numéro de compte ne peut en aucun cas servir de clé.
- **Décision proposée** : employer des UUIDv4 texte générés côté backend pour les comptes, transactions, imports, sources et décisions. Garder les dates `YYYY-MM-DD` comme colonnes de tri et les instants techniques en UTC `Z`.
- **Conséquences** : IDs opaques utilisables dans les liens d'audit/API et stables après backup/restore ; ils n'ordonnent pas les événements, donc toute pagination utilise une date métier puis l'UUID comme départage.

## ADR-014 — Pagination serveur par curseur signé (proposée pour arbitrage G0)

- **Date** : 2026-10-09 · **Statut** : acceptée (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : offset/limit devient instable lorsque des imports ajoutent des dates anciennes ou lorsque la file de revue change entre deux pages ; le plan demande tri stable par date et ID.
- **Décision proposée** : utiliser une pagination keyset opaque signée, bornée à 100 éléments et liée aux filtres. Transactions triées `(booked_on DESC, transaction_id DESC)` avec snapshot de création ; revue triée par priorité, date max puis clé stable et invalidée avec `CURSOR_STALE` si une décision change la file.
- **Conséquences** : pas de répétition/saut dû à un offset et résultats reproductibles pendant une navigation ; le client doit redémarrer une file de revue obsolète.

## ADR-015 — Idempotence des mutations et des imports (proposée pour arbitrage G0)

- **Date** : 2026-10-09 · **Statut** : acceptée avec amendement (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : le navigateur ou SQLite peuvent réessayer une requête après timeout ; un import identique doit rester traçable sans produire une seconde mutation.
- **Décision proposée** : chaque mutation HTTP porte `Idempotency-Key`; stocker clé, empreinte canonique de requête et réponse pour rejouer la même opération, et répondre `409` si le corps diffère. Réserver le hash complet aux imports committés via unicité partielle ; un nouvel essai au hash connu est un `no_op` audité, tandis qu'un hash rejeté peut être réessayé après correction du parseur.
- **Conséquences** : retry réseau sans double décision ou double import. Les réponses persistées n'incluent aucun secret ni libellé brut au-delà du besoin contractuel ; les entrées sont redigées et expirées selon une politique documentée.
- **Arbitrage orchestrateur** : `Idempotency-Key` est **obligatoire pour les POST de création/commit/application** (import commit, décisions, application de règle, backup) et optionnelle pour PUT/PATCH/DELETE déjà protégés par `version` optimiste. Les GET n'en portent pas.

## ADR-016 — Migrations Alembic monotones, propriétaire unique (proposée pour arbitrage G0)

- **Date** : 2026-10-09 · **Statut** : acceptée (arbitrage orchestrateur, 2026-10-09)
- **Contexte** : plusieurs tickets travailleront sur SQLite ; modifier une révision déjà appliquée peut rendre une base locale irréparable.
- **Décision proposée** : un seul propriétaire par lot de migration, parents/révisions Alembic linéaires, sauvegarde SQLite cohérente et vérifiée avant upgrade, `upgrade` et `downgrade` raisonnables. Un downgrade n'est accepté que s'il préserve les données ; toute transformation irréversible s'arrête explicitement et demande une restauration sûre ou une migration corrective vers l'avant. Une migration appliquée n'est jamais réécrite.
- **Conséquences** : aucun patch concurrent sur un même historique de migration ; les changements de catégories, identité ou flux nécessitent backfill/version explicites et tests sur copie.
