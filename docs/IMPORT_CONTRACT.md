# Contrat d'identité et de réconciliation des imports

**Version :** 1 · **Ticket :** T02 · **Gate visé :** G0 · **Références :** PLAN §B/C/D/F, MATRICE IMP-01..17, INV-01..05/08/09/11/14, ADR-017..024.

Ce contrat définit l'identité des sections de compte, l'identité stricte d'une opération et la réconciliation de ses occurrences. Il ne dépend ni de l'ordre des lignes ni d'une date maximale déjà importée. Il ne fusionne jamais des opérations par ressemblance approximative.

## Compte sans numéro bancaire en clair

### Empreinte et association humaine

1. Le lecteur reconnaît le numéro de compte dans le préambule de section (`Compte … n° …;`) en mémoire uniquement. Il ne l'écrit ni dans SQLite, ni dans les fichiers de log, ni dans les erreurs, ni dans les réponses REST, ni dans les captures. Les erreurs identifient la section par son ordinal dans le fichier et par un code.
2. À la première initialisation, le backend génère une clé secrète aléatoire de 256 bits. Il la conserve comme paramètre secret versionné dans `app_settings` (jamais renvoyé par l'API). Il extrait le texte après `n°` et avant le `;` de fin de préambule. Le seul blanc retiré aux bords ou ignoré entre chiffres est l'un de U+0020 SPACE, U+0009 TAB, U+00A0 NBSP, U+202F NNBSP ou U+2009 THIN SPACE. Il parcourt ensuite le token : Unicode `Nd` est converti en son chiffre décimal ASCII ; U+002D HYPHEN-MINUS est ignoré seulement s'il est immédiatement encadré par deux chiffres `Nd`; toute autre ponctuation ou lettre provoque une erreur de section à résoudre, jamais une suppression permissive. Il exige au moins un chiffre et conserve tous les zéros initiaux. Il calcule ensuite `bank_fingerprint = HMAC-SHA-256(clé_locale, "credit-agricole\0" || numéro_canonique_UTF8)` ; le préfixe de domaine empêche la réutilisation accidentelle de la même empreinte à une autre fin.
3. Seule l'empreinte hexadécimale et la version de clé sont enregistrées dans `accounts` et `import_account_sections`. Le numéro reçu ne sert jamais d'alias. L'alias affiché (ex. « courant familial ») est choisi explicitement par l'utilisateur ; l'API n'en propose pas un en recopiant l'en-tête.
4. Pour une section dont l'empreinte n'existe pas, l'aperçu indique « compte inconnu — section N » et propose une association explicite : choisir un compte existant ou créer un compte et saisir son alias/rôle. Aucun commit n'est autorisé tant que chaque section exploitable n'a pas de mapping explicite. L'aperçu ne montre pas le numéro complet ; tout exemple éventuel est masqué sans valeur de retour réversible.
5. Aux imports suivants, une empreinte égale retrouve le même `account_id` et le mapping validé reste stable. Une empreinte inconnue ne se rattache jamais à un compte par alias, libellé ou proximité. Une empreinte correspondant à plusieurs candidats ou à une clé locale perdue bloque le commit et demande une résolution humaine ; aucune nouvelle identité silencieuse n'est créée.
6. Une rotation de clé est une opération explicite, sauvegardée et auditée qui recalcule les empreintes à partir d'une entrée protégée ou d'un remappage humain. Elle ne renumérote ni les `account_id`, ni les transactions. Chaque backup inclut la clé HMAC nécessaire aux empreintes ; l'archive du backup reste locale, ignorée par Git et en mode `0600` (ADR-023).

La clé HMAC n'est pas une donnée de diagnostic. Les journaux ne contiennent ni le numéro, ni la valeur HMAC si elle peut servir de traceur inter-session ; ils peuvent contenir l'`account_id` opaque et l'alias choisi dans l'interface locale. L'API ne renvoie jamais la clé. L'accès à `app_settings` et aux archives est limité au compte local.

### Octets de fichier et archivage

Le SHA-256 d'idempotence est calculé directement sur les octets exacts reçus, avant décodage. L'archive conserve ces mêmes octets bit pour bit, y compris BOM, fins de ligne et encodage, sans chiffrement pour la v1. Les imports `committed` et `rejected` gardent les octets sous un nom opaque et non dérivé du numéro de compte dans `data/originals/<import_file_id>.bin`, avec le chemin relatif enregistré en base. Le répertoire `data/originals/` est ignoré par Git ; le fichier est créé en mode POSIX `0600` (`rw-------`). Le backend vérifie le mode après création/renommage et refuse l'accès si les permissions attendues ne sont pas respectées. L'archive n'est jamais servie par l'API ni mentionnée dans les logs ou erreurs. Les aperçus temporaires sont supprimés à expiration.

Une tentative dont le SHA-256 correspond déjà à un import commité devient immédiatement `no_op`, statut final tracé avec `duplicate_of_import_file_id`, hash, taille et audit. Elle ne conserve aucun octet : la copie d'upload temporaire est supprimée et `archive_relpath`, `archive_storage_mode` et `archive_permissions_octal` restent `NULL`. Le chemin réservé aux archives ne peut jamais désigner un fichier absent pour un `no_op` (n-09/IMP-17).

La protection des octets bruts repose sur le stockage local privé, l'exclusion de Git et les permissions `0600`, pas sur un chiffrement applicatif. Le hachage peut être conservé pour l'idempotence ; il ne permet pas de récupérer les octets. La base et les archives sont des données privées locales et sont exclues de Git, des logs et des artefacts de test.

## Identité d'une opération

### Clé stricte

La clé logique `identity_key` est le tuple ordonné :

```text
(account_id, booked_on, amount_cents signé, identity_label)
```

`amount_cents` est un entier signé : débit négatif, crédit positif. Un débit `-1123` et un crédit `+1123` sont donc deux identités. `flow_kind`, payee, catégorie, statut de revue et numéro de ligne n'entrent jamais dans l'identité : ce sont des données de classification ou de provenance, pas des faits bancaires fiables pour dédoublonner.

Le format Crédit Agricole rejette tout montant nul (`ZERO_AMOUNT`) au preview. Il ne crée ni transaction ni `source_row` pour cette ligne et le fichier entier ne peut pas être commité tant que l’erreur n’est pas corrigée ou le fichier rejeté (ADR-019.i).

`identity_label` est dérivé prudemment du libellé CSV décodé :

1. normalisation Unicode NFC ;
2. remplacement de chaque suite de caractères de la propriété Unicode `White_Space` par une espace ASCII ;
3. suppression des espaces ASCII de début et de fin.

La casse, les accents, la ponctuation, les chiffres et l'ordre des mots sont conservés. Il n'y a ni `casefold`, ni suppression des accents, ni tri de mots, ni translittération, ni suppression de chiffres. Les caractères de catégorie Unicode `Cf` (par exemple U+200B ou un BOM interne U+FEFF) sont préservés et donnent un avertissement visible au preview ; ils ne sont jamais supprimés silencieusement. Cette règle est délibérément prudente : IMP-08 demande que deux libellés de casse différente ne soient pas fusionnés automatiquement. Une différence de casse produit donc deux identités strictes ; un alias de marchand peut rapprocher leur présentation ultérieurement sans fusionner les transactions.

`raw_label` garde le champ d'origine tel que décodé (y compris ponctuation, casse et espaces internes). La clé stockée `identity_key_sha256` est un SHA-256 technique des quatre champs typés et cadrés sans ambiguïté (longueur UTF-8 + octets pour chaque composant, montant en base 10 signé). Les comparaisons d'autorité restent faites sur les quatre colonnes, et l'unicité SQL est `(account_id, booked_on, amount_cents, identity_label, occurrence_rank)` ; un hash n'est jamais l'unique preuve d'égalité.

## Réconciliation à multiplicité

### Règle normative

Pour chaque tuple `identity_key`, le commit compte (après validation du mapping et des résolutions humaines du preview) :

- `n_existing = max(occurrence_rank)` déjà présent pour ce compte et cette identité, ou `0` si aucune occurrence n'existe ; le service vérifie `count(*) = max(occurrence_rank)` avant d'utiliser ce maximum. Un trou de rang est une anomalie bloquante, jamais comblée implicitement ; les rangs validés sont contigus de `1` à `n_existing` ;
- `n_new` : nombre d'occurrences de la même identité dans le fichier courant, tous les rangs comptés avant d'écrire.

Il crée les transactions manquantes pour `n_existing + 1 … max(n_existing, n_new)` et aucune autre. Le résultat est `max(n_existing, n_new)`, jamais `n_existing + n_new`. Chaque occurrence admise du fichier reçoit une `source_row`, liée au rang correspondant ; les résolutions explicites d’une ambiguïté de jour peuvent lier une ligne à un rang existant ou demander le prochain rang supplémentaire. Les lignes identiques non ambiguës sont ordonnées par `logical_row_idx` uniquement pour distribuer leurs provenances ; l'ensemble automatique de transactions et ses rangs ne dépend pas de cet ordre.

Le service calcule le SHA-256 des octets reçus, ouvre une connexion en mode autocommit (`isolation_level=None`), démarre `BEGIN IMMEDIATE`, puis lit le hash sous le verrou d'écriture. Si un import de ce hash est déjà commité, il insère et committe le registre final `no_op` sans transaction métier ni `source_row`, puis efface le preview temporaire. Sinon il valide sous ce même verrou la cohérence des rangs, puis écrit toutes les sections, transactions, `source_rows` et audits dans cette transaction avant de passer l’unique import SQL de `previewed` à `committed`. `UNIQUE(account_id, booked_on, amount_cents, identity_label, occurrence_rank)` est la dernière barrière contre un doublon concurrent.

### Exemples et invariance

| État | `n_existing` | `n_new` | Créations | Résultat |
|---|---:|---:|---:|---:|
| A apporte deux paiements identiques | 0 | 2 | ranks 1 et 2 | 2 |
| B contient trois occurrences identiques | 2 | 3 | rank 3 seulement | 3, IMP-04 |
| Un autre export contient une occurrence | 3 | 1 | aucune | 3 |
| Même export A répété | 2 | même hash déjà commité | aucune ; tentative no-op tracée | 2, IMP-01 |

Pour tout ordre d'import des contenus distincts, le nombre final de transactions d'une identité est le maximum des multiplicités de ces contenus : `max(2, 3) = 3`, que l'on fasse A puis B ou B puis A (IMP-05). Une permutation interne des lignes ne change aucun compte : elle ne change que les `logical_row_idx` des nouvelles `source_rows`. Le hash de fichier rend la répétition exacte idempotente sans modifier allocations ou décisions humaines (INV-08).

### Cas limites obligatoires

- **Exports plus ancien/plus récent couvrant le même jour :** aucune règle `booked_on > max(booked_on)` n'est permise. Chaque clé de chaque ligne est comparée à l'ensemble existant, même si des lignes plus récentes ont déjà été commitées. Une ligne ancienne réapparaissant dans B (IMP-09) est liée à son rang existant ; une occurrence de plus crée le rang manquant.
- **Fenêtres non communes :** les lignes de dates sans correspondance exacte s'ajoutent. La couverture est l'union des intervalles explicitement annoncés par section ; les dates sans transaction à l'intérieur d'un intervalle ne sont pas un trou. On ne déduit aucune suppression d'une opération absente d'un autre export.
- **Jour de génération (IMP-11) :** la date du préambule `Téléchargement du JJ/MM/AAAA` est `generated_on`; si elle manque, `received_on_fallback` utilise la date locale de réception uniquement comme date de repli et traite le dernier jour portant des opérations comme provisoire. Pour un en-tête présent, `complete_through = min(coverage_through, generated_on - 1 jour)`. En repli, `complete_through = min(coverage_through, max(booked_on de la section) - 1 jour)` ; une section sans opération a `complete_through=null` faute de borne fiable. Les opérations du jour provisoire restent dans les sommes. La complétude d'un compte est l'union de tous ses intervalles fermés `[coverage_from, complete_through]` issus des sections commitées : un jour est complet s'il appartient à cette union. Un mois est complet si chaque jour du mois appartient à l'union pour chaque compte sélectionné. Ainsi, un nouvel export commençant le lendemain d'un jour provisoire ne comble pas ce jour manquant. Cette règle explicite d'ADR-024 prévaut sur le passage historique d'ADR-012 concernant la section concernée et les journées entières ; aucun statut de complétude n'est enregistré sur une section.
- **Ligne hors période (IMP-12) :** une ligne dont `booked_on` est hors des bornes déclarées de sa section est marquée `OUT_OF_PERIOD` et exclue du maximum automatique pour sa clé de jour. Elle bloque le commit du fichier entier tant qu'une décision auditée n'est pas prise dans l'aperçu : `link_existing` vers un rang existant ou `create_occurrence` au rang supplémentaire suivant. Elle n'est jamais une simple alerte que l'on pourrait ignorer.
- **Troisième paiement strictement identique dans B :** A a `n=2`, B `n=3` ; rank 3 est créé une fois, même si B est importé deux fois ensuite. Si B n'en contient que deux, aucun rank 3 n'est supprimé (IMP-04, jamais de purge automatique).
- **Clés proches mais différentes :** date, signe/montant, compte ou casse/libellé distincts restent distincts. Une recherche candidate peut proposer à l'audit des identités de même compte/date/montant dont le libellé normalisé est proche ; elle enregistre les deux IDs et le motif, sans fusionner, réécrire l'identité ni supprimer une ligne. Une décision humaine explicite et auditée est requise pour toute correction ultérieure.
- **Plusieurs sections dans le même fichier (IMP-13) :** chaque section doit être explicitement mappée à un compte. Si deux sections du même fichier pointent vers le même `account_id`, le fichier est structurellement rejeté ; on ne calcule jamais `n_new` sur un compte répété. Des sections sur des comptes distincts sont autorisées et restent isolées.
- **Une ligne rejetée ou ambiguë :** elle reste visible par son numéro logique et son code d'anomalie. Une erreur structurelle ou monétaire bloque le commit de tout le fichier ; seuls les avertissements non bloquants peuvent coexister avec un commit complet. Les rejets persistés gardent code/champ/ligne et message rédigé, jamais l'extrait brut. Aucun libellé hostile ne devient une instruction ; aucun échec partiel ne laisse de transactions.

### Atomicité par fichier et statuts

L'unique unité d'atomicité métier est le fichier (ADR-017). Les statuts SQL sont `previewed`, `committed`, `rejected` et `no_op`; `committed`, `rejected` et `no_op` sont terminaux. Le preview REST et ses décisions résident temporairement dans le stockage de preview ; aucune ligne métier n'est écrite avant le commit. Au début du commit, le service crée dans la transaction SQL l'import `previewed`, ses sections et décisions, puis le fait passer en un seul saut à `committed` ou `rejected`. Les sections et les clés de jour ne sont jamais commitées partiellement : si un compte n'est pas mappé, une plage manque, une ligne hors période n'est pas résolue, un rang est ambigu/non contigu, ou le parsing rencontre une erreur bloquante, aucune transaction et aucune `source_row` du fichier n'est écrite. Une anomalie récupérable laisse le preview en attente de résolution humaine ; un fichier irrécupérable est enregistré `rejected`, sans aucune `source_row`. Après résolution, le même aperçu peut être commité en entier. Les décisions/résolutions de `import_rejections` ne sont persistées qu'avec l'import ; elles sont immuables après `committed`. Si le hash est déjà commité, le service jette le preview et insère directement une tentative `no_op` distincte, finale et sans provenance ni archive. Aucun contrat ou résultat d'import n'utilise l'état `partial`.

## Provenance `source_rows` et idempotence de fichier

Une `source_rows` représente une occurrence dans un contenu de fichier commité : elle référence `(import_file_id, section_index, logical_row_idx)`, un `transaction_id`, un hash de ligne et `identity_key_sha256`, sans recopier le numéro bancaire. Un trigger exige que ce dernier soit égal au hash d'identité de la transaction cible, y compris lors de `link_existing` (n-08). Il existe une ligne pour chaque occurrence interprétée de chaque fichier commité. Plusieurs `source_rows` d'exports chevauchants pointent vers une seule transaction (`N → 1`). L'unicité `(import_file_id, section_index, logical_row_idx)` empêche une même ligne logique de provenance d'être inscrite deux fois.

Avant le commit, le service garde aussi le compte attendu de chaque identité de ce fichier : pour chaque clé, `count(source_rows pour import + identity_key_sha256) = n_new`, après résolution humaine des cas ambigus ; au total ce compte égale `parsed_operation_count` (ADR-019.c/IMP-16). Le schéma impose la capacité totale, l'égalité du hash de provenance à la transaction et au moins une provenance par transaction. Une transaction ne devient jamais l'identité d'une ligne différente parce qu'une résolution `link_existing` a choisi son ID.

Le SHA-256 du fichier entier est unique parmi les imports `committed`. Un nouvel essai avec les mêmes octets crée une tentative `no_op` rattachée à l'import commité de même hash et un événement d'audit, mais **0 transaction et 0 `source_row` nouvelle** : le contenu a déjà une représentation complète dans sa première provenance. Un fichier `rejected` ou encore `previewed` ne réserve pas son hash ; après correction/résolution humaine, les mêmes octets peuvent donc être commités. Deux fichiers différents qui contiennent les mêmes opérations gardent chacun leurs `source_rows`.

Les octets bruts sont archivés sans transformation et le fichier est immuable après son renommage atomique ; chemin relatif, mode de stockage `raw` et permissions attendues `0600` sont décrits dans `import_files`. Les champs sources de `transactions` (compte, date, centimes, `raw_label`, identité, rang, date d'import) et toutes les colonnes de `source_rows` sont protégés par les triggers SQL du schéma de référence ; une correction analytique ne les modifie jamais.

## Atomicité, erreurs et imports concurrents

Les octets reçus sont d'abord conservés dans un tampon temporaire privé en mode `0600` pendant le preview. Au commit, le backend démarre une unique transaction SQL `BEGIN IMMEDIATE`, puis lit le hash exact sous verrou avant toute décision d'écriture. Si ce hash est déjà commité, il crée le `no_op` terminal avec `archive_relpath=NULL`, supprime le tampon et ne conserve aucun octet. Sinon, il renomme atomiquement le tampon dans `data/originals/<import_file_id>.bin` et, dans cette transaction SQL, écrit statut/sections, empreintes et couverture, transactions manquantes, `source_rows`, candidats d'audit et événement final avant `previewed → committed`. Un échec SQL provoque `ROLLBACK` complet : aucune transaction, provenance ou allocation partielle n'est publiée (INV-04, IMP-06). Le renommage disque et SQLite ne partagent pas de transaction distribuée ; en cas de rollback après renommage, l'archive orpheline reste privée et sera nettoyée explicitement après vérification. Aucun import `rejected` n'a de `source_row`.

Avec deux demandes concurrentes sur le même hash calculé sur les octets originaux, chaque service exécute `BEGIN IMMEDIATE` avant sa lecture du hash. Le premier écrivain committe ; le second attend le verrou, relit ensuite le hash commité et est tracé `no_op`, sans archive. Si le délai d'attente SQLite expire, il répond par une erreur réessayable, sans écriture partielle. L'index unique partiel des hashes committés protège aussi contre une course résiduelle (IMP-07). Les imports rejetés conservent les octets originaux sous `data/originals/` en mode `0600` et un diagnostic rédigé ; ils n'ont aucune provenance et n'altèrent jamais les imports committés, leurs sources, transactions, allocations ou décisions.

L'archive est écrite atomiquement (fichier temporaire dans le même répertoire, permissions `0600`, puis renommage), avant la transaction DB. Si l'écriture DB échoue, l'archive brute orpheline reste dans `data/originals/` sans référence métier ; un nettoyage explicite et audité peut la retirer après vérification. Aucun code d'erreur ne renvoie le chemin ou le numéro de compte.

## Couverture et contrôle de solde

Chaque section conserve `coverage_from` / `coverage_through` issus de la phrase de période, et éventuellement `complete_through`, `balance_as_of` / `header_balance_cents`. `complete_through` est une borne calculée de la section, pas un statut de solde. Un import de plusieurs comptes garde une plage et un solde séparés pour chaque section et chaque compte. La couverture complète d'un compte est l'union, jour par jour, des intervalles fermés `[coverage_from, complete_through]` des sections commitées ; seule cette union dit si une journée demandée est complète. Une absence de jour dans l'union est un trou. Une exportation ultérieure non chevauchante ne complète pas le jour provisoire absent.

L'absence d'opération un jour ou un mois ne prouve pas une absence de couverture. Un trou ne peut être signalé que lorsqu'une période demandée sort des intervalles couverts ou qu'un intervalle entre deux plages attestées n'est pas couvert. La période antérieure au premier intervalle connu est « non couverte / non démarrée », jamais une anomalie ; en particulier, 2025 non importé ne déclenche pas d'alerte de trou (ADR-003, IMP-10, KPI-05).

Le contrôle de solde est calculé à la lecture à partir de tous les soldes d'en-tête commis du compte, triés par `balance_as_of`, quelle que soit leur arrivée. Un solde n'est comparable que si sa date `balance_as_of <= complete_through` dans sa section source ; les soldes datés après cette borne sont `not_comparable`. Entre deux soldes comparables D0 et D1, la couverture complète doit inclure chaque jour de `(D0,D1]`. Le recalcul exact est `solde(D0) + Σ amount_cents` des transactions uniques du compte telles que `D0 < booked_on ≤ D1`, comparé au solde d'en-tête à D1. `balance_checks` conserve, en append-only et avec version/empreinte des entrées, les résultats de lecture ; `input_snapshot_sha256` est le SHA-256 d'un encodage déterministe cadré par longueur des sections (IDs, couvertures, dates/montants d'en-tête) et transactions (date, cents), chaque ensemble trié par identifiant/champs canoniques. Il change quand les sections, soldes, couverture ou transactions changent, indépendamment de l'ordre d'import, et les anciennes vérifications restent historiques. Aucune colonne `balance_check_status` n'est figée dans `import_account_sections`. Un écart réel donne `mismatch` et rend `month_completeness=unknown` pour la période concernée ; données manquantes ou soldes intrajournaliers donnent `not_comparable`, jamais une égalité inventée. Un écart affiche les centimes et un diagnostic ; il ne corrige ni les transactions ni le solde source.

## Remboursements multi-catégories et supersession

Un lien `refund_of` confirmed référence le crédit source et l'achat cible, son montant positif en centimes et, si une catégorie d'achat précise est visée, `target_allocation_id`. Cette allocation doit être active, appartenir à la transaction cible et être de `flow_kind='expense'`. Si elle est absente, le service répartit le montant entre toutes les allocations expense actives de la cible au prorata de leur valeur absolue ; les centimes sont arrondis selon ADR-007 (poids décroissant, puis `category_id`, demi-loin-de-zéro pour chaque part sauf la dernière, reste signé attribué à la dernière). La somme des montants attribués reste exactement égale au montant du lien.

Les liens actifs d'une transaction source ne dépassent jamais la somme de ses allocations actives `refund`. Les liens dirigés vers une transaction cible ne dépassent jamais la somme des allocations actives `expense` de cette cible ; si `target_allocation_id` est renseigné, le cumul lié à cette allocation est aussi plafonné à sa valeur absolue. Ces plafonds ne se basent ni sur le montant brut du crédit/débit ni sur les seules catégories affichées.

Un lien `confirmed` n'est jamais édité ni supprimé en place. Sa correction requiert une nouvelle version auditée et une FK différée : dans la même transaction SQL, mettre à jour d'abord l'ancien lien (`superseded_by_transaction_link_id`, `superseded_by_audit_event_id`, `version+1`), puis insérer le lien de remplacement et son audit avant `COMMIT`. L'ordre inverse échoue : les anciens et nouveaux montants seraient comptés ensemble contre les plafonds, et l'allocation de remplacement pourrait dépasser la transaction. Cette séquence est également obligatoire pour superséder des allocations confirmées (ADR-019.e, n-11).

## Contrôles liés aux invariants

| Invariant/test | Règle contractuelle |
|---|---|
| INV-01 / IMP-02 | Chaque occurrence acceptée a sa provenance et son archive bit-à-bit identique aux octets reçus, sous `data/originals/` avec permissions `0600` ; les champs bancaires sources restent immuables. Un import `rejected` et un `no_op` n'ont aucune `source_row`; le `no_op` n'archive pas les octets. |
| INV-02 | Conversion décimale du CSV en centimes entiers avant toute opération SQL ; aucun calcul monétaire flottant. |
| INV-03 / IMP-01..05 | Hash de fichier et maximum des multiplicités ; mêmes signes opposés restent distincts. |
| INV-04 / IMP-06/07 | `BEGIN IMMEDIATE`, unicités SQL, rollback total, aucune fusion partielle concurrente. |
| INV-05 / IMP-10 | `account_id` dans chaque identité et chaque requête ; aucune fusion de sections. |
| INV-08 | Réimport = ajout de provenance ou no-op ; jamais d'écrasement de décisions ou allocations confirmées. |
| INV-09 | Dates, couverture, compte, formule et sources des chiffres analytiques restent retraçables. |
| INV-11 | Pas de numéro en clair en base/log/API ; l'original brut d'un import commité ou rejeté est conservé localement sous `data/originals/`, ignoré par Git, en mode `0600`, jamais exposé par l'API ni les logs. Un `no_op` n'a pas d'archive et son chemin est `NULL`. |
| INV-14 | Toute correction, rotation ou action de masse est prévisualisée, sauvegardée, auditée et réversible. |

### Garde-fous relationnels et de connexion (ADR-019)

Chaque connexion SQLAlchemy/SQLite doit poser `PRAGMA foreign_keys=ON` et `PRAGMA recursive_triggers=ON` dans son hook `connect`. Le service d'import ouvre le mode autocommit puis émet `BEGIN IMMEDIATE` avant la lecture d'idempotence. `INSERT OR REPLACE` / `REPLACE INTO` est interdit dans le code applicatif : l'option SQLite remplace par suppression + insertion et peut contourner la traçabilité si les triggers récursifs ne sont pas actifs. Le schéma et le vérificateur testent ce comportement sur chaque connexion ouverte.

Le parseur et le domaine n'acceptent pour les montants que des objets Python où `type(value) is int` ; cela rejette aussi `bool`, `float` intégral et texte décimal avant binding SQL. SQLite vérifie en plus le type entier et le rejet des zéros pour les transactions Crédit Agricole.
