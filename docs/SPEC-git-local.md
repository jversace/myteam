# SPEC — Client git local intégré (mode « répertoire local »)

> Statut : **décisions prises le 20 nov. 2026 (§ 15)** ; rien de ce qui suit n'est encore implémenté (hors chantier 0, § 0.1).
> Objectif : les agents travaillent sur un vrai dépôt git local ; **l'application** exécute git, pas les agents ; tout ce qui est mécanique se fait **sans IA** (économie de tokens, plus d'appels GitHub pour lire/écrire).
> Le fonctionnement actuel (API GitHub, mode A) est conservé tel quel.

## 0. Constat sur le code actuel

### 0.1 Pourquoi des agents « restent en attente » (à corriger indépendamment de ce chantier)

Lecture du code (`engine.py`), causes probables, par ordre de vraisemblance :

1. **Dernier retour d'actions jamais délivré en dialogue direct.** `say()` appelle `ask()` avec `MAX_STEPS = 6` tours et **ne reprend pas `stats["pending"]`** (contrairement à `run_task`). Si l'agent enchaîne les `read_file` (un par tour), le retour du 6ᵉ tour (ex. contenu de deux fichiers) est perdu : la boucle s'arrête, l'agent attend, et à la relance il répond « je n'ai pas reçu le contenu des deux derniers fichiers ». C'est exactement le symptôme observé.
2. **Bloc `action` invalide ignoré en silence.** `parse_acts` fait `except ValueError: pass` : un JSON mal formé (guillemets ou retours à la ligne non échappés dans `content`, deux actions collées dans un bloc) n'exécute rien et **ne renvoie rien à l'agent** → `fb` vide → fin du tour.
3. **Bloc contenant une liste JSON.** `x.get(...)` lève `AttributeError`, non rattrapée par `do_act` (`except Err`) ni par `say` (`except Err`) : le tour s'interrompt, trace uniquement dans la console.
4. Les erreurs GitHub (403/404/422) sont, elles, **renvoyées à l'agent** (`refuse(...)`) : cause moins probable. Aucun retry/backoff sur les 403/429 GitHub (seuls les appels IA en ont).

Correctifs du **chantier 0** — **appliqués** (19 nov. 2026, avec tests `Chantier0` et `LimitesGithub`), sans lien avec git local :
- `say()` : reprendre `pending` comme `run_task` (jusqu'à N tours), au lieu de l'abandonner.
- `parse_acts` : renvoyer à l'agent « bloc action n° k invalide : <erreur JSON> » ; accepter une liste d'actions dans un bloc ; ne jamais perdre un bloc en silence.
- `do_act` : intercepter `Exception` (pas seulement `Err`) et renvoyer un refus factuel à l'agent.
- `github.call` : retry avec backoff sur 403 (limite secondaire)/429/5xx, en respectant `Retry-After`.
- Reste à faire : les autres appelants d'`ask` (message d'agent à agent, `decide`…) ne reprennent pas encore le retour non traité ; détecter un agent « en attente » (dernier message sans retour traité) et le relancer automatiquement.

### 0.2 Ce qui est réutilisable

| Élément existant | Réutilisation |
|---|---|
| `locks.py` : `root`, `resolve` (confinement, liens symboliques, `FORBIDDEN_WRITE`), `list_dir`, `read_text`, `write_text`, arrêt/annulation (`stop`/`cancel`), expiration | `root(p)` devient `root(p, agent)` (le worktree de l'agent). Les verrous restent utiles **hors git** (répertoire local non versionné) |
| `engine.local_act` / `local_agent(p, a)` / `a["source"]` | Point d'entrée déjà en place ; on y branche le worktree et les commits réels (`record_commit` reçoit aujourd'hui le faux sha `"local"`) |
| `github.Repo` (18 appels dans `engine.py`) | **Interface à extraire** (cf. § 10) : `sha, ensure_branch, reset_branch, read_file, list_dir, put_file, delete_file, compare, merge_branch, conflict_info, merge_with_resolution, pr_open, pr_state, pr_merge_number, pr_merge, release` |
| `conflict_info` / `merge_with_resolution` (résolution par contenu complet) | Modèle conservé : `merge --no-commit`, l'agent fournit le contenu résolu, l'appli écrit et committe |
| `diff_text`, `pull`, `submit_mr`, `merge_mr`, `sync_mrs`, `release`, `reconcile_conflicts` | Même logique, backend différent |
| Tests : faux serveur GitHub | Pour le local : **vrais dépôts git temporaires** et un **dépôt nu local en guise de remote** (aucun réseau) |

## 1. Modes

| Mode | Condition | Comportement |
|---|---|---|
| A. GitHub (API) | Pas de répertoire local, ou agent en `source = github` | **Inchangé** |
| B. Local, dépôt git existant | `localdir` contient un dépôt git | Worktrees, git local exécuté par l'application |
| C. Local, à initialiser | `localdir` sans `.git` | L'application initialise (§ 2), puis mode B |
| D. Local sans git (actuel) | Case à cocher **« sans git »** dans les paramètres du projet | Verrous de fichiers actuels (`locks.py`) ; si un `.git` existe, l'application l'ignore et le signale (aucun commit fait par elle) |

Règle de cohérence : dans un projet dont le répertoire local **n'a pas de remote**, aucun agent ne peut être en mode A (deux sources de vérité).

## 2. Initialisation (mode C) — opération de projet, pas d'agent

- Exécutée **par l'application**, une seule fois, sous mutex de projet (pas « le premier agent qui arrive » : course possible).
- Sans remote : `git init -b main`, `.gitignore` de base (`.env`, `data/`, `.myteam/`, caches), commit initial, création de `develop`.
- Avec remote, dossier **vide** : `git clone`.
- Avec remote, dossier **non vide** sans `.git` : cas qui ne doit pas se produire → le dossier est déclaré **inutilisable** (alerte explicite, agents concernés arrêtés, **aucun repli silencieux** vers le mode A) ; pas de réconciliation assistée.
- **Contrôles préalables** à l'activation du dossier (même traitement : alerte + arrêt des agents concernés) : dossier avec `.git` dont `origin` ≠ remote du projet ; modifications non commitées au démarrage.
- Prérequis contrôlés au démarrage et dans l'onglet *Git & releases* : git installé (Git for Windows sur PC), version minimale, `user.name` / `user.email` définis par worktree.

## 3. Branches

| Branche | Rôle |
|---|---|
| `main` | Release finale uniquement, tag annoté |
| `develop` | **Intégration permanente** quand aucune release n'est ouverte (la branche principale reste ainsi réservée) |
| `release/<version>` | Créée depuis `develop` (ou `main`) à l'initiation de la release, par l'agent principal ou l'agent « gardien du dépôt » (droit par rôle, comme `mr_review`) |
| `task/<prénom>/<id>-<titre>` | Une branche **par tâche** (un seul agent à la fois), créée depuis la cible courante. Espace de noms `task/` (et non `agent/`) : git interdit qu'une branche `agent/<nom>` (héritée) coexiste avec `agent/<nom>/…` |

**Décision : une branche par tâche.** Réglage de projet « stratégie de branches » (par agent / par tâche), appliqué aux **nouvelles** tâches seulement.

**Migration d'un projet en cours (une branche par agent)** : pas de bascule brutale ni de réécriture d'historique.
- Branche d'agent déjà fusionnée dans la cible : archivée/supprimée.
- Branche avec travail non fusionné : marquée « héritée » ; si l'agent n'a qu'une tâche en cours, elle est renommée `task/<prénom>/<id>-<titre>` ; s'il en a plusieurs, elle reste telle quelle jusqu'à fusion complète, ses nouvelles tâches démarrant ensuite en mode par tâche. Les MR en cours suivent leur branche.
- **Fichier de contexte de l'agent** : sorti des branches, `<localdir>/.myteam/agents/<prénom>.md`, ignoré par git, stable d'une tâche à l'autre (le mode A n'est pas touché).

**Un seul écrivain par branche** : une branche = un agent = un worktree (git refuse de sortir la même branche dans deux worktrees ; l'application applique la règle avant git). Deux agents sur une même fonctionnalité → sous-tâches, une branche chacune, fusionnées séparément (B dépend de A : B attend que A soit fusionnée). Changement d'agent sur une tâche : commit du travail en cours puis transfert du worktree (un seul détenteur à la fois). Deux tâches touchant les mêmes fichiers : conflit traité au § 6.

## 4. Arbres de travail (point critique)

- **Un `git worktree` par agent** : `<localdir>/.myteam/worktrees/<prénom>/`. Un seul arbre partagé est impossible : un `checkout` changerait les fichiers de tous les agents.
- Un **worktree d'intégration** dédié aux merges (jamais utilisé par un agent), sous mutex.
- Confinement : chemins validés par `engine.safe_path` puis `locks.resolve` borné au worktree ; `.git` jamais lisible/écrivable par un agent ; `.myteam/` exclu de la liste des fichiers.
- Les verrous de fichiers deviennent inutiles entre agents en mode B (conflits traités au merge) ; ils restent pour le mode D.

## 5. Cycle d'une tâche

1. **Début** (programmatique) : `fetch` si remote ; création de la branche de l'agent depuis la cible (`release/x` si release ouverte, sinon `develop`) ; intégration des nouveautés de la cible. Une tâche qui dépend d'une autre ne démarre qu'après la **fusion** de celle-ci dans la cible (comportement actuel conservé).
2. **Travail** : l'agent utilise les outils de haut niveau (§ 9). Il peut committer via `commit`.
3. **Fin** : commit final par l'application, tâche au statut « à vérifier », travail **resté en local** (comme demandé). **Push de la branche de l'agent** : à la validation de la tâche **ou** sur demande (bouton), uniquement si un remote est défini. Après la fusion, la branche distante est supprimée par défaut (option pour la conserver).
4. **Validation** : l'application exécute la **commande de test/lint du projet** (configurable) et fournit le diff (déjà en place) au validateur.
5. **Merge** (tâche validée) : dans le worktree d'intégration, `merge --no-ff` de la branche de l'agent dans la cible, puis `push` si remote. File d'attente **sérialisée**. Option `--squash` par projet.
6. **Nettoyage** : branche et worktree de l'agent supprimés une fois la tâche fusionnée (la branche est conservée si la tâche est rouverte).

## 6. Conflits

- **Mise à jour de la branche de l'agent** (début de tâche, avant validation) : l'agent résout dans son worktree ; sinon escalade selon la règle de conflits existante (utilisateur, auteur, agent désigné).
- **Intégration** : `merge --abort`, tâche en « conflit », renvoyée à l'agent de la tâche qui intègre la cible dans sa branche, résout, puis repasse en validation. Réutilise `conflict_info` / `describe_conflict` / `reconcile_conflicts`.
- Jamais de résolution automatique (`-X ours/theirs`).
- Push refusé (non fast-forward) : `fetch`, nouveau merge, nombre de tentatives borné, puis alerte.

## 6 bis. Contrôles automatiques (tests, lint, build)

Liste de contrôles par projet ; chacun : nom, commande, délai maximal, **bloquant / informatif**, moment (`validation`, `intégration`).
- **Avant la validation**, dans le worktree de l'agent : en cas d'échec, la tâche retourne **automatiquement** à l'agent avec la fin de la sortie (tronquée), sans solliciter le validateur IA ; tentatives bornées (2 par défaut), puis escalade.
- **Après le merge dans le worktree d'intégration, avant le push** : détecte les conflits sémantiques ; échec bloquant → merge annulé, tâche renvoyée à l'agent.
- Par défaut : tests bloquants à l'intégration, lint informatif ; réglable par contrôle.
- **État de référence** : un contrôle ne devient bloquant qu'après une première exécution verte sur la cible. Option « rejouer une fois » pour les tests instables.
- **Sécurité** : la commande est exécutée par l'application avec les droits de son compte ; délai maximal, taille de sortie limitée, environnement nettoyé (sans jeton ni secrets de l'application), une exécution à la fois par défaut.
- **Qui décide** : un agent ne peut **jamais activer** ni modifier une commande ; il peut seulement la **proposer** (§ 6 ter). Seule l'approbation de l'utilisateur connecté l'active.
- Sans contrôle configuré : le validateur est informé (« aucun contrôle automatique »), sans que cela soit présenté comme un succès.

## 6 ter. Proposition et approbation d'une commande de contrôle

L'utilisateur n'a pas à connaître la commande : les agents la proposent, il approuve en langage courant.

- **Action `propose_check`** (droit par rôle « proposer des contrôles ») : nom, commande, moment, caractère bloquant, **explication en langage non technique** (ce que ça vérifie, ce que ça modifie, pourquoi la proposition ou le changement). Au démarrage d'un projet, l'agent principal examine le dépôt et propose les contrôles adaptés.
- **Rien ne s'exécute avant approbation**, pas même une fois ; la commande active reste l'ancienne jusqu'à la décision. Sans commande active : « aucun contrôle automatique » (§ 6 bis).
- **Carte d'approbation** (même mécanisme que la confirmation des tâches de premier niveau, avec pastille « à valider ») : explication de l'agent, **ce qui change** par rapport à la commande actuelle, **niveau de vigilance calculé par l'application, indépendamment de l'agent** (l'explication d'un agent peut être inexacte), et la commande exacte repliée sous « détail technique ». Boutons : **Approuver / Refuser** (avec commentaire facultatif, renvoyé à l'agent).
- **Vigilance calculée** par analyse déterministe du texte : *faible* (lecture, tests, lint) ; *moyenne* (télécharge des paquets sur Internet, écrit des fichiers, enchaîne plusieurs commandes) ; *élevée* (supprime des fichiers, accède au réseau pour envoyer ou exécuter du code, touche aux secrets ou en dehors du dossier de travail) → confirmation renforcée ; certains motifs (`sudo`, téléchargement exécuté directement, suppression hors du dossier de travail) sont **refusés automatiquement**, sans carte.
- **L'approbation porte sur le texte exact** : toute modification, même minime, redemande une approbation.
- **Historique versionné** : proposant, date, approbateur, ancienne et nouvelle version, motif ; restauration d'une version précédente possible.
- L'API de décision n'est **pas exposée aux outils des agents** (session utilisateur uniquement).

## 7. Releases et merge requests

- **Démarrer** : crée `release/x.y.z`. **Publier** : refuse s'il reste une tâche « à vérifier » ou un conflit ouvert ; merge de la release dans `main`, **tag annoté** `vX.Y.Z`, push de la branche et des tags, fusion retour dans `develop`. Release GitHub créée via l'API si remote GitHub.
- **MR déduites de git** (fin des « blockers fantômes ») : `ouverte` = branche non fusionnée dans la cible ; `terminée` = fusionnée (`git branch --merged`) ; `annulée/abandonnée` = branche supprimée ou tâche annulée. Le bouton manuel « marquer comme terminée » reste en secours.
- **Remote GitHub** : push direct par l'application sur `develop` et `release/*` (la validation a déjà eu lieu dans l'application) ; **une seule pull request par release** (release → `main`), créée et fusionnée via l'API ; **repli automatique en mode PR** si GitHub refuse le push direct (branche protégée, erreur `GH006`) : push de la branche de l'agent, ouverture de la PR, fusion via l'API ou attente des revues/contrôles exigés.
- Permissions du jeton : **Contents** et **Pull requests** en lecture/écriture ; **Workflows** si les agents modifient `.github/workflows`.

## 8. Sécurité

- **Jeton** : jamais dans l'URL du remote ni dans `.git/config` ; transmis par `GIT_ASKPASS` (ou en-tête HTTP temporaire), jamais passé aux agents, jamais journalisé. SSH possible en alternative.
- **Exécution** : `subprocess` avec liste d'arguments (pas de shell), timeout, `GIT_TERMINAL_PROMPT=0`, environnement nettoyé, **liste blanche de sous-commandes**.
- **Hooks** : `core.hooksPath` pointé vers un dossier contrôlé par l'application (un hook d'un dépôt cloné ne s'exécute pas).
- **Identité** : `user.name` = prénom de l'agent, e-mail technique `<prénom>@myteam.local`, trailer `Agent-Service: <service IA>` pour la traçabilité.
- **Secrets** : écriture refusée sur `.env`, clés, `.git`, `.myteam` ; `.gitignore` de base.

## 9. Outils exposés aux agents

| Outil | Effet |
|---|---|
| `list_files`, `read_file`, `write_file`, `delete_file` | Dans le worktree de l'agent (existants, backend local) |
| `search` | Recherche texte dans le worktree (réduit les lectures successives) |
| `status`, `diff` | État et diff de la branche de l'agent (sans IA) |
| `commit` | Commit programmatique, message normalisé avec l'id de tâche |
| `sync_with_target` | Intègre la cible dans la branche de l'agent |
| `request_validation` | Passe la tâche en « à vérifier » |
| `git_log`, `git_show` | Lecture seule |

**Aucun** `push`, `checkout`, `reset --hard`, `rebase`, `--force` ni commande git libre.

## 10. Architecture logicielle

- Nouveau module `gitlocal.py` (bibliothèque standard, `subprocess`) exposant **la même interface que `github.Repo`** ; `engine.py` obtient son backend via une fabrique `repo_for(p, agent)` au lieu de 18 appels directs à `github.Repo(p)`.
- État projet : `p.git = {mode, remote, target, worktrees{agent→chemin}, integrating, health}` ; état agent : `a.worktree`, `a.branch` (par tâche si retenu).
- Journal des commandes git (dernières N, sans jeton) visible dans le panneau Équipe.
- Fonctionnalité activable par projet (drapeau) : le mode A reste le défaut.

## 11. Robustesse

- Au démarrage (`recover`) : `index.lock` orphelins supprimés, `merge`/`rebase` interrompus annulés, `git worktree prune`, vérification de l'état de chaque worktree.
- Agent planté, arrêté ou annulé (`stop_agent`) : commit « WIP » automatique sur sa branche, ou retour au dernier commit selon l'option ; jamais de perte silencieuse.
- Windows : `core.autocrlf`, chemins longs (`core.longpaths`), fichiers différant seulement par la casse.
- Gros fichiers/binaires : taille maximale en écriture, Git LFS optionnel.
- Indicateur « santé du dépôt » (propre / en retard sur la cible / conflit / verrou git) et bouton **Resynchroniser**.

## 12. Ce qui est programmatique (0 token)

Initialisation, création des branches et worktrees, fetch, mise à jour, merge, push, commits, tags, état des MR, exécution des tests, nettoyage, reprise après crash. L'IA ne sert qu'à écrire le code, résoudre les conflits et valider.

## 13. Tests

Vrais dépôts git temporaires ; remote = dépôt nu local (`git init --bare`) ; scénarios : init (avec/sans remote, dossier non vide), deux agents en parallèle, mise à jour sans conflit, conflit à l'intégration puis résolution, push refusé puis nouvelle tentative, release complète (merge, tag, retour dans `develop`), crash et reprise, jeton absent de `.git/config` et des journaux.

## 14. Étapes (état au 20 nov. 2026)

0. **Chantier 0** (§ 0.1) : reprise en dialogue, retours d'erreur d'actions, retry GitHub — **fait**.
1. Interface commune + `gitlocal.py`, mode B, worktrees, outils agents. **Fait (20 nov. 2026)** : `gitlocal.py` (35 tests) et raccordement au moteur derrière le réglage de projet `nogit` (désactivé par défaut) : worktree par agent, outils `status`/`commit`/`diff`/`search`/`sync`/`git_log`/`git_show`, contrôles préalables, contexte hors des branches, case à cocher dans l'interface (16 tests, dont la non-régression du mode sans git). **Non fait** : le choix d'une interface commune formelle avec `github.Repo` est reporté à l'étape 2 (les deux modes sont aiguillés dans `repo_act` ; `github.Repo` reste inchangé).
2. Cycle tâche → validation → merge, file d'intégration, MR déduites de git. **Fait (20 nov. 2026)** : soumission programmatique, validation, intégration sérialisée, conflits renvoyés à l'auteur, état des MR déduit de git, push de la branche de l'agent et de la cible, remote qui avance ou injoignable ; **branches par tâche** (défaut) avec branche libre, changement de branche sans perte (WIP), **branche héritée** conservée jusqu'à sa fusion, suppression des branches fusionnées (locale et distante, option pour conserver), poussée à la demande (bouton). 247 tests. **Reste** : règle de conflit du projet appliquée à l'intégration, renommage automatique d'une branche héritée à tâche unique.
3. Mode C (initialisation), releases, tags.
4. Conflits, reprise après crash, tableau de bord, option push de branche.

## 15. Décisions (20 nov. 2026)

1. **Branche par tâche** ; migration progressive des projets existants (§ 3).
2. **Push de la branche de l'agent** : à la validation de la tâche ou sur demande, uniquement si un remote est défini ; branche distante supprimée après fusion par défaut, avec option pour la conserver.
3. **Remote GitHub** : push direct sur `develop`/`release/*`, une PR par release vers `main`, repli automatique en mode PR si le push est refusé (§ 7).
4. **Mode sans git conservé** : case à cocher dans les paramètres du projet.
5. **Dossier local non vide avec remote** : alerte, dossier inutilisable (pas de réconciliation assistée) ; mêmes contrôles préalables pour un `origin` différent ou des modifications non commitées (§ 2).
6. **Contrôles automatiques** : liste de contrôles par projet, bloquants ou informatifs, avant validation et à l'intégration (§ 6 bis) ; **proposés par les agents avec explication non technique, activés uniquement par approbation de l'utilisateur** (§ 6 ter).
