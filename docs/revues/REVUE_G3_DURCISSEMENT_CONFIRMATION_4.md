# Contre-revue indépendante G3 — confirmation 4

- **Date** : 2026-10-10
- **Rôle** : relecteur indépendant; je n'ai écrit aucun correctif.
- **Périmètre** : M-G3C-09 bis, `backend/app/main.py` (`_expire_transaction_snapshots`) et son test; chemins d'annulation, de dépassement du délai, de reprise et de redémarrage.
- **Copie de revue** : `/tmp/revue_g3h5`, séparée du dépôt réel. Créée sans `.git`, `.venv`, `node_modules`, `csv_references/`, `data/`, `tmp.zip`, `.env*` ni base SQLite. Un Git vide a été initialisé dans cette copie pour les scripts qui l'exigent; `npm ci` a ensuite installé les dépendances frontend depuis le lockfile.
- **Verdict : BLOCK** — **0 constat bloquant, 1 constat majeur**. L'attente bornée corrige l'annulation simple et son dépassement est proprement journalisé. Une deuxième annulation interrompt toutefois cette attente avant l'inscription du callback qui consomme le résultat tardif; le nettoyage ferme alors les moteurs tandis que le worker tourne encore, puis asyncio signale une exception de Future non récupérée.

## BLOQUANT

Aucun.

## MAJEUR

### M-G3C-09 bis — Une deuxième annulation interrompt l'attente et laisse le worker détaché

La sonde synthétique annule le task du lifespan pendant que `expire_expired` est bloqué, puis annule une seconde fois alors que `_expire_transaction_snapshots` attend `asyncio.wait`. Cette seconde annulation fait sortir immédiatement de l'attente bornée. Le chemin ajoute donc le callback `_consume_sweep_result` seulement après la branche de timeout, mais n'y arrive pas dans ce cas.

Résultat observé : `close_all()` et les deux `dispose()` sont appelés alors que `worker_finished` vaut encore `False`. Après libération du worker qui lève une `RuntimeError` synthétique, le handler de boucle reçoit `Future exception was never retrieved`. Le lifespan propage bien `CancelledError`, mais il ne garantit plus la synchronisation du worker avec le nettoyage lorsque l'annulation est répétée. Cela laisse subsister la course que M-G3C-09 bis devait éliminer.

Le problème est dans la séquence de protection de l'attente/récupération autour de la seconde annulation, pas dans le délai lui-même. Il faut que toute sortie par annulation de cette attente inscrive la récupération du résultat tardif avant de laisser le lifespan poursuivre son nettoyage, tout en conservant le comportement borné demandé.

## MINEUR

Aucun.

## Résultats par exigence

### Annulation, attente bornée et fermeture

- Annulation simple, worker libéré après demande d'arrêt : le lifespan ne rend pas la main avant la libération du worker; `CancelledError` est propagée et `close_all()`/les deux `dispose()` s'exécutent. **Réussi.**
- Timeout patché à **0,2 s**, worker bloqué au-delà du délai puis libéré avec une exception synthétique : l'avertissement `transaction_snapshot_sweep_shutdown_timeout` est émis avec uniquement le gabarit et la valeur du délai; le marqueur synthétique de requête n'apparaît pas. `close_all()` et les deux `dispose()` sont exécutés, `CancelledError` est propagée, le résultat tardif est consommé et le handler asyncio ne reçoit aucune exception « never retrieved ». **Réussi.**
- Exception du worker pendant l'attente d'annulation : l'exception est consommée, le lifespan propage `CancelledError` et le nettoyage s'exécute. **Réussi.**
- Double annulation pendant l'attente : `worker_finished_at_cleanup=False`, puis `Future exception was never retrieved`. **Échec majeur M-G3C-09 bis.**

### Boucle normale et redémarrages

- Deux exceptions synthétiques consécutives puis un sweep réussi : la tâche reprend; intervalles mesurés entre appels de **0,127 s** et **0,127 s**, conformes au backoff de 100 ms en plus de l'intervalle de sweep. Annulation ensuite propagée. **Réussi.**
- 20 cycles de `TestClient` sur la même application : chaque arrêt laisse zéro snapshot et zéro connexion retenue sur les deux pools. **Réussi.**

### Régressions demandées

Toutes les commandes ont été lancées dans `/tmp/revue_g3h5`; aucune donnée réelle n'a été consultée. Les quatre cas `opt_in_real_data` sont exclus comme demandé.

| Commande | Résultat réel |
|---|---|
| `uv run pytest -q -m 'not opt_in_real_data'` | **420 passed, 4 deselected**, un `StarletteDeprecationWarning`, 139,73 s. |
| `uv run ruff format --check .` | **116 files already formatted**. |
| `uv run ruff check .` | **All checks passed**. |
| `make verify-contracts` | **Succès**; vérifications SQLite/schema et fixtures réussies; plan : 46 tickets, 12 gates, 87 IDs de catégories, 81 tests référencés. |
| `bash scripts/check_no_private_data.sh --with-untracked` | **OK**, 304 fichiers inspectés avant création de ce rapport dans la copie. |
| `bash scripts/test_e2e_import.sh` | Premier lancement impossible (`playwright: not found`) après copie sans `node_modules`; après `npm ci`, **3/3 tests passés**, dont audit Axe page/aperçu. Avertissements Node `NO_COLOR`/`FORCE_COLOR` uniquement. |
| `CI=1 bash scripts/test_e2e_smoke.sh` | Build Vite réussi; Playwright **1/1 base migrée** et **1/1 sans migration**. Avertissements Node `NO_COLOR`/`FORCE_COLOR` uniquement. |
| `uv run python review_lifespan_adversarial.py` | Annulation simple, timeout 0,2 s, exception worker, backoff et TestClient x20 réussis; double annulation reproduisant le constat majeur ci-dessus. Script synthétique temporaire dans la copie. |

Le contrôle de confidentialité a été relancé après ajout du présent rapport à la copie; il confirme **305 fichiers inspectés, aucune donnée privée détectée**.

## Retour au format AGENTS.md

```text
Ticket : contre-revue indépendante de confirmation G3 — itération 4
Objectif livré : verdict BLOCK; l'attente et le timeout corrigent l'annulation simple, mais le double cancel révèle un worker détaché et une exception asyncio non récupérée.
Fichiers changés : docs/revues/REVUE_G3_DURCISSEMENT_CONFIRMATION_4.md uniquement dans le dépôt réel.
Interfaces modifiées : aucune.
Tests ajoutés : aucun dans le dépôt réel; sonde adversariale temporaire dans /tmp/revue_g3h5.
Commandes réellement exécutées + résultats : pytest 420 passed / 4 deselected; Ruff format/check verts; verify-contracts vert; check_no_private_data vert; e2e import 3/3 et smoke 2/2 après npm ci; sonde adversariale avec échec du double cancel. Détails ci-dessus.
Invariant vérifié : annulation simple attend la fin du worker; timeout borné journalise sans marqueur de requête, ferme les ressources, propage CancelledError et consomme l'exception tardive; exceptions répétées reprennent avec backoff; TestClient x20 ne laisse ni snapshot ni connexion retenue.
Limites / cas non couverts : test limité aux workers et bases SQLite synthétiques; aucun système de fichiers réseau ni donnée bancaire réelle utilisé. La seconde annulation pendant asyncio.wait laisse échapper l'attente.
Risques / sécurité / données privées : constat majeur M-G3C-09 bis, course worker/nettoyage au double cancel et Future exception never retrieved. Aucun fichier privé lu ou copié.
Relecture recommandée : propriétaire du lifespan/snapshot store; corriger le chemin de double annulation puis rejouer la sonde et le gate G3.
Hypothèses : FastAPI mono-processus, base SQLite temporaire et worker bloqué par événement synthétique.
Instructions de reproduction : dans /tmp/revue_g3h5, exécuter `uv run python review_lifespan_adversarial.py`; la ligne `double cancellation probe: ['cancelled', 'loop_error:Future exception was never retrieved'] worker_finished_at_cleanup: [False]` reproduce le constat.
```
