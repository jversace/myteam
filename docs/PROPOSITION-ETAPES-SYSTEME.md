# Proposition — étapes de workflow exécutées par le système (sans IA)

*Statut : proposition du 24 nov. 2026, en attente de validation. Rien n'est implémenté.*

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

## 9. Points à trancher

1. **Confirmation par défaut** des actions sensibles : à garder (recommandé), ou autonomie totale ?
2. **Périmètre du MVP** : mécanique simple (statuts, affectation, notifications, attente, MR et branches) puis conditions en v2, ou conditions dès le MVP ?
3. **Échec d'une étape système** : par défaut question à l'utilisateur (recommandé), ou arrêt du workflow ?
