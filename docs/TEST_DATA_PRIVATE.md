# Protocole des tests locaux sur données privées

Ce protocole encadre les tests d'acceptation CSV-06 (G2) et IMP-10. Les CSV réels servent uniquement à une vérification locale opt-in ; ils ne sont pas des fixtures de CI.

## Emplacement et activation

Placer les exports dans `csv_references/` ou `data/private/`, deux emplacements ignorés par Git. Configurer localement `COMPTES_REAL_CSV` avec le chemin du CSV 2026 à contrôler. Ne jamais ajouter le fichier, une base dérivée, une capture d'écran ou un journal contenant une opération au dépôt.

Le profileur indépendant `scripts/real_csv_profile.py` accepte aussi un chemin en argument. Il utilise uniquement la bibliothèque standard, traite les montants en centimes entiers, et affiche des agrégats anonymes : nombres par section, dates extrêmes, sommes en centimes, soldes d'en-tête, doublons stricts, types d'opérations, seuil de montant et cohérence. Il ne montre aucun libellé, identité, numéro, montant d'opération individuel ni chemin d'entrée. L'option `--json` produit les mêmes agrégats en JSON.

```sh
COMPTES_REAL_CSV='/chemin/local/vers/export-2026.csv' python3 -I scripts/real_csv_profile.py --json
```

Le script ne consulte `data/private_expectations.json` qu'avec `--assert-expected`. Ce fichier optionnel contient seulement des agrégats anonymes, est ignoré par Git avec `data/`, et peut être créé explicitement avec `--write-expected`. Ne jamais y ajouter de libellé ou de valeur d'opération individuelle. Sans chemin et sans variable, `--skip-if-unset` affiche « non configuré — test opt-in ignoré » et termine avec le code 0.

Avant G2 sur les données réelles, l'orchestrateur exécute lui-même le profileur local et compare le nombre d'opérations au corpus attendu : **1 024 opérations**, sur la période du **2026-01-01 au 2026-10-09**, dans une section de compte. Toute différence est examinée localement sans copier d'opération ou de libellé dans le rapport. Le profilage est une vérification de contrôle indépendante ; il ne remplace pas le parseur T09.

## Test pytest opt-in

Les tests utilisent le marqueur `opt_in_real_data`, exclu par défaut en CI (voir `docs/CONVENTIONS_TESTS.md`). Voici un squelette à copier dans un test d'intégration lorsque le parseur applicatif sera disponible. Il ne journalise que des agrégats :

```python
import os
from pathlib import Path

import pytest


@pytest.mark.opt_in_real_data
def test_CSV06_real_2026_aggregate_only():
    configured = os.environ.get("COMPTES_REAL_CSV")
    if not configured:
        pytest.skip("COMPTES_REAL_CSV non défini ; test réel opt-in ignoré")

    result = parse_csv(Path(configured))  # remplacer par l'interface du parseur T09
    assert result.operation_count == 1024
    assert result.section_count == 1
    assert result.min_date.isoformat() == "2026-01-01"
    assert result.max_date.isoformat() == "2026-10-09"
```

Ne pas ajouter de `print`, snapshot ou assertion sur un libellé réel. Les seuls résultats partageables sont le statut et des agrégats. Le test IMP-10 reste une régression de compatibilité synthétique sur `ca_2026_multi_sections.csv` : la section PEA est distinguée de celle du compte courant. L'export 2025 de référence ne doit jamais être importé dans la base 2026 par défaut ; une couverture 2025 n'est pas un objectif de reconstruction.

## Confidentialité, rapports et incident

- Une session d'agent hébergée ne doit jamais lire, ouvrir, lister le contenu ni exécuter un outil sur `csv_references/` ou `data/`. L'orchestrateur garde l'exécution réelle sur son environnement local et ne transmet aux agents que des agrégats anonymes.
- Aucune capture d'écran, sortie de commande, trace d'exception, rapport de CI ou message de revue ne doit contenir de libellé, de nom, de numéro de compte/carte/IBAN ou de montant individuel associé à une opération. Les exceptions doivent être génériques et ne jamais inclure le contenu d'une ligne CSV.
- Le rapport de gate indique seulement `données réelles utilisées : oui, local uniquement`, puis les agrégats nécessaires (comptage, dates et sommes totales), sans détail transactionnel.
- Avant toute proposition de commit, exécuter `./scripts/check_no_private_data.sh --with-untracked`. Le contrôle doit être vert ; ne jamais contourner un résultat en retirant le test ou en masquant l'échec.
- Si une fuite est détectée : interrompre le partage, retirer le fichier et les artefacts concernés, purger l'historique Git selon la procédure du dépôt et vérifier les clones/caches publiés. Si un secret ou identifiant d'accès est impliqué, le révoquer et le faire tourner. Signaler l'incident à l'orchestrateur sans recopier les données exposées.

Références : invariants INV-01 (source vérifiable), INV-02 (centimes exacts), INV-11 (confidentialité), CSV-06, IMP-10 et SEC-04.
