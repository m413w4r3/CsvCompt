# Contrat d'identité et de réconciliation des imports

**Version :** 1 · **Ticket :** T02 · **Gate visé :** G0 · **Références :** PLAN §B/C/D/F, MATRICE IMP-01..10, INV-01..05/08/09/11/14.

Ce contrat définit l'identité des sections de compte, l'identité stricte d'une opération et la réconciliation de ses occurrences. Il ne dépend ni de l'ordre des lignes ni d'une date maximale déjà importée. Il ne fusionne jamais des opérations par ressemblance approximative.

## Compte sans numéro bancaire en clair

### Empreinte et association humaine

1. Le lecteur reconnaît le numéro de compte dans le préambule de section (`Compte … n° …;`) en mémoire uniquement. Il ne l'écrit ni dans SQLite, ni dans les fichiers de log, ni dans les erreurs, ni dans les réponses REST, ni dans les captures. Les erreurs identifient la section par son ordinal dans le fichier et par un code.
2. À la première initialisation, le backend génère une clé secrète aléatoire de 256 bits. Il la conserve comme paramètre secret versionné dans `app_settings` (jamais renvoyé par l'API). Il extrait le texte après `n°` et avant le `;` de fin de préambule. Le seul blanc retiré aux bords ou ignoré entre chiffres est l'un de U+0020 SPACE, U+0009 TAB, U+00A0 NBSP, U+202F NNBSP ou U+2009 THIN SPACE. Il parcourt ensuite le token : Unicode `Nd` est converti en son chiffre décimal ASCII ; U+002D HYPHEN-MINUS est ignoré seulement s'il est immédiatement encadré par deux chiffres `Nd`; toute autre ponctuation ou lettre provoque une erreur de section à résoudre, jamais une suppression permissive. Il exige au moins un chiffre et conserve tous les zéros initiaux. Il calcule ensuite `bank_fingerprint = HMAC-SHA-256(clé_locale, "credit-agricole\0" || numéro_canonique_UTF8)` ; le préfixe de domaine empêche la réutilisation accidentelle de la même empreinte à une autre fin.
3. Seule l'empreinte hexadécimale et la version de clé sont enregistrées dans `accounts` et `import_account_sections`. Le numéro reçu ne sert jamais d'alias. L'alias affiché (ex. « courant familial ») est choisi explicitement par l'utilisateur ; l'API n'en propose pas un en recopiant l'en-tête.
4. Pour une section dont l'empreinte n'existe pas, l'aperçu indique « compte inconnu — section N » et propose une association explicite : choisir un compte existant ou créer un compte et saisir son alias/rôle. Aucun commit n'est autorisé tant que chaque section exploitable n'a pas de mapping explicite. L'aperçu ne montre pas le numéro complet ; tout exemple éventuel est masqué sans valeur de retour réversible.
5. Aux imports suivants, une empreinte égale retrouve le même `account_id` et le mapping validé reste stable. Une empreinte inconnue ne se rattache jamais à un compte par alias, libellé ou proximité. Une empreinte correspondant à plusieurs candidats ou à une clé locale perdue bloque le commit et demande une résolution humaine ; aucune nouvelle identité silencieuse n'est créée.
6. Une rotation de clé est une opération explicite, sauvegardée et auditée qui recalcule les empreintes à partir d'une entrée protégée ou d'un remappage humain. Elle ne renumérote ni les `account_id`, ni les transactions. La sauvegarde/restauration doit préserver le secret local, sous chiffrement/protection du backup.

La clé HMAC n'est pas une donnée de diagnostic. Les journaux ne contiennent ni le numéro, ni la valeur HMAC si elle peut servir de traceur inter-session ; ils peuvent contenir l'`account_id` opaque et l'alias choisi dans l'interface locale. L'accès à `app_settings` et aux archives est limité au compte local.

### Octets de fichier et archivage

Le SHA-256 d'idempotence est calculé directement sur les octets exacts reçus, avant décodage. L'archive conserve ces mêmes octets bit pour bit, y compris BOM, fins de ligne et encodage, sans chiffrement pour la v1. Chaque fichier validé ou rejeté est placé sous un nom opaque et non dérivé du numéro de compte dans `data/originals/<import_file_id>.bin`, avec un chemin relatif enregistré en base. Le répertoire `data/originals/` est ignoré par Git ; le fichier est créé en mode POSIX `0600` (`rw-------`). Le backend vérifie le mode après création/renommage et refuse l'accès si les permissions attendues ne sont pas respectées. L'archive n'est jamais servie par l'API ni mentionnée dans les logs ou erreurs. Les aperçus temporaires sont expirés/supprimés ; les octets d'un import commité ou rejeté restent archivés.

La protection des octets bruts repose sur le stockage local privé, l'exclusion de Git et les permissions `0600`, pas sur un chiffrement applicatif. Le hachage peut être conservé pour l'idempotence ; il ne permet pas de récupérer les octets. La base et les archives sont des données privées locales et sont exclues de Git, des logs et des artefacts de test.

## Identité d'une opération

### Clé stricte

La clé logique `identity_key` est le tuple ordonné :

```text
(account_id, booked_on, amount_cents signé, identity_label)
```

`amount_cents` est un entier signé : débit négatif, crédit positif. Un débit `-1123` et un crédit `+1123` sont donc deux identités. `flow_kind`, payee, catégorie, statut de revue et numéro de ligne n'entrent jamais dans l'identité : ce sont des données de classification ou de provenance, pas des faits bancaires fiables pour dédoublonner.

Un montant nul peut être conservé comme opération source si le parseur du format la juge valide ; il reste sans allocation confirmée (par exemple `unknown` ou `reconciliation`) et ne constitue jamais un flux positif/négatif. Une ligne zéro jugée invalide par le format reste une anomalie bloquante au preview, jamais une valeur corrigée silencieusement.

`identity_label` est dérivé prudemment du libellé CSV décodé :

1. normalisation Unicode NFC ;
2. remplacement de chaque suite de caractères Unicode blancs par une espace ASCII ;
3. suppression des espaces de début et de fin.

La casse, les accents, la ponctuation, les chiffres et l'ordre des mots sont conservés. Il n'y a ni `casefold`, ni suppression des accents, ni tri de mots, ni translittération, ni suppression de chiffres. Cette règle est délibérément prudente : IMP-08 demande que deux libellés de casse différente ne soient pas fusionnés automatiquement. Une différence de casse produit donc deux identités strictes ; un alias de marchand peut rapprocher leur présentation ultérieurement sans fusionner les transactions.

`raw_label` garde le champ d'origine tel que décodé (y compris ponctuation, casse et espaces internes). La clé stockée `identity_key_sha256` est un SHA-256 technique des quatre champs typés et cadrés sans ambiguïté (longueur UTF-8 + octets pour chaque composant, montant en base 10 signé). Les comparaisons d'autorité restent faites sur les quatre colonnes, et l'unicité SQL est `(account_id, booked_on, amount_cents, identity_label, occurrence_rank)` ; un hash n'est jamais l'unique preuve d'égalité.

## Réconciliation à multiplicité

### Règle normative

Pour chaque tuple `identity_key`, le commit compte :

- `n_existing` : nombre/rang maximal déjà présent pour ce compte et cette identité ; les rangs validés sont contigus de `1` à `n_existing` ;
- `n_new` : nombre d'occurrences de la même identité dans le fichier courant, tous les rangs comptés avant d'écrire.

Il crée les transactions manquantes pour `n_existing + 1 … max(n_existing, n_new)` et aucune autre. Le résultat est `max(n_existing, n_new)`, jamais `n_existing + n_new`. Chaque occurrence du nouveau fichier reçoit toutefois son `source_row`, liée au rang correspondant `1 … n_new`, y compris lorsque la transaction de ce rang existait déjà. Les lignes équivalentes sont ordonnées par `logical_row_idx` uniquement pour distribuer leurs provenances ; l'ensemble de transactions et ses rangs ne dépendent pas de cet ordre.

Le commit traite tous les groupes d'identité dans une seule transaction SQL `BEGIN IMMEDIATE`. Il relit les rangs sous le verrou d'écriture, puis s'appuie sur `UNIQUE(account_id, booked_on, amount_cents, identity_label, occurrence_rank)` comme dernière barrière contre un doublon concurrent.

### Exemples et invariance

| État | Nombre existant | Occurrences du fichier | Créations | Résultat |
|---|---:|---:|---:|---:|
| A apporte deux paiements identiques | 0 | 2 | ranks 1 et 2 | 2 |
| B contient trois occurrences identiques | 2 | 3 | rank 3 seulement | 3, IMP-04 |
| Un autre export contient une occurrence | 3 | 1 | aucune | 3 |
| Même export A répété | 2 | même hash déjà commité | aucune ; tentative no-op tracée | 2, IMP-01 |

Pour tout ordre d'import des contenus distincts, le nombre final de transactions d'une identité est le maximum des multiplicités de ces contenus : `max(2, 3) = 3`, que l'on fasse A puis B ou B puis A (IMP-05). Une permutation interne des lignes ne change aucun compte : elle ne change que les `logical_row_idx` des nouvelles `source_rows`. Le hash de fichier rend la répétition exacte idempotente sans modifier allocations ou décisions humaines (INV-08).

### Cas limites obligatoires

- **Exports plus ancien/plus récent couvrant le même jour :** aucune règle `booked_on > max(booked_on)` n'est permise. Chaque clé de chaque ligne est comparée à l'ensemble existant, même si des lignes plus récentes ont déjà été commitées. Une ligne ancienne réapparaissant dans B (IMP-09) est liée à son rang existant ; une occurrence de plus crée le rang manquant.
- **Fenêtres non communes :** les lignes de dates sans correspondance exacte s'ajoutent. La couverture est l'union des intervalles explicitement annoncés par section ; les dates sans transaction à l'intérieur d'un intervalle ne sont pas un trou. On ne déduit aucune suppression d'une opération absente d'un autre export.
- **Hypothèse de complétude nécessaire au maximum :** `n_new` n'est comparable à `n_existing` que si la période déclarée du CSV est complète, inclusive et non tronquée pour chaque journée couverte (toutes les opérations bancaires de cette journée sont présentes). Le format fourni exprime des dates, pas des heures ; les bornes du relevé sont traitées comme des journées entières. Si l'aperçu ne peut pas établir cette complétude — fichier tronqué, section sans plage, export explicitement partiel — il marque les clés de jour concernées comme ambiguës et bloque leur commit automatique ; il ne choisit ni de fusionner ni de créer un rang incertain. L'utilisateur fournit un export complet ou tranche via une action explicite et auditée. Sans ID bancaire unique, un export réellement partiel ne permet pas de distinguer sûrement un doublon réexporté d'un nouveau paiement identique.
- **Troisième paiement strictement identique dans B :** A a `n=2`, B `n=3` ; rank 3 est créé une fois, même si B est importé deux fois ensuite. Si B n'en contient que deux, aucun rank 3 n'est supprimé (IMP-04, jamais de purge automatique).
- **Clés proches mais différentes :** date, signe/montant, compte ou casse/libellé distincts restent distincts. Une recherche candidate peut proposer à l'audit des identités de même compte/date/montant dont le libellé normalisé est proche ; elle enregistre les deux IDs et le motif, sans fusionner, réécrire l'identité ni supprimer une ligne. Une décision humaine explicite et auditée est requise pour toute correction ultérieure.
- **Plusieurs sections dans le même fichier :** le compte fait partie de la clé. Deux sections de comptes différents ne se rapprochent pas ; le mapping de section est validé avant calcul des clés.
- **Une ligne rejetée ou ambiguë :** elle reste visible par son numéro logique et son code d'anomalie. Une erreur structurelle ou monétaire bloque le commit de tout le fichier ; seuls les avertissements non bloquants peuvent coexister avec un commit complet. Les rejets persistés gardent code/champ/ligne et message redigé, jamais l'extrait brut. Aucun libellé hostile ne devient une instruction ; aucun échec partiel ne laisse de transactions.

## Provenance `source_rows` et idempotence de fichier

Une `source_rows` représente une occurrence dans un contenu de fichier commité : elle référence `(import_file_id, section_index, logical_row_idx)`, un `transaction_id` et un hash de ligne, sans recopier le numéro bancaire. Il existe une ligne pour chaque occurrence interprétée de chaque fichier commité. Plusieurs `source_rows` d'exports chevauchants pointent vers une seule transaction (`N → 1`). L'unicité `(import_file_id, section_index, logical_row_idx)` empêche une même ligne logique de provenance d'être inscrite deux fois.

Le SHA-256 du fichier entier est unique parmi les imports `committed`. Un nouvel essai avec les mêmes octets crée une tentative `no_op` rattachée à l'import d'origine et un événement d'audit, mais **0 transaction et 0 source_row nouvelle** : le contenu a déjà une représentation complète dans sa première provenance. Deux fichiers différents qui contiennent les mêmes opérations gardent chacun leurs `source_rows`.

Les octets bruts sont archivés sans transformation et le fichier est immuable après son renommage atomique ; chemin relatif, mode de stockage `raw` et permissions attendues `0600` sont décrits dans `import_files`. Les champs sources de `transactions` (compte, date, centimes, `raw_label`, identité, rang, date d'import) et toutes les colonnes de `source_rows` sont protégés par les triggers SQL du schéma de référence ; une correction analytique ne les modifie jamais.

## Atomicité, erreurs et imports concurrents

Le service écrit d'abord les octets exacts dans un fichier temporaire sous `data/originals/`, lui applique le mode `0600`, puis le renomme vers `data/originals/<import_file_id>.bin`. Il exécute ensuite le commit métier sous un unique `BEGIN IMMEDIATE` : statut/sections, empreintes et couverture, transactions manquantes, `source_rows`, candidats d'audit et événement final. Une erreur de parsing bloque le commit avant cette transaction. Une erreur SQL/contrainte interrompt le commit et provoque `ROLLBACK` complet : aucune transaction, provenance ou allocation partielle n'est publiée (INV-04, IMP-06).

Avec deux demandes concurrentes sur le même hash calculé sur les octets originaux, SQLite sérialise les écrivains ; la seconde relit le hash commité et est tracée `no_op`. Si le délai d'attente SQLite expire, elle répond par une erreur réessayable, sans écriture partielle. L'index unique partiel des hashes committés protège aussi contre une course résiduelle (IMP-07). Les tentatives rejetées conservent les octets originaux sous `data/originals/` en mode `0600` et un diagnostic rédigé ; elles n'altèrent jamais les imports committés, leurs sources, transactions, allocations ou décisions.

L'archive est écrite atomiquement (fichier temporaire dans le même répertoire, permissions `0600`, puis renommage), avant la transaction DB. Si l'écriture DB échoue, l'archive brute orpheline reste dans `data/originals/` sans référence métier ; un nettoyage explicite et audité peut la retirer après vérification. Aucun code d'erreur ne renvoie le chemin ou le numéro de compte.

## Couverture et contrôle de solde

Chaque section conserve `coverage_from` / `coverage_through` issus de la phrase de période, et éventuellement `balance_as_of` / `header_balance_cents`. Un import de plusieurs comptes garde une plage et un solde séparés pour chaque section et chaque compte. La couverture d'un compte est l'union des plages des sections commitées de ce compte ; toute vue expose ses bornes et son état.

L'absence d'opération un jour ou un mois ne prouve pas une absence de couverture. Un trou ne peut être signalé que lorsqu'une période demandée sort des intervalles couverts ou qu'un intervalle entre deux plages attestées n'est pas couvert. La période antérieure au premier intervalle connu est « non couverte / non démarrée », jamais une anomalie ; en particulier, 2025 non importé ne déclenche pas d'alerte de trou (ADR-003, IMP-10, KPI-05).

Le contrôle du solde est exact en centimes. Si le compte et deux soldes d'en-tête successifs sont identifiés, que le dernier solde connu est daté `D0`, que le nouveau est daté `D1`, et que la couverture documente sans rupture `(D0, D1]`, le recalcul est `solde(D0) + Σ amount_cents` des transactions uniques du compte telles que `D0 < booked_on ≤ D1`. Il est comparé au solde d'en-tête à `D1`. Sinon `balance_check_status=not_comparable` ou `unknown`, jamais une égalité inventée. Un écart affiche les deux centimes et un diagnostic ; il ne corrige ni les transactions ni le solde source automatiquement.

## Contrôles liés aux invariants

| Invariant/test | Règle contractuelle |
|---|---|
| INV-01 / IMP-02 | Chaque occurrence acceptée a sa provenance et son archive bit-à-bit identique aux octets reçus, sous `data/originals/` avec permissions `0600` ; les champs bancaires sources restent immuables. |
| INV-02 | Conversion décimale du CSV en centimes entiers avant toute opération SQL ; aucun calcul monétaire flottant. |
| INV-03 / IMP-01..05 | Hash de fichier et maximum des multiplicités ; mêmes signes opposés restent distincts. |
| INV-04 / IMP-06/07 | `BEGIN IMMEDIATE`, unicités SQL, rollback total, aucune fusion partielle concurrente. |
| INV-05 / IMP-10 | `account_id` dans chaque identité et chaque requête ; aucune fusion de sections. |
| INV-08 | Réimport = ajout de provenance ou no-op ; jamais d'écrasement de décisions ou allocations confirmées. |
| INV-09 | Dates, couverture, compte, formule et sources des chiffres analytiques restent retraçables. |
| INV-11 | Pas de numéro en clair en base/log/API ; l'original brut est conservé localement sous `data/originals/`, ignoré par Git, en mode `0600`, jamais exposé par l'API ni les logs. |
| INV-14 | Toute correction, rotation ou action de masse est prévisualisée, sauvegardée, auditée et réversible. |
