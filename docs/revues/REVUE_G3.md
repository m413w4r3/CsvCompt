# Revue G3 — relecture critique adversariale indépendante (T12, T13, T14, T15) et confirmation de la levée de G2

- **Relecteur** : session `claude-code:opus` indépendante (n'a écrit aucun livrable T12 à T15 ni les corrections ADR-026).
- **Date** : 2026-10-10.
- **Base relue** : `1066748` (« actual state »), extraite par `git archive 1066748` dans `/tmp/revue_g3` (copie propre, `git init` local pour les gardes, `uv sync --all-groups`, `npm ci`). L'arbre de travail du dépôt réel n'a été ni lu ni modifié ; seul ce fichier y a été créé.
- **Données** : uniquement des fixtures synthétiques et des CSV fabriqués par les scripts. Numéros de compte fictifs (`98765432101`, `11112222333`, etc.), libellés `X`, `OPERATION n`, `LIBELLE_SECRET_ZQX`. `csv_references/`, `data/` et `tmp.zip` n'ont jamais été ouverts.
- **Scripts jetables** (hors dépôt, dans `/tmp/revue_g3_scripts/`) : `h.py` (aides), `p1_idem.py`, `p2_conc.py`, `p3_atom.py`, `p4_kill_perf.py`, `p5_t14.py`, `p6_g2_privacy.py`, `p7_tx_api.py`, `p8_accounts_race.py`, `p9_mutants.sh`. Les sorties sont dans `p*.out`. Exécution : `/tmp/revue_g3/.venv/bin/python -I <script>`. Les mutants ont été appliqués puis restaurés par `git checkout` sur la copie jetable uniquement.

## Verdict

**PASS sous réserves pour G3.** 0 BLOQUANT, 2 MAJEUR, 11 MINEUR.

**G2 : levée confirmée.** B-01 et M-01 à M-05 sont corrigés et vérifiés par reproduction des scénarios de REVUE_G2. Il reste une réserve mineure sur B-01 : un total d'aperçu au-delà de 2^53 est encore renvoyé au frontend (voir m-03).

Les critères bloquants littéraux de G3 sont tous tenus et prouvés par exécution :

- réimport du même fichier : 0 transaction et 0 `source_row` nouvelles, contenu des tables métier inchangé ;
- chevauchement : seul le delta est ajouté ;
- `-11,23` et `+11,23` restent deux identités ;
- les doublons stricts d'un même fichier sont conservés ;
- concurrence sur un vrai serveur uvicorn : aucune double écriture ;
- échec injecté en milieu de commit, y compris un `kill -9` : rollback total ;
- centimes : la somme exacte vaut le maximum des multiplicités, quel que soit l'ordre d'import.

Les deux MAJEUR ne cassent pas ces critères, mais doivent être traités avant l'usage réel :

1. **MAJ-01** : la résolution `OUT_OF_PERIOD` dans l'UI pousse l'utilisateur à **dupliquer** une opération déjà importée. Le texte affiché est faux, et `link_existing` est inutilisable depuis l'interface (INV-03).
2. **MAJ-02** : chaque échec de commit survenu après l'archivage laisse une **copie brute complète du relevé**, non référencée, dans `data/originals/`. Aucun inventaire ni outil de purge n'existe, et les copies s'accumulent à chaque nouvel essai (INV-11).

**Levée proposée** : corriger MAJ-01 et MAJ-02, chacun avec un test de non-régression nommé, ou les faire arbitrer par ADR. Les MINEUR peuvent rester des réserves suivies dans `docs/REGISTRE_RISQUES.md`.

## Commandes standard (copie propre)

```text
$ uv run pytest -q -m 'not opt_in_real_data'
290 passed, 4 deselected, 1 warning in 27.77s
$ uv run ruff check .                 -> All checks passed!
$ uv run ruff format --check .        -> 100 files already formatted
$ uv run mypy backend/app             -> Success: no issues found in 57 source files
$ make verify-contracts               -> PASS verify_schema ; 34 fixtures vérifiées ; selftest profil 4/4 OK ;
                                         « OK dossier : 46 tickets, 12 gates, 87 IDs de catégories, 81 tests référencés »
$ ./scripts/test_frontend.sh          -> Test Files 4 passed (4) ; Tests 42 passed (42)
$ (cd frontend && npm run typecheck)  -> OK (tsc -b)
$ ./scripts/lint.sh                   -> ruff OK ; eslint OK
$ make test-integration               -> 102 passed, 192 deselected
$ make test-e2e-smoke                 -> exit 0 (CI-01 OK)
$ ./scripts/test_e2e_import.sh        -> 2 passed (UI-05 import/no_op et UI-05 axe clair/sombre 375/768/1280)
$ scripts/check_no_private_data.sh --with-untracked -> « OK : aucune donnée privée détectée (268 fichier(s) inspecté(s)) »
```

Le test d'aperçu signalé comme en cours de correction (collision fortuite « 0001 » dans un UUID) est passé lors de cette exécution. Il n'est pas re-signalé : aucune vraie fuite n'a été trouvée (voir « Vie privée »).

## Statut des constats G2

| Constat G2 | Statut | Preuve (`p6_g2_privacy.py`, sortie `p6.out`) |
|---|---|---|
| **B-01** montants non bornés, 500, `.bin` orphelin | ✅ **levé** (réserve m-03) | 4 299 chiffres → `200`, `AMOUNT_OUT_OF_RANGE` bloquant ; `100000000000,01` (10^13+1 c) → bloquant ; `100000000000,00` (10^13 c) accepté ; 2^63 c → bloquant ; solde d'en-tête 10^15 c → bloquant ; `.bin sans .json : 0` ; les `.bin`/`.tmp` orphelins sont purgés par `ImportPreviewStore.purge_expired`. Réserve : 1 000 lignes à 10^13 c donnent `debit_total_cents=-10000000000000000`, renvoyé tel quel (> 2^53), alors que le fichier est bien bloqué |
| **M-01** remappage d'une empreinte connue | ✅ **levé** | section connue → autre compte : `409 ACCOUNT_MAPPING_LOCKED` ; → `new_account` : `ACCOUNT_MAPPING_LOCKED` ; empreinte inconnue → compte d'empreinte différente : `ACCOUNT_FINGERPRINT_CONFLICT` ; le commit revérifie aussi (`_resolve_accounts`) |
| **M-02** section sans numéro | ✅ **levé** | sans `n°` : `MISSING_ACCOUNT_NUMBER` bloquant, mapping → `ACCOUNT_MAPPING_INVALID` ; `nº` (U+00BA) reconnu (`unknown`, mapping exigé) ; en-tête sans préambule : `MISSING_ACCOUNT_NUMBER` + `SECTION_RANGE_REQUIRED` |
| **M-03** PATCH `coverage` contournant `OUT_OF_PERIOD` | ✅ **levé** | PATCH sur une section à période déclarée → `422` ; couverture ne contenant pas `min_date` → `422` |
| **M-04** collision d'identité par troncature NFC | ✅ **levé** | 2 libellés de 9 001 caractères (U+FB2C, ×3 en NFC) → `LABEL_TOO_LONG` bloquant, `can_commit False` ; `_prepare_rows` refuse aussi `LABEL_TRUNCATED` au commit |
| **M-05** `link_existing` sans cible | ✅ **levé** | sans `transaction_id` → `422` ; UUID inexistant → `422` ; identité différente → `422` ; la cible est revérifiée avant l'archive et sous `BEGIN IMMEDIATE` ; historique audité `[(…, None, 'create_occurrence'), (…, {'action': 'create_occurrence'}, 'link_existing')]` ; décision persistée immuable (`committed_import_rejection_immutable`) |
| m-04 alias numérique/IBAN/HTML | ✅ levé | `1234 5678 9012`, IBAN fictif groupé et `<script>` → `VALIDATION_ERROR` |
| m-05 lignes « Madame… » ignorées | ✅ levé | ligne `Madame …` après l'en-tête → `CSV_INVALID/INVALID_DATE` bloquant |
| m-06 dates de génération | ✅ levé | opération postérieure au téléchargement → `FUTURE_DATED_ROW` |
| m-13 alias dupliqués | ⚠️ partiel | refusé dans un aperçu et contre les comptes existants (`ACCOUNT_ALIAS_CONFLICT`) ; **non revérifié au commit** (voir m-02) |
| m-14 dates extrêmes | ✅ levé | 1999 → `DATE_IMPLAUSIBLE` bloquant |
| m-03 boucle d'événements | ✅ levé (code) | `def create_preview` (pool de threads), contrôle de `Content-Length`, plafond `max_preview_anomalies` |
| m-10 `repr` des libellés | ✅ levé | `field(repr=False)` sur `raw_label`, `identity_label` et `search_label` |

## Constats

### BLOQUANT

Aucun.

### MAJEUR

#### MAJ-01 — Résolution `OUT_OF_PERIOD` : l'UI annonce un effet faux et rend `link_existing` impraticable, ce qui conduit à dupliquer des opérations (INV-03)

- **Preuve** (`p3_atom.py` §4) : A (janvier) commite `ABONNEMENT FICTIF −9,99 €` au 15/01. B (période février) contient la même ligne datée du 15/01, donc `OUT_OF_PERIOD`. On choisit « Créer une occurrence » :
  ```text
  4) B hors période : [('OUT_OF_PERIOD', 'blocking')] can_commit False
     create_occurrence sur une ligne DÉJÀ importée : 200 2 | occurrences ABONNEMENT 15/01 : [(1,), (2,)]
  ```
  - Côté backend, `create_occurrence` crée **toujours** le rang `max(n_existing, n_new) + offset` (`backend/app/services/import_commit.py:699-704`), ce qui est conforme à IMPORT_COMMIT.md.
  - Côté UI, le texte (`frontend/src/pages/Transactions/index.tsx:235`) dit : « « Créer une occurrence » ajoute la provenance et **ne crée un nouveau rang que si la multiplicité l'exige** ». C'est faux.
  - L'alternative `link_existing` exige de saisir à la main l'UUID d'une transaction. Or l'UUID n'est affiché nulle part dans l'UI : le tiroir de détail montre la date, le compte, le libellé, l'identité, le rang et les sources, mais pas l'`id`. L'API d'aperçu ne propose aucun candidat de même identité, alors qu'ADR-018 (2) dit que « l'aperçu propose « lier à l'occurrence existante » ».
- **Impact** : l'utilisateur est mécaniquement orienté vers « Créer une occurrence ». Le cas typique d'une ligne déjà importée réapparaissant hors période dans un export suivant crée alors un **doublon définitif** : les transactions sont non supprimables par trigger, et la correction exige une procédure qui n'existe pas encore. Le test `test_IMP_11_IMP_12_provisional_day_and_explicit_out_of_period_decision` fige d'ailleurs ce comportement (rangs `[1, 2]`). Le contrôle de solde pourrait révéler le doublon, mais il est souvent `not_comparable` (voir m-05). Le risque réel est aujourd'hui faible, puisque ADR-018 (4) n'observe aucune ligne hors période dans le profil 2026. Il devient concret dès qu'un export en contient.
- **Correction proposée** :
  1. Dans l'aperçu, pour chaque ligne `OUT_OF_PERIOD`, exposer les transactions candidates de même identité stricte (`transaction_id`, rang, date d'import), sans libellé supplémentaire ;
  2. dans l'UI, proposer « Lier à l'opération existante (rang k) » par sélection et non par saisie d'UUID, et mettre ce choix par défaut quand un candidat existe ;
  3. corriger le texte : « Créer une occurrence crée toujours une nouvelle opération (rang n+1) » ;
  4. demander une confirmation explicite quand `create_occurrence` vise une identité qui a déjà des transactions.
  Tests : UI-05 complété par une ligne hors période liée sans doublon, et test API listant les candidats.
- **Ticket porteur** : T15 (UI), T13/T11 (exposition des candidats) ; ADR si la forme de réponse de l'aperçu change.

#### MAJ-02 — Archives brutes orphelines à chaque échec de commit, sans inventaire ni purge (INV-11, IMPORT_CONTRACT « nettoyage explicite et audité »)

- **Preuve** (`p2_conc.py` §5, `p3_atom.py` §1, §2 et §4, `p4_kill_perf.py`, `p8_accounts_race.py`) :
  ```text
  5) commit sous verrou : (503, 'DATABASE_BUSY') 5.0s | nouveaux .bin : 1 | .bin non référencés au total : 1
     retry même aperçu : (200, 'committed') | ... | .bin non référencés après retry : 1
  1) échec SQL à la 25e source_row : 409 IMPORT_CONFLICT | tables modifiées : [] | orphelines : 1
  2) RuntimeError dans save_response : (500, 'INTERNAL_ERROR') | tables modifiées : [] | orphelines : 1
     deux liens vers la même cible : can_commit True
     commit x3 : 409 MULTIPLICITY_RESOLUTION_INVALID | nouvelles archives orphelines : 3
  kill -9 pendant le commit : ... fichiers originals (orphelins) : 1
  commit f2 (empreinte créée entre-temps) : 409 IMPORT_CONFLICT | archives orphelines + 1
  ```
  - L'archive est écrite avant `BEGIN IMMEDIATE` (`import_commit.py:1007`).
  - Elle n'est supprimée que pour `replay`, `no_op` et `IDEMPOTENCY_CONFLICT`. Les chemins `DatabaseBusyError`, `IntegrityError`, les autres `AppError` et les exceptions inattendues la laissent en place (`import_commit.py:1043-1058`).
  - `503 DATABASE_BUSY` est pourtant le chemin normal « réessayable » sous concurrence.
  - En outre, l'aperçu déclare `can_commit=true` quand deux lignes `link_existing` visent la même transaction. `_validate_link_targets_before_archive` ne contrôle pas les doublons de cible : chaque essai écrit donc une archive, puis échoue.
- **Impact** : le rollback SQL est parfait (aucune table modifiée), mais chaque essai raté conserve une copie bit à bit du relevé bancaire. Ces copies ne sont référencées par aucune ligne SQL, sont invisibles dans l'API et l'UI, et ne sont jamais purgées : `purge_expired` ne couvre que `import-previews/`. IMPORT_COMMIT.md et IMPORT_CONTRACT.md renvoient à « un nettoyage explicite et vérifié » qui n'est implémenté nulle part. Les données privées sont donc conservées sans finalité (INV-11), et l'espace disque croît avec les tentatives.
- **Correction proposée** :
  1. Dans `commit_preview`, sur toute exception levée avant la confirmation du `COMMIT`, supprimer l'archive si aucun `import_files` commité ne la référence. Sinon, la déplacer dans une quarantaine inventoriée.
  2. Au démarrage, ajouter une purge ou un inventaire audité des `.bin` de `originals/` non référencés et plus vieux qu'un délai.
  3. Contrôler les cibles `link_existing` en double dans `_recalculate` (`can_commit=false`) et dans `_validate_link_targets_before_archive`.
  Tests : 503, `IntegrityError` et `RuntimeError` injectés ⇒ 0 `.bin` non référencé ; deux liens vers la même cible ⇒ `can_commit=false`.
- **Ticket porteur** : T12 ; ADR de politique d'archive orpheline (amende ADR-010 et IMPORT_COMMIT « Archive, transaction et quarantaine »).

### MINEUR

| ID | Constat et preuve | Correction proposée | Ticket |
|---|---|---|---|
| m-01 | **Commit super-linéaire et timeout UI.** Mesures (`p4_kill_perf.py`) : n=2 000 → 2,5 s ; 8 000 → 12,8 s ; 16 000 → 32,3 s. Le verrou d'écriture est tenu pendant toute la durée : les autres écritures reçoivent `503` après 5 s. Causes : des recherches linéaires dans des boucles (`row_targets` avec `next(... for item in normal_rows ...)`, `import_commit.py:722` ; `next(key for key, value in source_ids.items() ...)` pour les candidats ; une requête par ligne pour les candidats proches). Le client frontend coupe à 8 s (`frontend/src/lib/api/client.ts:20`) : au-delà d'environ 6 000 lignes, l'UI affiche « Le serveur local ne répond pas » alors que le commit réussit. Le réessai rejoue correctement grâce à la clé conservée. Le fichier réel de 1 024 lignes n'est pas concerné (environ 1,3 s). | Indexer par dictionnaires (O(n)) ; regrouper les requêtes de candidats par `(compte, date, montant)` ; timeout de commit plus long ou poll de statut ; plafond documenté de lignes par fichier. | T12/T13, T15 |
| m-02 | **Courses entre aperçus sur les comptes** (`p8_accounts_race.py`). (a) Deux aperçus ouverts en parallèle avec les alias `Courant` et `courant` sont tous deux commités : `comptes : [('Courant',), ('courant',)]`. Le refus d'ADR-026.3 (m-13) n'est vérifié qu'au PATCH et ne l'est plus au commit. (b) Deux fichiers d'un même compte inconnu, mappés chacun vers `new_account` : le second commit échoue en `409 IMPORT_CONFLICT` générique (sur la contrainte UNIQUE de l'empreinte), l'aperçu reste `unknown`/`can_commit True` (périmé) et une archive orpheline est créée (MAJ-02). Le remappage manuel fonctionne ensuite. Aucune double écriture. | Revérifier l'unicité d'alias (casefold) et l'absence d'empreinte dans `_resolve_accounts` avec un code dédié (`ACCOUNT_ALIAS_CONFLICT`, `ACCOUNT_FINGERPRINT_CONFLICT`) ; à terme, un index unique sur `lower(alias)` dans une migration 0002. | T12 |
| m-03 | **Entiers > 2^53 vers le frontend** (réserve B-01). L'aperçu de 1 000 lignes à 10^13 c renvoie `debit_total_cents=-10000000000000000` (`imports.py:80`), alors qu'ADR-026.1 exige « aucun entier hors 2^53 n'atteint jamais le frontend ». `formatCentsToEuro` lève `RangeError` sur un `number` non sûr, et aucune `ErrorBoundary`/`errorElement` n'existe : la page entière plante. Le même risque existe en théorie pour `summary` de `/transactions` (sommes multi-imports). | Ne pas exposer un total hors borne (`null` + code), ou sérialiser les centimes en chaîne au-delà de 2^53 ; ajouter une `errorElement` sur les routes. | T11/T15 |
| m-04 | **Mois antérieur au premier import renvoyé `partial`** (`p5_t14.py`) : `2025-06` → `period.completeness='partial'`, `coverage.status='not_started'`, `missing_days=[]`. `accounts.py:234` convertit `not_started` en `partial`, car `PeriodResponse` n'admet que `complete/partial/unknown`. Aucune fausse plage manquante (✅ KPI-05 au sens strict), mais un tableau de bord G6 qui signale les mois `partial` alerterait sur 2025, contrairement à ADR-003. De plus, `coverage.status` expose `not_started`, absent de l'énumération documentée dans API.md (`complete|partial|unknown`). | ADR : ajouter `not_started` à `period.completeness`, ou documenter `partial` + `coverage.status='not_started'` comme « non démarré, non alertable » ; aligner API.md et METRICS.md. | T14 / ARCH |
| m-05 | **Contrôle de solde potentiellement inerte sur les exports réels.** Si le « Solde au » du CA est daté du jour de téléchargement (cas fréquent ; la fixture `overlap_A` le date au 31/03), ADR-024.2 le rend toujours `not_comparable` (`balance_after_complete_through`, vérifié sur un exemple fabriqué). Le filet de sécurité contre les exports tronqués (G2 m-12) et les doublons (MAJ-01) ne servirait alors jamais. Non vérifiable par ce relecteur (accès aux données réelles interdit). | L'orchestrateur vérifie sur le profil réel, en agrégats seulement, la relation `balance_as_of` ↔ `generated_on`. Si elle est égale, ADR pour définir un solde comparable (par exemple, solde au jour J hors opérations de J) ou documenter la limite. | T14 / ARCH |
| m-06 | **Trous de tests révélés par les mutants** (`p9_mutants.sh`, 16 mutants, 7 tués). Survivants **non couverts** : (1) tri `transaction_id ASC` dans la pagination (doublons ou trous avec des dates ex aequo ; le code réel est correct, voir P7) ; (2) filtre `snapshot_at` supprimé ; (3) archive conservée sur `no_op` (IMP-17, octets conservés à tort) ; (4) contrôle de cible double supprimé (rattrapé seulement par l'UNIQUE SQL, en 409 générique) ; (5) lignes hors période incluses dans le maximum (aucun test de `create_occurrence` sur une identité nouvelle). Survivants **masqués par une seconde barrière** (acceptables) : revérification `link_existing` (×2), contrôle final du nombre de sources (trigger `commit_guard`), `complete_through` du jour provisoire (`min` avec la valeur stockée). Par ailleurs, `scripts/test_e2e_import.sh` (UI-05) ne fait partie ni de `make check` ni de la CI (`.github/workflows/ci.yml` n'exécute que le smoke). | Ajouter des tests nommés pour (1) à (5) ; ajouter `test_e2e_import.sh` à `make check` et à la CI. | QA / T12–T15 |
| m-07 | **Statut `rejected` jamais produit ; rejets d'un import commité invisibles dans l'UI.** Aucun chemin n'écrit `rejected`. Le contrat le permet (« peuvent »), mais IMPORT_CONTRACT §Atomicité le décrit pour les fichiers irrécupérables. L'UI n'affiche « Afficher les rejets » que pour les statuts autres que `committed` et `no_op` (`index.tsx:193`) : le bouton est mort, et les avertissements ou résolutions persistés d'un import commité ne sont consultables que par l'API. | Afficher les rejets et diagnostics de tout import ; documenter que `rejected` n'est pas utilisé en v1, ou l'implémenter. | T12 / T15 |
| m-08 | **Même clé d'idempotence, commits simultanés** (`p2_conc.py` §3) : 6 ou 7 × `200` rejoués, mais 1 ou 2 × `410 IMPORT_PREVIEW_EXPIRED`. La recherche rapide de rejeu précède la lecture de l'aperçu, qui est supprimé entre-temps. Un nouvel essai donne bien le rejeu. Aucune double écriture. | Sur `410`, refaire `find_replay` avant de répondre. | T12 |
| m-09 | **Accessibilité clavier.** Le tiroir `role="dialog" aria-modal` (`index.tsx:181`) ne déplace pas le focus, ne le piège pas et ne se ferme pas avec Échap. Les onglets (`index.tsx:158`) n'ont ni `aria-controls` ni navigation aux flèches. axe passe en clair et en sombre (375, 768, 1280), mais ces points relèvent du clavier. Par ailleurs, `linkTargets` n'est indexé que par `logical_row_idx` (collision entre sections), et les rôles `pea`/`cash` sont absents du sélecteur de nouveau compte. | Gestion du focus et Échap ; tabs ARIA complets ; clé `section:row`. | T15 |
| m-10 | **Divergences documentaires de l'API des opérations.** (a) TRANSACTIONS_API dit que `%`, `_` et les guillemets « restent littéraux ». En pratique, `search_label` supprime la ponctuation : `q='100%'` trouve `PCTX 100a ax` et `q='_x_'` trouve `ax`. Ce ne sont pas des jokers SQL, mais ce ne sont pas des littéraux non plus. (b) Le curseur contient `filters.q` en clair (base64 non chiffré) dans l'URL, alors que le document dit « les libellés recherchés ne devraient pas être copiés dans l'URL » (`q` est d'ailleurs déjà un paramètre GET). (c) Le snapshot filtre sur `imported_at_utc`, fixé à l'INSERT et non au COMMIT : un commit long, commencé avant `snapshot_at` et validé après, pourrait injecter des lignes en page 2 (théorique, non reproduit). | Corriger TRANSACTIONS_API ; exclure `q` du curseur (empreinte HMAC des filtres) ; horodater au commit ou filtrer par l'import `committed` avant le snapshot. | T15 / docs |
| m-11 | **PATCH concurrent d'un commit.** Le commit travaille sur l'aperçu de version v. Un PATCH accepté pendant ce temps répond `200` (v+1), puis sa décision disparaît avec l'aperçu supprimé. Le commit reste cohérent avec la version v approuvée (`expected_version`), mais la réponse du PATCH est trompeuse. Constat issu de la lecture du code, non reproduit. | Verrou d'aperçu (marqueur « commit en cours » ⇒ `409` au PATCH). | T11/T12 |

## Points attaqués et trouvés conformes

- **Idempotence et multiplicité** (`p1_idem.py`). Séquence A, B, A, C, A_bis, B : `committed 5/5`, `committed 2/5`, `no_op 0/0`, `committed 0/1`, `committed 0/5`, `no_op 0/0`. Résultat : 7 transactions et 16 provenances ; rangs `X` : `[(1, 4 sources), (2, 3), (3, 1)]`, soit max(2, 3)=3 et non 5. Un export C à une seule occurrence ne supprime rien. `-11,23`/`+11,23` donnent deux identités. L'ordre B, A produit **la même signature** que A, B (IMP-05 `True`), et la somme en centimes vaut la somme attendue (`-3255` des deux côtés). Réimport du même fichier : seules `import_files` (+1 `no_op`), `audit_events` et `idempotency_records` changent ; transactions +0, `source_rows` +0, `.bin` 1.
- **Concurrence réelle** (`p2_conc.py`, uvicorn et 8 threads HTTP derrière une barrière) :
  - 8 commits du même aperçu → 1 `committed` + 7 `no_op`, 300 tx / 300 sources, 1 seule archive ;
  - 8 aperçus du même hash → 1 `committed` + 7 `no_op` ;
  - deux fichiers chevauchants commités simultanément, 10 itérations → `anomalies = 0` (3 tx et 5 sources à chaque fois) ;
  - verrou externe → `503 DATABASE_BUSY` après 5,0 s, sans écriture ;
  - `integrity_check: ok`, `foreign_key_check: []`.
- **Atomicité** (`p3_atom.py`, `p4_kill_perf.py`). Échec SQL injecté à la 25e `source_row`, `RuntimeError` dans `save_response`, `_audit_preview_decisions` ou `near_duplicate_candidates`, échec d'archive : **aucune table modifiée** (comparaison de contenu de toutes les tables). L'aperçu reste présent et peut être recommité. `kill -9` à la 500e transaction d'un commit de 2 000 lignes : 0 transaction, 0 source, 0 import, 0 compte, `integrity_check ok` ; après redémarrage, le même aperçu se commite (2 000 créations).
- **Garde-fous SQL** : `docs/examples/schema.sql` est identique à `migrations/sql/0001_initial.sql` (hors commentaires et lignes vides) ; aucun `INSERT OR REPLACE`/`REPLACE INTO` dans `backend/app`. Triggers vérifiés : immutabilité des transactions, `source_rows`, sections et rejets commités ; transitions `previewed → committed|rejected` ; `no_op` lié au même hash ; garde de commit (sections, `complete_through`, rejets non résolus, nombre de sources, transaction sans source) ; rangs contigus ; index unique partiel du hash commité. PRAGMA `foreign_keys`/`recursive_triggers` posés à chaque connexion ; `BEGIN IMMEDIATE` via le hook `begin`.
- **Argent (INV-02)** : aucun `float` ni `Decimal` dans `backend/app` (seule mention : la docstring d'`amounts.py`). `type(x) is int` est exigé au binding. Le frontend formate en `bigint` (`formatCentsToEuro`) ; l'e2e UI-05 vérifie que la somme des cellules affichées égale le résumé, au centime près.
- **T14** (`p5_t14.py`) :
  - A, B cohérents → `match` (Δ 0) ; B, A → même statut et mêmes bornes. Les identifiants diffèrent uniquement parce que les `import_file_id` diffèrent entre les deux bases.
  - Écart d'1 centime → `mismatch` Δ 1, `2026-02` → `unknown` ; `2026-01` reste `complete`.
  - Solde daté du jour de téléchargement → `not_comparable/balance_after_complete_through`.
  - Export suivant non chevauchant → janvier `partial`, `missing_days=[2026-01-31]`.
  - Repli `received_on_fallback` → `partial`.
  - **Aucune écriture pendant les lectures** (contenu de toutes les tables identique, `balance_checks` : 0 ligne). `balance_check_status` n'existe pas sur les sections.
- **API des opérations** (`p7_tx_api.py`) :
  - 60 opérations le même jour, `limit=7` → 9 pages, 63 lignes uniques, ordre `booked_on DESC, id DESC` stable, somme des pages = `summary.net_cents` (avec et sans `q`) ;
  - curseur falsifié ou filtre changé → `CURSOR_INVALID` ; nouvel import pendant la navigation → non injecté ;
  - validations `limit` 0/101, plage de montants inversée, période inversée, `flow_kind`, `direction`, `q` > 200 caractères, UUID invalide, montant négatif → `422` ;
  - recherche insensible aux accents (`ete ca` → `Été Ça`) ; détail inconnu → `404`.
- **Vie privée (INV-11)** : aucun numéro de compte, empreinte HMAC ni libellé dans les réponses d'import (liste, détail, rejets, commit), dans `audit_events`, `idempotency_records` et `import_rejections.message`, ni dans les aperçus stockés. Aucun numéro dans le dump SQLite. Les messages d'anomalie du parseur sont fixes (`_MESSAGES[code]`). Les erreurs 500 ne journalisent qu'un `correlation_id` ; les logs d'accès httpx sont rédigés (`[URL REDACTED]`). Le libellé brut n'est exposé que par `/transactions`, ce qui est voulu. Le nom de fichier client n'est jamais conservé ; archives en `0600`, répertoire en `0700` (test IMP-01).
- **UI imports et opérations (UI-05)** : e2e réel (backend et navigateur) réussi : import A, import B, `no_op`, rapprochement des montants affichés avec le résumé, axe sans violation en clair et en sombre aux 3 largeurs et pendant l'aperçu. États de chargement, d'erreur (messages par statut 410/413/415/409/503) et de vide présents. La pagination « Charger plus » utilise le curseur. Les libellés sont rendus comme du texte (aucun `dangerouslySetInnerHTML`).

## Critères G3

| Critère (PLAN §F, gate G3) | Statut | Preuve |
|---|---|---|
| Réimport du même fichier → 0 transaction nouvelle | ✅ | P1 (contenu des tables), P2 §1/§2, `test_IMP_01_…` |
| CSV chevauchant → uniquement le delta | ✅ | P1 (B après A : +2), P2 §4 |
| `-11,23` et `+11,23` ne s'annulent pas | ✅ | P1 `Y signés : [(-1123, 1), (1123, 1)]`, `test_IMP_08_…` |
| 2 lignes identiques conservées | ✅ | P1 (rangs 1..3), `test_IMP_02_IMP_03_IMP_04_IMP_09_…` |
| La concurrence ne duplique pas | ✅ | P2 (uvicorn, 8 threads), `test_IMP_07_…` |
| Échec provoqué au milieu → rollback total | ✅ (réserve MAJ-02 sur le disque) | P3 §1/§2/§3, P4 `kill -9` |
| Totalité des centimes réconciliée | ✅ | P1 (somme exacte A, B = B, A), e2e UI-05 |
| T14 : 2026 seul ⇒ aucun faux trou en 2025 ; mois partiel reconnu | ✅ (réserve m-04) | P5 |
| T15 : aperçu/commit, liste paginée, erreurs et références d'origine visibles | ✅ (réserves MAJ-01, m-07, m-09) | P7, e2e UI-05 |
| Vrai fichier 2026 | ⚪ non rejoué | accès interdit à ce relecteur |

## Recommandations d'ADR (arbitrage de l'orchestrateur)

1. **Politique d'archive orpheline** (MAJ-02) : suppression sur échec avant le `COMMIT` confirmé, inventaire et purge audités au démarrage ; amende ADR-010 et IMPORT_COMMIT.
2. **Résolution `OUT_OF_PERIOD`** (MAJ-01) : l'aperçu expose les candidats de même identité ; sémantique et libellé de `create_occurrence` (« crée toujours le rang n+1 ») ; confirmation quand l'identité existe déjà.
3. **`month_completeness` avant la couverture** (m-04) : valeur `not_started`, ou règle d'affichage non alertable (ADR-003, KPI-05).
4. **Solde d'en-tête comparable** (m-05) : à trancher après vérification en agrégats sur le profil réel.
5. **Unicité d'alias en base** (m-02) : migration 0002 (`lower(alias)`), puisque 0001 est gelée selon ADR-025.2.

## Propositions pour les documents de suivi

- `REGISTRE_RISQUES.md` : ajouter MAJ-01 (doublon par `create_occurrence`, lié à R-17), MAJ-02 (rétention d'octets bruts), m-01 (durée du commit et verrou), m-05 (contrôle de solde inerte).
- `ETAT_IMPLEMENTATION.md` : G2 levé (B-01, M-01 à M-05 confirmés) ; G3 « PASS sous réserves », réserves MAJ-01 et MAJ-02.

---

```text
Ticket : revue G3 (T12, T13, T14, T15) + confirmation G2 (ADR-026)
Objectif livré : revue adversariale indépendante, verdict G3 = PASS sous réserves (0 BLOQUANT, 2 MAJEUR, 11 MINEUR) ; levée G2 confirmée (B-01, M-01..M-05).
Fichiers changés : docs/revues/REVUE_G3.md (création, seul fichier écrit dans le dépôt réel).
Interfaces modifiées : aucune.
Tests ajoutés : aucun dans le dépôt ; scripts jetables /tmp/revue_g3_scripts/{h,p1_idem,p2_conc,p3_atom,p4_kill_perf,p5_t14,p6_g2_privacy,p7_tx_api,p8_accounts_race}.py et p9_mutants.sh.
Commandes réellement exécutées + résultats : pytest 290 passed/4 deselected ; ruff OK (100 fichiers) ; mypy OK (57) ; make verify-contracts PASS ; vitest 42/42 ; tsc OK ; eslint OK ; make test-integration 102 passed ; make test-e2e-smoke exit 0 ; scripts/test_e2e_import.sh 2 passed ; check_no_private_data OK (268 fichiers) ; P1–P8 (sorties p*.out) ; 16 mutants : 7 tués, 9 survivants dont 5 vrais trous de tests.
Invariant vérifié : INV-01, INV-02, INV-03 (réserve MAJ-01), INV-04 (réserve MAJ-02 côté disque), INV-05, INV-08 (décisions immuables), INV-09/10 (T14), INV-11 (réserve MAJ-02).
Limites / cas non couverts : vrai CSV 2026 non rejoué (accès interdit) ; m-05 et m-10(c) non vérifiés sur données réelles, m-11 issu de la lecture du code ; performance mesurée jusqu'à 16 000 lignes seulement ; UI testée par l'e2e existant et la lecture du code (pas de session manuelle au clavier).
Risques / sécurité / données privées : aucune donnée réelle lue ni citée ; numéros et libellés du rapport fictifs ; rétention d'archives orphelines (MAJ-02).
Relecture recommandée : orchestrateur pour l'arbitrage ADR (MAJ-01, MAJ-02, m-04, m-05) ; auteur T12/T13 pour MAJ-02 et m-01 ; auteur T15 pour MAJ-01, m-07 et m-09.
Hypothèses : le « Solde au » du CA peut être daté du jour de téléchargement (m-05, à confirmer) ; base de revue 1066748 = état soumis au gate.
Instructions de reproduction : rm -rf /tmp/revue_g3 && mkdir /tmp/revue_g3 && git -C <dépôt> archive 1066748 | tar -x -C /tmp/revue_g3 && cd /tmp/revue_g3 && git init -q && git add -A && git commit -qm s && uv sync --all-groups && (cd frontend && npm ci) ; puis /tmp/revue_g3/.venv/bin/python -I /tmp/revue_g3_scripts/pN_*.py et bash /tmp/revue_g3_scripts/p9_mutants.sh.
```
