# Taxonomie des flux et catégories

La copie exploitable par l'application est [`config/categories.v1.yaml`](../config/categories.v1.yaml). Elle doit rester byte-identique à la taxonomie source dans `.nimbalyst/plan_implementation_comptes_familiaux/config/categories.v1.yaml`. Le domaine Python de [`backend/app/domain/flow_kinds.py`](../backend/app/domain/flow_kinds.py) encode la matrice `flow_kind` ↔ ID ; son test compare les paires exactes à la table de [`DOMAIN_MODEL.md`](DOMAIN_MODEL.md). Les 87 IDs sont présents et la matrice a 88 paires, car `income_benefits` est admissible pour deux flux selon la cadence observée. `unknown` ne porte aucune catégorie.

## Chargement et synchronisation

`load_taxonomy()` valide la version entière positive, les IDs globalement uniques, les libellés, les relations parent/enfant, les dimensions et l'existence d'au moins un flux admissible pour chaque ID. La valeur `sha256` est l'empreinte des octets YAML source, BOM et fins de ligne compris. Les objets chargés sont gelés.

La synchronisation est exécutée sous `write_transaction()` (`BEGIN IMMEDIATE`) seulement si la base correspond à la révision Alembic courante. Elle ajoute les catégories et paires admissibles manquantes, met à jour les libellés et la version de taxonomie, et journalise chaque ajout ou modification dans `audit_events`. Deux synchronisations identiques ne modifient ni les lignes ni l'audit. Les paires historiques ne sont jamais effacées.

Les IDs sont des clés métier immuables. Une future taxonomie ne doit jamais omettre ou renommer un ID : marquez la catégorie `active: false` dans la nouvelle version, gardez son ID et ses paires dans le fichier, et incrémentez `version`. Elle reste consultable et ses allocations historiques gardent leur référence. L'ajout d'ID demande d'abord une mise à jour coordonnée de la matrice Python, de `DOMAIN_MODEL.md`, du YAML et des tests de parité. Un changement de compatibilité de flux exige un ADR avant toute synchronisation ; le service le refuse en comparant les paires déjà en base.

## Évolution contrôlée

1. Proposer et faire arbitrer un ADR qui décrit l'impact comptable, les anciens IDs et les allocations déjà enregistrées.
2. Augmenter `version`; ne pas réutiliser un ID supprimé et ne pas renommer un ID existant. Garder les IDs retirés avec `active: false`.
3. Mettre à jour ensemble le YAML exploitable et sa source, le domaine, la table de `DOMAIN_MODEL.md`, et les tests de parité. La copie racine est testée byte par byte.
4. Si des données doivent être reclassées, concevoir une migration corrective avant application : conserver les versions antérieures, écrire les événements d'audit avant/après et ne pas réécrire les allocations confirmées. Les migrations SQL sont gérées par un propriétaire unique ; le service de synchronisation n'est pas un mécanisme de migration d'allocations.
5. Exécuter les tests `RULE-00`, les parités YAML/code/DB/document, puis les contrôles du ticket et une revue DOMAIN indépendante avant le gate G4.

## Dimensions transversales

`beneficiary`, `cadence`, `flexibility`, `acquisition`, `decision` et `origin` ne créent pas de nouveau `flow_kind` et ne changent pas la compatibilité catégorie/flux. Elles sont portées par chaque ligne `allocations` (colonnes présentes dans le schéma v1) ; elles ne sont pas des attributs de transaction ou de catégorie. Les valeurs et validations sont définies par les enums de `backend/app/domain/types.py` et les listes `classification_dimensions` du YAML. `income_benefits` peut être récurrent ou autre selon la cadence vérifiée.

`origin` autorise `manual`, `rule`, `history`, `llm` et `unclassified`; `decision` autorise `confirmed`, `review` et `unknown`. Une proposition issue d'heuristique conserve `origin=unclassified` et `decision=review` ; le texte `heuristic` est une méthode de suggestion, pas une origine. Les décisions confirmées restent explicites et auditées. L'API expose les six listes valides avec l'arbre afin que le client ne réplique pas une liste différente.

## API

`GET /api/v1/categories` renvoie l'arbre actif et inactif avec `category_id`, parent, libellé, version, flux admissibles et statut actif, ainsi que la version/empreinte de la taxonomie actuelle et ses dimensions. `taxonomy_version` filtre les lignes par version. La route est en lecture seule ; l'absence de base migrée répond `DATABASE_NOT_READY` sous l'enveloppe API normalisée.
