# myteam — gestion de projets par une équipe d'agents IA

Application web (SPA) + backend Python **sans aucune dépendance** (bibliothèque standard uniquement).
Les agents travaillent **côté serveur** : un travail lancé continue même si vous fermez le navigateur.
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
1. **Services IA** : cliquez sur **Configurer la clé** : un lien ouvre la console du fournisseur, vous y créez une clé API et la collez dans l'application, qui la **teste** aussitôt (bouton **Tester**). Le modèle se règle via **Modifier**. L'assistant se rouvre automatiquement si vous recrutez un agent ou lui écrivez alors que son service n'a pas de clé.
2. **Projets** : créez un projet, puis dans *Git & releases* > *Configurer* : URL du dépôt GitHub, branche de base, **token** (le dépôt et le token sont propres au projet).
3. **Équipe** (mode de connexion par agent : **API** automatique, ou **chat manuel** pour utiliser votre abonnement de chat sans payer l'API : l'application prépare le prompt dans un bandeau en haut du projet, vous le collez dans le chatbot, puis collez sa réponse) : recrutez des agents (service IA, rôle, branche, droits). Le premier est l'**agent principal** (modifiable). À l'onboarding, chaque agent reçoit les règles, sa branche est créée si besoin et son fichier `.agents/<Prénom>.md` est poussé.
4. **Dialogue** avec n'importe quel agent, ou prompt global vers l'agent principal dans la *Vue globale* : il peut écrire aux autres agents, créer des tâches, tenir les spécifications à jour.
5. **Modules, tâches, bugs** : une tâche ou un bug peut appartenir à plusieurs modules. **▶ Exécuter** lance une tâche affectée à un agent.
6. **Fichiers du dépôt** : les agents peuvent lister, lire, écrire et supprimer des fichiers **sur leur branche** (jamais sur la branche de base), et vous voyez leurs commits dans *Git & releases*.
7. **Release** (*Git & releases*) : l'agent principal fusionne les branches des agents dans la branche de base et crée le tag.
8. **Coût** : l'onglet *Coût* donne, par agent et pour tout le projet, les appels, les tokens (entrée/sortie) et le coût estimé. Renseignez d'abord le **tarif** (en $ par million de tokens) de chaque service via *Services IA > Modifier* ; sans tarif, seuls les tokens sont comptés. Estimation indicative : la facture du fournisseur fait foi.
9. **Récapitulatif** (fonctionnel/technique) et **Historique** (toutes les discussions) sont accessibles à tout moment.

## Droits des agents et décisions
- **Droits par rôle** (onglet *Rôles*) : pour chaque capacité (sous-tâches, tâches de 1er niveau, modules, bugs, évolution et vérification des bugs, liens aux modules) : ✔ libre, ✋ soumis à votre validation, ou — interdit. Réglages par défaut : sous-tâches libres sous ses propres tâches, tâches de 1er niveau et modules soumis à validation pour le chef de projet et l'architecte, bugs libres pour tous sauf le rédacteur, vérification des bugs pour le QA.
- **Autonomie du projet** (*Vue globale*) : *Prudent* (tout à valider), *Équilibré* (selon les rôles, par défaut), *Autonome* (seules les questions remontent).
- **Décisions** (onglet *Décisions*, avec pastille de notification) : vos validations (vous pouvez corriger le titre et la description avant d'accepter, ou refuser avec un motif) et les **questions** des agents (ambiguïtés, décisions structurantes) avec options et recommandation. Votre réponse leur est transmise ; si la décision est structurante, elle s'ajoute aux instructions partagées.
- **Bugs** : ouvert, en correction, corrigé, vérifié ou rouvert. Un bug crée une tâche « Corrections : module » avec des sous-tâches de correction ; une fois terminées, il passe à « corrigé » et le QA le vérifie.
- **Instructions partagées** (*Vue globale*) : ce que vous dites de durable à l'agent principal y est consigné et transmis aux agents concernés (global, par rôle, par agent ou par module). Les spécifications sont lisibles par tous les agents.
- **Plafond de budget** (onglet *Coût*) : au-delà, les agents en mode API sont mis en pause.

Token GitHub : permissions *Contents* et *Pull requests* en lecture/écriture sur le dépôt concerné.

Chaque agent garde son historique (20 derniers messages) et le renvoie au service IA à chaque tâche. Si le service répond « quota atteint » (HTTP 429), l'agent passe en **suspendu** puis relance la tâche automatiquement après le délai indiqué.

## Tests
```
python -m unittest discover -s tests
```
Aucun réseau ni clé nécessaires (faux GitHub local, IA simulée).

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
Pas de base de données ; les agents lisent/écrivent des fichiers mais n'exécutent pas de code ; un redémarrage du serveur interrompt les travaux en cours (les reprises après quota sont, elles, replanifiées).
