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
requiert une décision humaine : `link_existing` fournit un identifiant de
transaction existante du même compte, vérifié pendant l'aperçu, revérifié avant
l'archive et une seconde fois sous `BEGIN IMMEDIATE` contre les quatre champs
d'identité (`account_id`, date, montant signé, `identity_label`) ; un index logique partagé entre sections
exige `section_index` ;
`create_occurrence` ajoute explicitement le rang suivant après le maximum
automatique. Une cible incompatible ou déjà utilisée par une autre ligne du
même fichier bloque le commit. Une journée provisoire est conservée dans les
sommes, reste hors de la couverture complète, et n'est pas extrapolée.

## Archive, transaction et quarantaine

Après validation de l'aperçu, l'archive écrit les octets uploadés sans
transformation sous un nom UUID dans `data/originals/`. Le nom d'origine du
fichier n'est jamais conservé. Le répertoire est en `0700`, les fichiers en
`0600`; l'écriture utilise un fichier temporaire, `fsync`, puis renommage
atomique. L'archive est brute, sans chiffrement applicatif, selon ADR-010.
Seul le chemin relatif `data/originals/<import_file_id>.bin` est enregistré dans
SQLite. L'API ne renvoie jamais ce chemin ni les octets.

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
exception avant validation SQL annule l'ensemble des écritures. Comme SQLite et
le système de fichiers ne partagent pas de transaction, l'archive écrite avant
la transaction reste alors orpheline dans `originals/`, sans référence dans la
base. Elle est en quarantaine et n'est supprimée automatiquement ni par
l'endpoint ni par le rollback.

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
  logique, le code, le champ et le message fixe.

Les erreurs suivent l'enveloppe REST standard. Les résolutions manquantes sont
des conflits `409`; les données ou mappings invalides sont `422`; le verrou
SQLite est `503 DATABASE_BUSY`; les collisions d'intégrité sont ramenées à une
erreur `409 IMPORT_CONFLICT`. Les messages ne contiennent ni libellé, numéro,
empreinte HMAC ni chemin disque.

## Limites v1

L'algorithme ne peut pas déduire les occurrences absentes d'une journée
provisoire et ne fusionne jamais par similarité. Une décision explicite est
requise pour une ligne hors période. Le contrôle de solde et la couverture
analytique ne sont pas calculés par T12/T13 ; les seuls champs de solde et de
couverture déclarés dans l'en-tête sont conservés par section pour T14.
L'archive orpheline après rollback nécessite un nettoyage explicite et vérifié.
