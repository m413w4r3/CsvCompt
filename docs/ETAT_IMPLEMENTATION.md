# État d'implémentation — Comptes familiaux

> Tenu par l'orchestrateur. Mis à jour à chaque ticket validé. Une ligne n'est `PASS` que si une preuve d'exécution est référencée.

## Configuration d'orchestration

| Élément | Valeur réelle |
|---|---|
| Orchestrateur | Claude Sonnet 5.5 (`claude-sonnet-5-5`), effort high |
| Sous-agents demandés | GPT-6 Luna (high / xhigh) |
| Sous-agents réellement utilisés | **GPT-6 Luna disponible** via sous-sessions Nimbalyst (`spawn_session`, modèle `openai-codex:gpt-6-luna`), effort `high` ou `xhigh` selon le backlog. (L'outil `Agent` n'expose pas ce modèle : il n'est utilisé que pour des revues indépendantes ponctuelles, jamais pour implémenter.) |
| Revues indépendantes | Sous-session GPT-6 Luna distincte de l'auteur ; pour les composants financiers sensibles, revue complémentaire par un modèle d'une autre famille (`claude-code:opus`). |
| Règle | L'orchestrateur **n'implémente pas** : il rédige les missions, contrôle, intègre et committe. |
| Spécifications | `.nimbalyst/plan_implementation_comptes_familiaux/` (non déplacées) |
| Racine du projet | racine du dépôt (le plan prévoit `comptes-familiaux/` : ici le dépôt *est* cette racine) |

## Jalons / gates

| Gate | Jalon | Statut | Rapport |
|---|---|---|---|
| G0 | M0 contrats | **PASS** (2026-10-10, après 3 revues indépendantes et amendements ADR-017..024 ; réserves R1/R2 → vérifiées à G3 via T14, R3 → G4 via T17) | docs/revues/REVUE_G0_opus.md, REVUE_G0_luna.md, REVUE_G0_confirmation.md |
| G1 | M1 | **PASS** (2026-10-10 ; revue Opus : 0 bloquant, 7 majeurs levés par le durcissement ADR-025, mineurs m-01..m-17 traités sauf m-14 [épinglage SHA : risque accepté, docs/CI.md]) | docs/revues/REVUE_G1.md ; `make check` rejoué : 119 backend, 21 intégration, 41 frontend, 2 E2E |
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
| T00 | 0 | PASS | Sonnet (outil Agent, avant correction de config) | dbf46ca | `check_no_private_data.sh` OK ; `validate_plan.py` OK ; `git check-ignore` confirmé | contrôle orchestrateur (revue indépendante non requise : ARCH/doc) |
| T01 | 0 | PASS | GPT-6 Luna xhigh (66da88ba) | 310066e, 08baf71, b01dac9 | verify_metrics_examples PASS (29 oracles, re-exécuté) | Opus + Luna (G0) |
| T02 | 0 | PASS | GPT-6 Luna xhigh (3e637e8f) | 2ac50be, 797001c, 73f485c | verify_schema PASS (preuves de mutation /tmp, re-exécuté) | Opus + Luna (G0) |
| T03 | 0 | PASS | GPT-6 Luna xhigh (1d3b360f) | 0a6572e, 76e2b0f | verify_fixtures PASS (34 fixtures), déterminisme prouvé | Opus + Luna (G0) |
| T04 | 0 | PASS | GPT-6 Luna high (79be58cf) | f60215c | self-test 4 OK ; profil local CSV réel : 1 024 opérations, 2026-01-01→2026-10-09, 1 section, 2 groupes identiques (mult. max 4) | revue Luna : F-05 (test pytest réel à T09) |

| T05 | 1 | PASS | GPT-6 Luna high (00951927) | 2ebdabf, 9dcb3f0, 04efd55 | make check vert (re-exécuté) | revue G1 Opus |
| T06 | 1 | PASS | GPT-6 Luna xhigh (fa4084ae) | 9dcb3f0, 04efd55 | parité migrée↔schema.sql, garde-fous rejoués sur base migrée, make check vert | revue G1 Opus |
| T07 | 1 | PASS | GPT-6 Luna high (9633eb61) | 39290d2, e58d240 | 41 tests Vitest, axe-core 0 violation, 3 tests a11y | revue G1 Opus |
| T08 | 1 | PASS | GPT-6 Luna high (e7342a88) | a4e6d2e, 04efd55 | make check, smoke E2E chaîne réelle | revue G1 Opus |

| T09 | 2 | PASS (revue G2 à faire) | GPT-6 Luna xhigh (22c63a14) | 3d55f9c | 78 tests ; test opt-in exécuté localement sur le CSV réel : passed (1 024 opérations, 0 anomalie bloquante) | revue interne ; revue G2 à faire |
| T10 | 2 | PASS (revue G2 à faire) | GPT-6 Luna xhigh (6fe1c22a) | 6a553d2 | 70 tests importers | revue interne ; revue G2 à faire |
| T11 | 2 | actif | GPT-6 Luna high | — | — | — |

(T12..T45 : `en attente`, voir `.nimbalyst/plan_implementation_comptes_familiaux/backlog.yaml`.)

## Constats d'audit initial (09/10/2026)

- Plan validé par `scripts/validate_plan.py` : 46 tickets, 12 gates, 87 IDs de catégories.
- CSV 2026 local : 1 024 opérations, 01/01/2026 → 09/10/2026, CP1252, LF, une seule section « Compte de Dépôt carte » (confirme CSV-06). Le CSV 2025 contient deux sections (compte courant + compte espèces PEA) : fixture de compatibilité uniquement.
- Dépôt Git sans commit initial au démarrage.
- Aucune contradiction bloquante détectée (voir `DECISIONS.md` pour les arbitrages de cadrage mineurs).
