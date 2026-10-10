# Revue indépendante du durcissement G3

- **Date** : 2026-10-10
- **Rôle** : relecteur indépendant, aucun correctif de cette branche n'a été écrit par le relecteur.
- **Base** : arbre de travail comparé à HEAD ; revue exécutée dans la copie isolée prescrite, sous /tmp/revue_g3h.
- **Verdict** : **BLOCK**.

## Motif du verdict

La purge au démarrage peut supprimer l'archive d'un commit encore en cours. Le scénario a été reproduit : l'archive est écrite avant BEGIN IMMEDIATE, son âge apparent dépasse le délai de 24 heures, la purge la supprime pendant que le commit est suspendu, puis le commit réussit en enregistrant son chemin. La base référence alors une archive absente. La règle MAJ-02 exige que cette archive ne soit jamais supprimée.

Un arrêt brutal laisse aussi parfois le fichier temporaire contenant les octets bruts dans originals ; aucun démarrage ne le purge. Enfin, le contrôle des soldes expose un entier monétaire supérieur à 2^53 au frontend et la pagination de transactions peut inclure dans une page suivante une opération validée après la borne de snapshot. Ces défauts empêchent de valider le durcissement.

## Périmètre et références

Instructions et contrats consultés : [AGENTS.md](/home/nill/Desktop/compte/mescompts/AGENTS.md), [REVUE_G3.md](/home/nill/Desktop/compte/mescompts/docs/revues/REVUE_G3.md), [DECISIONS.md](/home/nill/Desktop/compte/mescompts/docs/DECISIONS.md) (ADR-030, ADR-010/012/018/024/026), [IMPORT_COMMIT.md](/home/nill/Desktop/compte/mescompts/docs/IMPORT_COMMIT.md), [IMPORT_API.md](/home/nill/Desktop/compte/mescompts/docs/IMPORT_API.md), [API.md](/home/nill/Desktop/compte/mescompts/docs/API.md) et [TRANSACTIONS_API.md](/home/nill/Desktop/compte/mescompts/docs/TRANSACTIONS_API.md).

La revue couvre les chemins import, archive, aperçu, commit, comptes, transactions, leur frontend, les nouveaux tests G3 et le script E2E. Les changements allocations, refund_links, schéma/migrations, sauvegardes et sécurité ont été exclus de l'analyse fonctionnelle. Ils restent présents dans le diff global de l'arbre de travail. Aucun fichier de csv_references/, data/ ou tmp.zip n'a été ouvert ; les essais et chiffres ci-dessous utilisent des données synthétiques. Aucun chemin local ni libellé bancaire réel n'a été inclus dans les réponses, journaux ou audits examinés.

## État MAJ-01, MAJ-02 et m-01 à m-11

| Point | Statut | Preuve et limite |
|---|---|---|
| MAJ-01 — candidats OUT_OF_PERIOD | **Partiellement levé** | L'API expose seulement transaction_id, occurrence_rank et imported_at_utc ; le test API vérifie l'absence de libellé. L'interface sélectionne par défaut le premier candidat, affiche le rang, décrit la création comme un nouveau rang n+1 et demande confirmation pour créer une identité déjà présente. Le test API de commit prouve que link_existing ajoute une provenance sans transaction. L'E2E réel échoue plus tôt sur les diagnostics résolus et n'atteint donc pas son assertion UI de non-duplication ; la confirmation du choix « créer » n'est pas exercée en E2E. |
| MAJ-02 — archivage, rollback et purge | **Non levé — bloquant** | Les tests injectés couvrent 503 DATABASE_BUSY, IntegrityError, RuntimeError, doublons de cibles link_existing et rollback SQL ; ces échecs ordinaires laissent zéro .bin non référencé. Une archive commise de plus de 24 h survit à la purge normale. Le SIGKILL au milieu de la transaction annule les écritures métier, mais laisse un .bin récent pendant la période de grâce ; après vieillissement synthétique de 25 h, il est supprimé avec un audit agrégé. En revanche, une purge concurrente peut supprimer l'archive d'un commit actif si son mtime est vu comme antérieur à 24 h. Un SIGKILL avant le renommage laisse en plus un fichier .tmp jamais nettoyé. Voir B-G3-01 et M-G3-02. |
| m-01 — performance et résultats | **Partiellement levé** | Sur le même environnement synthétique, l'ancien commit HEAD puis le commit actuel ont donné : n=2 000, 2,972 s puis 2,452 s ; n=8 000, 19,105 s puis 13,669 s ; n=16 000, 52,050 s puis 44,208 s. Les temps ont baissé, mais 16 000 lignes occupent encore le verrou SQLite environ 44 s ; les autres écritures peuvent alors répondre 503 après cinq secondes. Le plafond de 20 000 lignes est cohérent dans l'API, le service et la documentation ; l'UI n'annonce pas ce plafond et transforme le 413 TOO_MANY_OPERATIONS en message « taille maximale » générique. Signatures de résultats identiques sur un corpus synthétique à doublons et chevauchements, pour A,B,A et B,A. Voir M-G3-08 et M-G3-04. |
| m-02 — alias, empreinte, codes et concurrence | **Levé dans les chemins testés** | L'alias est comparé avec casefold ; alias et empreinte sont revérifiés au commit avec ACCOUNT_ALIAS_CONFLICT et ACCOUNT_FINGERPRINT_CONFLICT. Les tests synthétiques couvrent les courses d'aperçus. L'index unique lower(alias) reste reporté après T17 comme l'indique ADR-030. |
| m-03 — entiers sûrs et erreur de route | **Partiellement levé — majeur** | Les totaux de prévisualisation et le résumé de /transactions sont neutralisés hors de la plage JavaScript sûre. Cependant /accounts/{id}/balance-checks renvoie encore directement les champs cents calculés ; reproduction synthétique : recalculated_balance_cents et delta_cents valent -10 000 000 000 000 000 et 10 000 000 000 000 000. La page Transactions a un composant ErrorBoundary, mais la route n'a pas l'errorElement explicitement exigé par ADR-030. |
| m-04 — not_started | **Levé** | Le schéma Pydantic, les contrats documentés et le type/libellé frontend acceptent not_started ; le test pré-démarrage attend cette valeur neutre. |
| m-05 — comparaison du solde d'en-tête | **Reporté explicitement** | ADR-030 confirme le profil, rend ce contrôle not_comparable et prévoit T14b. Aucune donnée réelle n'a été ouverte pour cette revue. |
| m-06 — trous de tests | **Partiellement levé** | Des tests ajoutent la couverture des candidats, des liens répétés, de not_started, de replay et du PATCH pendant le commit. Les assertions de tri et du filtrage après snapshot ne détectent pas la course d'un commit commencé avant le snapshot et finalisé ensuite. scripts/test_e2e_import.sh n'est référencé ni dans Makefile ni dans la CI ; son échec actuel empêche par ailleurs la preuve E2E complète. |
| m-07 — rejets et diagnostics | **Partiellement levé** | Le bouton de diagnostics est désormais disponible pour tous les statuts. Aucun chemin ne produit toutefois le statut rejected ; l'API continue de l'annoncer. Les diagnostics résolus d'un import commité sont retournés sans resolution_status, donc l'interface les présente comme des rejets non résolus et l'E2E échoue sur l'attente d'une liste vide. |
| m-08 — replay concurrent | **Levé dans le scénario reproduit** | Deux commits simultanés de même clé ont renvoyé le même résultat committed après suppression du fichier aperçu ; aucun 410 n'a été observé. Le test séquentiel d'aperçu expiré est aussi présent. |
| m-09 — accessibilité clavier | **Levé dans le code ; E2E partiel** | Le tiroir restaure le focus, se ferme avec Échap et conserve le focus dans le dialogue ; les onglets ont les attributs ARIA et répondent aux flèches. Le sélecteur inclut pea et cash. Le test axe isolé passe en clair et sombre ; le troisième scénario du script E2E n'a pas été atteint après l'échec précédent. |
| m-10 — recherche, curseur et snapshot | **Partiellement levé — majeur** | Le curseur ne contient plus q en clair : les filtres sont liés par HMAC SHA-256 et une falsification répond CURSOR_INVALID (HTTP 422). Restent la divergence entre la documentation et la normalisation de ponctuation de search_label (100% est normalisé en 100) et la course de snapshot reproduite : un commit commencé avant le snapshot mais validé après peut entrer dans une page ultérieure. API.md documente en outre CURSOR_INVALID en 400, tandis que l'endpoint renvoie 422. |
| m-11 — PATCH pendant le commit | **Levé** | begin_commit marque l'aperçu et update renvoie IMPORT_COMMIT_IN_PROGRESS avec HTTP 409. Le test IMP-11 vérifie le PATCH après cette marque. |

## Nouveaux constats

### BLOQUANT

#### B-G3-01 — La purge peut supprimer l'archive d'un commit actif

Dans [import_commit.py](/home/nill/Desktop/compte/mescompts/backend/app/services/import_commit.py:1036), l'archive est écrite avant write_transaction/BEGIN IMMEDIATE. Dans [import_archive.py](/home/nill/Desktop/compte/mescompts/backend/app/services/import_archive.py:60), la purge inventorie les seules références commitées, puis supprime tout .bin non référencé dont le mtime dépasse 24 heures. Aucun état « commit actif » ni verrou partagé avec la purge ne protège cette fenêtre.

Reproduction, dans la copie isolée : lancer python -I /tmp/revue_g3h_scripts/purge_commit_race.py. Le script suspend le commit juste après ImportArchive.write, vieillit le mtime synthétique au-delà de la fenêtre, exécute la purge puis laisse reprendre le commit. Résultat : during_commit_files_before_sweep=1, purged=1, files_after_sweep=0, commit_status=committed, committed_archive_exists=False. Il s'agit d'une course reproductible dès que le mtime est décalé ou qu'un commit reste actif au-delà du délai ; cela invalide la garantie « jamais supprimer une archive de commit en cours » et laisse la base avec un chemin mort.

**Recommandation :** coordonner la purge et le cycle de commit sous un verrou ou un mécanisme de réservation fiable ; ne pas utiliser le seul mtime comme preuve d'absence de commit actif. Ajouter un test concurrent qui bloque le commit entre l'écriture d'archive et le COMMIT, lance la purge et vérifie la référence après reprise.

### MAJEURS

#### M-G3-02 — Un SIGKILL peut laisser un fichier .tmp brut qui ne sera jamais purgé

ImportArchive.write écrit d'abord les octets dans originals/.<uuid>.tmp, puis fait os.replace. Son finally ne s'exécute pas après SIGKILL. La purge d'archives ne parcourt que *.bin et ne traite pas les .tmp ; le nettoyage de ImportPreviewStore vise un autre répertoire.

Reproduction : python -I /tmp/revue_g3h_scripts/kill_archive_tmp.py. Le processus enfant est tué par SIGKILL au renommage : aucun .bin ni écriture métier ne subsiste, mais un .tmp de mode privé reste présent immédiatement, après redémarrage, puis après vieillissement synthétique de 25 h. Les scénarios SIGKILL après le début de la transaction laissent au contraire un .bin récent pendant la période de grâce, puis la purge le retire après 25 h. Cette période est conforme à la politique documentée pour .bin, mais le .tmp brut n'a pas de politique de rétention ni d'audit.

**Recommandation :** inventorier et nettoyer les temporaires d'archive orphelins au démarrage, avec une règle qui ne puisse pas supprimer un fichier encore en cours d'écriture. Tester l'arrêt après fsync et avant os.replace.

#### M-G3-03 — L'API de contrôle des soldes émet des centimes non sûrs

[accounts.py](/home/nill/Desktop/compte/mescompts/backend/app/api/v1/accounts.py:97) déclare les cents comme int, puis [get_balance_checks](/home/nill/Desktop/compte/mescompts/backend/app/api/v1/accounts.py:255) renvoie check.as_dict() sans neutraliser les nombres hors plage JavaScript. Le plafond par opération ne borne pas les cumuls.

Reproduction : python -I /tmp/revue_g3h_scripts/unsafe_account_calculation.py construit 1 000 opérations synthétiques de -10^13 cents et calcule réellement le contrôle. L'endpoint répond 200 avec recalculated_balance_cents=-10000000000000000 et delta_cents=10000000000000000. Au passage JSON/JavaScript, ces valeurs ne sont pas représentées exactement. Cela contredit m-03 et ADR-030, même si aucun float Python n'est utilisé.

**Recommandation :** appliquer la même politique de bornage que le résumé de transactions à tous les champs cents sortant vers le frontend, avec un indicateur contractuel de dépassement et des tests API.

#### M-G3-04 — Un import validé après snapshot apparaît dans la navigation existante

La requête de [transactions.py](/home/nill/Desktop/compte/mescompts/backend/app/api/v1/transactions.py:171) filtre sur Transaction.imported_at_utc <= snapshot. Cette date est inscrite avant le COMMIT. Un import dont l'insertion est déjà en cours au moment où la première page choisit son snapshot peut devenir visible après la validation SQL, tout en portant une date antérieure au snapshot.

Reproduction : python -I /tmp/revue_g3h_scripts/snapshot_race.py suspend le commit après les insertions et avant COMMIT, récupère la première page puis finalise le commit et demande la page suivante avec l'ancien curseur. Résultat : first_page_count=21, next_page_count_after_commit=22 et new_row_leaked_into_old_snapshot=True. Le test existant importe seulement après le snapshot et ne couvre donc pas ce cas.

**Recommandation :** borner par une propriété de visibilité au commit, ou matérialiser un instantané stable ; ajouter ce scénario entre deux pages.

### MINEURS

#### m-G3-05 — Suppression possible sans événement d'audit

La purge supprime les fichiers avant d'écrire son événement agrégé : [import_archive.py](/home/nill/Desktop/compte/mescompts/backend/app/services/import_archive.py:87). Si l'écriture de l'audit échoue, le fichier est déjà supprimé et aucun événement n'existe.

Reproduction : python -I /tmp/revue_g3h_scripts/purge_audit_failure.py force DatabaseBusyError à l'écriture d'audit. Résultat : orphan_deleted=True, audit_events=0. L'événement prévu ne contient pas de chemin ni de libellé, ce qui est bon pour la vie privée, mais la suppression n'est alors plus traçable.

#### m-G3-06 — L'E2E confond diagnostic résolu et rejet non résolu

Le service conserve une ligne OUT_OF_PERIOD après résolution avec resolution_status=resolved ([import_commit.py](/home/nill/Desktop/compte/mescompts/backend/app/services/import_commit.py:227)). L'endpoint de rejets ne renvoie pas ce statut ([imports.py](/home/nill/Desktop/compte/mescompts/backend/app/api/v1/imports.py:635)) ; le frontend affiche donc le diagnostic comme s'il restait à résoudre. Dans [transactions.spec.ts](/home/nill/Desktop/compte/mescompts/frontend/tests/e2e/transactions.spec.ts:84), l'E2E attend au contraire « Aucun rejet ni diagnostic enregistré » après un lien commité.

Résultat de scripts/test_e2e_import.sh : le premier scénario import/no_op passe ; le second échoue à cette attente à la ligne 88 ; le scénario axe sériel ne s'exécute pas. L'assertion ultérieure qui devait confirmer une seule transaction n'est donc pas atteinte par l'E2E, bien que le test API confirme l'absence de doublon. Le test axe isolé passe.

**Recommandation :** choisir le contrat voulu : exposer et afficher clairement les diagnostics résolus, ou les filtrer de cette liste ; aligner l'E2E, puis le relancer jusqu'à son assertion de non-duplication.

#### m-G3-07 — Contrat de recherche et statut d'erreur du curseur divergents

[TRANSACTIONS_API.md](/home/nill/Desktop/compte/mescompts/docs/TRANSACTIONS_API.md:55) dit que la ponctuation reste littérale, mais la normalisation retire la ponctuation. Le test direct synthétique matches_query('PCTX 100a ax', '100%') retourne vrai et search_label('100%') donne '100'. Par ailleurs, la documentation globale annonce CURSOR_INVALID en HTTP 400 alors que la falsification observée répond HTTP 422 avec le code attendu.

**Recommandation :** documenter la recherche comme une recherche après normalisation, ou changer le comportement sans altérer silencieusement le contrat ; harmoniser le statut HTTP.

#### m-G3-08 — L'interface ne distingue pas le plafond de lignes du plafond en octets

Le service répond 413 TOO_MANY_OPERATIONS après 20 000 opérations, mais [humanError](/home/nill/Desktop/compte/mescompts/frontend/src/pages/Transactions/index.tsx:25) traduit tout 413 en « Le fichier dépasse la taille maximale autorisée. » L'interface ne présente pas le plafond de lignes avant l'envoi. Un utilisateur qui dépasse la limite reçoit donc une raison incorrecte, malgré le contrat documenté.

**Recommandation :** traiter TOO_MANY_OPERATIONS séparément de FILE_TOO_LARGE et afficher le plafond de 20 000 opérations dans le parcours d'import.

## Régressions et vérifications réellement exécutées

### Installation et tests

Dans la copie isolée prescrite : uv sync --all-groups a réussi ; cd frontend && npm ci a réussi (389 paquets installés, aucune vulnérabilité signalée). Un dépôt Git local a été initialisé uniquement dans cette copie afin que les gardes contractuelles disposent d'une racine Git.

- Tests G3 ciblés : uv run pytest -q backend/tests/integration/test_g3_hardening_imports.py backend/tests/integration/test_g3_hardening_accounts.py backend/tests/integration/test_g3_hardening_transactions.py — **20 passed**, 1 avertissement StarletteDeprecationWarning.
- Backend complet : uv run pytest -q -m 'not opt_in_real_data' --tb=no — **360 passed, 8 failed, 4 deselected**, 1 avertissement, 96,34 s. Les échecs sont : test_backups::test_OPS_02_reimport_after_restore_is_no_op_then_known_import_commits (409 ACCOUNT_MAPPING_LOCKED pour le mapping d'un compte déjà reconnu) ; test_backups::test_OPS_02_rejects_corrupt_incoherent_future_or_truncated_backup[missing_triggers] ; tests migrations test_OPS_02_existing_database_is_backed_up_before_upgrade_with_0600_and_integrity, test_MAJ_15_upgrade_failure_message_matches_backup_state, test_T06_downgrade_succeeds_only_with_explicit_verified_backup, test_T06_health_reports_initialization_schema_age_and_revision et test_OPS_02_backup_api_captures_wal_database_consistently ; test_repo_guards::test_SEC_06_code_applicatif_sans_replace_float_ou_division_centimes (float_call/float_annotation dans backups.py). Les changements de sauvegarde, migration et sécurité étaient hors périmètre ; le test de mapping échoue sur une règle de remappage connue déjà présente dans HEAD. Ces échecs restent à traiter par leurs propriétaires avant une validation globale.
- Frontend : npm test -- --run — **6 fichiers, 48 tests passés** ; npm run typecheck — succès ; npm run lint — succès.
- Contrats : make verify-contracts — succès (schéma, 34 fixtures, auto-test 4/4, 46 tickets, 12 gates, 87 IDs de catégories, 81 tests référencés).
- E2E demandé : scripts/test_e2e_import.sh — **échec** au scénario OUT_OF_PERIOD sur le diagnostic après commit ; l'axe sériel n'a pas été exécuté.
- Axe isolé : un test axe en clair et sombre, 375/768/1280 px — **passé**.
- Données privées : scripts/check_no_private_data.sh --with-untracked — **OK**, 288 fichiers inspectés dans la copie.
- Contrôle statique monétaire des services imports/transactions/accounts relus : aucune occurrence de float, Decimal ou division de montant. Aucun test opt-in sur données réelles lancé.

### Performance et stabilité des identités

Le benchmark compare HEAD et le service actuel, avec mêmes versions Python, base temporaire, données et procédure ; seul import_commit.py est remplacé par la version HEAD dans la base de référence. Temps de commit : 2 000 opérations, 2,972 s / 2,452 s ; 8 000, 19,105 s / 13,669 s ; 16 000, 52,050 s / 44,208 s. Le résultat produit le nombre attendu de nouvelles transactions à chaque taille.

Corpus synthétique overlap_A / overlap_B avec opérations identiques et chevauchements : signatures identiques entre HEAD et le correctif, pour A,B,A (committed, committed, no_op ; 34 transactions, 40 sources) et B,A (committed, committed ; 34 transactions, 40 sources). Signature : 768452cfa4ccb65b8819ffe32230e8dbefcb5c0827354c9e3a47bd3f836f911c. L'idempotence et les rangs du corpus ne régressent pas.

### Assertions de tests modifiées

Comparaison du diff des tests existants ciblés :

- test_import_commit_atomic.py : dans le cas d'échec en milieu de commit, trois attentes de quarantaine (une archive, octets exacts, mode 0600) sont remplacées par l'attente qu'aucune archive ne reste. C'est conforme à la nouvelle règle de nettoyage ; les assertions de succès qui vérifient les octets, SHA-256 et modes 0600/0700 sont conservées ailleurs dans le fichier.
- test_imports_preview_api.py : le contrôle générique « 0001 absent de toute la réponse » et les contrôles de sous-chaînes de chaque numéro dans réponse/logs sont remplacés par des scanners qui connaissent les numéros synthétiques complets/fragments et les champs où les UUID ou identifiants de section peuvent légitimement contenir les mêmes chiffres. De nouveaux cas exercent la collision de « 0001 », les champs value/key/account_alias et les journaux. Ces assertions ont été affinées pour éviter un faux positif d'UUID ; je n'ai pas trouvé d'affaiblissement du contrôle de fuite.
- test_quality_imports.py : trois vérifications globales « 0001 absent » sur les réponses de période, soldes et liste sont remplacées par le scanner de numéro synthétique avec chemins d'identifiants explicitement autorisés. Même motif de faux positif ; les nouveaux tests couvrent les endpoints et le numéro fictif entier.
- test_quality_imports.py : l'attente completeness == partial avant le premier import devient not_started, conformément à ADR-030/m-04.
- Le test E2E transactions.spec.ts ne retire pas d'assertion existante ; il ajoute le scénario de lien et les assertions clavier/onglets. Les nouveaux tests de durcissement n'ont pas modifié d'assertions existantes.

Je n'ai relevé aucune assertion existante supprimée pour faire passer une suite en échec. Les substitutions privacy ci-dessus sont plus ciblées ; la sémantique a été corrigée pour not_started selon l'arbitrage.

## Invariants et recommandation

- **INV-02** : respecté dans les services financiers relus : calculs en entiers, aucun float ou Decimal détecté. Le défaut m-03 est une sérialisation d'entier hors plage sûre vers JavaScript, pas un calcul Python en virgule flottante.
- **INV-11** : aucun libellé, numéro, chemin d'archive ou q en clair trouvé dans les réponses, journaux, audits synthétiques ou curseur inspectés. La signature HMAC du filtre est vérifiée et une falsification est rejetée. La rétention brute non maîtrisée des .tmp et la purge sans audit restent des problèmes de confidentialité et de traçabilité.
- Candidats OUT_OF_PERIOD : aucun libellé supplémentaire ni chemin n'est exposé ; la forme de réponse est strictement limitée aux identifiants opaques, rang et date.

**Recommandation finale :** ne pas valider G3. Corriger d'abord la course de purge B-G3-01, purger proprement les .tmp M-G3-02, borner les cents de balance-checks M-G3-03 et rendre le snapshot stable M-G3-04. Aligner diagnostics/E2E, puis relancer la suite E2E entière. Consigner le résultat et les échecs hors périmètre dans les documents de suivi selon l'orchestrateur.

## Retour au format AGENTS.md

Ticket : revue indépendante du durcissement G3 (MAJ-01, MAJ-02, m-01..m-11).

Objectif livré : revue adversariale exécutée ; verdict BLOCK pour G3, avec constats et preuves reproductibles dans ce rapport.

Fichiers changés : docs/revues/REVUE_G3_DURCISSEMENT.md uniquement dans le dépôt réel. Aucun code, test, contrat ou document de suivi n'a été corrigé.

Interfaces modifiées : aucune.

Tests ajoutés : aucun dans le dépôt. Scripts jetables synthétiques dans /tmp/revue_g3h_scripts : purge_commit_race.py, kill_archive_tmp.py, kill_commit.py, committed_archive.py, purge_audit_failure.py, unsafe_account_calculation.py, preview_safe.py, snapshot_race.py, cursor_tamper.py, replay_race.py, benchmark_commit.py, signatures.py. Ils ne sont pas livrables persistants.

Commandes réellement exécutées + résultats : voir « Régressions et vérifications réellement exécutées ». En résumé : 20 tests G3 ciblés passés ; backend complet 360 passés, 8 échoués, 4 deselected ; Vitest 48/48, typecheck et eslint OK ; make verify-contracts OK ; E2E demandé échoué au diagnostic OUT_OF_PERIOD ; axe isolé passé ; check_no_private_data OK. Reproductions MAJ-02 et snapshot exécutées avec résultats dans le rapport.

Invariant vérifié : INV-02 respecté dans le code monétaire relu ; INV-11 respecté pour les réponses, journaux, audits et curseurs inspectés, sous réserve de la rétention des temporaires et de la traçabilité de purge.

Limites / cas non couverts : aucune donnée réelle utilisée ; les scénarios s'appuient sur des CSV, comptes et libellés synthétiques. L'E2E complet s'arrête avant le contrôle final de non-duplication et avant l'axe sériel. Les échecs liés aux sauvegardes, migrations et sécurité étaient hors du périmètre de revue et demandent une analyse de leurs propriétaires.

Risques / sécurité / données privées : défaut bloquant de suppression d'archive de commit actif ; rétention de fichiers .tmp bruts après SIGKILL ; entiers de solde non sûrs ; navigation à snapshot instable ; événement d'audit perdu si son écriture échoue après suppression. Aucun CSV réel, libellé bancaire réel, IBAN ni numéro de compte réel n'a été ouvert ou cité.

Relecture recommandée : propriétaire T12 pour archive/purge et audit ; propriétaire T14/API comptes pour les bornes d'entiers ; propriétaire T15 pour le snapshot, les diagnostics et l'E2E ; orchestrateur pour l'arbitrage de levée du gate G3.
