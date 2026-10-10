# Règles de classification — contrat T19

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
classées, exceptions et conflits, puis appelle `allocations.allocate()` avec
`origin='rule'`, `mode='complete'` et la version de transaction du précontrôle.
Un rejeu ne touche pas une transaction déjà exécutée. Chaque application
allant jusqu'à l'écriture de sa trace reçoit une ligne `rule_executions` et un
`audit_event`; ces deux lignes incluent la version de règle dans leurs
snapshots avant/après. La séparation entre écriture de l'allocation et écriture
de la trace est une limite connue, détaillée ci-dessous.

### Limite transactionnelle à arbitrer (proposition ADR-029)

Le service v1 `allocate(engine, ...)` ouvre et valide sa propre transaction
SQLite pour **une** transaction. T19 ne peut donc pas, sans changer cette API,
inclure un lot de transactions, leurs allocations et leurs `rule_executions` /
`audit_events` dans un unique commit. L'implémentation prévalide le lot, puis
chaque allocation est atomique et optimiste, avec sa trace écrite ensuite dans
une transaction append-only. Une panne ou une concurrence sur un élément tardif
peut laisser les éléments précédents déjà appliqués. T19 ne prétend pas que le
lot est atomique.

**Proposition d'arbitrage** : T20 ajoute un cœur `allocate_in_session(session,
...)` tout en gardant `allocate(engine, ...)` comme façade compatible ; T19/T20
utilisent ce cœur sous un unique `write_transaction()`. Ajouter un identifiant
de lot et `rule_version` dans `rule_executions` avec une migration propre après
gel de `0001` :

```sql
ALTER TABLE rule_executions ADD COLUMN batch_id TEXT;
ALTER TABLE rule_executions ADD COLUMN rule_version INTEGER;
CREATE INDEX ix_rule_executions_batch_id ON rule_executions(batch_id);
```

Cela permet l'atomicité des allocations, audits et exécutions, le rollback
complet et la reprise idempotente du lot. Aucune
signature existante ni migration n'est changée par T19.

Deux fenêtres de panne supplémentaires découlent de cette séparation : si
l'allocation réussit et l'écriture ultérieure de `rule_executions`/`audit_events`
échoue, le rejeu voit une transaction déjà classée et ne répare pas la trace ;
et la relecture de version/statut approuvé n'est pas verrouillée avec
`allocate()`, donc une modification concurrente peut survenir entre ce contrôle
et l'allocation. Le même cœur transactionnel doit verrouiller/revalider la règle
et écrire son allocation et ses traces sous le même commit. Ces fenêtres sont
connues et ne sont pas masquées par les tests actuels.

Cette limite empêche de déclarer satisfait le critère « jamais d'écriture
partielle » pour une application multi-transaction jusqu'à l'arbitrage et au
travail T20.
