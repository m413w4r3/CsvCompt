# Dossier pour l'orchestrateur — Comptes familiaux

**Ce dossier contient un plan d'exécution et des contrats de validation, pas l'application implémentée.**

## Lecture conseillée

1. `PROMPT_ORCHESTRATEUR.md` : instruction prête à fournir au modèle qui supervise le travail.
2. `PLAN_ORCHESTRATION.md` : rapport complet, architecture, 12 lots, 46 tâches et gates.
3. `backlog.yaml` : déclinaison en tâches machine-readable avec dépendances et attentes.
4. `docs/MATRICE_TESTS.md` : scénarios testables et résultats attendus.
5. `docs/PROTOCOLE_LLM_TOLERANT.md` : classification en texte libre récupérable ; aucune exigence de JSON strict de la part du modèle.
6. `AGENTS.md` : conventions pour orchestrateur et sous-agents.
7. `config/categories.v1.yaml` : taxonomie déjà conçue, à conserver dans le dépôt futur.

## Décisions utilisateur intégrées

- Démarrer les comptes à **2026**, sans reconstituer le trou de 2025. Le CSV de mars 2025 n'est qu'une éventuelle fixture de compatibilité et ne doit pas entrer dans la base par défaut.
- Un CSV mensuel peut chevaucher des relevés précédents ; **ni dernière date seulement ni `drop_duplicates` sur `(date,montant,libellé)`**.
- Sortie LLM **souple** : texte compact facultativement délimité, parsing partiel tolérant, validation métier a posteriori. L'API REST application peut évidemment être JSON et typée.
- Modèle IA compatible OpenAI **optionnel**. Les règles et validations humaines sont prioritaires et le fonctionnement local hors ligne est complet.
- Distinction des dépenses réelles, allocations personnelles, épargne et transferts, emprunts, retrait d'espèces, achats via intermédiaires, revenus réguliers/exceptionnels.

## Utilisation

Transmettre le **ZIP entier** à l'orchestrateur, puis le texte de `PROMPT_ORCHESTRATEUR.md`. Conserver les deux fichiers bancaires réels sur la machine de développement, hors Git, et indiquer au système leur emplacement local uniquement lors du test opt-in. Les premières tâches du backlog (T00–T04) construisent les contrats exécutables et les fixtures synthétiques ; elles n'importent pas de données réelles.

### Références techniques utilisées pour les tests

- [FastAPI / Tests avec pytest](https://fastapi.tiangolo.com/tutorial/testing/)
- [Playwright / Assertions E2E](https://playwright.dev/docs/test-assertions)
- [SQLite / Online Backup API](https://www.sqlite.org/c3ref/backup_finish.html)
- [OWASP / Prompt injection et frontières de confiance](https://genai.owasp.org/llmrisk2023-24/llm01-24-prompt-injection/)
