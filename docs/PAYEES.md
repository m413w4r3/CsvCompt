# Payees, alias et mémoire

## Identité prudente

Un payee représente une contrepartie reconnue à partir des signaux structurés de `parse_ca_label`. `derive_payee_key()` ne consulte jamais `search_label` pour construire la clé : cette représentation avec pertes ne sert qu’à présenter des rapprochements à auditer.

La clé canonique est un JSON stable contenant la version, le nom lisible exact, le type CA de l’opération, la source (`card` ou `creditor`) et le rôle détecté (marchand, intermédiaire ou marketplace). Elle préserve la casse, les accents et la ponctuation du nom. Le suffixe de carte, la date, le montant, les références et les lignes brutes ne sont jamais inclus. Le nom est refusé s’il contient un IBAN ou une longue séquence numérique.

Un paiement par carte et un prélèvement sont des signaux distincts. Ainsi, les exemples synthétiques `CARREFOUR BANQUE` comme créancier et `Carrefour` comme enseigne de carte ont des clés différentes. Un candidat exact absent reste non résolu lors de `ensure_payee_for_transaction()` ; une création est une action explicite par `create_payee_for_transaction()`. Un rattachement par clé ou alias exacte produit un événement d’audit avec l’acteur système `system:exact-key`. Les retraits, virements, remboursements et opérations sans enseigne exploitable ne créent pas de marchand.

La clé exacte réduit les regroupements erronés mais ne prouve pas la destination bancaire. Une enseigne CA tronquée peut ne pas produire de clé. Il faut alors rester non résolu ou créer un alias après revue et preuve.

## Marketplaces et intermédiaires

`AMBIGUOUS_MARKETPLACES` dans `backend/app/classification/matcher.py` est la liste configurable des signaux révisables : Amazon, Vinted, AliExpress, Temu, PayPal, eBay, Klarna et Alma. Un payee créé à partir de ces signaux expose `ambiguous=true` dans son modèle de lecture. Cette marque empêche la mémoire de proposer une catégorie unique. Carrefour Banque et les organismes de crédit sont plutôt marqués comme intermédiaires ; leur historique demande aussi une revue.

Un nom de marketplace ne signifie donc pas que deux paniers sont de même nature. Une confirmation humaine isolée ne devient ni une règle, ni une suggestion catégorielle pour les autres opérations de la plateforme (INV-13).

## Alias exacts et contextuels

`add_payee_alias()` n’accepte que `exact` et `with_context`. Le premier rapproche une enseigne dont le texte normalisé strict correspond caractère pour caractère. Le second ajoute au texte exact le type d’opération CA et, facultativement, le signe signé de la transaction. Aucun rapprochement approximatif n’est fait. `ensure_payee_for_transaction()` ne crée jamais de payee et n’attache une opération que sur clé canonique exacte ou alias explicitement approuvé.

Un alias requiert un acteur et au moins une transaction source déjà reliée au payee. Les événements d’audit gardent la preuve (IDs de transaction), l’acteur et leur horodatage ; l’événement d’attachement indique aussi l’alias exact utilisé. Les noms qui coïncident avec un autre payee canonique ou son nom d’affichage sont refusés, y compris pour une clé legacy opaque. Le texte d’alias est limité à un nom court et les chiffres, marqueurs de mandat/référence et IBAN sont refusés. `remove_payee_alias()` supprime la correspondance active en gardant son état antérieur dans l’audit. Les opérations déjà rattachées ne sont pas effacées ni reclassées lors du retrait d’un alias.

`find_ambiguous_aliases()` compare les `search_label` des alias de payees différents. Elle retourne une liste à auditer et ne fusionne ni ne réécrit aucune donnée.

## Aperçu, fusion et annulation

`preview_payee_merge()` renvoie les IDs des transactions et alias touchés, leur nombre et une empreinte des liens courants. `merge_payees()` exige cet aperçu, un acteur et une confirmation explicite ; tout changement de contenu de l’aperçu ou de l’état courant est refusé. Un intermédiaire et un marchand magasin ne peuvent être fusionnés qu’avec une confirmation spécifique. La fusion déplace les liens vers la cible, désactive la source et écrit le snapshot des liens précédents dans un `audit_event` immuable.

`unmerge_payees()` lit ce snapshot et restaure les liens de transaction, alias et l’état actif de la source. Si un lien a été modifié depuis la fusion, l’annulation échoue sans écraser cette modification. Une annulation ajoute son propre événement ; l’historique d’audit reste append-only. Les colonnes `version` avancent lors de la fusion et de l’annulation suivant le verrouillage optimiste de la base. L’égalité avant/après porte donc sur les champs métier et les rattachements, pas sur les numéros de version technique ni sur les événements d’audit ajoutés.

## Mémoire de classification

`history_candidates()` est une lecture seule. Elle examine les allocations actives avec `origin=manual` et `decision=confirmed` de transactions antérieures distinctes dont la clé canonique est exactement celle du payee cible. Les allocations de règles, de LLM et de mémoire ne sont pas des preuves humaines. L’ID de chaque transaction source, la distribution des catégories et la dernière validation accompagnent la proposition.

Une validation isolée est insuffisante. Deux validations cohérentes peuvent donner une proposition `probable`; au moins trois validations cohérentes donnent `exact`. Dès que plusieurs catégories ou flux apparaissent pour ce payee, la réponse porte `ambiguous_history=true`, `confidence=ambigu` et aucune catégorie unique. Une portée `payee_plus_context` filtre sur le type d’opération, le signe et, si demandé, une tolérance entière en centimes. Ce filtre ne contourne pas un historique contradictoire au niveau du payee.

Une ventilation historique multiple n’est jamais retournée comme split applicable. Les marketplaces et intermédiaires répondent `needs_review` sans catégorie. Toute proposition reste une suggestion : elle n’écrit aucune allocation, ne modifie aucune décision confirmée et ne réduit aucun résidu (INV-08). `persist_suggestions()` est explicite, idempotente et n’écrit que dans `classification_suggestions` avec `status=review`.

## Limites du schéma v1

Le schéma référence ne contient pas de colonne `ambiguous`, de scope `with_context`, de colonnes de preuve d’alias, ni de table de snapshot de fusion. Le service encode le contexte de clé dans `payees.normalized_name`, l’alias exact/contextuel dans `payee_aliases.normalized_alias` (`match_kind=exact` pour les deux, car les deux comparaisons restent exactes), et les preuves/snapshots dans `audit_events` immuables. Les champs de preuve d’une suggestion sont conservés dans `classification_suggestions.reason`; `confidence_label` mappe `exact/probable/ambigu` vers `certain/doute/inconnu`. Cette adaptation garde la référence et les migrations intactes, mais une évolution ultérieure du schéma rendrait ces éléments plus facilement requêtables.

Les tests locaux insèrent seulement des transactions synthétiques dans une base temporaire migrée. Aucun CSV privé n’est nécessaire.
