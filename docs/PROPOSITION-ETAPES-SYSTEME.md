# Proposition — étapes de workflow exécutées par le système (sans IA)

*Statut : validée le 25 nov. 2026. **Lots MVP et v2 (partiel) implémentés** : voir § 10 et § 11. Le reste du catalogue (v3) n'est pas implémenté.*

## 1. Problème

Aujourd'hui, toute étape de workflow est exécutée par un agent (`kind: ask`), avec un appel IA et donc des tokens, y compris pour des gestes purement mécaniques : changer le statut d'une tâche, affecter un bug, resynchroniser la branche d'un agent, marquer une MR terminée, etc.

## 2. Principe

On garde les étapes en texte libre pour les agents et on ajoute un nouveau type d'étape, **Système**. L'application l'exécute elle-même : 0 token, résultat déterministe, instantané, journalisé.

Types d'étapes (`kind`) :

| kind | exécutant | état |
|---|---|---|
| `ask` | agent (texte libre) | existant |
| `start_tasks` | application (`dispatch`) | existant, deviendra l'action système `launch_tasks` |
| `ask_user` | utilisateur | existant |
| **`system`** | **application** | **nouveau** |

Une étape système a la forme `{id, kind:"system", action:"<id>", params:{...}, on_ok, on_ko, goto}`.

- Elle rend toujours `ok` ou `ko` et un commentaire court. Ce compte rendu est transmis aux étapes suivantes, comme celui d'une étape `ask`.
- `on_ko` existe déjà (arrêter, continuer, question à l'utilisateur, saut). On ajoute `on_ok` (suite ou saut), pour pouvoir aiguiller.
- Un `ko` système peut renvoyer vers une étape agent. Exemple : intégration en conflit → étape `ask` pour l'auteur de la MR.
- Les étapes `ask` et `system` se mélangent librement dans un même workflow.

### Types de paramètres

- **Acteur**, même syntaxe que `who` : `principal`, `assignee`, `requester`, `agent:<id>`, `role:<id>` (nom ou rôle de l'agent).
- **Statut de tâche** ou **statut de bug** (listes fermées : `rights.STATUS`, `rights.BUG_STATUS`).
- **Référence git** : `agent` (branche courante de l'agent), `release`, `develop`, `main`.
- **Texte avec variables** : `{task.title}`, `{task.id}`, `{task.assignee}`, `{run.requester}`, `{step.N.comment}`.
- **Module**, **nombre**, **durée**, **booléen**.

### Garde-fous

- **Validation à l'enregistrement** : `prules.lint` vérifie l'action, les paramètres obligatoires, les acteurs, les statuts et les sauts.
- **Actions irréversibles ou sensibles** (publier la release, intégrer dans `main`, supprimer une branche, forcer un déverrouillage) : case « confirmer par l'utilisateur » cochée par défaut, via une décision `ask_user` automatique. L'utilisateur peut la décocher explicitement.
- Les droits par rôle (`rights.py`) s'appliquent aux agents. Une étape système agit avec l'autorité du workflow, que l'utilisateur a écrit et activé.
- Les limites existantes (`WF_MAX_STEPS`, `WF_MAX_PER_HOUR`) restent valables.
- Chaque exécution est tracée dans `wf_runs` (action, paramètres résolus, résultat) et visible dans l'onglet Règles / workflows.
- **Essai à blanc** (« simuler ») : affiche ce que ferait le workflow sans rien modifier.

## 3. Catalogue d'actions proposé

Légende : **MVP** = première livraison, **v2** = livraison suivante, **v3** = plus tard.

### A. Tâches

| Action | Paramètres | Lot |
|---|---|---|
| `set_task_status` | tâche (cible du workflow ou `{id}`), statut | MVP |
| `assign_task` | tâche, acteur (nom ou rôle) | MVP |
| `create_subtask` | parent, titre (avec variables), description, acteur, modules | v2 |
| `link_task_modules` | tâche, modules | v2 |
| `add_task_note` | tâche, texte | MVP |
| `set_task_dependency` | tâche, dépend de, ajouter/retirer | v2 |
| `launch_task` / `launch_tasks` | tâche ou toutes les tâches prêtes | MVP (reprend `start_tasks`) |
| `close_subtasks` | tâche, statut cible | v2 |

### B. Bugs

| Action | Paramètres | Lot |
|---|---|---|
| `set_bug_status` | bug, statut | MVP |
| `assign_bug_fix` | bug, acteur | MVP |
| `create_bug_fix_subtask` | bug, titre, acteur | v2 |
| `link_bug_modules` | bug, modules | v2 |
| `close_bug` | bug : passe à « vérifié » et clôture ses sous-tâches de correction | v2 |

### C. Git et merge requests

Ces actions réutilisent `gitlocal.py` (mode local) et `github.py` (mode GitHub).

| Action | Paramètres | Lot |
|---|---|---|
| `sync_agent_branch` | acteur, source (`release`/`develop`/`main`) | MVP (cas cité) |
| `sync_all_agent_branches` | source, rôle (facultatif) | v2 |
| `create_task_branch` | tâche | v2 |
| `sync_mrs` | (vérifie l'état réel des MR dans le dépôt, retire les « blockers fantômes ») | MVP |
| `mark_mr_done` | MR (cible ou `{id}`) | MVP |
| `merge_mr` | MR, vers (`release`/`develop`), règle de conflit du projet | v2 |
| `run_checks` | (commande de contrôle déjà approuvée) | v2 |
| `push_branch` | acteur ou `agent`, branche | v2 |
| `cleanup_merged_branches` | conserver les branches distantes oui/non | v2 |
| `release_start` | version | v3 |
| `release_publish` | version, notes, confirmation utilisateur | v3 |
| `push_main_develop_tags` | — | v3 |

### D. Agents et équipe

| Action | Paramètres | Lot |
|---|---|---|
| `notify_agent` | acteur, texte avec variables. Il est ajouté au fil de l'agent et lu à son prochain tour, sans lancer d'appel IA | MVP |
| `stop_agent` | acteur | v2 |
| `resume_agent` | acteur | v2 |
| `force_unlock` | fichier ou agent | v2 |
| `reset_usage` | service IA | v2 |
| `refresh_agent_context` | acteur (republie le fichier de contexte) | v2 |

### E. Utilisateur et information

| Action | Paramètres | Lot |
|---|---|---|
| `notify_user` | texte avec variables, niveau | MVP |
| `log_history` | texte (historique des discussions) | v2 |
| `export_recap` | récapitulatif fonctionnel ou technique | v3 |

### F. Flux de contrôle

| Action | Paramètres | Lot |
|---|---|---|
| `wait` | durée | MVP |
| `if` | condition (voir § 4), étape si vrai, étape si faux | v2 |
| `goto` / `end` | étape ou résultat (`ok`/`ko`) | MVP |
| `run_workflow` | workflow, tâche cible | v2 |
| `for_each_subtask` | étape ou workflow à appliquer à chaque sous-tâche | v3 |

## 4. Conditions possibles (action `if` et `on_ok` / `on_ko` conditionnels)

- Statut de la tâche ou du bug égal à une valeur.
- Toutes les sous-tâches terminées.
- MR ouverte, fusionnable ou en conflit.
- Contrôles automatiques réussis.
- La branche de l'agent a un écart avec la release.
- Agent libre, en cours ou suspendu faute de tokens.
- Décision utilisateur en attente.
- Jour ou heure.

## 5. Exemples de workflows

**Validation d'une tâche (une étape agent, le reste en système) :**

1. `ask` principal : relire le diff, verdict ok/ko (seul vrai besoin d'IA).
2. `system` `set_task_status` → terminée (si ok).
3. `system` `merge_mr` vers la release (conflit → étape agent de l'auteur).
4. `system` `sync_all_agent_branches` source release.
5. `system` `notify_user`.

**Nettoyage nocturne (minuterie, 100 % système) :**
`sync_mrs` → `cleanup_merged_branches` → `reset_usage` → `notify_user` (résumé).

**Démarrage d'une tâche :**
`sync_agent_branch` (source release) → `set_task_status` en cours → `launch_task`.

## 6. Gains attendus

- Les étapes mécaniques ne consomment plus de tokens, et une étape système ne dépend ni du contexte ni de l'oubli de l'agent. Cela corrige aussi un défaut actuel : l'étape `ask` doit conclure par `wf_step`, sinon elle est relancée, ou considérée « ok sans verdict ».
- Le principal reste sollicité uniquement pour les jugements (revue, arbitrage).
- Les blockers fantômes se règlent par un workflow système à minuterie (`sync_mrs`).

## 7. Mise en œuvre

1. Nouveau module `system_actions.py` : registre `{id: {libellé, schéma des paramètres, gérant(ctx, params) -> (ok, commentaire)}}`. Il appelle les fonctions existantes de `engine.py`, `rights.py`, `gitlocal.py` et `github.py` (aucune logique dupliquée).
2. `engine.wf_do_step` : branche `kind == "system"` avant l'appel IA. `wf_after` gère `on_ok`.
3. `project_rules.py` : validation (`lint`), texte des workflows pour les prompts (« [système] changer le statut… » : l'agent sait que l'étape est automatique), `resolve_actor` réutilisé pour les acteurs.
4. Interface : dans l'éditeur d'étapes, sélecteur « Type : Agent / Système / Décision utilisateur », puis liste d'actions et formulaire généré depuis le schéma, avec un bouton « simuler ».
5. Rétrocompatibilité : une étape sans `kind` reste `ask`. Aucune migration des workflows existants.
6. Tests : une classe par famille d'actions, avec faux GitHub et dépôts git temporaires déjà présents dans `tests/`.

## 8. Extension possible : déclencheurs sur événement

Aujourd'hui un workflow se déclenche par l'utilisateur, un agent ou une minuterie. On pourrait ajouter des événements : tâche passée « à vérifier », MR soumise, bug déclaré, release démarrée. Combinés aux étapes système, ils automatisent des enchaînements complets sans aucun appel IA. À traiter après le MVP.

## 9. Points tranchés (25 nov. 2026)

1. **Confirmation par défaut des actions sensibles** : conservée. Case décochable par étape (« Ne jamais demander »).
2. **Périmètre** : mécanique simple d'abord. Les conditions (`if`), sous-workflows, boucles et actions git avancées restent en v2 / v3.
3. **Échec d'une étape système** : par défaut question à l'utilisateur (continuer, relancer l'étape, arrêter) ; « Arrêter le workflow » est sélectionnable par étape.

## 10. Livré (première livraison)

- **Module `system_actions.py`** : catalogue `ACTIONS`, exécution (`run_step`), validation (`lint_step`), description (`describe`), variables (`render`). Aucun appel d'IA.
- **Étape `kind: "system"`** : `action`, `target` (tâche, bug ou MR ; vide = tâche cible du déroulement), `value`, `who`, `text`, `confirm`, `on_ko` (défaut `ask_user`), `on_ok` (`next` ou `goto`) et `goto_ok`.
- **Actions** : `set_task_status`, `assign_task`, `add_task_note`, `launch_task`, `set_bug_status`, `assign_bug_fix`, `sync_agent_branch` (git local et GitHub, source `release`, `develop` ou `main` ; un conflit annule l'intégration et rend un ko), `sync_mrs`, `mark_mr_done`, `notify_agent`, `notify_user`, `wait` (1 à 600 s), `end_workflow`.
- **Confirmation** : par défaut pour une clôture de tâche (`set_task_status` → « terminée », qui contourne la vérification) et pour un bug « vérifié ». Décision « Exécuter / Arrêter le workflow », posée une seule fois par étape.
- **Moteur** : `engine.wf_do_step` (branche système), `wf_after` (`on_ok`, échec par défaut = question), `wf_continue` (action `end_workflow`), reprise après confirmation (`wf_after: sysconfirm`).
- **Prompts des agents** : l'étape est décrite « [automatique, sans IA — aucune action de ta part] » et n'est jamais proposée comme étape à réaliser.
- **Contrôle de cohérence** : action inconnue, statut invalide, cible disparue, minuterie sans tâche cible, acteur introuvable, durée, saut invalide, action sensible sans confirmation.
- **Interface** : type d'étape « Le système exécute une action », liste d'actions avec aide, cible, valeur, confirmation, « si réussite » (suite ou saut).
- **Tests** : 8 nouveaux (statuts, affectation, notes, bug, clôture avec confirmation, refus, échec par défaut ou arrêt, saut et fin, MR, synchronisation GitHub, cohérence et prompt) ; 415 au total.

### Reste à faire après la première livraison

Voir § 11 pour ce qui a été livré ensuite.

## 11. Lot v2 (partiel), livré le 25 nov. 2026

- **Condition `if`** : réussit si la condition est vraie, échoue sinon. On aiguille avec « si réussite » et « si échec » (saut ou suite). Un échec de condition continue sans question à l'utilisateur (c'est une réponse « faux », pas une erreur). Conditions : `task_status=<statut>`, `bug_status=<statut>`, `subtasks_done`, `mr_active`, `agent_free`, `decision_pending`, `release_open`.
- **Nouvelles actions** : `create_subtask` (profondeur maximale respectée, doublons refusés), `create_bug_fix_subtask`, `link_modules`, `set_task_dependency` (`add:<id>` / `remove:<id>`, dépendance circulaire refusée), `close_subtasks` (clôture : confirmation par défaut), `sync_all_agent_branches` (un échec par agent est rapporté, les agents en répertoire local sans git sont ignorés), `stop_agent`, `run_workflow` (un workflow ne peut pas se relancer lui-même ; les limites par heure s'appliquent aux boucles A→B→A).
- **Simulation** : bouton « Simuler » sur un workflow. Il décrit chaque étape dans l'historique, sans rien exécuter ni modifier : confirmation demandée, conditions évaluées sur l'état actuel, problèmes détectés, étapes d'agent signalées comme consommatrices de tokens. Les sauts ne sont pas suivis.
- **Tests** : 6 nouveaux ; 421 au total.

### Reste à faire (v3 et au-delà)

`merge_mr`, `run_checks`, `push_branch`, `create_task_branch`, nettoyage des branches fusionnées, `release_start` / `release_publish` / poussée de `main`, `develop` et des tags, `force_unlock`, `reset_usage`, `refresh_agent_context`, `log_history`, `export_recap`, boucle sur les sous-tâches, déclencheurs sur événement (§ 8).
