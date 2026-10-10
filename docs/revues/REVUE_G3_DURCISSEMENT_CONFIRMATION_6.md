# Contre-revue indépendante finale G3 — durcissement M-G3C-01 (6)

- **Date** : 2026-10-10
- **Rôle** : relecteur indépendant; je n’ai écrit aucun correctif.
- **Base** : HEAD `131c864` — `refactor: arrêt du balayage sans manipulation d'annulation, store fermé`; arbre propre au début de la revue.
- **Copie isolée** : `/tmp/revue_g3h7`, créée sans `.git` source, `.venv`, `node_modules`, `csv_references/`, `data/`, `tmp.zip`, `.env*` ni base SQLite. Git a été initialisé uniquement dans la copie pour les scripts de garde.
- **Verdict : PASS — 0 bloquant, 0 majeur.** Le store fermé rend les balayages tardifs inoffensifs après fermeture des moteurs; les annulations extérieures restent visibles et les critères fonctionnels de M-G3C-01 sont confirmés.

## Périmètre

J’ai lu `AGENTS.md`, les cinq revues de confirmation antérieures, le contrat G3 et l’ADR-032, puis relu `backend/app/main.py`, `backend/app/services/transactions.py`, `backend/app/db/session.py`, leurs tests G3 et les tests UI/E2E de pagination et de recherche.

Aucun fichier de `csv_references/`, `data/`, `tmp.zip` ou `.env*` n’a été lu ou copié. Toutes les bases, requêtes et données utilisées sont temporaires et synthétiques. Aucun code ou test du dépôt n’a été modifié.

## Résultats

### Store, worker tardif et annulations

- Après `close_all()`, `create()` répond `DATABASE_BUSY`/503, `use()` lève `SnapshotUnavailable` (contrat de curseur stale), `expire_expired()` retourne `0` et aucun snapshot ni connexion ne reste retenu. Le test `test_IMP_14_closed_snapshot_store_preserves_cursor_and_create_contracts` confirme aussi les contrats HTTP `409 CURSOR_STALE` et `503 DATABASE_BUSY`.
- Le verrou commun sérialise `use`, `create`, `expire_expired` et `close_all`. Un stress synthétique de **200 cycles**, chaque cycle démarrant quatre threads synchronisés pour ces opérations, termine sans interblocage; chaque magasin finit vide et le pool n’a aucune connexion retenue.
- Les tests d’annulation paramétrés **x1/x2/x3** passent. Le worker est libéré après `close_all()` et le `dispose()` des deux moteurs; il appelle alors `expire_expired()` sur le store fermé (résultat `0`) puis lève une exception synthétique. La Future consomme cette exception, les compteurs de connexions restent à zéro et le handler asyncio ne reçoit pas de « never retrieved ».
- Deux sondes supplémentaires préservent une annulation externe pendant le nettoyage : (1) `Task.cancel()` arrive avant l’échéance d’un `asyncio.timeout` extérieur, laquelle expire alors que l’attente de shutdown est retenue; (2) le parent d’un `TaskGroup` est annulé pendant que l’enfant est dans le lifespan. Dans les deux cas, l’issue observée est `CancelledError`, pas `TimeoutError`; le worker tardif est libéré après fermeture, et aucune exception non récupérée ni aucun avertissement asyncio/ResourceWarning n’est observé.
- Le cas de dépassement du délai de shutdown reste couvert par `test_IMP_14_snapshot_sweeper_timeout_logs_without_request_data`, avec délai ramené à 0,2 s : fermeture effectuée, journal limité au code et au délai, sans marqueur synthétique.

### Critères M-G3C-01 et intégrité des pages

- Avec la fixture synthétique `overlap_A.csv`, **200 premières pages `limit=1` répondent 200**, chacune avec un curseur suivant; maximum mesuré **0,0479 s**. La limite du store reste à trois snapshots.
- Une liste, un commit et `GET /accounts` répondent chacun en **moins d’une seconde**, même après ces navigations.
- La saturation des trois snapshots utilisés et celle du pool dédié répondent au contrat **503 `DATABASE_BUSY`**; le test ciblé le confirme.
- `test_IMP_07_real_threads_serialize_same_hash_and_two_overlapping_files` passe : les imports concurrents conservent l’idempotence et les lignes chevauchantes.
- Avec un TTL synthétique de 150 ms et sans nouvelle requête de liste, le balayeur vide le store. Les pools principal et dédié ont zéro connexion retenue, le suivi SQLAlchemy ne compte aucune transaction active et `PRAGMA wal_checkpoint(TRUNCATE)` retourne `(0, 0, 0)`.
- Un curseur expiré/évincé répond `409 CURSOR_STALE`; un curseur invalide répond `422 CURSOR_INVALID`. Les tests de pagination conservent aussi la stabilité du snapshot quand un import est finalisé après la première page.
- Vitest garde l’oracle de debounce : 50 frappes rapides ne créent aucune requête à 180 ms, une seule requête part après le délai de 300 ms avec la dernière valeur, et une réponse initiale lente devenue obsolète n’est pas affichée. Aucun oracle de recherche n’a été retiré.

### Assertions touchées par le remplacement du test d’absorption

La comparaison de `HEAD^..HEAD` montre que le test qui exigeait que le worker finisse avant le retour du lifespan a été remplacé par `test_IMP_14_snapshot_sweeper_late_worker_is_safe_after_lifespan_cancellation`, paramétré x1/x2/x3. Le nouvel oracle vérifie le comportement retenu : le lifespan ferme le store et dispose les moteurs même si le thread est encore bloqué, puis ce thread tardif ne peut plus utiliser le store et son exception est récupérée. Les assertions de fermeture, de zéro connexion, de propagation de `CancelledError` et d’absence d’exception non récupérée sont explicites. Le délai borné et son journal restent testés séparément. Je ne relève donc pas d’affaiblissement d’une assertion utile pour la sûreté actuelle; la seule assertion abandonnée imposait l’attente du worker avant shutdown, propriété remplacée par l’invariant de sûreté après fermeture.

## Vérifications réellement exécutées dans la copie isolée

| Commande | Résultat |
|---|---|
| `uv sync --all-groups` ; `cd frontend && npm ci` | Succès; CPython 3.12.4. `npm ci` : 389 paquets, 0 vulnérabilité signalée. |
| `uv run pytest -q backend/tests/integration/test_g3_hardening_transactions.py` | **17 passed**, avertissement `StarletteDeprecationWarning` lié à `httpx`/`TestClient`. |
| `uv run pytest -q backend/tests/integration/test_import_reconciliation.py::test_IMP_07_real_threads_serialize_same_hash_and_two_overlapping_files` | **1 passed**, même avertissement `StarletteDeprecationWarning`. |
| `PYTHONPATH=backend/tests/integration:backend uv run python review_g3h7_probes.py` | 200 pages, délais API, TTL/WAL, transaction active, 200 cycles concurrents, annulation sous `asyncio.timeout` et `TaskGroup` : tous les oracles ci-dessus passés. Script synthétique temporaire dans `/tmp/revue_g3h7`. |
| `uv run pytest -q -m 'not opt_in_real_data'` | **425 passed, 4 deselected**, 1 `StarletteDeprecationWarning`, 116,10 s. Aucun test opt-in réel lancé. |
| `uv run ruff format --check .` ; `uv run ruff check .` | **116 files already formatted**; **All checks passed**. |
| `make lint` ; `make typecheck` | Succès : Ruff, ESLint, mypy (**61 fichiers source**) et `tsc -b`. |
| `npm run test -- --run` | **7 fichiers, 50 tests passés**. |
| `make verify-contracts` | Succès : schéma, fixtures et oracles vérifiés; plan = 46 tickets, 12 gates, 87 IDs de catégories et 81 tests référencés. Auto-tests = 4/4. |
| `bash scripts/test_e2e_import.sh` × 2 | Deux exécutions réussies, **3/3 chacune**, dont le test Axe page/aperçu en clair et sombre. |
| `CI=1 bash scripts/test_e2e_smoke.sh` | Build réussi; Playwright **2/2**, base migrée et sans migration. |
| `bash scripts/check_no_private_data.sh --with-untracked` | **OK**, aucune donnée privée détectée; **307 fichiers inspectés** après ajout de ce rapport et du script de sonde à la copie. |

Les E2E ont émis l’avertissement Node `NO_COLOR`/`FORCE_COLOR`; aucun échec associé. Le seul avertissement pytest est la dépréciation Starlette déjà rencontrée aux revues précédentes, sans rapport avec asyncio.

Les premières itérations du script de sonde temporaire ont nécessité de corriger son `TestClient` (host/origin loopback) et son ordre de libération du worker. Les exécutions finales consignées ci-dessus terminent et passent; ces erreurs étaient dans le harnais jetable, pas dans le produit.

## Confidentialité et risques résiduels

Le balayage de confidentialité a été relancé après copie du rapport dans `/tmp/revue_g3h7`; aucun fichier privé interdit n’est inclus. Aucun constat nouveau bloquant ou majeur n’est ouvert. Les constats mineurs historiques `m-G3C-04..06` ne sont pas rouverts.

Limites : vérification sous SQLite temporaire, CPython 3.12.4 et application mono-processus; aucun filesystem réseau ni base utilisateur testé.

## Retour au format AGENTS.md

```text
Ticket : contre-revue indépendante finale G3 — M-G3C-01, itération 6
Objectif livré : PASS, 0 bloquant et 0 majeur; sûreté des snapshots fermés et critères G3 revalidés.
Fichiers changés : docs/revues/REVUE_G3_DURCISSEMENT_CONFIRMATION_6.md uniquement dans le dépôt réel.
Interfaces modifiées : aucune.
Tests ajoutés : aucun dans le dépôt réel; sonde adversariale temporaire dans /tmp/revue_g3h7.
Commandes réellement exécutées + résultats : voir le tableau; suite complète 425 passed / 4 deselected, Ruff, lint, typecheck, Vitest, verify-contracts, contrôle de confidentialité, test_e2e_import.sh x2, smoke CI, imports concurrents et sondes de course/annulation/WAL.
Invariant vérifié : store fermé irréversible; aucun snapshot, connexion ou transaction retenu; exceptions tardives récupérées; annulations externes préservées; aucune page 500 sous 200 premières navigations; curseurs et stabilité de snapshot conformes; imports concurrents idempotents.
Limites / cas non couverts : SQLite temporaire et mono-processus seulement; pas de filesystem réseau ni de base utilisateur.
Risques / sécurité / données privées : aucun constat majeur ou bloquant. Aucun chemin de données réelles interdit consulté; avertissements non bloquants Starlette et Node décrits ci-dessus.
Relecture recommandée : orchestrateur pour arbitrage final du gate G3.
Hypothèses : fixtures synthétiques, Python 3.12.4, copie isolée excluant tous les chemins privés indiqués.
Instructions de reproduction : dans /tmp/revue_g3h7, lancer `PYTHONPATH=backend/tests/integration:backend uv run python review_g3h7_probes.py`, puis les commandes du tableau.
```
