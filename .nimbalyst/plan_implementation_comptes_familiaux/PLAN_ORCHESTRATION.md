# PLAN D'IMPLÉMENTATION — Comptes familiaux

**Document directeur pour un orchestrateur multi-agents**  
**Version :** 2.0 · 9 octobre 2026  
**Statut :** spécification d'exécution, pas code applicatif  
**Cible :** site local de suivi des finances d'un compte commun familial, à partir de CSV Crédit Agricole, avec ventilation semi-automatique, analytique détaillée et commentaires IA.  
**Période de démarrage :** **2026 exclusivement** pour la base métier initiale ; l'export 2025 reste facultatif comme échantillon technique et ne doit pas être importé dans les données réelles par défaut.  
**Autorité :** ce document et les contrats annexes prévalent sur le rapport de cadrage v1 lorsqu'ils diffèrent, notamment sur la sortie des LLM.

---

## A. Mandat de l'orchestrateur

Tu pilotes l'implémentation d'un **produit réellement utilisable**, et non une démonstration visuelle. Délègue des tâches petites, testables et isolées à des sous-agents configurés en *gpt-luna* **high/xhigh** si cette configuration est accessible dans ton environnement. Ne présume pas de l'existence d'une capacité qui n'est pas fournie : adapte les noms des modèles/outils sans changer le protocole de contrôle qualité.

Pour chaque tâche :

1. Fournir à l'agent : **objectif**, contexte métier limité mais suffisant, **fichiers autorisés**, interfaces existantes, dépendances, tests à créer, critères d'acceptation, interdictions.
2. Exiger un **plan court avant modification** des fichiers et une implémentation suivie de tests ; éviter les réécritures opportunistes.
3. Produire une branche/zone de travail isolée par agent lorsque l'environnement le permet. Ne pas laisser deux agents modifier simultanément les mêmes fichiers ou les migrations.
4. Faire relire le résultat par un **agent critique distinct** pour la logique monétaire, l'idempotence, la sécurité, les tests et la cohérence avec ce plan.
5. Ne fusionner qu'après vérification des tests pertinents, lint, typage, migrations, compatibilité API, documentation et démonstration des cas d'usage.
6. Tenir à jour `docs/DECISIONS.md`, `docs/ETAT_IMPLEMENTATION.md` et `docs/REGISTRE_RISQUES.md`. Toute modification des invariants ou de la taxonomie est une décision d'architecture, pas une initiative silencieuse de sous-agent.
7. Suspendre la progression à un **jalon bloquant** (gate) si une propriété métier critique n'est pas prouvée, même si le frontend « a l'air fini ».
8. Après chaque lot, présenter une courte synthèse : livrables, tests réellement exécutés, échecs, incertitudes, prochaine dépendance. **Ne jamais affirmer qu'un test a passé sans trace d'exécution.**

### Ordre des priorités en cas de conflit

**Intégrité des données > exactitude comptable > protection de la vie privée > traçabilité des décisions > ergonomie de la revue > justesse des statistiques > esthétique > richesse des graphiques > sophistication IA.**

### Ce qui est fixé

- Usage **local sur une machine**, mono-utilisateur en v1 ; aucune connexion bancaire requise ; CSV fournis manuellement chaque mois.
- **Backend Python/FastAPI + SQLite + SQLAlchemy/Alembic**, **frontend React/TypeScript/Vite + Apache ECharts**, API REST locale versionnée (`/api/v1`).
- Une base pour 2026, dans laquelle les opérations bancaires brutes sont immuables ; les catégories et décisions sont stockées séparément.
- Mémoire de ventilation, règles validées et arbitrage humain prioritaires sur le LLM.
- LLM **facultatif**, fourni via endpoint **OpenAI-compatible**, clé backend uniquement. **Aucun JSON strict / tool calling / schema de sortie exigé du LLM.** Demander du texte compact et tolérer les réponses partiellement mal formées ; valider le résultat normalisé *dans notre application* avant usage.
- Les LLM proposent, l'humain peut corriger, les règles fiables peuvent appliquer. Le modèle seul ne promeut jamais une suggestion en règle globale autoritaire.
- Les dépenses, les transferts d'épargne, les allocations vers comptes personnels, les emprunts, les mouvements exceptionnels et les revenus ne sont pas interchangeables.
- Les deux salaires entrent sur le compte commun ; ménage, garde, courses, enfants, abonnements, loisirs, retraits cash et achats multi-usage font partie des cas à traiter.

### Hors périmètre v1

Connexion directe aux banques, OCR/PDF de relevés, multi-utilisateur distant, application mobile native, récupération automatique d'achats détaillés chez les marchands, véritable bilan patrimonial sans autres comptes, outils internet accordés librement au LLM, base vectorielle obligatoire, apprentissage supervisé entraîné sur les données privées, synchronisation cloud, contrôle budgétaire normatif. Ne pas rajouter ces fonctionnalités pour « faire plus complet ».

---

## B. Invariants non négociables (à encoder en tests)

**INV-01 — Source vérifiable.** Chaque transaction provient d'une ligne/occurrence de CSV conservée et traçable jusqu'à l'import. Les champs d'origine ne sont jamais écrasés par la normalisation.

**INV-02 — Centimes exacts.** Montants signés en centimes entiers, jamais `float` pour calculer ; débit négatif, crédit positif ; total crédits + débits = variation bancaire calculée.

**INV-03 — Non-duplication.** Réimporter le même fichier, ou plusieurs exports chevauchants, ne recrée pas les transactions existantes. Des occurrences réellement identiques présentes deux fois **dans un seul CSV** restent deux transactions.

**INV-04 — Atomicité.** Un import interrompu, invalide ou concurrent ne laisse aucune transaction partiellement importée. Les fichiers bruts peuvent être archivés comme « rejetés », sans altérer les écritures validées.

**INV-05 — Multi-comptes.** Chaque opération est attachée à son compte. Aucun agrégat de flux ne confond courant, épargne, PEA ou compte personnel ; le périmètre de l'agrégat est explicite.

**INV-06 — Sommes d'allocations.** Pour une transaction classée, la somme de ses allocations **signées** vaut exactement son montant signé ; pas de résidu perdu ; transactions non ventilées identifiables et visibles.

**INV-07 — Flux distincts.** Une sortie vers un livret n'est pas une dépense de vie. Un crédit provenant d'un emprunt n'est pas un salaire. Un remboursement de capital n'est pas une consommation, mais reste une sortie bancaire.

**INV-08 — Priorité de l'humain.** Une décision humaine acceptée n'est jamais écrasée par réimport, nouveau prompt LLM, modification de règle ou recalcul d'analyse. Modification explicite et auditée seulement.

**INV-09 — Justification.** Tout chiffre affiché expose la formule, les dates, comptes, exclusions et transactions sous-jacentes. Les chiffres ne sont jamais « inventés » par l'IA.

**INV-10 — Incertitude visible.** Mois en cours, trou de couverture, montant non ventilé et catégorie suspecte doivent être signalés. Ne pas comparer mécaniquement octobre 2026 partiel avec un mois plein.

**INV-11 — Confidentialité.** Secrets hors code/frontend/logs ; aucune transaction transmise à un service distant sans choix explicite de l'utilisateur ; la configuration LLM non activée laisse toute l'application fonctionnelle.

**INV-12 — Résilience LLM.** Une ligne de réponse illisible, un ID absent, une catégorie inconnue, un timeout ou une erreur du fournisseur n'empêchent pas de continuer les autres classifications et ne corrompent pas la base.

**INV-13 — Pas de catégorisation massive par analogie vague.** « Amazon », « Vinted », « Carrefour Banque », retraits et virements particuliers ne sont pas automatiquement classés comme un poste précis sans règle à portée contrôlée.

**INV-14 — Réversibilité.** Toute action de masse, fusion de marchands, reventilation ou restauration est précédée d'un aperçu / sauvegarde pertinente et peut être annulée ou corrigée via un historique intelligible.

---

## C. Modèle métier et vocabulaire commun

### C1. Transaction ≠ allocation ≠ suggestion

- **Transaction** : mouvement réellement inscrit sur le relevé, montant signé, date comptable, compte, libellé brut, référence à l'import.
- **Allocation** : répartition de ce mouvement dans une ou plusieurs catégories de **flux** et éventuellement dans des sous-catégories de consommation, avec tags et provenance. Une transaction peut être fractionnée ; l'UI ne doit jamais prendre une catégorie de marchand pour une vérité générale.
- **Suggestion** : hypothèse non appliquée du moteur, avec motif, méthode et statut ; peut être révisée.
- **Règle** : critère explicite de correspondance avec champ de portée (`exact_payee`, `label_pattern`, `payee_plus_context`, type de flux), priorité, exceptions et preuve de validation.
- **Décision** : action humaine validant ou remplaçant une suggestion, éventuellement applicable à une sélection d'opérations ou aux futurs matchs.
- **Marchand/counterparty** : acteur normalisé distinct des étiquettes bancaires ; la fusion de deux marchands est auditée et annulable.
- **Compte** : identifiant technique local, alias humain, rôle (courant, épargne, PEA, personnel, espèces virtuelles), inclusion dans les vues. Ne pas faire dépendre la clé primaire du numéro de compte bancaire.

### C2. Type de flux et catégories : deux dimensions différentes

Une opération a un `flow_kind` parmi : `expense`, `income_recurring`, `income_other`, `transfer_savings`, `transfer_owned`, `allocation_personal`, `loan_disbursement`, `loan_repayment`, `capital_event`, `cash_withdrawal_pending`, `refund`, `intermediary_pending`, `reconciliation`, `unknown`. Chaque type impose les catégories admissibles ; utiliser les IDs stables de `config/categories.v1.yaml`. La taxonomie est **versionnée** ; ne pas supprimer/renommer des IDs sans migration.

- Les comptes personnels reçoivent une **enveloppe d'allocation personnelle** : sortie du commun, visible dans un budget familial, mais pas dépense finale observée. Il faut une option de lecture « coût de vie » et « coût de vie + allocations personnelles », jamais leur addition à de nouvelles opérations du même compte si celui-ci est importé plus tard.
- Un retrait d'espèces non documenté est en attente (`cash_withdrawal_pending`), sans conclusion automatique « ménage ». L'opérateur peut expliquer la destination et affecter tout ou partie du retrait ; éviter le double comptage en cas de suivi ultérieur des espèces.
- Un remboursement d'achat (`refund`) peut être lié à l'achat initial pour le rendre **net** dans la bonne catégorie, tout en restant crédit bancaire positif. Sans lien validé, ne pas l'affecter arbitrairement.
- Emprunts : sans échéancier renseigné, laisser `loan_repayment` non scindé ; ne pas deviner capital/intérêts.
- Carrefour Banque : `intermediary_pending` ou proposition spécifique tant que le détail du mécanisme n'a pas été confirmé.

### C3. Formules canoniques côté backend

- `bank_net_flow = sum(transaction.amount_cents)` sur la sélection ; **toujours** égal à `credits - debits`.
- `regular_income = sum(allocations tagged income_recurring)` ; exclut prêts et revenus extraordinaires.
- `consumption_net = -sum(signed allocations of expense and matched purchase refunds)` ; un achat débité augmente les dépenses ; un remboursement apparié les diminue.
- `personal_allowances = -sum(signed allocations allocation_personal)`.
- `savings_net_transferred = -sum(transferts signés visibles sur le compte courant et liés à une épargne identifiée)` : un virement **−200 €** vers épargne puis un retour **+50 €** donne **+150 €** d’épargne transférée nette. Si les deux comptes sont importés, apparier les deux jambes et ne compter chaque transfert qu’une fois.
- `operational_saving_capacity = regular_income - consumption_net - personal_allowances - other explicitly selected cash commitments`, sans compter deux fois les échéances/frais ; toujours afficher sa définition exacte dans l'UI.
- `operational_saving_rate = saving_capacity / regular_income` uniquement lorsque dénominateur et couverture sont valides.
- `unclassified_amount_share = abs(sum unclassified relevant amounts) / sum abs(all relevant amounts)` ; afficher également couverture en nombre d'opérations ; ne pas confondre avec total de flux bancaires exceptionnels.

À spécifier dans `docs/METRICS.md` avec exemples chiffrés synthétiques et méthodes communes à tous endpoints. Si le type `loan_repayment` est non scindé, **ne pas le masquer** : afficher dans cash-flow et engagements financiers, le signaler comme limitation pour le coût total du crédit.

---

## D. Architecture et structure cible

```text
comptes-familiaux/
  AGENTS.md                        # protocole de travail de l'orchestrateur
  README.md
  .gitignore                       # data/, .env, *.sqlite*, fixtures bancaires privées
  .env.example
  pyproject.toml                   # gestion Python reproductible
  frontend/package.json            # dépendances lockées
  config/categories.v1.yaml
  config/rules.seed.yaml            # règles prudentes; jamais de PII
  backend/app/
    main.py
    api/v1/{health,imports,accounts,transactions,categories,rules,review,analytics,insights,settings,backups}.py
    core/{config,security,logging,errors}.py
    db/{base,session,models}.py
    db/migrations/
    domain/{types,entities,metrics}.py
    importers/{credit_agricole,encoding,normalize,overlap}.py
    classification/{engine,matcher,rule_store,suggestions,llm_text_parser,llm_client}.py
    analytics/{definitions,queries,quality,series,forecast}.py
    services/{imports,allocations,review,backups}.py
  backend/tests/{unit,integration,contract,golden}/
  frontend/src/
    app/{router,providers}/
    pages/{Overview,Expenses,Income,Family,Merchants,Budgets,Review,Insights,Transactions,Settings}/
    components/{layout,filters,charts,transactions,classification,ui}/
    lib/{api,format,money,dates,filters}/
    types/
  frontend/tests/{unit,component,e2e}/
  docs/{ARCHITECTURE,DECISIONS,DOMAIN_MODEL,IMPORT_CONTRACT,METRICS,API,PRIVACY,TEST_MATRIX,ETAT_IMPLEMENTATION,REGISTRE_RISQUES}.md
  scripts/{dev,run,backup,restore,verify-data}.sh
  data/{db,originals,backups}/      # uniquement local, ignoré par Git
```

Arbitrages : pas de Redis, Celery, Postgres, Docker ou monorepo complexe nécessaires à la v1 ; privilégier de simples fonctions/modules testables et des jobs synchrones ou localement pilotés avec progression quand nécessaire. Le build frontend de production est servi en loopback par l'application locale. API en `127.0.0.1` seulement ; toute requête de mutation doit respecter une défense anti-CSRF / origine locale et validation côté serveur. Les calculs sont dans le backend, les graphiques n'effectuent que la présentation.

### Protocole API

- Namespace `/api/v1`, réponse réussie à champs explicites, erreurs normalisées **par notre backend** (`code`, `message`, `details` utiles, pas de PII). **La restriction « pas de JSON strict » porte uniquement sur la *sortie du modèle LLM*, pas sur les contrats API REST ni sur les données métier.**
- Pagination serveur des transactions et file de revue, tri stable (`date`, `id`) et filtres synchronisés dans l'URL du frontend.
- Les mutations ont identifiant de demande idempotent quand approprié, statut, `version` optimiste ou équivalent pour éviter écrasements lors de deux modifications.
- Pour tous les endpoints analytiques, retourner `period`, `accounts`, `coverage`, `filters`, `metric_definition_id`, `raw_cents` et unités quand cela a du sens.
- Un `GET /api/v1/health` doit fonctionner en mode totalement hors ligne.

### Modèle relationnel minimum

`accounts`, `import_files`, `import_account_sections`, `source_rows`, `transactions`, `payees`, `payee_aliases`, `categories`, `allocations`, `classification_suggestions`, `classification_decisions`, `rules`, `rule_executions`, `transaction_links` (ex. remboursement), `audit_events`, `app_settings`, `llm_runs`, `budgets`, `insights`, `backup_manifests`.

Contraintes de DB à imposer : FK, unicité de `(import_file_id, section, logical_row_idx)` pour les lignes sources, hash entier de fichier unique quand import commité, `UNIQUE` contrôlée pour identité d'occurrence *par réconciliation*, contraintes de signes/centimes, index `account_id + booked_on`, index classification/review, migrations Alembic monotones. Une ligne source de chaque import peut référencer une transaction déjà créée par un import antérieur ; ne pas recréer celle-ci.

---

## E. Organisation des sous-agents

| Rôle | Responsabilités | Ne fait pas |
|---|---|---|
| **ARCH** | Contrats, invariants, schéma, décisions, migrations délicates | Remplacer seul un schéma accepté sans revue |
| **DATA** | Parsing CSV, anti-doublons, normalisation, rapprochement | Modifier les UI/définitions KPI |
| **DOMAIN** | Allocation, règles, mémoire, historique, classification | Appeler le LLM ou décider d'une catégorie métier sans traces |
| **AI** | Connecteur OpenAI-compatible, parser texte tolérant, suggestions et analyses écrites | Écrire les allocations finales ou accepter ses propres propositions |
| **ANALYTICS** | Formules, requêtes, qualité des périodes et données | Recalculer ad hoc des agrégats dans React |
| **UI** | Navigation, filtres, tableaux, graphiques, accessibilité | Modifier la logique métier cachée dans le client |
| **QA** | Tests unitaires, intégration, fixtures adversariales, E2E, revue critique | Adapter les tests pour masquer une erreur sans avis |
| **OPS** | Installation locale, sécurité, sauvegarde, doc d'exploitation | Exposer API ou clés sur réseau public |

Un agent peut prendre plusieurs rôles sur de petits lots, mais **chaque fonctionnalité critique doit recevoir une revue croisée** par une perspective indépendante. Donne aux agents le chemin exact du fichier de contrat à respecter et les cas d'essai minimaux. L'orchestrateur reste seul propriétaire des choix d'architecture et de la séquence de fusion.

### Format obligatoire d'une carte de tâche

`ID / Titre / Lot / Priorité / Dépendances / Agent / Périmètre fichiers / Entrées / Travail détaillé / Sorties / Tests (unitaires + intégration) / Critères d'acceptation binaires / Preuves à remettre / Risques / Hors périmètre`.

**Compte-rendu de sous-agent** : résumé des changements ; fichiers modifiés ; tests exécutés et résultats ; hypothèses ; limites ; erreurs détectées ; instructions de reproduction. Un agent qui omet ces éléments doit compléter sa livraison avant validation.

---

## F. Plan d'exécution séquencé et tests associés

> **Estimation en lots, pas en jours :** travailler dans l'ordre des gates ; certains tickets sont parallélisables après gel des contrats. Les IDs correspondent à `backlog.yaml`. Chaque lot porte ses preuves de validation.

### LOT 0 — Spécifications exécutables et fondations des tests [P0]

**Objectif :** fermer les ambiguïtés qui pourraient provoquer une refonte ultérieure.

- **T00 — Installer les conventions de collaboration** : `AGENTS.md`, politique des branches/reviews, `DECISIONS.md`, `.gitignore`, README initial, ligne de commande standard des tests.
- **T01 — Écrire le contrat financier** : glossaire, `flow_kind`, formule des métriques, crédits/débits, catégories compatibles, états d'allocation, règles de priorité ; créer une dizaine d'exemples numériques synthetic et leurs résultats attendus.
- **T02 — Fixer les contrats API et tables** : schéma SQL/diagramme, transactions immuables, source_rows multi-import, provenance des modifications, identifiants stables, stratégie de migration/versionnement.
- **T03 — Fabriquer fixtures publiques synthétiques** : CSV style CA 2026 en CP1252 et UTF-8, CRLF, libellés multilignes, entêtes de section, accents, espaces insécables, collisions et chevauchements, données « banque » adversariales, scénarios de finances familiales.
- **T04 — Construire le corpus d'acceptation privé local (non commité)** : pointer un test opt-in sur le véritable CSV 2026, noter le comptage attendu **1 024 opérations**, 2026-01-01 → 2026-10-09, les répétitions légitimes et opérations exceptionnelles repérées ; ne stocker aucun libellé sensible dans le dépôt.

**Tests / gate G0 :** chaque invariant INV-01..14 a au moins un test prévu et un propriétaire ; fixtures synthétiques sans PII ; l'orchestrateur valide la taxonomie et les deux lectures (banque / vie familiale) avant création du code métier. **2025 n'est pas un trou à réparer.**

### LOT 1 — Squelette exécutable, DB et intégration continue [P0]

- **T05 — Bootstrap backend** : FastAPI, config externalisée, health, erreurs cohérentes, DB injectable, logging redigé, commandes de dev reproductibles ; installation verrouillée.
- **T06 — Modèles SQLite / Alembic** : tables prioritaires, FK, contraintes, indexes, horodatages UTC, alias de comptes sans PII, migration `up` et `down` raisonnable (avec sauvegarde) ; migration de fixture en DB fraîche.
- **T07 — Bootstrap frontend** : React+TS+Vite, navigation, shell accessible, thème sobre responsive, client API typé, erreur/loading/empty state et test basique.
- **T08 — Automatisation qualité** : pytest + coverage, Ruff/typage Python, lint/TS, Vitest/Testing Library, Playwright ; commande `make check` ou équivalent ; pipeline CI sur fixtures synthétiques, jamais sur données privées.

**Gate G1 :** démarrage local sans LLM ni CSV, `health=OK`, frontend sert une page et consomme `health`, migrations passent deux fois sans modification indésirable, suite verte sur environnement propre.

### LOT 2 — Lecteur Crédit Agricole et import aperçu [P0]

- **T09 — Parser CA** : support de préambule, sections, en-tête `Date;Libellé;Débit euros;Crédit euros;`, `;`, champs guillemetés multilignes, CP1252/UTF-8, montants français, dates, soldes et détection de comptes ; aucune regex `split('\n')` destructrice.
- **T10 — Normalisations prudentes** : `raw_label` intouché, `identity_label` pour rapprochement peu destructeur (Unicode/espaces seulement), `search_label`/marchand distincts pour comparaison floue. En cas de collision incertaine, revue, pas rapprochement silencieux.
- **T11 — Preview import** : drag&drop et sélection de fichier, API preview **sans écriture métier**, comptes détectés, couverture 2026, nombre d'opérations, sommes crédits/débits, anomalies par ligne et exemples masqués, possibilité d'annuler.

**Tests / gate G2 :** les fixtures passent ; conversion exacte à 1 centime ; CSV hostile/imparfait sans plantage ni import partiel ; vrai fichier 2026 testé *si disponible localement* : **1 024** écritures lues, autres sections signalées, dates/limites correctes. Le preview ne modifie pas le nombre de transactions.

### LOT 3 — Commit idempotent et qualité de couverture [P0, CRITIQUE]

- **T12 — Import atomique** : commit SQL transactionnel, archivage source, hash SHA-256 de fichier, relation `source_rows → transactions`, restrictions d'accès au fichier, erreurs récupérables.
- **T13 — Réconciliation à multiplicité** : comparaison par `(compte, date, montant signé, libellé d'identité)` + multiplicité par fenêtre/source ; gérer strictement plusieurs opérations réellement identiques, recouvrement incomplet, nouveaux doublons dans export suivant, imports en ordre inversé, import simultané ; conserver une source_row par occurrence et par fichier importé.
- **T14 — Couverture et contrôle des soldes** : dates couvertes vs opérations constatées, mois complets/partiels, comparaison soldes d'en-tête si cohérentes, import journalisé avec résultat créations/liens/erreurs ; ne pas traiter les mois avant 2026 comme absents.
- **T15 — UI des imports et opérations brutes** : historique d'import, liste filtrable/paginée des opérations, compte, date, sens, montant, détail brut, source, état ; afficher anomalies sans inventer de catégorisation.

**Tests / gate G3 (bloquants) :** réimport même fichier → **0 transaction nouvelle** ; CSV chevauchant → uniquement delta ; `-11,23` et `+11,23` ne s'annulent pas comme identités ; 2 lignes identiques conservées ; concurrence ne duplique pas ; échec provoqué au milieu → rollback total ; totalité des centimes réconciliée. **Livrable utilisable hors IA : import mensuel fiable + exploration.**

### LOT 4 — Moteur de catégories et historique sans IA [P0, CRITIQUE]

- **T16 — Taxonomie versionnée** : importer `categories.v1.yaml`, valider IDs uniques et parents, catégories de flux et sous-catégories, migration/règles de désactivation, attributs transversaux (fixe, flexible, bénéficiaire, neuf/occasion, ponctuel, exceptionnel).
- **T17 — Allocations contrôlées** : service d'affectation unitaire et de fractionnement avec calculs en centimes, somme exacte, types admissibles, remboursements liés, éventuels changements de `flow_kind`, optimistic concurrency et transaction SQL ; garder le mouvement bancaire inchangé.
- **T18 — Normalisation des contreparties et mémoire** : alias prudents, reconnaissance d'occurrences antérieurement validées, limites de portée, aucun regroupement irréversible sur simple similarité ; distinction Carrefour Banque / magasin.
- **T19 — Règles déterministes** : règles ordonnées avec portée explicite, simulation/dry-run affichant les mouvements touchés, priorité humain > règle approuvée > mémoire exacte > heuristique > suggestion LLM (ordre concret final à consigner) ; exception par transaction. Aucune rétroactivité silencieuse.
- **T20 — Journal / annulation** : audit avant/après, auteur/méthode, révision de version, retour arrière des opérations de masse sans altération de la source bancaire.

**Tests / gate G4 :** split `-10000` cents en `-7000/-3000` passe ; `-7000/-2999` échoue ; protection contre allocation de `+` dans une dépense sans notion de remboursement ; Vinted classé une fois n'entraîne pas une règle globale ; correction manuelle résiste à nouvel import/règle ; simulation de règle préserve DB ; rollback de correction reproduit les soldes analytiques initiaux.

### LOT 5 — File de revue humaine, règles UX et mémoire [P0]

- **T21 — APIs de revue** : file groupée par contrepartie/qualité, agrégation nombre+euros, tri par impact et nouveauté, action confirmer/éditer/classer/séparer/ignorer, proposition future vs choix sur sélection.
- **T22 — Page de ventilation** : traitement rapide au clavier et souris, détail et montants, filtre indéterminés, modification multi-catégorie, choix « cette occurrence / cette sélection / futures correspondances », aperçu de règles, feedback immédiat.
- **T23 — Évaluation des règles** : compteur de couverture, euros validés, part « humain/règle/mémoire/suggestion », détection d'alias ambigus et conflits ; métriques de qualité sans se laisser tromper par les flux immobiliers.

**Gate G5 :** une personne peut ventiler, corriger et créer une règle sans SQL, sans IA, sur données fictives et réelles locales ; le rechargement conserve tout ; un achat Amazon isolé n'affecte pas ses voisins sans consentement ; l'UI accessible avec navigation clavier.

### LOT 6 — API analytique et cohérence comptable [P0, CRITIQUE]

- **T24 — Moteur de métriques** : `docs/METRICS.md`, implémentation unique des formules, agrégation dépenses, revenus, transferts, allocations personnelles, épargne, flux bancaire, couverture, delta mensuel ; périodes complètes et sélection cohérente de comptes.
- **T25 — API requêtes analytiques** : endpoints overview, cashflow, spending hierarchy, series, merchants, family, savings, data quality, drilldowns vers IDs de transactions ; performance mesurée sur 10k/100k lignes synthétiques.
- **T26 — Golden tests analytiques** : cas numériques indépendants pour brut/net, remboursement apparié, prêts, transfert aller-retour, espèces, allocation personnelle, dépenses fractionnées, périodes incomplètes, « inconnu ».

**Gate G6 :** `bank_net_flow` == somme des transactions pour chaque filtre ; `allocations` recollent avec la banque ; les cartes KPI ne comptent jamais 50 000 € de prêt comme salaire ni 98 265,61 € de capital comme alimentation ; données indéterminées signalées ; tous endpoints ont tests de drilldown cohérents.

### LOT 7 — Dashboards indispensables et exploration [P0]

- **T27 — Navigation et filtres communs** : choix plage/année (défaut 2026), mois complet uniquement lorsque comparaison, compte, catégorie, statut, mode « trésorerie / vie familiale », filtres synchronisés URL, état vide et avertissements qualité.
- **T28 — Cockpit** : KPI, séries revenus-dépenses-épargne, catégories mensuelles, Sankey traçable, variations vs période comparable ; cartes explicatives et accès à liste des opérations.
- **T29 — Dépenses/marchands/revenus** : treemap, heatmap catégorie×mois, 100 % empilé, Pareto des marchands, fréquence/ticket médian, waterfall trésorerie, tendances salaires/épargne ; drilldown fonctionnel sur chaque graphique.
- **T30 — Qualité visuelle/accessibilité** : libellés français, nombres FR, euros cents, responsive desktop/tablette, contraste, lecture au clavier, tooltips informatifs, affichage lisible de périodes manquantes et partialité.

**Gate G7 — MVP analytique** : une base 2026 importée, une portion validée, et des graphiques sincères ; navigation carte→série→transactions reproduit les mêmes totaux à 1 centime ; aucun graphique non vide n'est alimenté par des données mockées en mode réel ; screenshot E2E de la vue d'ensemble, dépenses et revue sans PII.

### LOT 8 — LLM de ventilation optionnel, texte souple [P1]

- **T31 — Connecteur de modèle** : paramétrer `base_url`, modèle, secret local, timeout, tentatives raisonnables, taille des lots, limites de coût, simulation/test de connexion, mode off ; interface mockable ; appels backend uniquement. Éviter les dépendances à `response_format=json_schema` / tool calls.
- **T32 — Prompt et parseur tolérant** : protocole dans `docs/PROTOCOLE_LLM_TOLERANT.md` : petites lignes `TX-ID | catégorie_id | doute/certain/inconnu | raison`, séparateurs facultatifs, français libre comme fallback, labels multi-lignes, champs absents ; récupération par transaction, gestion réponses tronquées et IDs dupliqués. **Pas de plantage global sur réponse irrégulière.**
- **T33 — Chaîne des suggestions** : ne soumettre que nouveaux libellés/ambigüités, prévisualiser le texte envoyé et le caviardage, n'exposer que IDs temporaires, limiter stockage des prompts/logs ; valider catégorie et flux admis après parsing ; suggestion mise en file de revue, jamais allocation autoritaire.
- **T34 — Mesurer la qualité LLM** : tests sans réseau avec fake serveur et 30+ réponses corrompues ; corpus de classification manuelle ; scores précision/couverture/taux de correction/hallucinations ; suivi consommation/erreurs, retour rapide au mode manuel.

**Gate G8 :** application fonctionne hors ligne ; arrêt connexion/model → file de revue utilisable ; une réponse « presque bonne » classe les autres lignes et ne provoque pas exception ; proposition inconnue devient `review/unknown` ; nouveau fournisseur OpenAI-compatible avec simple texte ne nécessite pas de modifier les contrats métier. **La sortie du LLM est tolérante ; l'acceptation des données reste rigoureuse.**

### LOT 9 — Budgets, prévisions et vues famille [P1]

- **T35 — Enfants et foyer** : garde, école, bébés, vêtements enfants, activités, bénéficiaires si connus ; aide CAF uniquement en compensation si clairement rapprochée ; comparaison brut et reste à charge.
- **T36 — Budgets et scénarios** : enveloppes mois/catégorie, réalisés/écarts, objectifs d'épargne, scénario paramétrable (`restaurants -15 %`, `abonnements -10 %`, etc.), prévision par historiques complets et exclusion d'exceptionnels ; hypothèses et incertitude affichées.
- **T37 — Abonnements et charges récurrentes** : heuristiques de périodicité, fréquence, montants variables, faux positifs signalés, hausse de tarif, calendrier approximatif, possibilité de marquer un faux abonnement.

**Gate G9 :** pas de prévision calculée à partir d'une fenêtre trop petite sans avertissement ; aucun frais de garde « net » non expliqué ; les budgets ne modifient pas les transactions ni les règles.

### LOT 10 — Observations IA et recommandations vérifiables [P1]

- **T38 — Moteur de faits** : produire dans le backend une liste de constats numériques attribuables (`metric_id`, dates, valeurs, comparateur, couverture, liens de drilldown), par exemple « restaurants +18 % vs médiane de 3 mois complets ».
- **T39 — Synthèse IA** : LLM rédige une synthèse et suggestions en **texte souple**, jamais la source des valeurs. Filtrer les énoncés non rattachables ; afficher les hypothèses, zones non ventilées et limites ; génération explicite par utilisateur.
- **T40 — Page Insights** : tendances, variations, questions à résoudre, scénarios d'économie, chaque affirmation liée aux chiffres/transactions ; sauvegarde locale des analyses opt-in et contrôle « ignorer ».

**Gate G10 :** forcer volontairement un LLM à annoncer un chiffre faux : l'UI n'expose pas ce chiffre comme fait vérifié ; chaque affirmation chiffrée renvoie à une métrique serveur ; si aucune analyse fiable disponible, affichage « données insuffisantes » et non invention.

### LOT 11 — Durcissement, sauvegarde, release locale [P0 pour sauvegarde/sécurité, P1 pour perfectionnement]

- **T41 — Backup/restore/export** : export DB cohérent via l'API SQLite de backup (pas copie brute d'un fichier WAL en cours d'écriture), manifeste versionné, option d'inclure CSV originaux, restore dans DB temporaire validée puis swap sûr, audit ; export standardisé CSV.
- **T42 — Sécurité locale et vie privée** : binding `127.0.0.1`, garde CSRF/origin pour mutations, CORS minimal, upload size limits, protections sur chemins, no HTML LLM brut, logs redigés, secrets masqués, `data/` ignoré, tests d'absence d'appel sortant hors LLM opt-in.
- **T43 — Packaging et documentation** : scripts Windows/macOS/Linux adaptés selon cible réelle, création DB/migrations, commande unique de lancement, diagnostic, sauvegarde/restore, mode sans réseau, guide import mensuel et revue.
- **T44 — E2E de vie réelle** : première importation 2026, revue, règles, reimport, nouveau mois chevauchant, un split, remboursement, suppression logique éventuelle, calculs, backup, restore sur DB distincte, réexécution et égalité des résultats.
- **T45 — Revue finale et release** : correction bugs, dette priorisée, audit des invariants, tests performance et non-régression, génération CHANGELOG, tag v1.0-local ; preuves de gates G0..G11.

**Gate G11 — Acceptation produit :** installation sans données privées dans Git ; utilisateur peut importer mensuellement, catégoriser et corriger sans cloud, voir une lecture pertinente des comptes, sauvegarder/restaurer ; données 2026 inchangées après reprise ; 0 erreur d'intégrité ni fuite connue.

---

## G. Critères de livraison par jalon

| Jalon | Cible utilisable | Gates obligatoires | Livraison concrète |
|---|---|---|---|
| **M0** | Contrats et tests figés | G0 | Glossaire, métriques, schéma, fixtures anonymisées, backlog |
| **M1** | Import bancaire fiable | G1–G3 | App locale + SQLite + import 2026 + anti-doublons + exploration |
| **M2** | Ventilation opérationnelle | G4–G5 | Catégories, mémoire, règles, correction/split, historique, file revue |
| **M3 (MVP)** | Comptes et dashboards | G6–G7 | Cockpit, dépenses, revenus, marchands, filtres et drilldowns fiables |
| **M4** | IA de catégorisation | G8 | Connecteur optionnel, suggestions résilientes, retour manuel |
| **M5** | Budgets et conseils | G9–G10 | Enfants, charges récurrentes, scénarios, insights vérifiés |
| **M6** | Usage mensuel pérenne | G11 | Backup/restore, scripts, sécurité, guide utilisateur, E2E |

**Ordre d'attention :** le minimum sûr pour une utilisation régulière combine **M1 + M2 + calculs principaux M3 + backup de T41 + sécurité de T42**. Ne pas attendre la « phase finale » pour faire des sauvegardes après la première vraie utilisation.

---

## H. Plan de tests transversal

Réaliser des tests continuellement, pas seulement en phase finale. Chaque ticket ajoute un test qui échoue avant correctif et passe après lorsqu'un comportement est réparable. Voir la matrice détaillée `docs/MATRICE_TESTS.md`.

1. **Unitaires Python** (`pytest`) : parsing, argent, dates, identities, allocateur, rules, métriques et parser LLM. Les erreurs de logique monétaire doivent être testées par cas explicites, éventuellement property-based.
2. **DB/intégration** : SQLite temporaire réelle + migrations, transaction/rollback, contraintes et concurrence ; aucune banque fictive « simulée » à la place des contraintes SQL.
3. **Contrats HTTP** : `/api/v1`, validation, pagination, erreurs, consentement IA ; clients TypeScript alignés avec contrats exposés.
4. **Unitaires React** (Vitest/Testing Library) : tableaux de ventilation, filtres, boutons commit/annuler, empty/loading/error states, clavier, formats français.
5. **E2E navigateur** (Playwright) : import→preview→commit→filtre→review→dashboard→reimport→backup, sans accès réseau tiers.
6. **Golden financiers** : snapshot des chiffres pour un univers de dépenses synthétique entièrement connu, signatures exactes en centimes, explication des inclusions/exclusions.
7. **Property-based/métamorphiques** : permutation de l'ordre des lignes, import identique, ajout d'un vrai doublon, éclatement 70/30, application/désapplication d'une règle, offset transfert/retour, translation des dates hors période.
8. **Adversariaux** : CSV malformé/énorme, Unicode, multiline, injection dans libellés, réponses LLM tronquées, latence réseau, sources hostiles, vie privée, CSRF.
9. **Performance** : par exemple 10k/100k transactions synthétiques pour agrégats ; mesures avant budgets absolus ; tracer le plan SQL si lent, ne pas sacrifier exactitude.
10. **Tests d'acceptation manuels** : une personne non développeuse importe et comprend où va l'argent, ce qui manque et comment corriger ; pas besoin d'éditeur de base ou de terminal au quotidien.

### Seuils de qualité attendus

- **0** échec d'invariant financier ou d'import, **0** migration destructive non couverte par sauvegarde.
- **100 %** des transactions valides du CSV 2026 détectées et rapprochées ; import même fichier répétable à l'infini sans augmentation d'effectif.
- Pour les montants : exactitude à **1 centime**, pas une simple tolérance en euros.
- **100 %** des actions d'écriture avec gestion d'erreur et traçabilité ; un timeout modèle ne modifie aucune allocation acceptée.
- Tests unitaires couvrant systématiquement les chemins critiques ; ne pas utiliser un chiffre de couverture global comme substitut aux tests métier.
- E2E vert sur un navigateur principal ; aucune donnée réelle présente dans les artefacts de CI, snapshots, captures d'écran ou rapports publics.
- Mesurer la précision des règles et suggestions sur le corpus annoté ; ne pas imposer un seuil arbitraire au modèle pour déclarer une vérité. **Le seul seuil qui autorise l'application automatique est une règle déterministe explicitement approuvée.**

### Pratique CI locale recommandée

`make lint` → `make typecheck` → `make test-backend` → `make test-frontend` → `make test-integration` → `make test-e2e-smoke` → `make check`. Si `make` indisponible sur la cible, fournir équivalents `scripts/` et documenter la différence.

---

## I. Coordination et parallélisation

**Chemin critique :** T00–T04 → T05–T08 → T09–T11 → T12–T15 → T16–T20 → T21–T23 → T24–T26 → T27–T30 → T31–T34 → T35–T40 → T41–T45. Certains blocs se chevauchent sans être fusionnables avant leur gate.

**Parallèles conseillés :**
- Après T01/T02 : DATA prépare des fixtures et parseur ; UI prépare un shell statique ; OPS prépare CI, sans toucher aux migrations de ARCH.
- Après G3 : DOMAIN travaille l'allocateur, UI la liste des transactions ; QA construit les corpus et tests de priorité.
- Après G5 : ANALYTICS crée métriques et API ; UI construit composants de graphiques sur contrats synthétiques stricts ; QA maintient le golden dataset.
- Après G6/G7 : AI implémente provider et parser tolérant en indépendance ; UI construit le panneau de consentement, en s'appuyant sur mock HTTP.
- Dès G3 : OPS traite sauvegardes et sécurité, en branche séparée ; tests d'intégration avant usage de données réelles.

**Pas de parallélisme à risque :** deux agents sur la migration SQL identique, l'identité d'import en même temps que le schéma source_rows, deux équipes changeant la sémantique de `flow_kind`/`METRICS`, UI qui redéfinit indépendamment les formules.

### Procédure de correction d'un échec de gate

1. Faire enregistrer un test minimal reproduisant l'erreur (donnée synthétique), trace et cause probable.
2. Bloquer fusion du lot dépendant.
3. Déléguer la correction au rôle responsable et un audit du fix à QA / ARCH.
4. Exécuter la suite du lot, puis les golden tests et l'E2E concerné.
5. Inscrire dans `REGISTRE_RISQUES` toute limitation résiduelle ; jamais de contournement silencieux dans l'UI.

---

## J. Risques connus à conserver dans le suivi

| Risque | Signaux de détection | Politique de traitement |
|---|---|---|
| Exports non uniques sans ID banque | Identités `(compte,date,montant,libellé)` en collisions | Multiplicité conservée ; rapprochement ambigu en file d'audit ; jamais suppression automatique |
| Quelques écritures ambiguës importantes | Carrefour Banque, prêts, cash, virements particuliers | `flow_kind` prudent, revue priorisée aux gros montants |
| Fichier bancaire privé chez un agent / CI | Fichier réel copié dans fixtures, logs ou Git | Isolation, faux CSV pour CI, test réel opt-in et contrôle pré-commit |
| « Tout catégoriser » à tort | LLM trop assuré, règles merchant-wide | Validation humaine, scores de précision, portée de règle explicite, pas de confiance « 99 % » auto-déclarée |
| Des comparaisons fausses | mois en cours d'octobre, absence de 2025 | Fenêtres 2026 seulement, indicateur complet/partiel, « comparateur indisponible » |
| Chiffres contradictoires entre pages | filtre ou interprétation du prêt différents | Définitions uniques backend et drilldown testé |
| Risque de perte de DB | import massif, migration, disque | Backups avant actions destructives, restore automatique vérifié, intégrité SQLite |
| Serveur local exposé | 0.0.0.0, CORS `*`, absence origin checks | Bind loopback, limitations d'origine, tests réseau/sécurité |
| Provider OpenAI-compatible hétérogène | format refusé, tronqué, quotas, bloc de texte | Common denominator texte, erreurs partielles, provider mock, pas de dépendance aux tools/JSON |
| Effet de mode / UI trop vaste | multitude de graphiques au prix de la véracité | MVP import/ventilation/4 vues avant raffinements |

---

## K. Décision d'arrêt : définition du « projet terminé »

Ne déclarer **v1 utilisable** que si une session complète est reproductible : importer `CA20261009_221008.csv` (l'utilisateur le fournit localement), obtenir **1 024** transactions et la couverture 2026, revoir/ventiler des opérations récurrentes, distinguer prêt/épargne/argent personnel/dépenses, consulter cockpit et détails, recharger le même relevé sans duplication ni perte d'affectation, saisir un nouveau CSV chevauchant, restaurer une sauvegarde, utiliser l'application avec LLM désactivé. Chaque chiffre important doit être retrouvable dans la liste des transactions.

La **v1 enrichie** ajoute le LLM textuel résilient, les neuf groupes de vues, les budgets et les recommandations IA, sans affaiblir ces garanties.

**Consigne finale à l'orchestrateur :** livre dans cet ordre, prouve chaque gate, refuse les optimisations prématurées, et traite le LLM comme une aide faillible. Le résultat attendu est une comptabilité familiale exploratoire robuste, pas un dashboard fondé sur des suppositions.
