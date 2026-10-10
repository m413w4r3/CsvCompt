# Revue indépendante de confirmation G3 — itération 2

- **Date** : 2026-10-10
- **Rôle** : relecteur indépendant. Je n'ai écrit aucun des correctifs examinés.
- **Base** : diff de correctifs présent au début de la revue (27 fichiers), comparé à `HEAD` dans `/home/nill/Desktop/compte/mescompts`.
- **Exécution** : copie isolée `/tmp/revue_g3h3`, sans `.git` source, `.venv`, `node_modules`, `csv_references/`, `data/`, `tmp.zip`, fichiers `.env*` ni bases SQLite. `git init` dans cette copie uniquement pour les scripts E2E qui exigent un dépôt Git.
- **Verdict : BLOCK** — aucun constat bloquant, **un constat majeur** sur l'exception du balayage périodique. Le pool dédié, le plafond LRU, le TTL, les codes contractuels, le frontend et le correctif Vite résistent aux scénarios exécutés. Le défaut majeur contredit le critère explicite de balayage non fatal et empêche une fermeture propre si le balayage échoue.

## BLOQUANT

Aucun.

## MAJEUR

### M-G3C-09 — Une exception du balayage arrête le balayage et fait échouer l'arrêt du lifespan

Dans [`main.py:34`](/home/nill/Desktop/compte/mescompts/backend/app/main.py:34), `_expire_transaction_snapshots` laisse remonter toute exception de `store.expire_expired()` et termine alors sa tâche périodique. Au shutdown, le lifespan attend cette tâche en ne supprimant que `CancelledError` ([`main.py:70`](/home/nill/Desktop/compte/mescompts/backend/app/main.py:70)). Une erreur antérieure ressort donc de `await sweeper` et saute `close_all()` ainsi que le `dispose()` des deux moteurs ([`main.py:73`](/home/nill/Desktop/compte/mescompts/backend/app/main.py:73)).

**Reproduction synthétique**, dans la copie isolée : remplacer `app.state.transaction_snapshot_store.expire_expired` par une fonction qui lève `RuntimeError("synthetic sweep failure")`, démarrer `TestClient`, attendre le premier tick du balayage, puis quitter le contexte. Résultat réel : `CONFIRMED FINDING lifespan shutdown raises after periodic sweep exception`. Une sonde directe de `_expire_transaction_snapshots` confirme également que la tâche est terminée après cette première exception.

Le défaut est conditionnel à une erreur de nettoyage, mais il contredit la demande « exceptions du balayage non fatales ». Une erreur temporaire ou un défaut de fermeture interrompt aussi toutes les expirations ultérieures. Consigner l'erreur sans donnée de requête, continuer les ticks, et garantir la fermeture des snapshots et moteurs par un `finally` imbriqué. Je n'ai pas appliqué ce correctif.

## MINEUR

### m-G3C-10 — L'audit de contraste E2E ne synchronise pas explicitement le thème

Dans [`transactions.spec.ts:125`](/home/nill/Desktop/compte/mescompts/frontend/tests/e2e/transactions.spec.ts:125), le test clique sur le bouton de thème sombre puis lance immédiatement Axe. Le thème est appliqué à l'attribut `data-theme` dans un `useEffect` de `AppShell`; le test ne vérifie pas cet attribut avant l'audit. Le changement de largeur est lui aussi suivi immédiatement d'un audit. La cause la plus plausible de l'échec intermittent signalé est cette course de synchronisation entre React, le breakpoint et Axe. Elle n'a pas été reproduite dans cette revue : les **3 exécutions sur 3** de `scripts/test_e2e_import.sh` ont passé les trois tests, y compris les audits clair/sombre et l'aperçu.

Pour rendre le diagnostic déterministe, attendre `html[data-theme="dark"]` après le clic, puis laisser le rendu au breakpoint se stabiliser avant Axe. C'est une hypothèse étayée par le code du test, pas une reproduction du défaut observé chez l'auteur.

## Résultats par exigence

### Pool, snapshots et cohérence

- `uv run python /tmp/revue_g3h3/review_adversarial.py` (script jetable, fixtures synthétiques) : **200 premières pages `limit=1`**, aucune 500; latence maximale observée **0,044 s**; pool partagé `checkedout=0` après chaque requête; plafond observé à 3 snapshots. Après le TTL de production de 60 s, sans requête de nettoyage : `snapshot_count=0`, `snapshot_engine.pool.checkedout()=0`, compteur transactionnel observé par événements SQLAlchemy `0`; `PRAGMA wal_checkpoint(TRUNCATE)` renvoie `(0, 0, 0)`.
- Dans le même script : GET accounts, détail d'opération et commit synthétique en moins d'une seconde avec trois snapshots retenus. Le curseur de la première page est `CURSOR_STALE` après éviction; après redémarrage il est aussi `CURSOR_STALE` (409). Un curseur falsifié est `CURSOR_INVALID` (422). Le curseur décodé ne contient ni la clé `q` ni sa valeur; le filtre est lié par HMAC. Inspection de [`errors.py`](/home/nill/Desktop/compte/mescompts/backend/app/core/errors.py) et [`logging.py`](/home/nill/Desktop/compte/mescompts/backend/app/core/logging.py) : erreurs internes journalisées avec identifiant de corrélation seulement, sans URL, filtre ou libellé.
- Concurrence jetable avec trois snapshots ouverts, huit requêtes de liste simultanées et un commit : statuts de liste observés `[200, 409, 200, 200, 200, 503, 200, 200]`, commit `200`, aucun 500 ni interblocage. Après TTL court de test, les deux pools reviennent à zéro. La saturation forcée par verrouillage des trois entrées renvoie `503 DATABASE_BUSY`; une nouvelle première page réussit après libération.
- Le test existant de commit postérieur à la première page passe dans la suite ciblée : ses pages conservent le compte initial et n'incluent pas la nouvelle opération.
- Arrêt concurrent testé avec une liste suspendue dans son snapshot et un GET accounts en parallèle : les deux réponses sont `200`, `snapshot_count=0`, connexions retenues par les deux pools `0` après l'arrêt.

### Interface, archive, docs et Vite

- `npm run test -- --run` : **7 fichiers, 50 tests passés**. Un test jetable supplémentaire dans la copie passe **3/3** : debounce (pas de requête avant 300 ms), ancienne recherche lente résolue après la nouvelle (réponse ignorée), `CURSOR_STALE` suivi de « Réessayer » (requête de première page sans curseur) et message accessible pour `503 DATABASE_BUSY` avec reprise.
- `test_g3_hardening_archives.py` couvre ENOLCK simulé : commit `503 ARCHIVE_LOCK_UNAVAILABLE`, démarrage et health `200`, journalisation sans chemin et aucune archive créée. Le test ciblé passe.
- `docs/API.md` et `docs/TRANSACTIONS_API.md` correspondent aux statuts, au plafond, au TTL, au balayage et aux curseurs observés. `ARCHIVE_LOCK_UNAVAILABLE` y est annoncé comme 503. Le retry UI après `CURSOR_STALE` repart de la première page via le bouton « Réessayer »; le cas a été exercé par test composant jetable.
- ADR-032 : pool dédié de 4 avec plafond à 3 et une connexion de réserve, plus 503 lorsque toutes les entrées sont utilisées, sont justifiés par le comportement et confirmés sous concurrence. Le TTL 60 s, balayage au plus chaque seconde et debounce 300 ms sont cohérents avec les scénarios et les mesures; les durées exactes restent des valeurs de politique, non validées par une mesure d'usage longue.
- `frontend/vite.config.ts` passe `changeOrigin: true`; `CI=1 bash scripts/test_e2e_smoke.sh` réussit sur base migrée et sans migration, ce qui valide le proxy et le cas CI-01.

### Assertions de tests existants touchées — inventaire exhaustif du diff

Aucune assertion existante n'a été supprimée, ignorée ou affaiblie. Les assertions ajoutées renforcent la couverture. Les seules assertions/entrées d'assertion modifiées sont :

1. `backend/tests/integration/test_g3_hardening_transactions.py` : `payload["total_count"] == first["total_count"]` est devenu `payload["total_count"] == expected_total` dans une boucle sur trois curseurs. Pour le curseur original, `expected_total` est exactement le compte capturé avant commit; l'assertion d'absence de la nouvelle opération est conservée et appliquée à chaque curseur. Cette extension ajoute deux filtres au même oracle transactionnel.
2. `backend/tests/integration/test_backups.py` : comparaison `schema_version == _installed_revision()` seulement remise sur une ligne.
3. `backend/tests/integration/test_g3_hardening_imports.py` : appel de récupération des rejets reformaté; l'assertion `purge_orphaned_archives(...) == 2` est uniquement parenthésée pour le format Ruff.
4. `backend/tests/integration/test_migration_0002.py` : asserts existants `count(*) >= 2` et `source_rows == (len(specs),)` reformatés; aucun opérateur ni opérande retiré.
5. `backend/tests/integration/test_migrations.py` : assertion `fetchone() is None` reformatée, condition inchangée.
6. `backend/tests/integration/test_refund_links_service.py` : assertions de comptage `== 0` et `== 2` reformatées; appels des lambdas de cas d'erreur réindentés sans changement de code d'erreur attendu ni d'arguments.
7. `backend/tests/integration/test_g3_hardening_accounts.py` : donnée d'entrée `-10**13` écrite `-(10**13)`; la valeur est identique, assert inchangé.
8. `backend/tests/unit/domain/test_money_split.py` : expression du dictionnaire `remaining` reformatée; toutes les assertions suivantes sont intactes.

Le nouveau test archive ENOLCK et les nouveaux tests de snapshots sont des ajouts. `frontend/tests/component/transactions.test.tsx` ajoute un scénario et n'en retire aucun. Cette liste couvre tous les fichiers de tests du diff (`git diff --name-only HEAD -- backend/tests frontend/tests`).

## Vérifications réellement exécutées dans la copie isolée

| Commande | Résultat |
|---|---|
| `uv sync --all-groups` ; `cd frontend && npm ci` | Succès; installation isolée. `npm ci` : 0 vulnérabilité rapportée. |
| `uv run pytest -q backend/tests/integration/test_g3_hardening_transactions.py backend/tests/integration/test_g3_hardening_archives.py` | **16 passed**, 1 avertissement `StarletteDeprecationWarning`. |
| `uv run pytest -q -m 'not opt_in_real_data'` | **417 passed, 4 deselected**, 1 `StarletteDeprecationWarning`, 114,08 s. Aucun test opt-in réel lancé. |
| `uv run ruff format --check .` ; `uv run ruff check .` | **116 files already formatted**; **All checks passed**. |
| `npm run test -- --run` | **7 fichiers, 50 tests passés**. |
| `npm run lint` ; `npm run typecheck` | Succès. |
| `make verify-contracts` | Succès; 46 tickets, 12 gates, 87 IDs de catégories, 81 tests référencés; auto-test 4/4; `verify_schema` OK. |
| `bash scripts/check_no_private_data.sh --with-untracked` | Succès; aucune donnée privée détectée, **303 fichiers** inspectés dans la copie après ajout d'une copie du présent rapport. |
| `CI=1 bash scripts/test_e2e_smoke.sh` | Succès; build Vite et **2/2** tests Playwright (base migrée, base sans migration). |
| `bash scripts/test_e2e_import.sh` × 3 | Trois exécutions vertes, **3/3 chacune** (9 tests au total); aucun échec de contraste reproduit. |
| Test composant jetable : stale, 503 et course entre deux recherches | **3 passed**; fichier créé uniquement dans `/tmp/revue_g3h3`. |
| `git diff --check HEAD` | Succès, aucune erreur d'espacement ou de conflit. |

## Limites, invariants et risques

- Aucun fichier de `csv_references/`, `data/`, `tmp.zip` ou `.env*` n'a été lu. Bases temporaires et fixtures synthétiques uniquement. Aucun CSV réel, libellé réel ou montant identifiable n'est inclus dans ce rapport.
- **INV-02** : le diff fonctionnel examiné n'ajoute aucun calcul monétaire flottant; les changements de prorata sont du formatage Ruff et la suite complète passe.
- **INV-11** : les curseurs ne contiennent pas `q`; erreurs et logs ne reflètent ni requête ni libellé. La vérification de confidentialité passe.
- Le défaut majeur ne concerne pas le chemin nominal mesuré; il concerne le rétablissement et le nettoyage après une exception du balayage.
- Les constats résiduels m-G3C-04, m-G3C-05 et m-G3C-06 de la revue précédente ne sont pas modifiés par ce diff et n'ont pas été réexaminés ici.
- Après la création de la copie isolée, le diff source a reçu deux modifications supplémentaires (`docs/ETAT_IMPLEMENTATION.md`, `docs/REGISTRE_RISQUES.md`) et un rapport non suivi (`docs/revues/RECONCILIATION_T17_T41.md`). Ces changements concurrents ne faisaient pas partie des 27 fichiers testés; ils n'ont pas été intégrés au verdict de cette revue et je ne les ai pas modifiés.

## Retour au format AGENTS.md

```text
Ticket : revue indépendante de confirmation G3 — itération 2
Objectif livré : verdict BLOCK avec un constat majeur nouveau (M-G3C-09); correction pool/snapshots, expirations nominales, codes d'erreur, frontend, ENOLCK et proxy confirmés sur scénarios synthétiques.
Fichiers changés : docs/revues/REVUE_G3_DURCISSEMENT_CONFIRMATION_2.md uniquement dans le dépôt réel.
Interfaces modifiées : aucune par le relecteur.
Tests ajoutés : aucun dans le dépôt réel. Script adversarial et test composant temporaires dans /tmp/revue_g3h3.
Commandes réellement exécutées + résultats : voir le tableau ci-dessus; suite complète 417 passed / 4 deselected, E2E smoke 2/2, test_e2e_import.sh 3/3 sur trois relances, ruff, Vitest, lint, typecheck, verify-contracts et contrôle de confidentialité verts. Sonde du balayage : RuntimeError synthétique termine la tâche et fait lever le shutdown.
Invariant vérifié : INV-02 et INV-11; snapshots transactionnels cohérents après commit; saturation 503; stale 409; curseur falsifié 422; compteurs de pools nuls après expiration nominale.
Limites / cas non couverts : E2E contraste non flaky dans 3 relances; cause de l'échec unique de l'auteur non reproduite, synchronisation de thème probablement en cause. Aucun test sur vrai système de fichiers NFS/ENOLCK; ENOLCK simulé. Pas de données réelles.
Risques / sécurité / données privées : exception de balayage arrête la tâche et fait échouer le lifespan shutdown avant fermeture des snapshots et moteurs (MAJEUR). Aucun contenu privé consulté ou inscrit.
Relecture recommandée : propriétaire du lifespan/snapshot store pour M-G3C-09; propriétaire frontend pour stabiliser le test Axe; orchestrateur pour le verdict G3.
Hypothèses : tests exécutés en copie isolée, mono-processus FastAPI, SQLite fichier temporaire et fixtures synthétiques.
Instructions de reproduction : `cd /tmp/revue_g3h3`; lancer `PYTHONPATH=backend uv run python review_adversarial.py` pour la charge 200 pages; le défaut de shutdown se reproduit en remplaçant `expire_expired` par une fonction qui lève RuntimeError avant le premier tick puis en quittant `TestClient`.
```
