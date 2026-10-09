# Matrice de validation et de non-régression

Ce document est l'outil de QA pour les **gates G0 à G11**. Les IDs ci-dessous doivent être liés à des tests automatisés lorsque c'est possible ; le statut initial est `À réaliser`. Les jeux réels sont autorisés seulement en local privé via variable d'environnement, jamais dans Git/CI.

| ID | Gate | Scénario | Oracle / condition de succès | Niveau |
|---|---|---|---|---|
| CSV-01 | G2 | Encodage Windows-1252, français, espace insécable | libellé intact, montant exact en centimes | unit |
| CSV-02 | G2 | Champ cité contenant `;` et saut de ligne | une seule transaction, contenu complet | unit |
| CSV-03 | G2 | Plusieurs blocs de comptes / métadonnées d'en-tête | sections distinctes, aucune ligne pseudo-transaction | integration |
| CSV-04 | G2 | Débit, crédit, zéro, signe anormal, date non valide | signes corrects, erreurs par ligne explicites, pas de valeur silencieuse | unit |
| CSV-05 | G2 | Aperçu du fichier | `transactions` inchangées | integration |
| CSV-06 | G2 | CSV 2026 réel fourni localement | 1 024 opérations, dates 01/01/2026–09/10/2026 (si données inchangées) | opt-in local |
| IMP-01 | G3 | Même fichier x2/x3 | 0 transactions nouvelles après le premier commit | integration |
| IMP-02 | G3 | Fichier B chevauche fichier A | nouveaux ajouts seulement, origine A+B traçable | integration |
| IMP-03 | G3 | Deux paiements identiques dans un fichier | deux transactions réelles, pas une | integration |
| IMP-04 | G3 | Un nouveau 3e paiement identique dans B | multiplicité 3 après réconciliation, pas 2/4 | integration |
| IMP-05 | G3 | Import B puis A | même ensemble de transactions et centimes qu'A puis B | metamorphic |
| IMP-06 | G3 | Erreur DB au milieu du commit | rollback : 0 création partielle | integration |
| IMP-07 | G3 | Deux imports simultanés du même export | cardinalité et allocations préservées, aucune violation | concurrency |
| IMP-08 | G3 | Différence de casse dans label banque | identités prudentes, alias marchand possible, pas fusion abusive | unit |
| IMP-09 | G3 | Date ancienne contenue dans nouvel export | import possible, jamais règle `date > max_date` | integration |
| IMP-10 | G3 | Import courant+PEA de référence ancien, séparément et *sans activer 2025* | aucun mélange de comptes ; en base 2026 réelle seulement 2026 | regression opt-in |
| MONEY-01 | G4 | Transaction -100,00 € split 70/30 | allocations -70/-30 ; somme -100 | unit |
| MONEY-02 | G4 | Transaction -100,00 € split 70/29,99 | rejet atomique | unit |
| MONEY-03 | G4 | Un remboursement +20 € apparié à achat -100 € | dépense nette 80 €, flux bancaire -80 € sur la paire | golden |
| MONEY-04 | G4 | Crédit +50 000 € de prêt | cashflow +50k, revenu salarié +0 | golden |
| MONEY-05 | G4 | Remboursement exceptionnel -98 265,61 € | cashflow débité, consommation courante +0 | golden |
| MONEY-06 | G4 | 200 € vers épargne, 50 € de retour | transfert net vers épargne = 150 € ; consommation 0 | golden |
| MONEY-07 | G4 | 250 € allocation personnelle | bancaire -250 ; budget personnel +250 ; coût de vie final non connu | golden |
| MONEY-08 | G4 | Retrait 100 € liquide non justifié | cash en attente, pas ménage automatique | golden |
| MONEY-09 | G4 | Carrefour Banque vs Carrefour magasin | marchands/type de flux distinguables, pas d'autocatégorisation erronée | unit |
| RULE-01 | G4 | Réglage manuel puis nouvelle règle globale | manuel gagne toujours, audit conservé | integration |
| RULE-02 | G4 | Règle « futurs achats Amazon » non approuvée | aucune propagation | integration |
| RULE-03 | G4 | Dry run de règle touchant 140 opérations | compte et exemples visibles avant appliquer ; DB inchangée | integration |
| RULE-04 | G4 | Annuler correction massive | anciennes allocations rétablies, audit présent | integration |
| RULE-05 | G5 | Revue groupée puis correction d'une seule occurrence | les autres occurrences restent inchangées | e2e |
| RULE-06 | G5 | Split avec écran clavier | interaction accessible ; somme exacte avant validation | e2e |
| KPI-01 | G6 | Bruts en centimes | crédit - débit = flux bancaire exact | golden |
| KPI-02 | G6 | 50k de prêt + salaire 3k | revenu régulier=3k, non 53k | golden |
| KPI-03 | G6 | Deux vues avec mêmes filtres | sommes identiques et même jeu de transactions | contract |
| KPI-04 | G6 | Octobre 2026 incomplet | mention « mois partiel » ; exclusion des moyennes de mois pleins | golden |
| KPI-05 | G6 | 2025 non importé, démarrage 2026 | pas d'alerte « manque avril–décembre 2025 » | golden |
| KPI-06 | G6 | 30 % des dépenses non ventilées | avertissement et montant non ventilé explicités | golden |
| KPI-07 | G6 | Requête drilldown catégorie/mois | détail recalculé = total du graphique | contract |
| UI-01 | G7 | Filtre 2026→septembre appliqué depuis cockpit | toutes pages reflètent la même URL et les mêmes comptes | e2e |
| UI-02 | G7 | Clic sur cellule heatmap | ouverture liste filtrée correcte | e2e |
| UI-03 | G7 | Mesures manquantes | message « données insuffisantes », zéro graphique trompeur | component |
| UI-04 | G7 | Responsive/tablette, lecteur d'écran/keyboard | navigation et libellés accessibles | a11y |
| LLM-01..18 | G8 | Voir protocole détaillé | réponses partielles récupérées, données en sécurité | unit/integration |
| BUD-01 | G9 | 3 mois complets + mois en cours | médiane sans mois partiel ; étiquette hypothèse | golden |
| BUD-02 | G9 | Aide CAF non reliée à garde | aide affichée distincte, pas de reste à charge fictif | golden |
| BUD-03 | G9 | Pseudo-abonnement faux positif | utilisateur peut rejeter et l'exclure des alertes | integration |
| INS-01 | G10 | LLM invente « +57 % » avec vrai calcul +12 % | aucune affirmation non reliée et incorrecte publiée comme vérité | integration |
| INS-02 | G10 | Revenus insuffisants / catégories douteuses | limites affichées ou absence de recommandation | integration |
| OPS-01 | G11 | Sauvegarde alors que WAL actif | restauration cohérente et `PRAGMA integrity_check=ok` | integration |
| OPS-02 | G11 | Restore sur base temporaire puis commit | pas de données mixtes ni orphelins ; mêmes KPI | integration |
| SEC-01 | G11 | Serveur démarre par défaut | socket sur 127.0.0.1 uniquement | integration |
| SEC-02 | G11 | Origine externe malveillante fait POST | bloqué ; pas de DB modifiée | security |
| SEC-03 | G11 | Import CSV « `<script>` » dans libellé | texte affiché échappé ; pas JS exécuté | e2e |
| SEC-04 | G11 | Clé LLM dans .env | clé absente du JS, stdout, logs, exceptions et snapshots | security |
| SEC-05 | G11 | Mode LLM off | 0 appel sortant depuis import/dashboard | integration |
| FULL-01 | G11 | Session complète, 2026 import→revue→dashboard→reimport→backup→restore | cardinalité identique, statistiques et décisions inchangées | e2e |

## Jeu « golden » synthétique de base — montants en EUR

| ID | Opération | Classement | Effet attendu |
|---|---|---|---|
| S01 | +3 000,00 salaire A | revenu régulier | +3 000 revenu |
| S02 | +2 500,00 salaire B | revenu régulier | +2 500 revenu |
| S03 | -500,00 courses | dépense alimentation | 500 dépense |
| S04 | -120,00 restaurant | dépense restaurant | 120 dépense |
| S05 | -300,00 garde | dépense enfant | 300 dépense |
| S06 | -200,00 vers épargne | transfert épargne | +200 épargne transférée |
| S07 | +50,00 depuis épargne | retour épargne | -50 épargne transférée |
| S08 | -250,00 argent personnel A | allocation personnelle | 250 enveloppe personnelle |
| S09 | +50 000,00 prêt débloqué | flux emprunt | pas de revenu récurrent |
| S10 | -98 265,61 remboursement prêt | dette/capital non consommé | pas de consommation |
| S11 | +20,00 avoir lié à S03 | remboursement de dépense | courses nettes 480 |
| S12 | -100,00 retrait espèces | en attente espèces | consommation inconnue |

**Oracle avant sélection de catégories :**

- Revenus réguliers = **5 500,00 €**.
- Consommation nette = **900,00 €** (courses 480 + restaurant 120 + garde 300).
- Allocations personnelles = **250,00 €**.
- Épargne transférée nette = **150,00 €**.
- Flux bancaire brut = **−44 165,61 €** (vérifier indépendamment dans les tests via somme de montants).
- La capacité d'épargne opérationnelle indicative (si aucune autre charge retenue) = **4 350,00 €**, avec libellé indiquant les exclusions de prêt/cash et non une variation de solde.

**Nota :** le test doit stocker les opérations synthétiques et recomputer avec la logique métier ; les nombres ci-dessus sont des *oracles indépendants*, ne pas les hardcoder dans le moteur d'agrégation.

## Rapport par gate (format imposé)

```text
Gate : G<n>
SHA/branch :
Tickets vérifiés :
Tests exécutés (commandes exactes) :
Passés / Échoués / Ignorés :
Données réelles utilisées ? [non / oui, local uniquement]
Concordance monétaire à 1 centime :
Preuves : liens fichiers/rapports/captures synthétiques
Bugs et risques restants :
Décision : PASS / BLOCK
Relecteur indépendant :
```

## Tests d'infrastructure et de livrable

| ID | Jalon | Scénario | Oracle | Niveau |
|---|---|---|---|---|
| DOC-01 | G0 | Conventions du dépôt et revue d'agents | tâches reproductibles, `.gitignore` sans données privées | manual |
| API-01 | G1 | `GET /api/v1/health` et erreurs API | code HTTP/contrat cohérent, aucun appel externe | contract |
| UI-00 | G1 | Page shell + santé backend | ouverture sans données, état vide lisible | component |
| CI-01 | G1 | Pipeline depuis environnement neuf | toutes suites synthétiques accessibles via commande unique | integration |
| UI-05 | G3 | Import preview/commit + liste opérations | origine, compte et pagination affichés correctement | e2e |
| RULE-00 | G4 | Taxonomie versionnée | IDs uniques, parents valides, pas de suppressions silencieuses | unit |
| OPS-03 | G11 | Installation depuis README sur machine vierge | API+UI fonctionnels en local, aucun secret demandé hors LLM | manual |
| RELEASE-01 | G11 | Check global, matrice gates, backup + E2E | PASS justifiés ou blocage release explicite | manual |
