# Règles de classification — contrat T19–T20

Les règles sont des propositions déterministes, bornées et réversibles. Leur
stockage utilise les colonnes v1 existantes de `rules` : `scope_json` contient
les prédicats, la portée d'application, les exceptions et la provenance ;
`effect_json` contient les dimensions et, le cas échéant, les ratios en points
de base. `flow_kind` et `category_id` restent les colonnes relationnelles
contrôlées par la taxonomie. Aucune entrée n'est interprétée comme SQL ou code.

## Priorité concrète

Pour une transaction, le moteur ordonne les candidats dans cet ordre :

1. **Décision humaine confirmée ou verrou humain** : allocation active `manual`,
   décision d'acceptation humaine (y compris une décision explicite de vider ou
   rejeter), ou allocation `history`/`llm` acceptée. Cette décision bloque les
   règles et suggestions. L'allocation présente est rendue comme état confirmé.
   Une décision `clear` reste un verrou même si son état `before` contenait une
   allocation d'origine `rule` : ce snapshot décrit ce qui a été retiré, tandis
   que `clear` matérialise le choix humain de revenir à « non ventilée ».
2. **Règle déterministe approuvée** : priorité numérique croissante (0 est la
   plus haute), puis ID de règle pour rendre l'ordre stable. Si plusieurs règles
   de priorité minimale proposent des actions différentes, il n'y a pas de
   gagnant automatique et leurs IDs sont signalés en conflit. Des actions
   identiques au même rang sont équivalentes.
3. **Mémoire exacte** : uniquement une proposition `confidence=exact`, sans
   ventilation historique, marketplace, intermédiaire ni conflit de mémoire.
   Elle reste une proposition à accepter.
4. **Heuristique** : suggestion stockée avec `method=heuristic`.
5. **LLM** : suggestion stockée avec `method=llm`; elle reste subordonnée aux
   candidats précédents.

Une règle approuvée n'est jamais exécutée automatiquement sur les opérations
déjà importées. `selection` exige des IDs choisis à l'application ;
`existing_and_future` exige un appel d'application explicite ; `future_only`
est exclu des opérations existantes. L'évaluation d'une future opération rend
un candidat `rule` pour revue. Le choix v1 est **suggestion non confirmée**,
stockable comme `classification_suggestions(method='rule', status='review')`
par l'appelant ; aucune allocation définitive n'est créée par l'import.

La condition `flow_kind`, quand elle est fournie, exige une suggestion en revue
de ce flux pour la transaction. L'action porte toujours son propre flux, validé
contre la catégorie et le signe par `allocations.allocate()`. Les montants de
condition sont des valeurs absolues en centimes ; débit/crédit est un prédicat
séparé.

## Portée et motifs

Une contrepartie cible uniquement son `payee_id` exact. Un alias n'est éligible
qu'après son rattachement validé au payee par le service existant ; les règles
ne résolvent ni nom libre ni similarité. Les comptes, sens, montants, dates et
flux restent des filtres indépendants. Une règle globale doit aussi avoir une
contrepartie exacte ou un motif de libellé borné.

Le motif n'est pas une expression régulière. Il contient au plus 128 caractères
et quatre jokers `*`; chaque fragment est compilé comme texte littéral et les
fragments doivent apparaître dans l'ordre. Chaque recherche est bornée par la
longueur maximale de libellé de l'import. Les groupes, classes, quantificateurs
ou références arrière ne sont jamais exécutés comme syntaxe regex. Les requêtes
SQL utilisent les paramètres SQLAlchemy.

Amazon, Vinted, PayPal et les autres valeurs de `AMBIGUOUS_MARKETPLACES` sont
ambigus. Une règle globale qui peut cibler une telle contrepartie est refusée
avec `AMBIGUOUS_MARKETPLACE_GLOBAL_SCOPE` si elle n'a pas une justification,
un motif de libellé explicite et une contrainte additionnelle distincte du motif
(compte, direction, montant, date ou flux). La justification est conservée dans
la provenance. Une règle `selection` reste
possible pour les opérations choisies individuellement. L'ambiguïté apparaît
dans la simulation depuis les métadonnées payee ou le libellé correspondant,
y compris quand aucun payee n'est lié ; elle n'est jamais déduite d'une
classification isolée.

## Ratios, exceptions et versions

Les ratios sont des entiers de points de base dont la somme vaut exactement
10000. L'ordre d'allocation suit ADR-007 : poids décroissant puis ID de
catégorie croissant ; chaque part sauf la dernière est arrondie au centime le
plus proche, demi loin de zéro, et la dernière reçoit le reste signé. La
ventilation est transmise à `allocations.allocate()` comme allocation complète.

Créer une règle crée toujours l'état `draft`. Approuver, modifier et désactiver
exigent `expected_version`; chaque changement métier avance la version, met à
jour l'horodatage et ajoute un `audit_event`. Modifier une règle approuvée la
ramène en brouillon. `approved_by` et le journal portent l'auteur et la date.
Une exception « ne pas appliquer à cette transaction » est enregistrée dans le
`scope_json` de la règle, avec auteur, motif et nouvelle version. Elle survit
aux redémarrages et aux relectures ; elle ne modifie pas la transaction.
Le nombre d'exceptions est plafonné à 10 000 par règle ; dépasser ce plafond
renvoie `RULE_EXCEPTION_LIMIT` sans changer la règle.

Les événements d'audit de règle conservent son ID, sa version et les données
métier nécessaires. Un `label_pattern` n'y apparaît qu'en longueur et empreinte
SHA-256 ; les raisons libres d'exception sont résumées de la même manière. Le
motif et les raisons restent dans `rules.scope_json`. Les `rule_executions`
gardent des snapshots d'allocations limités aux catégories, centimes et
dimensions de revue, sans texte de transaction ni motif.

## Simulation et application

`simulate_rule()` n'appelle que `session_scope()` (SQLite `query_only=ON`). Elle
ne crée ni `rule_execution`, ni `audit_event`, ni horodatage de modification.
Le balayage est plafonné à 10 000 transactions ; le résultat comporte le nombre
scanné, la pagination, les IDs éligibles, les montants signés en centimes, les
totaux par catégorie en centimes et en euros formatés (sans conversion flottante),
les conflits, les lignes protégées par une décision humaine et les payees ambigus.
Les agrégats sont indiqués comme tronqués si le plafond est atteint. Chaque
simulation inclut aussi sa durée de balayage en nanosecondes.

`apply_rule()` exige une règle approuvée, sa version attendue et un acteur. Il
refait la simulation, ignore les décisions humaines, transactions déjà
classées, exceptions et conflits. Pour chaque opération, il ouvre une
transaction d'écriture, revalide sous le verrou SQLite la version et
l'approbation de la règle, puis appelle
`allocations.allocate_in_session()` avec `origin='rule'`, `mode='complete'` et
la version de transaction du précontrôle.
Une transaction explicitement vidée par une personne est rapportée parmi les
opérations ignorées comme décisions humaines, même si la ventilation retirée
avait été appliquée par une règle antérieure.
L'allocation, sa décision/audit et les lignes `rule_executions` et
`audit_event` sont validés dans la même transaction. Les snapshots de trace
contiennent la version de règle. Si la trace échoue, l'allocation est annulée;
un rejeu après succès retrouve l'exécution et ne touche pas l'allocation.

### Limite transactionnelle du lot (ADR-029 acceptée)

Chaque opération traitée et sa trace forment une unité atomique, mais une
application portant sur plusieurs transactions n'est pas un commit unique. Si
une opération tardive échoue, les opérations précédentes restent appliquées et
tracées; un rejeu les saute de manière idempotente et poursuit les autres.
Cette limite conserve le résultat vérifiable et ne laisse pas d'allocation
sans trace, mais ne fournit pas l'annulation atomique du lot.

T20 ajoute `batch_id`, `rule_version`, l'empreinte de portée et le lien vers la
décision produite sur chaque exécution, ainsi que la version transactionnelle
de cette décision. Le code conserve l'atomicité par opération déjà décrite
ci-dessus et rend chaque lot annulable dans une transaction SQLite. Un rejeu
avec le même `batch_id` reprend les opérations non encore exécutées et n'ajoute
pas de trace en double. L'application partielle entre opérations est la
politique métier acceptée par ADR-029 ; l'atomicité reste assurée par opération.
Pour pouvoir rejouer ou annuler un appel qui échoue après un commit d'opération,
l'appelant doit générer puis fournir un `batch_id` stable avant l'appel. Si le
service le génère lui-même, cet ID n'est disponible que dans le résultat d'un
appel réussi et ne peut donc pas servir à retrouver un lot après une exception.
Voir le contrat de revue dans [`docs/REVIEW.md`](REVIEW.md) et la migration
T20 dans ADR-034.

`undo_rule_application()` vérifie les versions attendues, les allocations
actives et les décisions humaines postérieures sous le verrou d'écriture. La
confirmation explicite permet de remplacer une décision humaine postérieure;
sans elle, la demande échoue sans écriture. `undo_decision()` fournit le même
contrôle pour une reventilation manuelle. Les deux services préservent le
montant bancaire, écrivent une nouvelle décision et un événement d'audit, puis
créent de nouvelles lignes d'allocation en centimes exacts. Les lots T19
antérieurs à cette migration sont conservés comme lots singleton, sans deviner
leurs frontières historiques.

Si une règle est modifiée ou désactivée depuis la simulation, sa nouvelle
version est détectée sous le verrou et l’opération échoue sans allocation.
