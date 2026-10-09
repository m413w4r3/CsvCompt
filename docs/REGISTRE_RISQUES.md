# Registre des risques

> Tenu par l'orchestrateur. Reprend le §J de `PLAN_ORCHESTRATION.md`, complété par les risques propres à l'environnement d'exécution. Toute limitation résiduelle d'un échec de gate y est inscrite ; jamais de contournement silencieux.

Statuts : `ouvert` (à surveiller), `mitigé` (contrôle en place, preuve à fournir), `clos`. Propriétaire = rôle du plan (ARCH, DATA, DOMAIN, AI, ANALYTICS, UI, QA, OPS) ; l'orchestrateur arbitre.

| ID | Risque | Signaux de détection | Politique de traitement | Statut | Propriétaire |
|---|---|---|---|---|---|
| R-01 | Exports non uniques sans ID banque | Identités `(compte,date,montant,libellé)` en collisions | Multiplicité conservée ; rapprochement ambigu en file d'audit ; jamais de suppression automatique | ouvert (traité aux tickets T12-T13) | DATA |
| R-02 | Quelques écritures ambiguës importantes | Carrefour Banque, prêts, cash, virements particuliers | `flow_kind` prudent, revue priorisée aux gros montants | ouvert | DOMAIN |
| R-03 | Fichier bancaire privé chez un agent / en CI | Fichier réel copié dans fixtures, logs ou Git | Isolation, faux CSV pour la CI, test réel opt-in et contrôle pré-commit (`scripts/check_no_private_data.sh`) | mitigé (garde créée en T00, voir R-12) | OPS |
| R-04 | « Tout catégoriser » à tort | LLM trop assuré, règles merchant-wide | Validation humaine, scores de précision, portée de règle explicite, pas de confiance « 99 % » auto-déclarée | ouvert | DOMAIN / AI |
| R-05 | Des comparaisons fausses | Mois en cours d'octobre, absence de 2025 | Fenêtres 2026 seulement, indicateur complet/partiel, « comparateur indisponible » | ouvert | ANALYTICS |
| R-06 | Chiffres contradictoires entre pages | Filtre ou interprétation du prêt différents | Définitions uniques backend et drilldown testé | ouvert | ANALYTICS |
| R-07 | Risque de perte de DB | Import massif, migration, disque | Backups avant actions destructives, restore automatique vérifié, intégrité SQLite | ouvert | OPS |
| R-08 | Serveur local exposé | 0.0.0.0, CORS `*`, absence de contrôle d'origine | Bind loopback, limitations d'origine, tests réseau/sécurité | ouvert | OPS |
| R-09 | Provider OpenAI-compatible hétérogène | Format refusé, tronqué, quotas, bloc de texte | Dénominateur commun texte, erreurs partielles, provider mock, pas de dépendance aux tools/JSON | ouvert | AI |
| R-10 | Effet de mode / UI trop vaste | Multitude de graphiques au prix de la véracité | MVP import/ventilation/4 vues avant raffinements | ouvert | UI |
| R-11 | GPT-6 Luna indisponible : qualité des sous-agents non homogène | Rapports incomplets, tests faibles, divergences entre agents d'un même lot | Compensée par revue indépendante (agent distinct, `fable` pour les composants sensibles), preuves d'exécution obligatoires, ADR-002 | ouvert | orchestrateur |
| R-12 | Fichiers privés présents dans le workspace (`csv_references/`) | Fichier `*.csv` hors fixtures, `csv_references/`, `*.sqlite*`, `.env` ou `tmp.zip` suivi par Git ; motif IBAN FR76 ou numéro de 8+ chiffres dans un CSV suivi | `.gitignore` + contrôle pré-commit `scripts/check_no_private_data.sh` exécuté avant chaque proposition de commit ; les agents n'ouvrent pas ces fichiers ; test réel opt-in via `COMPTES_REAL_CSV` | mitigé (garde en place, à rejouer à chaque commit) | OPS / orchestrateur |
