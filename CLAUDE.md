# CLAUDE.md — contexte du projet « myteam »

> Lire ce fichier en premier : il évite de ré-analyser tout le code à chaque demande.
> **Le mettre à jour à chaque modification structurante** (nouvelle route, nouveau champ d'état, nouvelle action d'agent, décision d'architecture).

## But
Application de gestion de projets de développement par une **équipe d'agents IA** (Claude, Mistral, ChatGPT, Gemini, DeepSeek…). Tourne en local sur Windows 11 (usage actuel) ou sur un serveur Linux (plus tard). Langue de l'interface, des prompts et des commentaires : **français**.

## Stack et contraintes
- Backend : `server.py`, **Python 3.10+, bibliothèque standard uniquement** (aucun `pip install`, ne pas en introduire sans décision explicite).
- Front : `static/index.html`, SPA **fichier unique** (HTML + CSS + JS, sans framework ni build). Servie par le backend.
- Persistance : fichiers JSON dans `data/` (ignoré par git) : `users.json` (hash PBKDF2), `sessions.json`, `users/<user>.json` (**état complet** d'un utilisateur). Pas de base de données pour l'instant.
- Git/GitHub : **API REST GitHub** via `urllib` (pas de git local requis). Token au niveau projet.
- Windows : `start.bat` ; variables `APM_PORT` (8000), `APM_HOST` (127.0.0.1), `APM_DATA`, `APM_ALLOW_REGISTER`.

## Fichiers
| Fichier | Rôle |
|---|---|
| `server.py` | HTTP (ThreadingHTTPServer), comptes/sessions, état, `call_ai`, `git_op` |
| `static/index.html` | Toute l'IHM et le **moteur d'agents côté client** |
| `README.md` | Installation Windows 11 et usage |
| `CLAUDE.md` | Ce fichier |

## API backend (JSON, `Authorization: Bearer <token>`)
- `POST /api/register|login` → `{token}` ; `POST /api/logout`
- `GET|PUT /api/state` : état complet de l'utilisateur (le front sauvegarde 300 ms après chaque modification, `flush()` avant les appels agents/git)
- `POST /api/agent/run` `{serviceId, system, messages}` → `{text}`. Format Anthropic (`kind:"anthropic"`) ou compatible OpenAI. HTTP 429/529 → erreur 429 + `retry_after` (l'agent se suspend et reprend seul pour les tâches).
- `POST /api/git` `{projectId, op, ...}` ; le serveur relit dépôt+token dans l'état du projet. Ops : `ensure_branch{branch}`, `put_file{branch,path,content,message}`, `pr_merge{head,title,body,base?}` (crée la PR puis la fusionne ; `skipped` si aucun commit), `release{tag,name,body}` (crée le tag + la release GitHub sur la branche de base).
- Les clés API des services sont stockées **en clair** dans l'état (serveur local uniquement).

## Modèle de données (état utilisateur)
`{services:[{id,name,url,kind,model,apiKey,account,pros,cons}], projects:[...]}`
Projet : `{id,name,open,desc,rules,spec,mainId, repo:{url,branch,token}, roles[], agents[], tasks[], bugs[], modules[], log[], commits[], releases[], mrs[]}`
- Agent : `{id,name,service,role,branch,canBug,talk[ids],notes,history[],state}`. **Le dépôt et le token sont au niveau projet** ; seule la **branche** varie par agent (plusieurs agents peuvent partager une branche).
- Tâche : `{id,parent,title,desc,who,status("à faire|en cours|terminée"),modules[],result,running}` (arborescence via `parent`).
- Bug : `{id,t,d,sev,status("ouvert|en cours|résolu"),modules[],by}`. Module : `{id,name,desc}`.
- `log[]` : `{d,from,to,text}` = historique de **toutes** les discussions (Vous, agents, « Système », « Application »).
- `ensure(p)` (front) migre/complète les anciens états à chaque chargement : **y ajouter tout nouveau champ**.

## Moteur d'agents (dans `static/index.html`)
- `ask(p,a,text,from,depth)` : prompt système (`sysPrompt`) + historique de l'agent (30 derniers messages) → `/api/agent/run` → extraction des blocs ```` ```action {json} ``` ```` (`parseActs`) → exécution (`doAct`).
- Actions : `message` (agents joignables ; l'agent principal joint tous ; profondeur max 3), `context` (réécrit les notes de l'agent + repousse son fichier), `task_status`, `bug` (si `canBug` ou principal), `task` (principal seul), `spec` (principal seul, pousse `SPEC.md`).
- **Agent principal** (`p.mainId`, le premier recruté par défaut) : dialogue via le prompt de la Vue globale, écrit aux autres agents, crée les tâches, tient les spécifications, **réalise les merge requests + le tag à la release** (`doRelease`).
- **Onboarding** (`onboard`/`provision`) : crée la branche de l'agent si absente (`ensure_branch`, depuis la branche de base) et pousse `.agents/<Prénom>.md` (`ctxText` : règles équipe + règles projet + rôle + droits + actions + notes). Les mêmes règles sont injectées dans le prompt système à chaque appel. « Synchroniser » ou la modification des règles du projet re-pousse tout.
- Règles génériques : constante `RULES` ; protocole d'actions : constante `ACTIONS`. **Toute nouvelle règle ou action se déclare là** pour que les agents la reçoivent.
- Release : pour chaque branche d'agent ≠ base → `pr_merge`, puis `release` (tag). Les échecs sont consignés dans l'historique, sans bloquer la suite.
- Récapitulatifs fonctionnel/technique : générés localement (`recap`) à partir de l'état et de `p.spec`.

## Onglets d'un projet
Vue globale (indicateurs, prompt vers l'agent principal, règles), Tâches, Modules, Équipe, Dialogue (avec n'importe quel agent), Historique (tout / entre agents), Récapitulatif, Git & releases, Bugs, Rôles.

## Conventions de travail
- Modifier `static/index.html` par remplacements ciblés ; vérifier la syntaxe JS (extraire le `<script>` et `node --check`).
- Tester le backend avec un faux service OpenAI local (aucune clé réelle nécessaire) ; ne jamais lancer de tests git sur des branches réelles sans les supprimer ensuite.
- Ne jamais committer `data/`, de token ni de clé API. Un token a été collé dans une conversation : il doit être révoqué.
- Token GitHub requis : *Contents* et *Pull requests* en lecture/écriture (sinon la fusion échoue en 403 avec un message explicite).

## Limites connues / suite prévue
- Pas de base de données ; sessions sans expiration ; mots de passe/clés non chiffrés au repos côté clés API.
- Les agents n'exécutent pas de code ni ne lisent le dépôt : ils produisent du texte et des actions (fichier de contexte, spécifications). Prévu : lecture/écriture de fichiers du dépôt par les agents.
- La reprise automatique après quota ne survit pas à un rechargement de la page ; l'orchestration est côté navigateur (à déplacer côté serveur).
- Non testé : exécution réelle sous Windows 11, fusion de PR et release avec un token disposant des bons droits.
