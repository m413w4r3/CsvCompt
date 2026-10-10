# Normalisation des libellés d'import (T10)

Le module `backend/app/importers/normalize.py` est pur et déterministe. Il reçoit le champ décodé par T09 ; il ne lit aucun fichier, n'écrit aucun journal et ne modifie ni `ParsedRow` ni son interface. Le texte bancaire reste une donnée non fiable : il n'est jamais interprété comme du code ou une instruction.

## Trois représentations distinctes

| Valeur | Règle | Usage |
|---|---|---|
| `raw_label(raw)` | Renvoie exactement l'objet et les caractères reçus. | Conservation et provenance (INV-01). |
| `identity_label(raw)` | NFC, chaque suite Unicode `White_Space` devient une espace ASCII, trim des espaces ASCII. Casse, accents, ponctuation, chiffres, ordre des mots et `Cf` sont conservés. | Identité stricte d'import, conformément au contrat et à ADR-011/019.g. |
| `search_label(raw)` | `casefold`, décomposition NFKD, suppression des marques, ponctuation et contrôles remplacés par des espaces, puis réduction des espaces. | Recherche humaine et détection de paires à auditer seulement. |

`IdentityLabel` et `SearchLabel` sont des sous-classes de `str` : elles se manipulent comme du texte, et exposent aussi `.value` et `.warnings`. `normalize_label` rassemble les trois valeurs sans transformer le brut. Ainsi, `"CAFÉ, Rue"` et `"cafe rue"` ont des identités différentes mais une représentation de recherche identique. Cette égalité floue n'autorise aucune fusion (INV-03).

Les caractères Unicode de catégorie `Cf`, par exemple un espace sans chasse (ZWSP) ou un BOM interne, restent dans `identity_label` et ajoutent l'avertissement fixe `CF_CHARACTER`. Le parseur ne renvoie jamais le texte source dans un message d'erreur : les avertissements sont des codes constants.

La normalisation est bornée à `MAX_LABEL_CHARS = 20_000` caractères. Au-delà, les représentations dérivées utilisent le préfixe borné et portent `LABEL_TRUNCATED`; `raw_label` garde tout le texte d'origine. Le code appelant doit traiter cet avertissement avant toute persistance d'identité. La recherche comme l'extraction de signaux ne traitent que ce même préfixe. Cette borne évite une normalisation sans limite tout en rendant la troncature visible.

## Indices CA prudents

`parse_ca_label` renvoie `LabelParts` avec :

- `operation_type`, égal au premier type reconnu parmi les libellés CA attendus, sinon `"unknown"` ;
- `card_suffix`, seulement si une ligne commence par `X` suivi de quatre chiffres ; aucun autre numéro n'est extrait comme suffixe de carte ;
- `merchant_raw_hint`, texte entre ce préfixe carte et une date `JJ/MM`, en conservant le texte intérieur ; `merchant_display_hint`, texte après le séparateur ` - ` situé après cette date ;
- `creditor_hint` pour un prélèvement, limité au texte avant le premier séparateur ` - ` ; `reference_present` donne uniquement la présence probable d'une référence, jamais sa valeur ;
- des booléens de signaux `is_intermediary_candidate`, `is_cash_withdrawal_candidate`, `is_loan_payment_candidate` et `ambiguous_marketplace` ;
- `merchant_key_candidate`, candidat de comparaison exact préfixé par sa source (`card:` ou `creditor:`), pas un alias ni une clé de fusion.

La constante `INTERMEDIARY_SIGNAL_TOKENS` contrôle les motifs d'intermédiaires/crédit ; `AMBIGUOUS_MARKETPLACE_TOKENS` contrôle les noms dont le rapprochement global serait ambigu (dont `AMAZON` et `VINTED`). Ces listes sont des signaux révisables. Un signal n'est ni une catégorie, ni un `flow_kind`, ni une classification, ni une preuve que deux contreparties sont identiques (INV-13).

Exemple synthétique de distinction :

```text
Prélèvement
CARREFOUR BANQUE - CARREFOUR BANQUE - REF-SYNTHETIQUE
```

porte un signal d'intermédiaire avec un candidat `creditor:...`. À l'inverse :

```text
Paiement par carte
X6020 CARREFOUR VILLE 12/04 - Carrefour
```

porte un candidat de source `card:...` et aucun signal d'intermédiaire Carrefour Banque. Les deux valeurs candidates restent séparées ; le module ne crée pas d'alias. Un paiement `VINTED` ou `AMAZON` porte `ambiguous_marketplace=True`.

## Audit des libellés proches

`near_duplicate_candidates` reçoit des `OperationIdentity` déjà regroupables par compte, date et montant signé. Il retourne les paires de même groupe dont les `search_label` sont égaux mais les `identity_label` différents. C'est une file de contrôle humain : aucune ligne n'est supprimée et aucune identité n'est réécrite.

## Limites et sécurité

Les indices sont volontairement incomplets : formes tronquées, nouvelles enseignes, descriptions atypiques ou caractères hors du motif attendu peuvent ne rien produire (`None` ou `"unknown"`). `reference_present` est un signal lexical, pas une validation de référence. Le parser n'exécute pas HTML, SQL ou texte de prompt ; les chaînes comme `<script>`, des instructions ou des caractères de contrôle restent de simples données. Une entrée non textuelle échoue de façon fermée avec `INVALID_INPUT`, sans exception contenant sa valeur.

Les fonctions de recherche et d'extraction ne journalisent rien. Aucun rapprochement automatique de marchand n'est fait ; les cas proches restent à examiner selon les décisions humaines et les règles explicites de T18.
