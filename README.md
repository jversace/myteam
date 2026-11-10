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
7. **Release** (*Git & releases*) : voir « Release et branches » ci-dessous.
8. **Coût** : l'onglet *Coût* donne, par agent et pour tout le projet, les appels, les tokens (entrée/sortie) et le coût estimé. Renseignez d'abord le **tarif** (en $ par million de tokens) de chaque service via *Services IA > Modifier* ; sans tarif, seuls les tokens sont comptés. Estimation indicative : la facture du fournisseur fait foi.
9. **Récapitulatif** (fonctionnel/technique) et **Historique** (toutes les discussions) sont accessibles à tout moment.

## Release et branches
- **La branche principale n'est utilisée que pour la release finale.** Pour chaque release, cliquez sur **Démarrer une release** (*Git & releases*) : l'application crée une **branche de travail** `release/<version>`.
- Chaque agent travaille sur **sa branche**. Il se **synchronise (pull)** avec la branche de travail au démarrage de chaque tâche et avant sa merge request, puis, quand son travail est prêt, **soumet une merge request** vers la branche de travail. L'**agent principal la valide** (il relit les fichiers et les commits) ou la refuse en expliquant ce qu'il faut corriger.
- **Tâches dépendantes** : une tâche peut dépendre d'autres tâches (bouton *Dépend de…*, ou demandé par l'agent principal). Elle ne démarre que lorsque celles-ci sont terminées **et leurs merge requests fusionnées**.
- **Conflits de fusion** : la règle se choisit dans *Git & releases* > **Conflits de fusion** : *je décide* (une question vous est posée : l'auteur de la branche, l'agent principal, ou vous), *l'auteur de la branche résout*, ou *un agent désigné résout*. Si l'agent n'y arrive pas, la question vous revient. Si vous choisissez de vous en occuper, résolvez-le sur GitHub puis cliquez sur **Réessayer** sur la merge request.
- **Publier la release** : fusionne la branche de travail dans la branche principale, crée le tag et la release GitHub. Impossible tant qu'une merge request est ouverte.

## Débloquer et réinitialiser
- **Les agents n'ont pas d'« environnement » à nettoyer** : un agent, c'est une conversation, des notes, une branche GitHub et des tâches. Tout passe par l'API GitHub, il n'y a pas de `.git` local. Si un agent parle de `MERGE_HEAD` ou d'« environnement bloqué », il se trompe : cliquez sur **Diagnostic des blocages** (onglet *Équipe*) pour voir les causes réelles, relevées dans l'état du projet.
- **Conflit de fusion resté ouvert** (cause fréquente de blocage) : il se lève tout seul si plus aucun fichier n'est en conflit (vérification toutes les 5 minutes, ou au prochain démarrage de tâche). Sinon, dans *Git & releases* > **Conflits de fusion** : **Vérifier**, **Classer résolu**, **Relancer la résolution** ou **Abandonner**.
- **Réinitialiser un agent** (*Équipe* > **Réinitialiser…**) : efface sa conversation (et, au choix, ses notes), annule son travail en cours et ses conflits, et peut **remettre sa branche à zéro** depuis la branche de travail (⚠ ses commits non fusionnés sont perdus), puis rejoue son onboarding.
- **Question sans objet** d'un agent : bouton **Classer sans suite** dans *Décisions*.

## Autonomie des agents
- **Lancement automatique** : une tâche **assignée** à un agent démarre toute seule dès que ses dépendances sont satisfaites et que l'agent est libre (une tâche à la fois par agent). Plus besoin de cliquer sur ▶ ; les agents en **chat manuel** ne sont jamais lancés seuls. Réglage : *Vue globale* > **Lancement automatique des tâches**.
- **Un agent ne s'arrête plus à la première erreur** : tant qu'il n'a pas conclu (« à vérifier »), posé une question ou signalé un blocage, il est relancé automatiquement (jusqu'à 6 tours). S'il travaille encore à la fin, la tâche reprend toute seule. Une erreur inattendue du service est réessayée, un contexte trop long est compacté, une exécution figée est réinitialisée.
- **Coordination** : par défaut, **chaque agent peut contacter tous les autres** (champ « Agents qu'il peut contacter » : vide = tous, « aucun » = personne). Chaque agent voit qui travaille sur quoi, reçoit le compte rendu des tâches dont il dépend, et ses consignes lui demandent de se concerter avec les agents concernés avant de coder une interface partagée.
- L'onglet *Équipe* affiche pour chaque agent sa tâche en cours, sa prochaine reprise automatique et sa dernière activité.

## Suivi et vérification du travail
- **Une tâche n'est jamais « terminée » d'office** : quand un agent a fini, elle passe **« à vérifier »**. L'agent principal (ou le QA) relit le travail réellement fait dans le dépôt, avec les commits de l'agent comme preuve, puis la **clôture** ou la **refuse en listant ce qui manque** : l'agent la reprend alors avec ce motif. Après 3 refus, la question vous est posée.
- **Manque de tokens ou service indisponible** : l'agent est relancé **automatiquement** (délai du fournisseur, puis 5, 15, 30 minutes, puis toutes les heures), sans rien vous demander.
- **Autre problème** : l'agent principal interroge l'agent bloqué et le relance s'il peut. Si le problème persiste, ou s'il s'agit d'un plafond de budget, d'une clé invalide ou d'un modèle introuvable, **vous recevez une décision** (onglet *Décisions*) : relancer, mettre en pause ou abandonner.
- **Réponse à une question d'un agent** : sa tâche **reprend toute seule**, sans relance de votre part.
- Le suivi tourne toutes les minutes côté serveur et se désactive par projet (*Vue globale* > **Suivi automatique**). Une validation manuelle dans l'interface (statut « terminée ») court-circuite la vérification.

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
| `APM_SUPERVISE_SECONDS` | `60` | période du suivi automatique des tâches (`0` = désactivé) |

## Déploiement sur serveur Linux (plus tard)
Même commande : `APM_HOST=127.0.0.1 python3 server.py`, derrière un reverse proxy HTTPS (nginx ou Caddy), lancé par une unité systemd. Ne jamais exposer le port en HTTP clair sur Internet.

## Dépannage
- *« python n'est pas reconnu »* : rouvrez PowerShell après l'installation, ou utilisez `py server.py`.
- *Port déjà utilisé* : changez `APM_PORT`.
- *Pare-feu Windows* : inutile tant que l'accès reste local.

## Limites actuelles
Pas de base de données ; les agents lisent/écrivent des fichiers mais n'exécutent pas de code ; un redémarrage du serveur interrompt les travaux en cours (les reprises après quota sont, elles, replanifiées).
