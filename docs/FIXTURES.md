# Fixtures CSV synthétiques — ticket T03

Le corpus de `backend/tests/fixtures/` ne lit aucune donnée d'entrée. Il est généré avec la bibliothèque standard Python, une graine fixe et des montants entiers en centimes. Il contient 34 CSV : 9 scénarios principaux, 22 adversariaux et 3 cas de journée provisoire. Les personnes, comptes, références et opérations sont fictifs ; les libellés de marchands explicitement demandés sont des exemples synthétiques. Les fichiers CP1252 sont en LF ; les variantes UTF-8 sont en CRLF.

`manifest.yaml` est un document JSON valide selon le sous-ensemble YAML 1.2. Ce format permet au vérificateur indépendant de rester sans dépendance YAML. Pour chaque section, il donne le compte synthétique, les dates de téléchargement et de période déclarée, le nombre d'opérations, les débits et crédits en centimes, les dates extrêmes et le solde d'en-tête. Il décrit aussi les multiplicités, le statut attendu, et pour les cas adversariaux l'anomalie, sa ligne et le comportement attendu. Le scénario familial porte en plus l'oracle exact des opérations S01–S12 et leurs agrégats en centimes.

## Couverture

| Fixture | Cas de la matrice | Usage |
|---|---|---|
| `ca_2026_simple.csv` | CSV-01, CSV-02, CSV-05, IMP-01, IMP-03 | CP1252, accents, milliers, `;` et retours à la ligne cités, guillemets doublés, crédits et débits opposés le même jour, doublon strict ; entrée d'aperçu et réimport. |
| `ca_2026_utf8_crlf.csv`, `ca_2026_utf8_crlf_bom.csv`, `ca_2026_utf8_crlf_nbsp.csv` | CSV-01, CSV-05 | Même contenu logique en UTF-8 sans BOM, avec BOM, puis avec espace insécable pour les milliers ; fins CRLF, entrée d'aperçu. |
| `ca_2026_multi_sections.csv` | CSV-03, IMP-10 | Sections compte de dépôt et compte espèce PEA, avec soldes distincts ; les métadonnées ne sont pas des transactions. |
| `overlap_A.csv`, `overlap_B.csv`, `overlap_C_reversed_order.csv` | IMP-01, IMP-02, IMP-03, IMP-04, IMP-05, IMP-08, IMP-09 | Occurrences identiques, troisième paiement dans B, groupe de multiplicité quatre, chevauchement de mars, libellé variant par casse, ligne ancienne dans B ; C contient l'union réconciliée par maximum de multiplicité, en ordre mélangé. |
| `provisional_day_A.csv`, `provisional_day_B.csv`, `provisional_day_B_overlap.csv` | IMP-11 | A est téléchargé le jour de fin de période et porte une occurrence de X ; B commence le lendemain et apporte une seconde occurrence hors période, donc bloquante ; B_overlap recouvre le jour et porte les deux occurrences, résultat réconcilié `n=2`. Le mois reste incomplet et le manifeste demande le chevauchement. |
| `adversarial/date_out_of_period_2025.csv`, `adversarial/out_of_period_line.csv` | IMP-12 | Une ligne hors plage crée une ambiguïté bloquante pour sa clé de jour ; le fichier reste non commité tant que la décision n'est pas résolue. La fixture 2025 n'est plus un simple avertissement. |
| `adversarial/same_account_two_sections.csv` | IMP-13 | Même compte dans deux sections du même fichier ; rejet structurel pour éviter de compter une occurrence deux fois. |
| `adversarial/section_without_period.csv` | ADR-017 | Plage absente : import bloqué, statut attendu `previewed` pour résolution dans l'aperçu. |
| `adversarial/invalid_date.csv`, `both_amounts.csv`, `zero_amount.csv`, `negative_amount.csv`, `non_numeric_amount.csv`, `bad_field_count_4.csv`, `bad_field_count_6.csv`, `unclosed_quote_truncated.csv`, `missing_header.csv`, `duplicate_header.csv`, `empty.csv`, `preamble_only.csv`, `interleaved_blank_lines.csv` | CSV-04, CSV-05 | Dates et montants invalides, mauvais nombre de champs, guillemet non fermé, en-tête absent ou dupliqué, fichier vide/préambule seul et lignes vides ; entrées d'aperçu sans écriture métier. |
| `adversarial/script_label.csv` | SEC-03 | Le texte `<script>` doit rester du texte échappé à l'affichage. |
| `adversarial/prompt_injection_label.csv` | LLM-15 | Une instruction hostile dans le libellé reste une donnée ; aucune action externe ni fuite. |
| `adversarial/very_long_label_5100.csv`, `unicode_exotic_emoji_utf8.csv`, `invalid_cp1252_octet.csv` | CSV-01, CSV-04 | Longueur élevée, Unicode/emoji en UTF-8 et octet indéfini en CP1252. |
| `scenario_famille_2026.csv` | CSV-05, IMP-01, golden S01–S12, R-15 | 150 opérations synthétiques de janvier à septembre, marchands variés (Carrefour Banque, Vinted, Amazon, Biocoop et exemples fictifs) et types de libellé cohérents avec débit/crédit. La fenêtre du 03 au 14 janvier conserve S01–S12 et les montants exacts de la matrice. |

CSV-05 décrit le comportement applicatif de l'aperçu, qui sera vérifié au ticket T11 : les fixtures fournissent les entrées, elles ne prouvent pas à elles seules l'absence d'écriture.

## Régénérer et vérifier

À la racine du dépôt :

```sh
python3 -I backend/tests/fixtures/generate_fixtures.py
python3 -I backend/tests/fixtures/verify_fixtures.py
```

Le générateur réécrit les CSV et le manifeste de façon déterministe. Le vérificateur relit les octets, valide encodage/BOM/fins de ligne, analyse indépendamment les lignes avec `csv`, recalcule les sommes en centimes, les dates, soldes et multiplicités, puis contrôle les anomalies annoncées et les garde-fous de confidentialité. `verify_fixtures.py --root <copie>` permet de vérifier une copie isolée et de confirmer qu'une altération diverge du manifeste.
