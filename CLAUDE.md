# CLAUDE.md — contexte du projet « myteam »

> Lire ce fichier en premier : il évite de ré-analyser tout le code à chaque demande.
> **Le mettre à jour à chaque modification structurante** (route, champ d'état, action d'agent, décision d'architecture).

## But
Application de gestion de projets de développement par une **équipe d'agents IA** (Claude, Mistral, ChatGPT, Gemini, DeepSeek…). Tourne en local sur Windows 11 (usage actuel) ou sur un serveur Linux (plus tard). Langue de l'interface, des prompts et des commentaires : **français**.

## Stack et contraintes
- Backend : **Python 3.10+, bibliothèque standard uniquement** (aucun `pip install` ; ne pas en introduire sans décision explicite).
- Front : `static/index.html`, SPA **fichier unique** (HTML + CSS + JS, sans framework ni build), servie par le backend.
- Persistance : fichiers JSON dans `data/` (ignoré par git) : `users.json` (hash PBKDF2), `sessions.json`, `users/<user>.json` (**état complet** d'un utilisateur). Pas de base de données pour l'instant.
- Git/GitHub : **API REST GitHub** via `urllib` (pas de git local). Dépôt + token au niveau projet.
- Variables : `APM_PORT` (8000), `APM_HOST` (127.0.0.1), `APM_DATA`, `APM_ALLOW_REGISTER`, `APM_GITHUB_API` (tests). Windows : `start.bat`.

## Fichiers
| Fichier | Rôle |
|---|---|
| `server.py` | HTTP uniquement : routes, comptes/sessions, service du front |
| `store.py` | lecture/écriture de l'état, `mutate()` atomique, révisions, **fusion à trois voies** `merge3` |
| `engine.py` | **moteur d'agents serveur** : dialogues, actions, tâches, onboarding, releases, reprise après quota |
| `github.py` | classe `Repo` : branches, lecture/écriture/suppression de fichiers, PR+fusion, release |
| `ai.py` | `call_ai` : API Anthropic ou compatible OpenAI |
| `static/index.html` | toute l'IHM (aucune logique d'agent : elle envoie des travaux au serveur) |
| `tests/test_engine.py` | tests (faux GitHub local + IA scriptée) : `python -m unittest discover -s tests` |

## API (JSON, `Authorization: Bearer <token>`)
- `POST /api/register|login` → `{token}` ; `POST /api/logout`
- `GET /api/state` → `{rev, ser, state}` ; `GET /api/state?ser=N` → `{same:true}` si rien n'a changé (sondage du front, toutes les 2 s)
- `PUT /api/state` `{rev, state}` → `{rev, ser, state}` : le serveur **fusionne** l'état du navigateur avec l'état courant (voir ci-dessous) et renvoie le résultat.
- `POST /api/service/test` `{serviceId}` : appel minimal au service IA (valide la clé). Les clés API se créent dans la console du fournisseur (aucun flux OAuth public ne permet de les générer : vérifié pour Anthropic et OpenAI) ; le front guide la saisie (`keyset`) et teste aussitôt.
- `POST /api/manual/answer` `{id,text}` / `POST /api/manual/cancel` `{id}` : réponse (ou annulation) d'un échange **manuel** en attente.
- `POST /api/job` `{type, projectId, ...}` : lance un travail en arrière-plan (un thread). Types : `say{agentId,text}`, `run_task{taskId}`, `onboard{agentId}`, `sync`, `release{version,notes}`. Le front fait `flush()` (PUT) avant chaque travail.

## Concurrence navigateur / serveur (important)
Le navigateur et le moteur modifient le même état. `store.put` fait une fusion **à trois voies** : base = dernier instantané reçu par le navigateur (`rev`, 8 gardés + celui servant de base), theirs = état disque, mine = état envoyé. Les listes d'objets avec `id` sont fusionnées par id (suppressions locales respectées, créations serveur conservées) ; en cas de vrai conflit sur une valeur, **le navigateur gagne**. Conséquences :
- Tout nouvel élément de liste doit avoir un `id`. Les listes sans `id` (`log`, `commits`, `history`…) sont écrites **uniquement par le serveur**.
- Champs écrits par le serveur : `log, commits, mrs, releases, spec, releasing`, `agent.{history,notes,work,state}`, `task.{running,result,retry_at}`, tâches/bugs créés par les agents. Le navigateur ne doit pas les modifier (`ensure()` ne doit que **compléter** les champs manquants).
- Le front adopte l'état serveur (sondage ou réponse du PUT) sauf s'il a des modifications en attente ou une fenêtre modale ouverte.

## Modèle de données (état utilisateur)
`{services:[{id,name,url,kind,model,apiKey,account,pros,cons}], projects:[...]}`
Projet : `{id,name,open,desc,rules,spec,mainId,usage,ledger, repo:{url,branch,token}, roles[], agents[], tasks[], bugs[], modules[], log[], commits[], releases[], mrs[], releasing}`
- Agent : `{id,name,service,role,branch,canBug,talk[ids],notes,history[],state,work}` — **dépôt et token au niveau projet**, seule la **branche** varie par agent (peut être partagée).
- Tâche : `{id,parent,title,desc,who,status("à faire|en cours|terminée"),modules[],result,running,retry_at}`. Bug : `{id,t,d,sev,status,modules[],by}`. Module : `{id,name,desc}`.
- `log[]` : `{d,from,to,text}` = historique de **toutes** les discussions (Vous, agents, « Système », « Application »).
- **Coûts** : `ai.call_ai` renvoie un `Reply(str)` avec `.usage={in,out}` (Anthropic : `input_tokens`+cache, `output_tokens` ; OpenAI : `prompt_tokens`, `completion_tokens`). `engine.record_usage` cumule `{calls,in,out,cost}` dans `agent.usage` et `p.usage` (le total survit à la révocation d'un agent) et ajoute une ligne à `p.ledger` (300 max). Coût = tokens × tarifs du service (`service.pin` / `service.pout`, **$ par million de tokens, saisis par l'utilisateur**, jamais préremplis car les tarifs changent). Écrit par le serveur uniquement : **le front n'ajoute pas de valeur par défaut à `usage`/`ledger`** (sinon la fusion à trois voies écraserait les compteurs). Job `reset_usage`. Les agents en chat manuel ne sont pas mesurés.
- `ensure(p)` (front) complète les anciens états : **y ajouter tout nouveau champ**.

## Moteur d'agents (`engine.py`)
- `ask(u,pid,aid,text,frm,depth)` : boucle d'au plus `MAX_STEPS`=6 tours : prompt système (`sys_prompt`, reconstruit à chaque tour depuis l'état frais) + historique (30 derniers messages) → `call_ai` → blocs ```` ```action {json} ``` ```` (`parse_acts`) → `do_act`. Les **retours** (lecture de fichier, liste, réponse d'un autre agent, refus/erreurs) sont renvoyés à l'agent au tour suivant ; les écritures réussies sont seulement journalisées.
- Actions : `list_files`, `read_file`, `write_file`, `delete_file` (sur **la branche de l'agent** ; jamais sur la branche de base ; chemins validés par `safe_path`, `.github/`, `.git/` et `.agents/` interdits en écriture ; lecture tronquée à 12 000 car., écriture ≤ 200 000 car.), `message` (agents joignables ; le principal joint tous ; profondeur max 3 ; la réponse revient à l'expéditeur), `context`, `task_status`, `bug` (si `canBug` ou principal), `task` et `spec` (principal seul ; `spec` pousse `SPEC.md`).
- **Toute nouvelle règle ou action se déclare dans `RULES` / `ACTIONS`** (elles alimentent le prompt système ET le fichier `.agents/<Prénom>.md`).
- Onboarding/synchronisation (`provision`) : crée la branche de l'agent si absente et pousse `.agents/<Prénom>.md` (règles équipe + projet, rôle, droits, actions, notes).
- Tâches (`run_task`) : quota 429/529 → agent `suspendu`, `retry_at`, reprise automatique par `threading.Timer` ; `recover()` au démarrage remet les compteurs à zéro, marque les tâches interrompues et replanifie les reprises.
- Release (`release`) : pour chaque branche d'agent ≠ base → PR + fusion, puis tag + release GitHub ; échecs consignés sans bloquer la suite.
- Agent principal (`mainId`) : dialogue via le prompt de la Vue globale ; écrit à tous, crée les tâches, tient les spécifications, réalise MR + tag.

## Modes de connexion d'un agent (`agent.mode`)
- `api` (défaut) : `ai.call_ai` avec la clé du service. `manual` : **chat manuel**, sans API : `engine.manual_exchange` publie un prompt dans `p.pending` (`{id,agent,name,service,full,short,new,d}`), le thread **attend** (`WAIT`, 24 h) que l'utilisateur colle la réponse du chatbot (`/api/manual/answer`) ; les actions de la réponse sont ensuite exécutées comme d'habitude (les retours de lecture arrivent dans un nouveau prompt). `full` = instructions complètes (nouvelle conversation), `short` = suite (`agent.session` mémorise qu'une conversation existe).
- **Décision : on n'automatise pas les sites de chat** (claude.ai, chatgpt.com…) : pas d'API publique pour les abonnements grand public, conditions d'utilisation contraires (Anthropic l'applique techniquement), fragile. Alternatives gratuites sans automatisation de site : API à offre gratuite (ex. Gemini via AI Studio, avec limites) ou modèle local compatible OpenAI (ex. Ollama : service `kind:"openai"`, URL `http://localhost:11434/v1`, clé factice).
- `p.pending` est écrit par le serveur uniquement ; `recover()` le vide au démarrage.

## Conventions de travail
- Modifier `static/index.html` par remplacements ciblés ; vérifier la syntaxe JS (extraire le `<script>` et `node --check`).
- Lancer `python -m unittest discover -s tests` avant tout commit. Les tests n'utilisent ni réseau ni clé (faux GitHub, IA scriptée).
- Ne jamais committer `data/`, de token ni de clé API. Ne jamais tester sur le vrai dépôt sans supprimer ensuite les branches créées.
- Token GitHub requis : *Contents* et *Pull requests* en lecture/écriture (sinon fusion en 403 avec message explicite).

## Limites connues / suite prévue
- Les noms de modèles préconfigurés vieillissent (ex. `gemini-2.0-flash` a été retiré par Google, remplacé par `gemini-3.8-flash`) : à revérifier auprès du fournisseur ; une erreur 404 du service signale presque toujours un nom de modèle périmé.
- Pas de base de données ; sessions sans expiration ; clés API stockées en clair dans `data/`.
- Un serveur redémarré perd les travaux en cours (tâches marquées « Interrompu ») ; pas de file d'attente persistante.
- Les agents ne peuvent pas exécuter de code ni lancer de tests ; ils lisent/écrivent des fichiers du dépôt.
- Fusion à trois voies : si le navigateur reste très longtemps sans adopter l'état serveur (plus de 8 instantanés), le repli est « le navigateur gagne » (perte possible de données serveur récentes).
- Non testé : exécution sous Windows 11, fusion de PR et release sur le vrai GitHub avec un token disposant des droits.
