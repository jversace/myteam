# myteam — gestion de projets par une équipe d'agents IA

Application web (SPA) + backend Python **sans aucune dépendance** (bibliothèque standard uniquement).
- Les données sont persistées dans des fichiers JSON, dans le dossier `data/` (une base de données viendra plus tard).
- Le backend appelle les API des services IA (Claude via l'API Anthropic ; ChatGPT, Mistral, DeepSeek, Gemini via le format compatible OpenAI).

## Installation sur Windows 11

1. **Installer Python 3.10 ou plus** (PowerShell) :
   ```
   winget install -e --id Python.Python.3.12
   ```
   Fermez puis rouvrez PowerShell et vérifiez : `python --version` (ou `py --version`).
2. **Installer Git** (si absent) : `winget install -e --id Git.Git`, puis rouvrez PowerShell.
3. **Récupérer le projet** :
   ```
   git clone https://github.com/jversace/myteam.git
   cd myteam
   ```
4. **Lancer** : double-cliquez sur `start.bat`, ou dans PowerShell : `python server.py` (ou `py server.py`).
   Laissez la fenêtre ouverte. Pour arrêter : Ctrl+C.
5. **Ouvrir** http://127.0.0.1:8000 dans le navigateur et créer un compte (« Créer le compte »).

## Premier usage
1. **Services IA** : modifiez le service voulu et renseignez la **clé API** et le **modèle**.
2. **Projets** : créez un projet.
3. **Équipe** : recrutez un agent (service IA, rôle).
4. **Tâches** : créez une tâche, affectez-la à l'agent, cliquez sur **▶ Exécuter**. La réponse s'affiche sous la tâche.

Chaque agent garde son historique (20 derniers messages) et le renvoie au service IA à chaque tâche. Si le service répond « quota atteint » (HTTP 429), l'agent passe en **suspendu** puis relance la tâche automatiquement après le délai indiqué.

## Données et sécurité
- Tout est dans `data/` (ignoré par git) : comptes (mots de passe hachés PBKDF2), état de chaque utilisateur, **clés API en clair**. Ne partagez jamais ce dossier. Pour sauvegarder, copiez-le.
- Par défaut le serveur n'écoute que sur `127.0.0.1` (votre PC uniquement).

## Configuration (variables d'environnement)
| Variable | Défaut | Rôle |
|---|---|---|
| `APM_PORT` | `8000` | port d'écoute (PowerShell : `$env:APM_PORT=8080`) |
| `APM_HOST` | `127.0.0.1` | `0.0.0.0` pour écouter sur le réseau |
| `APM_DATA` | `./data` | dossier de données |
| `APM_ALLOW_REGISTER` | `1` | `0` pour fermer la création de comptes |

## Déploiement sur serveur Linux (plus tard)
Même commande : `APM_HOST=127.0.0.1 python3 server.py`, derrière un reverse proxy HTTPS (nginx ou Caddy), lancé par une unité systemd. Ne jamais exposer le port en HTTP clair sur Internet.

## Dépannage
- *« python n'est pas reconnu »* : rouvrez PowerShell après l'installation, ou utilisez `py server.py`.
- *Port déjà utilisé* : changez `APM_PORT`.
- *Pare-feu Windows* : inutile tant que l'accès reste local.

## Limites actuelles
Pas de base de données, pas d'intégration git/GitHub réelle (commits, releases et merge requests sont enregistrés dans l'application mais non exécutés sur le dépôt), la reprise automatique des agents ne survit pas à un redémarrage de la page.
