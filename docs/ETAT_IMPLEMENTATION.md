# État d'implémentation — Comptes familiaux

> Tenu par l'orchestrateur. Mis à jour à chaque ticket validé. Une ligne n'est `PASS` que si une preuve d'exécution est référencée.
> Dernier audit complet : 2026-10-10 (reprise d'orchestration, après le premier import réel réussi via le frontend).

## Configuration d'orchestration

| Élément | Valeur réelle |
|---|---|
| Orchestrateur | Claude Sonnet 5.5 (`claude-sonnet-5-5`), effort high |
| Sous-agents demandés | GPT-6 Luna (high / xhigh) |
| Sous-agents réellement utilisés | **GPT-6 Luna** via sous-sessions Nimbalyst (`spawn_session`, modèle `openai-codex:gpt-6-luna`), effort `high` ou `xhigh`. L'outil `Agent` n'expose pas ce modèle. Limite plateforme : 4 sous-sessions simultanées. |
| Revues indépendantes | Sous-session GPT-6 Luna distincte de l'auteur ; pour les composants financiers sensibles / gates, revue par `claude-code:opus`. |
| Règle | L'orchestrateur **n'implémente pas** : il rédige les missions, contrôle, intègre et committe. |
| Spécifications | `.nimbalyst/plan_implementation_comptes_familiaux/` (non déplacées) |
| Racine du projet | racine du dépôt |

## Jalons / gates

| Gate | Jalon | Statut | Rapport |
|---|---|---|---|
| G0 | M0 contrats | **PASS** (2026-10-10) | docs/revues/REVUE_G0_*.md |
| G1 | M1 | **PASS** (2026-10-10) | docs/revues/REVUE_G1.md |
| G2 | M1 | **PASS** (BLOCK de REVUE_G2 levé par ADR-026 ; levée confirmée par REVUE_G3 : B-01, M-01..M-05 corrigés) ; réserve m-03 (entiers > 2^53) traitée dans le durcissement G3 | docs/revues/REVUE_G2.md, REVUE_G3.md |
| G3 | M1 | **PASS sous réserves** (0 BLOQUANT, 2 MAJEUR, 11 MINEUR). Durcissement MAJ-01/MAJ-02/m-01..m-11 en cours ; m-05 (contrôle de solde inerte sur exports réels, confirmé sur le profil réel : « Solde au » = date de téléchargement) → correctif T14b à planifier | docs/revues/REVUE_G3.md |
| G4 | M2 | en cours (T16 PASS, T18 livré, T17 BLOCK → correctif en cours, T19/T20 à faire) | docs/revues/REVUE_T17.md |
| G5 | M2 | à venir | — |
| G6 | M3 | à venir | — |
| G7 | M3 | à venir | — |
| G8 | M4 | à venir | — |
| G9 | M5 | à venir | — |
| G10 | M5 | à venir | — |
| G11 | M6 | à venir (T41 livré, T42 en cours) | — |

## Tickets

| Ticket | Lot | Statut | Commit(s) | Preuve / revue |
|---|---|---|---|---|
| T00..T04 | 0 | PASS | dbf46ca … f60215c | G0 PASS (3 revues) |
| T05..T08 | 1 | PASS | 2ebdabf … a4e6d2e, 04efd55 | G1 PASS |
| T09, T10, T11 | 2 | PASS | 3d55f9c, 6a553d2, cbb30dd, bec0ba5, ef5c4b1 | G2 PASS (REVUE_G3 confirmation) |
| T12, T13 | 3 | PASS sous réserves | 4359a48, ef5c4b1 | REVUE_G3 : idempotence, concurrence, atomicité prouvées ; MAJ-02 (archives orphelines) en correction |
| T14 | 3 | PASS sous réserves | aeb7b32 | REVUE_G3 ; m-04, m-05 en correction |
| T15 | 3 | PASS sous réserves | aeb7b32 | REVUE_G3 ; MAJ-01 (OUT_OF_PERIOD), m-07, m-09 en correction |
| T16 | 4 | PASS (revue G4 à faire) | 4a3c7c4 | tests unitaires/intégration ; revue de G4 à faire |
| T17 | 4 | **BLOCK → correctif en cours** | 1066748 (checkpoint) | REVUE_T17 : plafond proratisé/ciblé, retrait ADR-027, trigger inter-comptes, docs |
| T18 | 4 | livré (revue G4 à faire) | 7870c7b | tests ; revue G4 à faire |
| T19..T40 | 4-10 | en attente | — | dépendances : T17 validé |
| T41 | 11 | livré, revue indépendante à planifier | non commité | auteur : tests OPS-01/OPS-02 ; revue formelle à faire |
| T42 | 11 | actif | — | — |
| T43..T45 | 11 | en attente | — | — |

## Corrections transverses

| Sujet | Statut |
|---|---|
| Test intermittent `0001` dans un UUID (`preview_id`) | cause prouvée (collision fortuite, pas de fuite) ; helper structurel + tests déterministes ; revue : ACCEPTÉ SOUS RÉSERVES (liste blanche contractuelle, 3 assertions analogues dans `test_quality_imports.py`) → correctif en cours |

## Constats d'audit initial (09/10/2026)

- Plan validé par `scripts/validate_plan.py` : 46 tickets, 12 gates, 87 IDs de catégories.
- CSV 2026 local : 1 024 opérations, 01/01/2026 → 09/10/2026, CP1252, LF, une seule section.
- Profil réel (2026-10-10, dates uniquement) : « Solde au » = date de téléchargement = date de la dernière opération (cf. REVUE_G3 m-05).
