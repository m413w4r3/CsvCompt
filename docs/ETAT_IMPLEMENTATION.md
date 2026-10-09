# État d'implémentation — Comptes familiaux

> Tenu par l'orchestrateur. Mis à jour à chaque ticket validé. Une ligne n'est `PASS` que si une preuve d'exécution est référencée.

## Configuration d'orchestration

| Élément | Valeur réelle |
|---|---|
| Orchestrateur | Claude Sonnet 5.5 (`claude-sonnet-5-5`), effort high |
| Sous-agents demandés | GPT-6 Luna (high / xhigh) |
| Sous-agents réellement disponibles | **GPT-6 Luna indisponible** dans cet environnement (l'outil Agent n'expose que `sonnet`, `opus`, `haiku`, `fable`). |
| Alternative retenue | `opus` (Claude Opus 5.5) pour les tickets `xhigh` / composants financiers ; `sonnet` pour les tickets `high` ; `fable` pour les revues critiques indépendantes des composants sensibles. |
| Spécifications | `.nimbalyst/plan_implementation_comptes_familiaux/` (non déplacées) |
| Racine du projet | racine du dépôt (le plan prévoit `comptes-familiaux/` : ici le dépôt *est* cette racine) |

## Jalons / gates

| Gate | Jalon | Statut | Rapport |
|---|---|---|---|
| G0 | M0 contrats | EN COURS | — |
| G1 | M1 | à venir | — |
| G2 | M1 | à venir | — |
| G3 | M1 | à venir | — |
| G4 | M2 | à venir | — |
| G5 | M2 | à venir | — |
| G6 | M3 | à venir | — |
| G7 | M3 | à venir | — |
| G8 | M4 | à venir | — |
| G9 | M5 | à venir | — |
| G10 | M5 | à venir | — |
| G11 | M6 | à venir | — |

## Tickets

| Ticket | Lot | Statut | Sous-agent (modèle) | Commit | Tests exécutés / résultat | Revue indépendante |
|---|---|---|---|---|---|---|
| T00 | 0 | actif | — | — | — | — |
| T01 | 0 | en attente | — | — | — | — |
| T02 | 0 | en attente | — | — | — | — |
| T03 | 0 | en attente | — | — | — | — |
| T04 | 0 | en attente | — | — | — | — |

(T05..T45 : `en attente`, voir `.nimbalyst/plan_implementation_comptes_familiaux/backlog.yaml`.)

## Constats d'audit initial (09/10/2026)

- Plan validé par `scripts/validate_plan.py` : 46 tickets, 12 gates, 87 IDs de catégories.
- CSV 2026 local : 1 024 opérations, 01/01/2026 → 09/10/2026, CP1252, LF, une seule section « Compte de Dépôt carte » (confirme CSV-06). Le CSV 2025 contient deux sections (compte courant + compte espèces PEA) : fixture de compatibilité uniquement.
- Dépôt Git sans commit initial au démarrage.
- Aucune contradiction bloquante détectée (voir `DECISIONS.md` pour les arbitrages de cadrage mineurs).
