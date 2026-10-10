# Revue indépendante — correctif du faux positif `0001`

**Verdict : ACCEPTÉ SOUS RÉSERVES.** Aucun fichier du correctif n'a été modifié.

## Constats

1. Les huit mutants requis (alias, message, détail, clé, UUID préfixé/suffixé par `0001`, valeur non UUID sous `*_id`, hash tronqué) sont détectés par `_assert_preview_has_no_account_numbers`.
2. Contournement confirmé : `{"account_alias_id": "00000001-0000-4000-8000-000000000001"}` passe le scanner malgré `0001`. `_is_technical_identifier_key` accepte toute clé finissant en `_id`/`_ids`, puis l'UUID canonique est masqué. Les modèles `PreviewResponse` et `SectionPreview` utilisent `extra="allow"`, et warnings/rejections sont des dictionnaires ouverts. Restreindre les exemptions aux champs et types réellement contractuels (notamment `preview_id`, `file_sha256`, `import_file_id`, `account_id`) éviterait ce faux négatif.
3. Regex : UUID complet minuscule avec tirets et digests hex complets de tailles usuelles (32/40/64/96/128), via `fullmatch`; pas d'acceptation des concaténations ni des hashes tronqués. Le remplacement du texte brut ne masque que les identifiants complets collectés. Les deux sens d'adjacence UUID + `0001` sont détectés.
4. Champs aléatoires légitimes visibles dans un aperçu : `preview_id` (UUID de `ImportPreviewStore.create`), `file_sha256` (SHA-256 du fichier), `import_file_id` si fichier déjà importé, et `account_id` dans les sections et leur `account_mapping` pour les comptes reconnus. Les empreintes de compte sont conservées sous `_fingerprints` et retirées par `public_preview`. Les indices de section/ligne sont des entiers; les dates et horodatages ne sont pas des identifiants aléatoires. `file_sha256` est donc un second candidat réel au faux positif, couvert par la clé contenant `hash`.
5. Trois assertions hors du fichier modifié restent exposées au même faux positif UUID : deux contrôles `"0001" not in ...text` dans `test_ADR_024_imported_provisional_day_requires_overlap` (réponses coverage), et un dans `test_IMP_10_multi_account_coverage_and_account_list_keep_current_and_pea_separate` (liste de comptes), dans `backend/tests/integration/test_quality_imports.py`. Les réponses incluent des `account_id` UUID. Aucun autre contrôle `not in response/caplog` trouvé ne compare `0001` à une réponse/log.

## Preuves d'exécution

- `uv run pytest -q backend/tests/integration/test_imports_preview_api.py` : **28 passed**, 1 avertissement Starlette/httpx.
- Test `test_all_adversarial_synthetic_fixtures_return_preview_without_500` : **20 exécutions, 20 PASS**.
- `uv run ruff check backend/tests/integration/test_imports_preview_api.py` : **All checks passed**.
- `uv run ruff format --check backend/tests/integration/test_imports_preview_api.py` : **1 file already formatted**.
- Mutants temporaires sous `/tmp/revue_fix0001/`; les huit mutants requis détectés; mutant UUID canonique sous `account_alias_id` non détecté.

## Limites et risque résiduel

La cause du test intermittent est plausible et correctement traitée pour `preview_id`, et le test déterministe force bien la collision. La réserve sur les clés génériques empêche d'accepter le scanner comme garde confidentialité robuste en présence de nouveaux champs extra. Les trois assertions de `test_quality_imports.py` n'ont pas été exécutées ici et demeurent probabilistes.

**Invariant revu :** INV-11, protection des numéros de compte, alias, réponse et logs. Aucun CSV réel, `data/`, `csv_references/` ou `tmp.zip` consulté.
