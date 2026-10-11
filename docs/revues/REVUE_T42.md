# Revue indépendante — T42 Sécurité locale, origines et confidentialité

Date : 2026-10-11  
Périmètre : copie isolée du HEAD et de l’arbre de travail, sans `.git`, `.venv`, `node_modules`, `csv_references/`, `data/`, `tmp.zip` ni `nimbalyst-local`. Aucune donnée bancaire réelle n’a été consultée.

## Verdict : BLOCK

Le bind et les refus d’origines hostiles sont globalement en place par défaut, et aucun marqueur synthétique de secret, IBAN ou libellé n’a fui pendant les probes. T42 ne peut pas être clôturé : la page frontend effectivement servie n’a pas de CSP ; le contrat API décrit un jeton et une route CSRF absents du code ; le mode de bind externe expose une API sans authentification applicative ; enfin, la suite backend demandée a un échec reproductible indépendant de T42.

## Constats

### MAJEUR — M-01 — La page frontend servie n’a pas de CSP effective

Le middleware pose une CSP lorsqu’il voit une réponse HTML ([security.py](../../backend/app/core/security.py), lignes 134–141). Or l’application FastAPI ne sert pas le build frontend : [main.py](../../backend/app/main.py), lignes 140–141, indique que cela reste un point d’extension futur. [vite.config.ts](../../frontend/vite.config.ts) ne configure aucun en-tête de sécurité, et [index.html](../../frontend/index.html) ne contient pas de CSP.

Preuve d’exécution : après `npm run build`, `npm run preview -- --host 127.0.0.1 --port 4173 --strictPort` a servi `/` en `200 text/html`; `Content-Security-Policy` et `X-Content-Type-Options` étaient absents. Le build passe donc à côté de la politique définie dans le backend. `docs/PRIVACY.md` ne documente pas de CSP.

Conséquence : la CSP `default-src 'none'`, `connect-src 'self'`, `frame-ancestors 'none'` protège les HTML servis par le backend, mais pas l’interface réellement servie par Vite/preview. Le test `test_SEC_02_api_security_headers_and_html_csp` ajoute une route HTML artificielle à FastAPI ; il ne vérifie pas le frontend distribué.

À faire : poser et vérifier les en-têtes au point qui sert réellement le frontend (build local compris), puis documenter la politique effective dans la documentation de confidentialité.

### MAJEUR — M-02 — Le contrat CSRF décrit une protection absente du code

[API.md](../API.md), lignes 39, 76 et 87, dit que chaque mutation exige un jeton CSRF, décrit `GET /api/v1/security/csrf` et un en-tête `X-CSRF-Token`. Aucun code backend ou frontend ne contient `csrf`, `csrf_token` ou `X-CSRF-Token`. L’OpenAPI de l’application construite pendant la revue expose `/docs` et `/openapi.json`, mais aucune route CSRF. Le client [client.ts](../../frontend/src/lib/api/client.ts) ne précharge ni n’envoie de jeton.

Le contrôle réellement appliqué dans [security.py](../../backend/app/core/security.py), lignes 88–97 et 205–211, est une vérification d’`Origin`, avec repli sur `Referer` quand `Origin` manque. Une origine hostile, `Origin: null`, un `Referer` hostile ou l’absence des deux ne sont pas acceptés ; un `Referer` local est accepté. Les probes ont confirmé ces cas. Les tests vérifient aussi un POST multipart hostile, un PATCH hostile et le préflight CORS.

Je n’ai pas reproduit de contournement navigateur de ce contrôle d’origine. Le défaut constaté est un désaccord de contrat : un client qui suit `API.md` attend un endpoint et un jeton qui n’existent pas, et le contrat n’explique pas la politique réelle pour `Origin` absent / `Referer` local. Le plan autorise une garde « CSRF/origin », mais aucune décision ADR ne tranche ce contrat.

À faire : arbitrer par ADR si le contrôle d’origine/referer constitue la défense attendue, ou si un jeton est requis ; aligner ensuite API, client et tests. Documenter explicitement le comportement des clients sans `Origin` et sans `Referer` (refus), des formulaires `text/plain` et des clients non navigateurs.

### MAJEUR — M-03 — Le mode de bind externe n’a pas d’authentification applicative

Par défaut, [config.py](../../backend/app/core/config.py), lignes 45–48 et 125–131, refuse un hôte non-loopback ; `ALLOW_NON_LOOPBACK=true` l’autorise. [PRIVACY.md](../PRIVACY.md), lignes 108–115, avertit du risque et demande un réseau contrôlé. Toutefois, les contrôles CORS et `Origin` ne sont pas une authentification : un client HTTP non navigateur peut fixer lui-même `Host`, `Origin` et `Referer`. Aucune authentification applicative n’apparaît dans le périmètre backend examiné. En mode externe, les lectures financières peuvent donc être accessibles aux machines capables d’atteindre le port, et un client non navigateur peut fournir les en-têtes locaux attendus.

Ce risque est conditionné à l’activation explicite du bind externe ; le bind loopback par défaut reste le bon comportement. [API.md](../API.md), lignes 43 et 85, affirme toutefois sans réserve que le serveur écoute uniquement sur loopback, contrairement au réglage accepté.

À faire : décider par ADR si le bind externe est supporté pour la v1. S’il le reste, documenter que CORS/origin ne protègent pas contre un client réseau direct et que l’option ne doit pas être utilisée sans protection réseau appropriée ; aligner `API.md` et `PRIVACY.md`.

### MINEUR — m-01 — Les requêtes HTTP IPv6 loopback sont rejetées

Les origines par défaut incluent `http://[::1]:5173` et `http://[::1]:9513` ([security.py](../../backend/app/core/security.py), lignes 13–20). Mais `parse_host_header("[::1]:9513")` renvoie l’hôte `"[::1]"`, tandis que `allowed_hosts` stocke `"::1"` après retrait des crochets ([security.py](../../backend/app/core/security.py), lignes 73–86). La probe retourne donc `False` pour `Host: [::1]:9513`, y compris lorsque l’origine IPv6 est configurée. C’est un refus d’accès, pas un contournement.

Les variantes adversariales vérifiées donnent : `LOCALHOST:9513` accepté ; `localhost.:9513`, `127.0.0.1.evil.com:9513`, un port non permis et l’userinfo refusés ; l’origine locale en majuscules est normalisée ; `Origin: null`, le suffixe DNS trompeur et l’userinfo sont refusés. `Host: 127.0.0.1:5173` est aussi accepté parce que ce port est ajouté depuis la liste d’origines.

À faire : normaliser l’hôte renvoyé par `parse_host_header` de façon cohérente et ajouter des tests Host/Origin sur les variantes demandées.

### MINEUR — m-02 — Les tests d’acceptation ne couvrent pas encore toutes les frontières

- SEC-01 teste les paramètres transmis à `uvicorn.run` et la garde `Host` via `TestClient`, mais ne prouve pas l’adresse d’un socket réellement ouvert.
- SEC-02 couvre POST/PATCH hostiles et un préflight explicite, mais pas `Origin: null` au niveau HTTP, un POST `text/plain` avec vérification d’un état persistant avant/après, ni les réponses d’erreur/304/fichiers pour `no-store`.
- SEC-03 est un test React avec réponse API mockée ; il vérifie que `<img onerror>` reste du texte, mais ne parcourt pas l’import CSV dans un E2E navigateur.
- SEC-04 injecte des valeurs synthétiques dans une erreur et un CSV, et vérifie `health`, `/openapi.json`, `repr` et les logs capturés. Une probe supplémentaire de cette revue a injecté des marqueurs synthétiques dans query, `Authorization` et un en-tête personnalisé vers une route levant une erreur : réponse générique 500 et aucun marqueur dans le corps ou les logs capturés. `uvicorn` a `access_log=False` ([__main__.py](../../backend/app/__main__.py), lignes 11–18). `/docs` n’est pas couvert par le test de fuite.
- SEC-05 vérifie les connexions sockets `connect` et `connect_ex` pendant lecture de transactions et aperçu d’import. Le test ne neutralise pas `getaddrinfo`/le DNS ni les sous-processus ; une tentative DNS pourrait sortir avant le blocage de `connect`, et un sous-processus n’hérite pas du monkeypatch. Le backend examiné ne contient actuellement pas de client HTTP LLM ni d’appel réseau sortant ; aucun appel sortant n’a été observé, mais le garde-test ne constitue pas un confinement général.

Les garde-fous présents sont utiles : les erreurs Pydantic n’incluent pas l’entrée brute ; les gestionnaires renvoient une enveloppe générique ; Uvicorn n’écrit pas de journaux d’accès ; les réponses dont le chemin commence par `/api/` reçoivent `Cache-Control: no-store` à chaque début de réponse, y compris erreurs et réponses fichier/304 si une route en produit ([security.py](../../backend/app/core/security.py), lignes 107–141). Les statuts 304/fichier n’ont toutefois pas d’oracle dédié.

### MINEUR — m-03 — Options de documentation et détails de rétention à préciser

FastAPI expose `/docs` et `/openapi.json` par défaut, sans réglage `Settings` pour les désactiver. Le schéma interrogé ne révèle pas la clé LLM ; ces pages restent cependant disponibles, et le mode externe les rend joignables depuis le réseau. Décider si elles doivent être désactivées dans le packaging local. La CSP backend stricte s’applique à `/docs`, mais la page Swagger par défaut dépend de scripts inline/CDN et n’est donc pas un oracle de CSP utilisable.

La section rétention de [PRIVACY.md](../PRIVACY.md) décrit la conservation indéfinie des opérations/audits, les aperçus de 30 minutes, les archives orphelines de 24 h, les sauvegardes et l’inclusion de la clé HMAC. Elle n’indique pas la rétention de 7 jours des entrées d’idempotence (API.md/ADR-023) ni le réglage `PREVIEW_DIR`, qui peut déplacer les aperçus hors de `DATA_DIR`. La procédure « tout effacer » doit mentionner ces chemins/configurations.

## Contrôles satisfaisants

- Loopback : `127.0.0.1:9513` est la valeur par défaut ; le bind externe échoue sans opt-in. Le lancement Uvicorn transmet le bind configuré.
- CORS : les origines sont explicites, limitées au loopback et normalisées ; aucun wildcard ni `Access-Control-Allow-Credentials` n’est émis. Le préflight autorisé renvoie l’origine exacte ; l’origine externe est refusée.
- Injection HTML : aucune utilisation de `dangerouslySetInnerHTML`, `innerHTML`, `insertAdjacentHTML` ou `DOMParser` dans `frontend/src`. React affiche le libellé hostile en texte ; le test vérifie qu’aucun élément `<img>` n’est créé.
- Client frontend : les chemins API sont relatifs à l’origine courante, `redirect: 'error'` interdit le suivi d’une redirection, et le test l’assert. La source ne contient pas d’URL externe chargée ; aucun motif de clé n’apparaît dans le bundle construit. Les URLs visibles dans le JS compilé sont des namespaces W3C ou des liens d’aide d’erreur React/React Router, pas des ressources chargées.
- Secrets et erreurs : `Settings` masque les champs sensibles dans son `repr`; `/health` ne renvoie qu’un booléen `llm_enabled`; erreurs et validation n’échoient pas les entrées. Les filtres de journalisation rédigent secrets usuels, IBAN et longues suites numériques.
- Confidentialité : les sauvegardes sont bien documentées comme privées, non chiffrées et contenant la clé HMAC ; la procédure d’effacement couvre les emplacements par défaut et demande de supprimer aussi les emplacements/exportations externes.

## Couverture SEC-01..SEC-05

| ID | Tests nommés présents | Évaluation |
|---|---|---|
| SEC-01 | `test_SEC_01_default_bind_and_cors_origins_are_loopback`, `test_SEC_01_non_loopback_bind_requires_explicit_opt_in`, `test_SEC_01_server_binds_to_loopback_by_default`, `test_SEC_01_loopback_host_is_accepted_and_nonlocal_host_is_rejected` | Bon contrôle de configuration et de garde Host ; socket réel et Host IPv6 manquent. |
| SEC-02 | `test_SEC_02_hostile_origin_is_rejected_for_mutations`, `test_SEC_02_local_referer_is_used_when_origin_is_absent`, `test_SEC_02_cors_preflight_is_explicit_and_never_wildcard`, `test_SEC_02_api_security_headers_and_html_csp`, `test_SEC_02_cors_rejects_wildcard_and_external_origins`, `test_SEC_02_referer_origin_parser_ignores_path_but_checks_origin`, `test_SEC_02_missing_mutation_source_is_not_trusted` | Origine hostile et politique CORS couvertes ; contrat du jeton diverge ; les cas d’état DB, content-type simple, 304/fichier et préflight `null` manquent. |
| SEC-03 | `it('SEC-03 renders an injected banking label as text')` | Échappement React vérifié sur réponse mockée, pas sur le parcours CSV E2E. |
| SEC-04 | `test_SEC_04_injected_secret_and_iban_are_redacted_from_captured_logs`, `test_SEC_04_health_openapi_and_internal_error_do_not_disclose_secrets_or_paths`, `it('SEC-04 frontend source keeps provider secrets out of the bundle')` | Bonne couverture corps/exception/health/OpenAPI/source ; query/en-têtes et `/docs` absents des tests nommés. Probe query/en-têtes supplémentaire de cette revue : aucun écho. |
| SEC-05 | `test_SEC_05_llm_disabled_import_and_transaction_reads_make_no_external_calls`, `test_SEC_05_no_external_domain_is_hardcoded_in_application_sources`, `it('SEC-05 refuses API redirects')` | Chemins courants sans sortie et redirections frontend couverts ; DNS, sous-processus et confinement réseau général non couverts. |

## Régression demandée

Préparation dans la copie isolée : `uv sync --all-groups` et `cd frontend && npm ci` ont réussi. `npm run build` a réussi. Pour les deux contrôles Git, un dépôt Git temporaire vide a été initialisé uniquement dans la copie isolée, car la copie demandée excluait `.git`.

- `uv run pytest -q backend/tests -m 'not opt_in_real_data'` : **452 passed, 1 failed, 4 deselected** en 166,30 s. Échec : `test_IMP_06_IMP_07_and_SEC_06_schema_contracts_replay_on_migrated_database`; le test passe `ignored-reference-path.sql`, puis `verify_schema` tente de lire ce fichier et reçoit `FileNotFoundError`. Cet échec ne porte pas sur T42, mais laisse la suite demandée rouge.
- `uv run ruff check backend` : réussi (`All checks passed!`).
- `uv run mypy backend/app` : réussi (`Success: no issues found in 65 source files`).
- `cd frontend && npm run test -- --run` : réussi, 7 fichiers et 50 tests.
- `cd frontend && npm run typecheck` : réussi.
- `cd frontend && npm run lint` : réussi.
- `make verify-contracts` : code retour 0 ; `verify_schema`, les 34 fixtures et les 4 tests du profil CSV/sécurité réussissent ; validation du plan : 46 tickets, 12 gates, 87 IDs de catégories, 81 références de tests.
- `scripts/check_no_private_data.sh --with-untracked` : réussi, 320 fichiers inspectés dans la copie isolée.

## Manques pour clore T42 et recommandations d’ADR

1. Ajouter une preuve HTTP de CSP sur le frontend construit et servi par le mode de lancement réellement livré ; compléter la documentation CSP.
2. Écrire un ADR court sur le contrat anti-CSRF (Origin/Referer seul ou jeton), puis aligner `API.md`, les routes et le client frontend.
3. Arbitrer l’exposition réseau de `ALLOW_NON_LOOPBACK` sans authentification applicative ; mettre en cohérence `API.md` et `PRIVACY.md`.
4. Corriger la normalisation IPv6 Host et couvrir les variantes adversariales, les mutations `text/plain`, l’absence de source, l’absence d’effet DB, les erreurs/304/fichiers et les logs Uvicorn.
5. Durcir SEC-05 pour que le test détecte ou bloque aussi la résolution DNS et les chemins subprocess pertinents ; conserver la preuve d’absence d’appel lorsqu’aucun client LLM n’existe.
6. Documenter la rétention idempotence, `PREVIEW_DIR`, l’état de `/docs`/`openapi.json`, et compléter l’effacement.
7. Résoudre ou isoler l’échec `test_IMP_06_IMP_07_and_SEC_06_schema_contracts_replay_on_migrated_database`, puis rejouer la suite backend avant de valider G11.

## Retour de revue — format AGENTS.md

Ticket : T42  
Objectif livré : revue indépendante adversariale sécurité locale, origines et confidentialité ; verdict BLOCK.  
Fichiers changés : `docs/revues/REVUE_T42.md` uniquement.  
Interfaces modifiées : aucune.  
Tests ajoutés : aucun ; revue seule.  
Commandes réellement exécutées + résultats : voir « Régression demandée » et les probes documentées ci-dessus.  
Invariant vérifié : aucun bind externe sans opt-in ; mutation refusée pour origine hostile ou absente ; pas de wildcard CORS ; réponses API marquées no-store par middleware ; aucun marqueur synthétique exposé dans la probe d’erreur/log.  
Limites / cas non couverts : socket loopback non démarré pour vérification directe ; pas de test réseau egress contre DNS ou sous-processus ; pas de navigateur E2E pour le CSV ; pas de lecture des données réelles.  
Risques / sécurité / données privées : CSP absente du frontend servi ; contrat CSRF/token divergent ; bind externe sans authentification si opt-in ; défaut de normalisation Host IPv6 ; un échec de régression backend indépendant de T42. Aucun secret ni donnée réelle inclus dans le rapport.  
Relecture recommandée : arbitrage ARCH/orchestrateur sur le contrat CSRF et l’opt-in réseau, puis une nouvelle revue OPS après résolution des constats majeurs.
