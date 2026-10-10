# Contre-revue indépendante G3 — confirmation 5

- **Date** : 2026-10-10
- **Rôle** : relecteur indépendant; aucun correctif écrit dans le code.
- **Périmètre** : [backend/app/main.py](/home/nill/Desktop/compte/mescompts/backend/app/main.py:42), `_expire_transaction_snapshots` et `lifespan`, après M-G3C-09 bis.
- **Copie isolée** : `/tmp/revue_g3h6`, créée sans `.git`, `.venv`, `node_modules`, `csv_references/`, `data/`, `tmp.zip`, `.env*` ni bases SQLite. `uv` et `npm ci` ont installé les dépendances dans cette copie.
- **Verdict : BLOCK** — **0 constat bloquant, 1 constat majeur, 1 constat mineur**. Le nettoyage borné, la récupération des erreurs tardives et les annulations simples/répétées passent. Un `uncancel()` sans distinction de provenance efface cependant l’annulation indépendante d’un appelant si elle coïncide avec l’expiration d’un `asyncio.timeout` pendant le nettoyage.

## BLOQUANT

Aucun.

## MAJEUR

### M-G3C-09 ter — une annulation extérieure concurrente est effacée par le nettoyage du compteur

Dans `lifespan` (`main.py:133-134`), chaque `CancelledError` reçu pendant l’attente du sweeper conduit à vider entièrement `current_task().cancelling()` avec `uncancel()`. Le booléen `shutdown_cancelled` mémorise qu’une annulation a eu lieu, mais pas son nombre ni sa provenance.

Reproduction synthétique dans `/tmp/revue_g3h6/timeout_plus_caller_cancel.py` : l’appelant entre dans `asyncio.timeout(0.2)`, sort normalement du corps du lifespan tandis que le worker est bloqué, reçoit un `Task.cancel()` indépendant pendant l’attente de fermeture, puis le timeout expire avant la libération du worker. Résultat observé : **`TimeoutError`**, compteur d’annulations de l’appelant à **0** après la sortie. Le `uncancel()` du lifespan a fait perdre le compteur de l’annulation extérieure; `asyncio.timeout` ne peut donc plus la distinguer de sa propre annulation et la convertit en timeout.

Le cas où le timeout est seul et expire pendant le nettoyage donne bien `TimeoutError`; un `TaskGroup` autour du lifespan propage bien une annulation extérieure seule. C’est la coexistence des deux sources pendant le nettoyage qui échoue. Un contrôle asyncio sans drainage du compteur conserve les comptes **1 puis 2** et propage `CancelledError` au lieu de le convertir en `TimeoutError`.

**Correction proposée, non appliquée** : conserver les compteurs d’annulation du task appelant pendant la reprise de l’attente. Si `shield(sweeper)` lève pendant que le sweeper tourne encore, boucler et attendre à nouveau sans appeler `uncancel()`. Quand le sweeper est terminé, propager `CancelledError` si `current_task().cancelling() > 0`; sinon traiter le `CancelledError` du sweeper comme l’arrêt normal. Cela permet aussi de retirer `shutdown_cancelled` et son drainage complet. Le drainage analogue dans `_expire_transaction_snapshots` peut également être retiré : le lifespan ne fait qu’un seul `sweeper.cancel()`, et les annulations répétées visées par ce ticket arrivent au task du lifespan, pas au sweeper.

## MINEUR

Les deux boucles de drainage `uncancel()` et l’état `shutdown_cancelled` compliquent la distinction entre annulation du sweeper et annulation de son appelant. La reprise de l’attente `shield` en conservant le compteur suffit pour les cas vérifiés, y compris les annulations répétées. Voir la proposition ci-dessus; aucun changement de code n’a été effectué.

## Résultats adversariaux

Les sondes synthétiques utilisent un worker thread bloqué par événement, des compteurs autour de `close_all()`/des deux `dispose()`, un handler d’erreurs de boucle et la capture des avertissements. Aucun CSV réel, base utilisateur ou libellé bancaire réel n’a été lu.

| Cas | Résultat |
|---|---|
| Une annulation du lifespan pendant que le worker est bloqué | Attend la libération du worker; `CancelledError` propagée; `close_all` et chaque `dispose` appelés une fois; aucun « never retrieved » ni avertissement asyncio. |
| Deux puis trois annulations pendant la même attente | Même résultat; le lifespan reste en attente jusqu’au worker; nettoyage appelé une fois par ressource. |
| Exception worker avant la fin de l’attente | Worker terminé avant le nettoyage; `CancelledError` propagée; exception récupérée; aucun avertissement asyncio. |
| Échéance de shutdown à 0,12 s, worker encore bloqué puis levée tardive d’une exception | Retour après 0,121 s; nettoyage exécuté une fois à l’échéance; callback récupère l’exception après libération; aucun « never retrieved ». |
| Arrêt normal sans annulation | Pas de `CancelledError` parasite; nettoyage appelé une fois; aucun `uncancel()` du task appelant. |
| `asyncio.timeout` seul, expiration pendant le corps puis pendant le nettoyage | `TimeoutError` dans les deux cas; nettoyage une fois. |
| `TaskGroup` autour du lifespan, annulation extérieure pendant le nettoyage | `CancelledError` propagée; nettoyage une fois. |
| `Task.cancel()` indépendant suivi de l’expiration d’`asyncio.timeout` pendant le même nettoyage | **Échec majeur** : `TimeoutError` masque l’annulation indépendante; compteur d’annulation effacé. |
| Trois exceptions de sweep puis succès | 4 appels; intervalles mesurés **0,127 s, 0,127 s, 0,127 s** (sweep de 25 ms + backoff de 100 ms); aucune boucle serrée. |
| 20 redémarrages `TestClient` sur la même application | 20 `close_all`, 20 `dispose` par moteur; 0 snapshot et 0 connexion retenue à chaque arrêt; aucune erreur asyncio. |

## Compatibilité Python

Le projet déclare `requires-python = ">=3.12,<3.13"`. `Task.cancelling()` et `Task.uncancel()` existent depuis Python 3.11; la plage déclarée les prend donc en charge. Exécution réalisée sous CPython **3.12.4** via `uv` (le shell système exposait aussi CPython 3.12.3). Aucun test Python 3.13 n’a été lancé, cette version étant hors plage du projet.

## Régressions demandées

Toutes les commandes ont été exécutées dans `/tmp/revue_g3h6`, copie excluant les chemins de données interdits. Les 4 tests `opt_in_real_data` ont été exclus.

| Commande | Résultat réel |
|---|---|
| `uv run pytest -q -m 'not opt_in_real_data'` | **422 passed, 4 deselected**, 117,62 s. Un `StarletteDeprecationWarning` existant sur l’usage de `httpx` avec `TestClient`. |
| `uv run ruff format --check .` | **116 files already formatted**. |
| `uv run ruff check .` | **All checks passed!** |
| `make verify-contracts` | **Succès** : schéma/contraintes et fixtures vérifiés; 46 tickets, 12 gates, 87 ID de catégories et 81 tests référencés. |
| `bash scripts/check_no_private_data.sh --with-untracked` | **OK**, aucune donnée privée détectée, 309 fichiers inspectés dans la copie. |
| `bash scripts/test_e2e_import.sh` | **3/3 tests passés**. Avertissement Node `NO_COLOR` ignoré car `FORCE_COLOR` est défini. |
| `CI=1 bash scripts/test_e2e_smoke.sh` | Build réussi; Playwright **1/1 base migrée** et **1/1 sans migration**. Même avertissement Node de couleurs. |
| `uv run pytest -q backend/tests/integration/test_g3_hardening_transactions.py -k 'snapshot_sweeper'` | **5 passed, 9 deselected**, un `StarletteDeprecationWarning`. |
| `uv run python review_lifespan_adversarial.py` | Annulations x1/x2/x3, exception worker, timeout, arrêt normal et nettoyage une fois : réussis. |
| `uv run python timeout_during_cleanup.py` | Timeout seul pendant nettoyage : `TimeoutError`, ressources nettoyées une fois. |
| `uv run python timeout_plus_caller_cancel.py` | Reproduit le constat majeur : `TimeoutError`, compteur de l’appelant à 0. |
| `uv run python taskgroup_cancellation.py` | Annulation extérieure dans un `TaskGroup` pendant le nettoyage : `CancelledError` propagée. |
| `uv run python normal_loop_and_restarts.py` | Backoff vérifié aux intervalles ci-dessus; 20 redémarrages propres. |

`npm ci` dans la copie a réussi (0 vulnérabilité signalée); il a affiché les avertissements d’environnement de scripts install et de version ESLint dépréciée. Les scripts E2E ont aussi affiché l’avertissement Node `NO_COLOR`/`FORCE_COLOR`; aucun de ces avertissements ne concerne asyncio.

Après création de ce rapport, `bash scripts/check_no_private_data.sh --with-untracked` a aussi réussi dans le dépôt réel : **305 fichiers inspectés, aucune donnée privée détectée**. `git status --short --untracked-files=all` ne montre que ce rapport.

## Retour au format AGENTS.md

```text
Ticket : contre-revue indépendante G3 — confirmation 5
Objectif livré : verdict BLOCK; un cancel extérieur est masqué par un asyncio.timeout concurrent pendant le nettoyage du lifespan.
Fichiers changés : docs/revues/REVUE_G3_DURCISSEMENT_CONFIRMATION_5.md uniquement dans le dépôt réel.
Interfaces modifiées : aucune.
Tests ajoutés : aucun dans le dépôt réel; sondes synthétiques temporaires dans /tmp/revue_g3h6.
Commandes réellement exécutées + résultats : pytest complet 422 passed / 4 deselected; Ruff format/check verts; make verify-contracts vert; check_no_private_data vert; E2E import 3/3 et smoke 2/2; sondes de shutdown, backoff, timeout/TaskGroup et TestClient x20. Détails ci-dessus.
Invariant vérifié : annulations simples, doubles et triples attendent le worker ou l’échéance; CancelledError propagée; nettoyage exactement une fois; erreur tardive consommée; aucun avertissement asyncio; backoff sans busy-loop.
Limites / cas non couverts : Python 3.13 hors plage déclarée; scénario combinant timeout et annulation extérieure échoue comme décrit.
Risques / sécurité / données privées : aucune donnée réelle ouverte ou copiée. Constat majeur M-G3C-09 ter sur la préservation des compteurs d’annulation.
Relecture recommandée : propriétaire du lifespan; corriger la préservation des compteurs puis rejouer timeout_plus_caller_cancel.py et les régressions.
Hypothèses : worker de sweep synthétique, SQLite temporaire, aucune donnée réelle.
Instructions de reproduction : dans /tmp/revue_g3h6, exécuter `uv run python timeout_plus_caller_cancel.py`; la sonde affiche `TimeoutError` et `caller cancel count after exit=0`. Les autres sondes sont nommées dans le tableau ci-dessus.
```
