# Confidentialité et fonctionnement local

Comptes familiaux est conçu pour analyser les relevés sur votre ordinateur. Le
backend écoute par défaut sur `127.0.0.1:9513`. Le serveur de développement du
frontend écoute sur `127.0.0.1:5173`. L'API refuse les en-têtes `Host` inconnus,
les mutations sans origine locale approuvée et les origines CORS qui ne figurent
pas dans sa configuration. Les réponses de l'API ne sont pas mises en cache.

## Données qui restent sur l'ordinateur

Sans activation volontaire du modèle de langage, aucun relevé, libellé,
opération, ventilation, décision, compte ou statistique n'est envoyé à un
service distant. L'application n'ajoute pas d'outil d'analyse d'audience, de
police distante ou de ressource CDN. Le modèle est désactivé par défaut
(`LLM_ENABLED=false`).

Par défaut, les données sont stockées sous `data/` à la racine du projet :

- `data/db/comptes.sqlite` contient les opérations, libellés bancaires bruts,
  comptes et alias, ventilations, décisions, audits et paramètres locaux ;
- `data/originals/` contient les archives de relevés conservées par les imports ;
- `data/import-previews/` contient temporairement les aperçus, purgés après
  30 minutes ;
- `data/backups/` contient les sauvegardes automatiques lorsqu'elles sont
  demandées.

Les opérations, décisions et audits de la base restent jusqu'à suppression
manuelle. Les archives brutes restent tant qu'un import validé les référence ;
les archives orphelines de plus de 24 heures peuvent être purgées au démarrage
du service d'import. Les sauvegardes `.cfbackup` n'ont pas d'expiration liée à
une date : la commande standard garde les dix dernières sauvegardes nommées
automatiquement dans `data/backups/`, tandis que les copies déplacées ou créées
ailleurs doivent être supprimées séparément.

La clé HMAC utilisée pour reconnaître les comptes est conservée dans la base
SQLite. Le chemin de base et le dossier de données peuvent être personnalisés
par la configuration (`DATABASE_URL` et `DATA_DIR`) ; vérifiez ces chemins avant
de supprimer les données. Les opérations, décisions et audits n'expirent pas
automatiquement. Les fichiers CSV que vous avez téléchargés restent également
à l'emplacement choisi par votre navigateur ou votre système.

## Appel du modèle de langage, uniquement sur demande

Pour utiliser les suggestions, activez explicitement le modèle dans la
configuration backend, renseignez sa clé dans l'environnement backend ou dans
le fichier local `.env`, puis demandez une suggestion dans l'application. La
clé ne va jamais dans le frontend. Avant l'appel, l'application affiche
l'aperçu des données caviardées qui seront transmises et demande une
confirmation explicite. Sans cette confirmation, aucun appel sortant n'est
effectué.

Seuls les champs affichés dans cet aperçu sont envoyés pour le petit lot
sélectionné : date, montant signé, identifiant temporaire et libellé caviardé,
avec les suppressions indiquées. Le relevé complet, la base locale, les
identifiants techniques des opérations et les décisions existantes ne sont pas
envoyés. Le fournisseur configuré reçoit ces données et applique sa propre
politique de conservation et d'utilisation ; consultez-la avant d'activer cette
fonction.

## Sauvegardes et exports

Une sauvegarde `.cfbackup` contient une copie cohérente de la base, les archives
de relevés encore référencées et un manifeste. **Elle contient aussi la clé
HMAC** nécessaire à la reconnaissance stable des comptes. Elle n'est pas
chiffrée. Traitez-la comme la base elle-même : gardez-la dans un emplacement
privé, chiffrez les supports externes et supprimez les copies devenues inutiles.
Les exports CSV et JSON contiennent des libellés et sont eux aussi privés.

La procédure, les emplacements par défaut et le contenu des sauvegardes sont
détaillés dans [BACKUP.md](BACKUP.md).

## Tout effacer

Arrêtez d'abord le backend. Depuis la racine du projet, si vous avez conservé
les chemins par défaut, supprimez la base, les relevés archivés, les aperçus et
les sauvegardes :

```sh
rm -rf data
```

Supprimez aussi `.env` si vous souhaitez effacer la clé fournisseur enregistrée
localement. Effacez séparément toute base, tout dossier de données, export,
sauvegarde `.cfbackup`, sauvegarde de sécurité de restauration ou CSV que vous
avez placés ailleurs. Videz les copies dans les sauvegardes système, la
synchronisation de fichiers et les supports amovibles selon leurs outils. La
suppression d'un fichier ne garantit pas son effacement physique sur un SSD ou
dans un historique de sauvegarde.

## Captures, journaux et rapports

N'ajoutez jamais à Git, aux captures d'écran, aux tickets, aux rapports de test
ou aux messages de diagnostic un relevé réel, un libellé bancaire, un numéro de
compte ou de carte, un IBAN, un montant identifiable, un chemin personnel ou une
clé. Les tests automatiques utilisent des données synthétiques. Pour partager
un problème, remplacez ces valeurs par des exemples fictifs et ne transmettez
que le code d'erreur, la commande et les résultats agrégés nécessaires. Les
journaux structurés masquent les secrets, les IBAN et les longues suites de
chiffres ; cette protection ne rend pas sûre la copie volontaire de données
bancaires dans un message de log.

## Réseau local

Le bind non loopback est refusé par défaut. `ALLOW_NON_LOOPBACK=true` permet de
le contourner : ne l'activez que pour un besoin explicite et sur un réseau
contrôlé, car cela peut rendre l'API joignable depuis d'autres machines.
`CORS_ALLOWED_ORIGINS` accepte une liste JSON d'origines HTTP(S) loopback, par
exemple `["http://127.0.0.1:5173"]` ; les origines wildcard et les domaines
externes sont refusés.
