# Revue indépendante T17 — précondition G4

Date : 2026-10-10. Relecteur indépendant, distinct de l’auteur de T17. Inspection de l’arbre courant et exécution sur des fixtures synthétiques; aucun CSV réel, base utilisateur ou secret n’a été ouvert.

## Verdict

**PASS pour la revue technique T17; la réserve N-4 est levée.** Je n’ai relevé aucun constat bloquant ni majeur sur les corrections examinées. Les montants restent exacts en centimes entiers, le prorata est borné, les propositions sont contrôlées et les décisions de lien sont versionnées et sérialisées.

Cette revue clôt N-4 tel que relevé dans [REVUE_T17_CONFIRMATION.md](/home/nill/Desktop/compte/mescompts/docs/revues/REVUE_T17_CONFIRMATION.md:86) et actualise la synthèse [RECONCILIATION_T17_T41.md](/home/nill/Desktop/compte/mescompts/docs/revues/RECONCILIATION_T17_T41.md).

Ce verdict ne valide pas le gate G4. La proposition de règle de prorata de [DECISIONS.md](/home/nill/Desktop/compte/mescompts/docs/DECISIONS.md:255) reste à numéroter et à arbitrer par l’orchestrateur; T19 et T20 restent également des préconditions de G4.

## Constats classés

- **BLOQUANT ouvert : aucun.** Les réserves financières examinées ne se reproduisent pas.
- **MAJEUR ouvert : aucun.** Les écritures hors capacité échouent atomiquement; les calculs et audits restent cohérents dans les scénarios limites et concurrents exécutés.
- **MAJEUR — N-1, propositions périmées : corrigé et confirmé.** [allocations.py](/home/nill/Desktop/compte/mescompts/backend/app/services/allocations.py) rejette avec audit les propositions liées à l’instantané d’allocations remplacé. Le test `MONEY_16` couvre réallocation et retrait des deux jambes, ainsi que le rejeu du refus.
- **MAJEUR — N-2, prorata non borné : corrigé et confirmé.** [money_split.py](/home/nill/Desktop/compte/mescompts/backend/app/domain/money_split.py:111) répartit en centimes par plus grands restes, avec redistribution déterministe sous plafonds. [refund_links.py](/home/nill/Desktop/compte/mescompts/backend/app/services/refund_links.py:238) calcule les capacités restantes par catégorie et par allocation. La propriété `MONEY_15` couvre 1 à 8 catégories; les essais jetables incluent égalités de restes, part d’un centime, capacité atteinte et dépassement d’un centime.
- **MINEUR — N-6, capacité restante ignorée : corrigé et confirmé.** [refund_links.py](/home/nill/Desktop/compte/mescompts/backend/app/services/refund_links.py:238) répartit selon les capacités restantes par catégorie et par allocation; les propriétés `MONEY_15`/`MONEY_20` et les scénarios de saturation confirment les plafonds.
- **MINEUR — N-3, doublon actif lors d’un remplacement : corrigé et confirmé.** [replace_refund_link()](/home/nill/Desktop/compte/mescompts/backend/app/services/refund_links.py:721) vérifie le doublon actif avant insertion et les erreurs d’intégrité sont traduites dans le contrat applicatif. La grille `MONEY_17` est comprise dans les tests ciblés.
- **MINEUR — N-4, preuves par mutation SQL manquantes : corrigé et prouvé.** [verify_schema.py](/home/nill/Desktop/compte/mescompts/scripts/verify_schema.py:3690) retire séparément le trigger complet d’insertion des parts, puis les triggers d’attribution INSERT et UPDATE. Chaque mutant est rejeté par son assertion ciblée; le schéma non muté passe le même vérificateur.
- **MINEUR — N-5, transitions SQL : corrigé et confirmé.** Le trigger `trg_refund_links_status_transition` de [0002_t17_refund_safety.sql](/home/nill/Desktop/compte/mescompts/backend/app/db/migrations/sql/0002_t17_refund_safety.sql:309) interdit les transitions qui ne partent pas de `proposed` vers `confirmed` ou `rejected`; la preuve MONEY-19 est incluse dans `verify_schema.py`.

Pour éprouver **spécifiquement** l’attribution INSERT, le contrôle ciblé supprime `trg_refund_links_initial_status` uniquement sur sa connexion SQLite en mémoire. Sans cette isolation, l’interdiction indépendante d’insérer directement un lien confirmé masquerait la mutation de l’attribution. La vérification normale conserve et éprouve cette interdiction sur le schéma intact.

## Revue adversariale exécutée

Le script jetable `/tmp/t17_g4_adversarial.py` utilise exclusivement des valeurs synthétiques et des bases temporaires.

- **Centimes et limites d’entiers :** splits signés testés à `±(2^53−1)`, `±2^53` et `±(2^53+1)`, avec somme exacte, types Python `int`, signe conservé et indépendance de l’ordre d’entrée. Le service complet a créé puis confirmé un remboursement de `2^53+1` centimes; la somme des parts est exacte et les montants bancaires source restent inchangés.
- **Arrondis et plafonds du prorata :** égalités de restes départagées par ID, remboursement d’un centime, redistribution après saturation d’une catégorie, capacité totale atteinte et refus au centime supérieur. Chaque résultat accepté totalise le lien et respecte les capacités.
- **Propositions, idempotence et versions :** dépassement de capacité source et cible refusé sans proposition; création rejouée renvoie le même lien; confirmation rejouée ne change ni version ni audit; refus rejoué ne crée pas un second audit; version périmée rejetée; lien refusé non confirmable.
- **Concurrence :** deux confirmations ciblées concurrentes sur la même capacité donnent une réussite et un conflit de plafond; le net reste non négatif et une seule confirmation est auditée. Une course confirmer/refuser donne un seul gagnant versionné, un conflit de version et un seul audit de décision. Les tests ciblés comprennent aussi les courses de confirmations et de remplacements `MONEY_14`.

## Preuves d’exécution

- `uv run python scripts/verify_schema.py` — **PASS verify_schema**. Les trois nouveaux journaux confirment que le retrait de chaque trigger fait échouer le contrôle attendu : garde INSERT des parts, attribution INSERT, attribution UPDATE.
- `uv run pytest -q backend/tests/unit/domain/test_money_split.py backend/tests/integration/test_allocations_service.py backend/tests/integration/test_refund_links_service.py backend/tests/integration/test_schema_parity.py backend/tests/unit/test_models.py` — **58 passed**, 1 avertissement de dépréciation Starlette/TestClient.
- `uv run pytest -q backend/tests -m 'not opt_in_real_data'` — **426 passed, 4 deselected**, 1 avertissement de dépréciation Starlette/TestClient, en 122,01 s. Les quatre tests exclus portent le marqueur opt-in réel.
- `PYTHONPATH=backend uv run python /tmp/t17_g4_adversarial.py` — **PASS** pour tous les essais limites, propositions, versions et courses décrits ci-dessus.
- `uv run ruff check backend scripts` — **All checks passed**.
- `uv run ruff format --check backend scripts` — **116 files already formatted**.
- `make verify-contracts` — **succès** : vérificateur de schéma et mutations, 34 fixtures, 4 autotests et cohérence du plan (46 tickets, 12 gates, 87 IDs de catégories, 81 références de tests).
- `bash scripts/check_no_private_data.sh` — **OK**, aucune donnée privée détectée parmi 306 fichiers inspectés.

Les premiers essais du seul script temporaire ont révélé des défauts de montage du harnais (date transmise sous forme de chaîne, lecture ORM après fermeture de session et attentes de codes d’erreur trop précises). Ils ont été corrigés dans `/tmp`; l’exécution finale ci-dessus est complète et réussie. Aucun code applicatif ni assertion existante n’a été modifié pour ces corrections.

## Retour au format AGENTS.md

**Ticket :** T17 — revue indépendante de précondition G4.

**Objectif livré :** revue adversariale courte des corrections T17 et clôture de N-4 par trois preuves de mutation distinctes.

**Fichiers changés :** `scripts/verify_schema.py`, `docs/revues/RECONCILIATION_T17_T41.md`, ce rapport `docs/revues/REVUE_T17_G4.md`.

**Interfaces modifiées :** aucune.

**Tests ajoutés :** trois cas de mutation dans `verify_constraint_mutation_proofs`; aucune assertion existante affaiblie ou remplacée.

**Invariant vérifié :** montants signés en centimes entiers; parts positives dont la somme égale le lien et dont chaque montant respecte la capacité; confirmation/refus versionnés, idempotents et sérialisés.

**Limites / cas non couverts :** aucun test sur fichier bancaire réel et aucun endpoint T21; ces parcours sont hors de cette revue. La migration et le service ont été exercés sur bases synthétiques temporaires.

**Risques / sécurité / données privées :** aucune donnée réelle ni aucun secret lu ou écrit. Le contrôle de confidentialité a réussi. La proposition ADR sur la règle de prorata reste à arbitrer; ce rapport ne marque pas G4 validé.

**Relecture recommandée :** arbitrage formel de la proposition ADR par l’orchestrateur, puis évaluation G4 après T19 et T20. Aucun commit n’a été réalisé.
