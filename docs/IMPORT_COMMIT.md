# Commit et réconciliation des imports (T12/T13)

## Unité de commit

Le service reçoit l'identifiant et la version d'un aperçu résolu. Il relit ses
octets temporaires, vérifie leur SHA-256, reparcourt le CSV et refuse toute
anomalie bloquante, section sans mapping, plage non résolue ou décision de
multiplicité manquante. Les comptes proposés par `new_account` sont créés dans
la transaction de commit. Un conflit de version ou une résolution impossible
ne crée aucune donnée métier.

Une identité stricte est `(account_id, booked_on, amount_cents signé,
identity_label)`. `identity_label` suit T10 ; `raw_label` reste le texte décodé
exact. Les montants liés à SQLite sont des `int` de centimes. Le commit ne crée
aucune allocation : les opérations restent non ventilées et visibles.

Pour chaque identité, les rangs stockés doivent être exactement `1..n`. Une
anomalie `count(occurrence_rank) != max(occurrence_rank)` bloque le fichier.
Pour les lignes ordinaires du fichier, les occurrences sont triées par
`logical_row_idx` pour leur attribuer les rangs `1..n_new`. Le service crée
uniquement les rangs de `n_existing + 1` à `max(n_existing, n_new)`. La ligne
logique ne participe qu'à la provenance, jamais à l'identité.

Exemple synthétique : si A contient deux opérations identiques et B en contient
trois, A crée les rangs 1 et 2 ; B ajoute seulement le rang 3. Les deux fichiers
ont chacun une `source_row` par occurrence. Le résultat est trois transactions
et cinq provenances. Importer B avant A donne le même ensemble de trois rangs.
Un nouvel export qui ne contient qu'un rang ne supprime jamais les rangs déjà
commités.

Les lignes `OUT_OF_PERIOD` ne participent pas au maximum automatique. L'aperçu
requiert une décision humaine et expose, pour chaque ligne concernée, les
candidats de même identité stricte : `transaction_id`, `occurrence_rank` et
`imported_at_utc`, sans libellé. `link_existing` fournit l'identifiant d'une
transaction existante du même compte, vérifié pendant l'aperçu, revérifié avant
l'archive et une seconde fois sous `BEGIN IMMEDIATE` contre les quatre champs
d'identité (`account_id`, date, montant signé, `identity_label`) ; un index
logique partagé entre sections exige `section_index`. `create_occurrence` crée
toujours une nouvelle transaction de rang suivant (`max(n_existing, n_new) + 1`).
Deux lignes du fichier ne peuvent pas lier la même cible. Une journée provisoire
est conservée dans les sommes, reste hors de la couverture complète, et n'est
pas extrapolée.

## Archive, transaction et quarantaine

Après validation de l'aperçu, l'archive écrit les octets uploadés sans
transformation sous un nom UUID dans `data/originals/`. Le nom d'origine du
fichier n'est jamais conservé. Le répertoire `originals/` est en `0700`, les
fichiers en `0600`; l'écriture utilise un fichier temporaire dans son répertoire
frère `originals.staging/` (également `0700`, sur le même système de fichiers),
`fsync`, puis renommage atomique vers `originals/`. Un arrêt pendant l'écriture
ne laisse donc pas de temporaire dans le répertoire validé par la restauration.
L'archive est brute, sans chiffrement applicatif, selon ADR-010. Seul le chemin
relatif `data/originals/<import_file_id>.bin` est enregistré dans SQLite. L'API
ne renvoie jamais ce chemin ni les octets.

Le service utilise ensuite `write_transaction`, qui exécute `BEGIN IMMEDIATE`
sur une connexion SQLite autocommit. Le SHA du fichier est relu sous ce verrou.
S'il existe déjà un import `committed` de même SHA, une tentative distincte
`no_op` et un audit sont enregistrés, sans transaction métier, `source_row` ni
archive conservée. Sinon, import, sections, mappings, transactions manquantes,
provenances, rejets résolus, candidats de libellés proches, audit et réponse
d'idempotence sont écrits dans la même transaction. Le statut passe de
`previewed` à `committed` une seule fois.

Le contrôle final vérifie le nombre total de provenances, leur identité et leur
compte, l'absence de transaction sans source et la continuité des rangs. Toute
exception avant confirmation du `COMMIT` annule l'ensemble des écritures. Si
aucun `import_files` commité ne référence l'archive, elle est supprimée avant
de renvoyer l'erreur. Un arrêt brutal peut laisser un `.bin` non référencé ou un
temporaire ; au démarrage, avant l'acceptation des requêtes, et via
`purge_orphaned_archives`, le backend inventorie puis purge après 24 heures les
archives non référencées nommées exactement `<uuid>.bin`, ainsi que les
temporaires nommés exactement `.<uuid>.tmp` dans `originals.staging/`. Les
temporaires de ce motif laissés par une ancienne version dans `originals/` sont
aussi nettoyés. Tout autre nom, lien symbolique ou répertoire est ignoré. L'âge
est le plus récent de `mtime` et `ctime`, afin qu'un simple réglage de `mtime`
ne rende pas un fichier récent purgeable.

Avant toute écriture de l'archive, le commit prend un verrou advisory `flock`
partagé sur le descripteur du répertoire de données, puis sur
`.archive-lifecycle.lock` (à côté de `originals/`, jamais dedans pour préserver
le contrat de restauration). Le verrou du répertoire garde l'exclusion commit /
purge même si le fichier advisory est supprimé et recréé pendant un commit.
Après acquisition, le service vérifie aussi que le descripteur du fichier
verrouillé et son chemin désignent le même inode et périphérique ; si le fichier
a été remplacé entre l'ouverture et le verrouillage, il ferme et réessaie
jusqu'à trois fois. Le commit garde les deux verrous jusqu'à confirmation du
COMMIT SQLite ou suppression de l'archive non référencée. La purge les prend en
exclusif non bloquant et saute tout l'inventaire si un commit ou une écriture
est actif, y compris dans un autre processus. Le verrou du fichier reste
compatible avec la restauration des sauvegardes. Aucune colonne métier ni
migration n'est requise.

La purge écrit et commit d'abord l'événement d'audit d'intention, avec les
seuls comptages `archive_count` et `temporary_count`, puis supprime les fichiers
candidats. L'échec d'écriture d'audit laisse tous les fichiers en place. Aucun
chemin, libellé ni identifiant de fichier n'apparaît dans l'événement. Les
archives de `no_op` sont supprimées après le commit de la tentative et leur
chemin reste `NULL`.

## Idempotence et concurrence

`Idempotency-Key` est obligatoire. Son enregistrement conserve le chemin HTTP,
la méthode, l'empreinte de la requête et la réponse REST sûre pendant sept jours.
Une répétition avec la même clé et la même requête rejoue la réponse sans
nouveau commit ; la même clé pour un corps ou un chemin différent répond
`409 IDEMPOTENCY_CONFLICT`. Les entrées expirées sont purgées au démarrage et
avant leur réutilisation.

`BEGIN IMMEDIATE` précède toute décision prise à partir de l'état métier. Deux
commits concurrents attendent le verrou puis relisent le SHA et les rangs
existants. Le second import des mêmes octets devient `no_op`; deux contenus
distincts qui se chevauchent recalculent chacun le maximum après le commit du
premier. Les index et triggers du schéma restent la dernière barrière contre un
doublon ou un rang non contigu. Une base occupée répond `503 DATABASE_BUSY`.

## API et erreurs

- `POST /api/v1/imports/{preview_id}/commit` répond `committed` ou `no_op`.
- `GET /api/v1/imports` utilise un curseur keyset signé, lié aux filtres et à
  une borne de snapshot.
- `GET /api/v1/imports/{import_file_id}` expose sections, couvertures, soldes
  source, compteurs, candidats d'audit et diagnostics rédigés.
- `GET /api/v1/imports/{import_file_id}/rejections` n'expose que l'index
  logique, le code, le champ, le message fixe et `resolution_status` (`resolved`,
  `pending` ou `not_required`), sans libellé.

Les erreurs suivent l'enveloppe REST standard. Les résolutions manquantes sont
des conflits `409`; les données ou mappings invalides sont `422`; le verrou
SQLite est `503 DATABASE_BUSY`; une collision d'alias ou d'empreinte constatée
au commit répond respectivement `409 ACCOUNT_ALIAS_CONFLICT` ou `409
ACCOUNT_FINGERPRINT_CONFLICT` et rend l'aperçu non commitable avec son état
recalculé. Les autres collisions d'intégrité sont ramenées à `409
IMPORT_CONFLICT`. Les messages ne contiennent ni libellé, numéro, empreinte
HMAC ni chemin disque.

## Limites v1

L'algorithme ne peut pas déduire les occurrences absentes d'une journée
provisoire et ne fusionne jamais par similarité. Une décision explicite est
requise pour une ligne hors période. Un import est plafonné par défaut à 4 000
lignes d'opérations afin de garder la section critique sous 10 secondes sur la
mesure synthétique documentée : 4 000 lignes prennent 4,97 s sur la machine de
validation. Avant ce plafond, la revue avait mesuré 2 000 lignes à 2,972 s puis
2,452 s, 8 000 à 19,105 s puis 13,669 s, et 16 000 à 52,050 s puis 44,208 s
sur ses deux révisions. Sur cette livraison, les mesures sont 2 000 à 2,205 s,
4 000 à 4,965 s et 8 000 à 13,069 s. La mesure à 8 000 a uniquement servi à
comparer les coûts et a temporairement contourné le plafond dans son harness;
elle ne correspond pas à une taille importable prise en charge.
Les identités et cibles de rapprochement sont chargées par lots, et les
correspondances ligne/cible sont indexées en mémoire. Le contrôle de
solde et la couverture analytique ne sont pas calculés par T12/T13 ; les seuls
champs de solde et de couverture déclarés dans l'en-tête sont conservés par
section pour T14. Les archives orphelines récentes sont purgées après 24 heures
si un arrêt brutal a empêché le nettoyage immédiat.
