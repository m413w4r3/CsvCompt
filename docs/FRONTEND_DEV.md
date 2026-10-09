# Développement du frontend

## Installation et commandes

Node 24 et npm sont utilisés, avec les versions exactes consignées dans `package-lock.json`.

```sh
cd frontend
npm ci
npm run dev
npm run build
npm run preview
npm run lint
npm run typecheck
npm run test -- --run
npm run test:e2e
```

Vite sert le client sur `127.0.0.1` et relaie `/api` vers `http://127.0.0.1:9513`. En développement, le client utilise l’API versionnée `/api/v1`. Les tests unitaires et de composants ne contactent pas le réseau : `fetch` est simulé. Playwright contient un test smoke ; l’installation d’un navigateur Playwright est requise pour l’exécuter.

## Architecture

- `src/app/` : routeur et fournisseurs applicatifs (TanStack Query).
- `src/pages/` : une destination par vue. Les pages métier restent honnêtement vides jusqu’aux tickets correspondants.
- `src/components/layout/` : shell, navigation et statut de santé local.
- `src/components/ui/` : primitives partagées accessibles ; `charts/ChartFrame` réserve un emplacement décrit par du texte et n’invente aucune série.
- `src/lib/api/` : client `fetch` typé, erreurs `ApiError`, timeout et health. L’API est sous `/api/v1` et les erreurs suivent `{error:{code,message,details}}`.
- `src/lib/money.ts` : conversions de présentation en centimes exacts.
- `src/lib/dates.ts` et `src/lib/filters.ts` : format français des dates ISO et sérialisation stable des filtres communs dans l’URL.

Les filtres partagés suivent `docs/API.md` : période inclusive, comptes, catégories, `flow_kind`, statut, mode `treasury|family_life` et `personal_mode=envelope|observed`. Les paramètres sensibles comme un libellé bancaire ne sont pas copiés dans l’URL.

## Conventions

- L’interface et ses libellés sont en français ; le code et les noms d’API sont en anglais.
- Les montants métier restent des centimes entiers signés. Aucun calcul financier n’est réalisé dans le navigateur. Le formateur accepte `bigint` ou un nombre entier sûr ; au-delà de `Number.MAX_SAFE_INTEGER`, fournir un `bigint`. Les entrées euros sont analysées sans flottant et renvoient des centimes `bigint`.
- Les dates API `YYYY-MM-DD` sont découpées et validées sans interprétation de fuseau implicite.
- Une valeur inconnue ou une absence d’opération reste vide. Pas de chiffre de démonstration, de catégorie déduite, ni de graphique simulé. Les mois partiels ne sont jamais extrapolés.
- La clé LLM et les secrets restent côté backend. Le frontend n’envoie aucune requête à un service tiers et charge les polices système localement.
- Les vues utilisent des titres hiérarchiques, des états ARIA, un lien d’évitement, un focus visible et une navigation clavier. Les animations réduisent leur durée avec `prefers-reduced-motion`.
- Le statut health distingue la connexion, une base à initialiser ou migrer, une base indisponible, une erreur HTTP et le serveur hors ligne. Ces états n’empêchent pas l’utilisation du shell.

## Direction visuelle

Le thème clair et le thème sombre suivent `prefers-color-scheme` au premier chargement ; le choix manuel est mémorisé localement. Les tokens CSS sont centralisés dans `src/styles.css` : palette, surfaces, lignes, espacements, rayons, ombres et états. La navigation utilise un vert profond, les contenus des surfaces aérées et une typographie système. Les montants et dates utilisent les chiffres tabulaires lorsque pertinent.

Les couleurs catégorielles réservées aux futurs graphiques sont `--chart-1` à `--chart-6`. Les composants graphiques devront exposer le titre et une description accessible, conserver les données calculées par l’API et rendre visibles période et couverture. Aucun graphique métier n’est activé par le shell.
