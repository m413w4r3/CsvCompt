# Contre-revue indépendante G3 — confirmation 3

- **Date** : 2026-10-10
- **Rôle** : relecteur indépendant; je n'ai pas écrit le correctif.
- **Périmètre** : M-G3C-09 et m-G3C-10, notamment [`main.py`](/home/nill/Desktop/compte/mescompts/backend/app/main.py), [`test_g3_hardening_transactions.py`](/home/nill/Desktop/compte/mescompts/backend/tests/integration/test_g3_hardening_transactions.py) et [`transactions.spec.ts`](/home/nill/Desktop/compte/mescompts/frontend/tests/e2e/transactions.spec.ts).
- **Copie de revue** : `/tmp/revue_g3h4`, séparée du dépôt source. Créée sans `.git`, `.venv`, `node_modules`, `csv_references/`, `data/`, `tmp.zip`, `.env*` ni base SQLite. Un dépôt Git vide a été initialisé dans cette copie seulement pour les scripts qui l'exigent.
- **Verdict : BLOCK** — aucun constat bloquant, **un constat majeur** résiduel sur l'annulation du balayage pendant un appel `asyncio.to_thread`. Le défaut de reprise après exception identifié à l'itération 2 est corrigé. Le test Axe conserve ses règles, pages, thèmes et largeurs; la synchronisation du thème et du breakpoint est maintenant explicite.

## BLOQUANT

Aucun.

## MAJEUR

### M-G3C-09 — L'annulation du lifespan détache le thread du balayage avant la fermeture des ressources

Dans [`main.py:39`](/home/nill/Desktop/compte/mescompts/backend/app/main.py:39), le balayage est exécuté par `asyncio.to_thread`. Quand le lifespan annule la tâche pendant cet appel, annuler la tâche asyncio n'arrête pas le thread. Le `finally` poursuit alors vers `close_all()` et la disposition des moteurs ([`main.py:80-97`](/home/nill/Desktop/compte/mescompts/backend/app/main.py:80)), alors que le travail de balayage peut encore être actif.

**Reproduction synthétique** dans `/tmp/revue_g3h4/review_lifespan_adversarial.py` : remplacer `expire_expired` par un balayage qui signale son démarrage puis attend un événement; entrer dans le lifespan avec un snapshot ouvert; annuler la tâche du lifespan; libérer le balayage après sa sortie. Résultat : `CancelledError` est propagée, le snapshot est fermé, les deux moteurs sont disposés une fois et leurs pools n'ont aucune connexion retenue, mais le thread n'avait **pas terminé au retour du lifespan** (`worker completed before shutdown returned=False`). Il termine seulement après sa libération ultérieure. `asyncio.run` a rejoint son executor à la fermeture de la boucle et aucun thread `asyncio_*` ne restait alors; cette jonction tardive ne rend pas le nettoyage du lifespan synchronisé avec le travail déporté.

Ce chemin laisse une opération du store s'exécuter après le début du nettoyage et la disposition du moteur. Le sweep nominal étant bref, cela ne s'est pas manifesté dans les redémarrages normaux; l'annulation pendant un appel bloqué confirme néanmoins un thread détaché et une course possible avec `close_all()`/`dispose()`. Attendre la fin du travail déjà lancé avant de fermer les snapshots et les moteurs, tout en propageant l'annulation du lifespan.

## MINEUR

Aucun.

## Résultats par exigence

### M-G3C-09 — exceptions, annulation et fermeture

- Sonde synthétique avec deux erreurs `RuntimeError` successives, puis un balayage réussi : **la tâche survit et reprend**; les intervalles observés entre erreurs sont d'au moins 100 ms, donc pas de boucle rapide. Les deux avertissements contiennent le type d'exception et un identifiant de corrélation, mais ni le message synthétique contenant un marqueur de requête ni ses données. L'annulation de la tâche et un `CancelledError` émis par le balayage sont propagés.
- Test ciblé `uv run pytest -q backend/tests/integration/test_g3_hardening_transactions.py` : **11 passed**, un `StarletteDeprecationWarning` provenant de `TestClient`/Starlette.
- Shutdown après exception du sweep : le test ajouté `test_IMP_14_snapshot_sweeper_error_does_not_block_shutdown_cleanup` passe; la fermeture du snapshot est effectuée et les deux pools affichent zéro connexion retenue.
- Shutdown où `close_all()` ferme d'abord le store puis lève une `RuntimeError` synthétique : l'exception reste visible, le store ne conserve aucun snapshot et les deux pools ont zéro connexion retenue. L'essai d'annulation instrumente en plus `dispose()` : chacun des deux moteurs est disposé exactement une fois.
- Annulation du lifespan pendant un sweep bloqué : **constat majeur ci-dessus**. Après la sortie du lifespan, le worker restait en cours jusqu'à sa libération; ensuite il a terminé, et la fermeture complète de `asyncio.run` n'a laissé aucun thread `asyncio_*` orphelin.
- Redémarrage de `TestClient` x20 sur la même application et les mêmes moteurs : chaque fermeture laisse `snapshot_count == 0` et `checkedout() == 0` pour les deux pools.

### m-G3C-10 — audit Axe non affaibli

[`transactions.spec.ts:123-139`](/home/nill/Desktop/compte/mescompts/frontend/tests/e2e/transactions.spec.ts:123) utilise toujours Axe sans désactivation ni exclusion de règles : audit de la page aux largeurs **375, 768 et 1280 px** sous les thèmes clair et sombre, puis audit de l'aperçu d'import en sombre. Le nouveau helper attend `html[data-theme]` et la visibilité du bouton de menu correspondant au breakpoint avant chaque audit. Les trois exécutions E2E ont passé l'audit page et aperçu; aucun axe/règle, thème, page ou largeur n'a été retiré.

### Régressions requises

Toutes les commandes ont été lancées dans `/tmp/revue_g3h4`. Aucun test n'a lu de données réelles; les quatre tests opt-in sont exclus comme attendu.

| Commande | Résultat réel |
|---|---|
| `uv run pytest -q -m 'not opt_in_real_data'` | **419 passed, 4 deselected**, un avertissement `StarletteDeprecationWarning`, 109,41 s. |
| `uv run ruff format --check .` | **116 files already formatted**. |
| `uv run ruff check .` | **All checks passed**. |
| `npm run test -- --run` | **7 fichiers, 50 tests passés**. |
| `bash scripts/test_e2e_import.sh` × 3 | Trois exécutions réussies, **3/3 tests chacune**; chaque exécution comprend l'audit Axe page et aperçu. Avertissements Node `NO_COLOR`/`FORCE_COLOR` uniquement. |
| `CI=1 bash scripts/test_e2e_smoke.sh` | Build Vite réussi; Playwright **2/2** (base migrée et sans migration). |
| `make verify-contracts` | Succès après `git init` dans la copie isolée : 46 tickets, 12 gates, 87 ID de catégories, 81 tests référencés; oracles et `verify_schema` réussis. |
| `bash scripts/check_no_private_data.sh --with-untracked` | **304 fichiers inspectés** après ajout du présent rapport et du script de sonde dans la copie, aucune donnée privée détectée. |
| `uv run python review_lifespan_adversarial.py` | Reprises, `CancelledError`, erreur de `close_all()`, annulation du lifespan, disposition instrumentée des deux moteurs et TestClient x20 exercés; anomalie du worker détaché reproduite. |

## Retour au format AGENTS.md

```text
Ticket : contre-revue indépendante de confirmation G3 — itération 3
Objectif livré : verdict BLOCK; reprise après erreurs du sweep et durcissement Axe confirmés; un constat majeur résiduel sur le thread to_thread détaché à l'annulation du lifespan.
Fichiers changés : docs/revues/REVUE_G3_DURCISSEMENT_CONFIRMATION_3.md uniquement dans le dépôt réel.
Interfaces modifiées : aucune.
Tests ajoutés : aucun dans le dépôt réel; script de sonde temporaire dans /tmp/revue_g3h4.
Commandes réellement exécutées + résultats : pytest complet 419 passed / 4 deselected; pytest ciblé 11 passed; Ruff format/check verts; Vitest 50 passed; test_e2e_import.sh 3/3 sur trois exécutions; smoke CI 2/2; verify-contracts et contrôle de confidentialité verts. Voir le tableau ci-dessus.
Invariant vérifié : les exceptions répétées ne tuent pas le balayage; temporisation sans busy-loop; CancelledError propagée; logs sans message de requête; close_all et disposition exécutés même lorsque le nettoyage lève; aucun snapshot/connexion retenu après fermeture nominale; audit Axe non affaibli.
Limites / cas non couverts : les erreurs de nettoyage ont été injectées synthétiquement; aucune donnée bancaire réelle ni filesystem NFS testé. La fermeture complète de asyncio.run rejoint son executor, mais le worker dépassait le retour du lifespan.
Risques / sécurité / données privées : risque de course entre le sweep toujours actif et le nettoyage/dispose après annulation pendant asyncio.to_thread (MAJEUR). Aucune donnée privée consultée ou ajoutée au rapport.
Relecture recommandée : propriétaire du lifespan/snapshot store pour synchroniser la fin du worker avant nettoyage; orchestrateur pour arbitrage du gate G3.
Hypothèses : application FastAPI mono-processus, SQLite temporaire, fixtures synthétiques et sweep bloqué par événement de synchronisation.
Instructions de reproduction : dans /tmp/revue_g3h4, exécuter `uv run python review_lifespan_adversarial.py`; la ligne `worker completed before shutdown returned=False` démontre le constat majeur. Relancer les suites via les commandes du tableau.
```
