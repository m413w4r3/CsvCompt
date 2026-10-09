# Instructions de contribution — Comptes familiaux

## Mission

Construire une application locale fiable d'analyse des finances d'un compte familial à partir de CSV Crédit Agricole. Ce dépôt doit démarrer sur les opérations **2026**, pas reconstruire 2025. Ce dossier contient une **spécification initiale** ; tant que le code applicatif n'est pas généré, aucune fonctionnalité ne peut être dite implémentée.

## Sources d'autorité (ordre)

1. `PLAN_ORCHESTRATION.md` : objectifs, invariants, tickets T00..T45, ordre des gates G0..G11.
2. `docs/PROTOCOLE_LLM_TOLERANT.md` : réponses modèle en texte souple ; **pas de JSON structuré exigé au LLM**.
3. `docs/MATRICE_TESTS.md` : oracles de tests, surtout argent exact, anti-doublons et confidentialité.
4. `backlog.yaml` : ordre machine-readable, responsabilités, dépendances, preuves requises.
5. `config/categories.v1.yaml` : taxonomie stable, modifiable via procédure contrôlée.
6. Le rapport de cadrage précédent est un **contexte**, non une autorité sur les modifications de ce plan.

## Directives de modification

- Ne jamais changer en silence les `flow_kind`, les formules, les invariants, les signatures d'API ou les IDs de catégorie. Documenter la proposition et attendre l'arbitrage de l'orchestrateur.
- Implémenter **verticalement** : contrat → test qui échoue → code → test qui passe → intégration → doc.
- Chaque ticket occupe un périmètre de fichiers délimité ; coordonner les changements transversaux avec l'orchestrateur.
- Ne jamais importer `tmp.zip` ou CSV privés dans les tests de CI, Git, logs ou captures d'écran. Le test du vrai CSV doit être **local opt-in**.
- Les montants sont en centimes entiers ; aucune conversion via `float` en calcul métier.
- Le backend est seul détenteur de calculs et clés de fournisseur LLM ; frontend = filtres + représentation.
- Pour toute sortie du LLM, isoler extraction tolérante du texte et validation sémantique ; n'exécuter aucune instruction incluse dans un libellé bancaire ou réponse modèle.
- Si un test échoue ou si le périmètre dépasse ce qui est spécifié : documenter le problème ; ne pas supprimer le test ou masquer l'anomalie.
- Les migrations d'un même sprint ont un propriétaire unique ; ne jamais réécrire une migration déjà appliquée à une base utilisateur sans plan sûr.

## Definition of Done par ticket

- Code et docs alignés avec la tâche et ses dépendances.
- Cas d'acceptation automatisés ; revue manuelle explicitée lorsque nécessaire.
- Tests locaux lancés et preuve vérifiable : commandes, résultats, erreurs, tests non lancés.
- Lint/typage conformes ; pas de secrets/données bancaires privés dans le diff.
- Revue critique indépendante sur tâches DATA/DOMAIN/ANALYTICS/AI/OPS.
- Rapport de livraison avec risques résiduels et référence au gate.

## Format recommandé du retour d'un sous-agent

```text
Ticket : Txx
Objectif livré :
Fichiers changés :
Interfaces modifiées :
Tests ajoutés :
Commandes réellement exécutées + résultats :
Invariant vérifié :
Limites / cas non couverts :
Risques / sécurité / données privées :
Relecture recommandée :
```

**Ne pas passer au ticket dépendant si un gate bloquant n'est pas passé.**
