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
