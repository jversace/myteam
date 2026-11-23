# Règles internes de l'application « myTeam »

> Inventaire des règles **codées en dur** dans l'application (non modifiables par l'utilisateur). Extrait du code (`engine.py`, `rights.py`, `project_rules.py`, `locks.py`, `checks.py`, `github.py`, `gitlocal.py`) et de `CLAUDE.md`, état au commit `1566fee`.
> Chaque règle a un identifiant stable (R = règle transmise aux agents, D = droit, G = git, T = tâches, E = erreurs/reprises, V = verrous, C = contrôles, S = sécurité/données) pour pouvoir y renvoyer et détecter les contradictions avec les cinq autres niveaux de règles (projet, agent, messages, décisions, workflows).
> **Source de vérité** : `RULES` dans `engine.py` pour les R01–R20 ; les autres sont des comportements du code (constantes indiquées).

## Niveau 0 — Règles du prompt (liste `RULES`, `engine.py`), transmises à tous les agents

| Id | Règle (reformulée) |
|---|---|
| R01 | Agent d'une équipe de développement ; répond en français, concis et factuel. |
| R02 | Travaille uniquement sur sa branche, jamais sur la branche de base. La release finale (fusion + tag) est faite par l'agent principal. |
| R03 | Lit/écrit via `list_files`, `read_file`, `write_file`, `delete_file` ; `write_file` remplace le fichier entier (contenu complet) ; commits clairs ; ne touche ni à `.github/` ni aux `.agents/` des autres. |
| R04 | Son fichier de contexte (`.agents/<prénom>.md` sur sa branche) fait foi : le respecter, le mettre à jour avec l'action `context` à chaque décision/convention durable. |
| R05 | Respecte ses droits ; une création soumise à validation va dans la boîte « Décisions » : continuer ce qui n'en dépend pas, ne pas la recréer. |
| R06 | Ambiguïté ou décision structurante : pas d'improvisation, `ask_user` avec 2–3 options et une recommandation, puis s'arrêter sur la tâche concernée. |
| R07 | Instructions de l'utilisateur et spécifications partagées par toute l'équipe : les respecter ; signaler manque/contradiction à l'agent principal. |
| R08 | Avancement d'un autre agent : `get_progress` d'abord (sans coût) ; message seulement pour une précision. |
| R09 | Tâche/bug rattachable à 0..n modules ; un bug se corrige par sous-tâches (`bug_fix`) → « corrigé » → vérifié par le QA. |
| R10 | Branche de travail `release/<version>` ; branche principale réservée à la release finale ; pull automatique (début de tâche, avant MR) ; MR vers la branche de travail, validée par l'agent principal ; tâche dépendante démarre quand la précédente est terminée **et** sa MR fusionnée ; conflit : la règle du projet décide (utilisateur, auteur, agent désigné), `resolve_merge` si confié. |
| R11 | Aucun environnement d'exécution local ni dépôt git local (ni `.git`, ni `MERGE_HEAD`) ; tout passe par les actions et l'API GitHub (ou par ces actions sur le répertoire local) ; lire le message de refus, ne jamais inventer de cause, ne jamais demander de « nettoyer » un environnement ; `get_blockers` avant toute question technique. |
| R12 | Règles globales, règles d'agent et workflows (écrits par l'utilisateur) priment sur l'initiative de l'agent ; un workflow se déclenche par `run_workflow` et chaque étape se conclut par `wf_step` ok/ko ; **deux règles contradictoires : appliquer la plus spécifique et signaler à l'agent principal**. |
| R13 | Coordination proactive : écrire à l'agent concerné avant de coder ce qui dépend de lui/l'impacte, le prévenir en cas de modification, répondre toujours aux messages. |
| R14 | Ne pas s'arrêter à la première erreur (analyser, corriger, continuer) ; si réellement bloqué, écrire à l'agent utile ou `ask_user` ; ne jamais laisser une tâche en suspens sans le dire. |
| R15 | Ne jamais déclarer une tâche terminée si le travail n'est pas fait : « à vérifier » + compte rendu précis ; l'agent principal (ou le QA) vérifie et clôture ; refus → corriger. |
| R16 | Sur le **répertoire local partagé** : verrouiller chaque fichier avant modification (`lock_file`, liste en une fois), libérer dès que possible (`unlock_file`), ne pas contourner un verrou ; ni pull ni MR. |
| R17 | Sur un **worktree git local** : il est à lui seul, pas de verrou, rien d'enregistré sans `commit` ; aucune commande git libre, seulement `status/commit/diff/search/sync/git_log/git_show`. |
| R18 | Ne peut ni exécuter ni modifier un contrôle automatique, seulement le **proposer** (`propose_check`) avec une explication non technique ; toute modification redemande l'approbation ; échec bloquant → corriger. |
| R19 | Une interruption (message, workflow, suivi) ne remplace pas la tâche en cours : répondre, puis reprendre sans attendre ; la section « Ta tâche en cours » fait foi, pas la mémoire. |
| R20 | Pour agir : terminer la réponse par un ou plusieurs blocs ```action```; sans bloc, simple texte. |

## Droits et autonomie (`rights.py`)

| Id | Règle |
|---|---|
| D01 | 9 capacités par rôle : `subtask, task_top, module, bug, bug_update, bug_verify, mr_review, link, check`, chacune `free | approve | no`. Surcharge par agent possible dans les données, sans interface. |
| D02 | Valeurs par défaut des rôles standard (Chef de projet, Architecte, Testeur QA : `mr_review` et `bug_verify` libres ; Rédacteur technique : bugs et contrôles interdits). À garder synchronisé avec `DEF_CAPS` de `static/index.html`. |
| D03 | L'agent principal ne peut pas descendre sous `approve` pour `task_top` et `module` ; il a toujours `mr_review`. |
| D04 | `mr_review` n'a que `free` ou `no` (pas de validation par l'utilisateur) ; repli sur `bug_verify` pour les anciens rôles. |
| D05 | `check` ne vaut jamais `free` : proposition soumise à l'utilisateur, ou interdit. |
| D06 | Autonomie du projet : `prudent` (toute création libre → validation), `balanced`, `auto` (tâches de 1er niveau et modules sans validation). |
| D07 | Sous-tâches libres sous une tâche dont l'agent est responsable (ou ancêtre) ou qu'il a créée ; partout pour le principal ; profondeur max 4 ; doublons de titre refusés ; max 8 créations par échange. |
| D08 | Une proposition `approve` ne crée rien : elle devient une décision ; le navigateur ne modifie jamais `decisions`. |
| D09 | Qui peut parler à qui : par défaut tous ; `["none"]` = personne ; sinon liste. Le principal joint tous ; profondeur de messages max 3. |
| D10 | Un rôle utilisé par un agent ne peut pas être supprimé. |

## Workflow git (GitHub et git local)

| Id | Règle |
|---|---|
| G01 | La branche principale est réservée à la release finale ; aucun agent n'écrit sur la base ni sur la branche de travail. |
| G02 | MR : agent → branche de travail ; validateur = agent principal ou rôle avec `mr_review`, jamais l'auteur ; pas de validateur possible → décision utilisateur. |
| G03 | Publication : exige un cycle actif et aucune MR ouverte/en conflit (en mode git : aucune tâche « à vérifier » non plus, tag inexistant) ; tag annoté + release. |
| G04 | Conflits : règle du projet `ask` (défaut) / `author` / `agent:<id>` ; un agent en chat manuel n'est jamais désigné automatiquement ; 2 échecs du résolveur, conflit > 8 fichiers/60 000 car. ou > 3 essais → escalade à l'utilisateur ; marqueurs de conflit dans une résolution = refus. |
| G05 | « Blockers fantômes » : l'application resynchronise les MR avec GitHub **sans IA** (au plus toutes les 120 s par MR, aussi quand le suivi est coupé, et avant tout refus lié à une MR) ; MR fusionnée/fermée → retirée des blocages. Erreur réseau ou MR introuvable : ne rien conclure. Bouton manuel « Marquer terminée ». Une MR terminée/annulée n'est jamais rouverte. |
| G06 | Dépendances : une tâche ne démarre pas tant qu'une dépendance n'est pas terminée ou qu'une MR de cette dépendance reste ouverte/en conflit. |
| G07 | Mode git local (réglage `nogit` = false, **désactivé par défaut**) : un worktree par agent, une branche `task/<prénom>/<id>-<titre>` par tâche (stratégie par défaut), jamais de `push/checkout/reset/rebase` côté agent, intégration `--no-ff` sous verrou global, jamais de force-push, remote injoignable = fusion locale gardée + avertissement. Dossier inutilisable → toutes les actions de l'agent refusées. |
| G08 | Release en mode git : `release/<version>` depuis `develop` ; publication → fusion dans la base, contrôles d'intégration (échec bloquant = retour arrière), tag annoté, report dans `develop`. |
| G09 | Appels GitHub : 3 nouvelles tentatives sur 429 / 403 « rate limit » (toute méthode) ; 5xx et réseau seulement en GET ; attente max 30 s ; 403 de permission non réessayé. |

## Tâches, vérification, décisions, suivi

| Id | Règle |
|---|---|
| T01 | Statuts : à faire, en cours, en attente, à vérifier, terminée. Une tâche n'est **jamais « terminée » d'office** : fin de travail ou `task_status terminée` d'un agent → « à vérifier ». Seuls le principal, un agent `bug_verify` libre (jamais l'assigné) ou l'utilisateur la clôturent. La validation manuelle dans l'interface contourne la vérification. |
| T02 | Vérification : preuves jointes (commits depuis le début) ; refus avec motif obligatoire → « à faire » ; 3ᵉ refus → escalade ; vérificateur muet 2 fois ou absent → décision utilisateur. |
| T03 | Bugs : `ouvert → en correction → corrigé → vérifié | rouvert` ; tâche système « Corrections : <module> » ; le bug passe à « corrigé » quand toutes ses sous-tâches sont terminées ; un changement fait à la main dans l'IHM ne déclenche pas cette mise à jour. |
| T04 | Décisions : toute décision passe par `add_decision` ; une décision plus récente sur le même sujet (même clé, question proche, ou `replaces`) **remplace** les anciennes en attente ; la précision saisie avec une option est prise en compte (« option — précision de l'utilisateur : … »). |
| T05 | Instructions « annule et remplace » : écrites seulement via `add_instruction` ; une instruction plus récente de même clé ou listée dans `replaces` désactive l'ancienne ; consolidation à la demande. |
| T06 | Question `ask_user` liée à une tâche → tâche « en attente » ; la réponse la relance automatiquement. |
| T07 | Lancement automatique (réglage `autopilot`) : une tâche « à faire » assignée démarre seule si dépendances satisfaites, agent libre (une tâche à la fois), pas en chat manuel, pas de conflit/échec en attente, budget non atteint ; max 8 lancements par tâche puis escalade. |
| T08 | Suivi (réglage `supervision`) : tâche inactive 10 min → question à l'agent principal (un seul par passage, délai de carence 10 min, max 3 relances) ; après 2 interventions sans succès ou principal en chat manuel → décision utilisateur. |
| T09 | Workflows : max 20 étapes exécutées par déroulement, max 10 déroulements/heure, pas de doublon sur la même tâche ; minuterie quotidienne manquée > 6 h non rattrapée ; étape sans verdict → `ok` « sans verdict explicite ». |
| T10 | Analyse de cohérence : sans mémoire d'agent ; entre deux instructions contradictoires la plus récente (n° le plus élevé) l'emporte ; instructions et décisions actives sont **plus récentes** que les règles. |

## Erreurs, reprises, coûts

| Id | Règle |
|---|---|
| E01 | Quota (429/529) et erreur temporaire → agent « suspendu », reprise automatique (délai du fournisseur puis 5, 15, 30 min, puis horaire) ; temporaire abandonné après 6 essais ; quota sans limite. |
| E02 | Budget (402), authentification, modèle → escalade directe à l'utilisateur ; autre erreur → 2 nouvelles tentatives puis échec ; erreur interne → échec + escalade. |
| E03 | Interruption : marquage + reprise automatique (max 3), **sauf** décision en attente, tâche bloquée/arrêtée/en échec, conflit ouvert, dépendances non satisfaites, agent manuel, `autopilot` off. |
| E04 | Tâche persistante : jusqu'à 6 tours de 8 étapes ; deux tours sans action = travail rendu (« à vérifier »). Dialogue direct : jusqu'à 4 échanges, puis « écris continue ». |
| E05 | Plafond de budget `p.budget` : tout appel API refusé (402) quand le coût l'atteint. |
| E06 | Économie de tokens : prompt en 3 blocs (stable / rare / changeant) ; **rien de changeant dans les blocs 1 et 2** ; historique abrégé (retours d'actions à 400 car., sauf les 2 derniers) ; diff fourni aux validateurs. |

## Verrous du répertoire local (`locks.py`)

| Id | Règle |
|---|---|
| V01 | Écriture/suppression exigent un verrou (éphémère si non demandé) ; lecture libre avec avertissement ; chemins bornés au répertoire (liens symboliques compris) ; écriture interdite dans `.git/`, `.github/`, `.agents/`. |
| V02 | Verrous exclusifs, tout ou rien (10 fichiers max), file équitable, attente max 90 s. |
| V03 | Secours : verrou circulaire refusé, expiration 15 min sans activité, orphelins 2 min, libération au démarrage et en fin de tâche, `stop_agent` (avec `cancel` : fichiers remis à l'état d'avant), `force_unlock` refusé si le détenteur travaille. |

## Contrôles automatiques (`checks.py`)

| Id | Règle |
|---|---|
| C01 | Seul l'utilisateur active un contrôle ; le **texte exact** approuvé est exécuté ; toute modification redemande l'approbation et remet la référence à zéro. |
| C02 | Vigilance calculée par l'application (jamais par l'agent) ; refus d'office : sudo, téléchargement exécuté, `git push`/config globale, ssh/nc, suppression hors dossier, arrêt machine, PowerShell encodé… |
| C03 | Exécution : délai max (3 600 s, défaut 300), sortie limitée à 2 500 car., environnement nettoyé (ni jeton ni clé), une exécution à la fois ; bloquant **seulement** si déclaré bloquant ET de référence verte. |
| C04 | Échec bloquant à la soumission : 2 essais puis transmis au validateur ; à l'intégration : retour arrière, MR « conflit » ; tâche renvoyée à l'agent jusqu'à 3 fois. |

## Données, concurrence, sécurité

| Id | Règle |
|---|---|
| S01 | Fusion à trois voies navigateur/serveur ; en vrai conflit **le navigateur gagne** ; tout élément de liste nouveau doit avoir un `id` ; les champs écrits par le serveur ne sont jamais modifiés par le front. |
| S02 | Jeton GitHub et clés API : jamais dans git (`data/` ignoré), jamais dans `.git/config` (passage par l'environnement), jamais transmis aux contrôles. |
| S03 | On n'automatise pas les sites de chat grand public (claude.ai, chatgpt.com…) : chat manuel (24 h d'attente max) ou API/modèle local. |
| S04 | Rôles et services IA sont **globaux** (réutilisables par tous les projets). |

---

## Contradictions et recouvrements détectés (à trancher)

1. **R02 / R03 / R04 / R10 vs R11 / R16 / R17** — le texte est écrit pour le mode « GitHub » (« ta branche », « pull », « MR », fichier de contexte sur la branche), alors que les modes local et git local fonctionnent autrement. **R11** dit « aucun dépôt git local » alors que **R17** décrit un worktree git local : contradiction frontale si les trois blocs sont lus ensemble ; seule la section « Dépôt et intégration » du prompt les départage.
2. **Emplacement du fichier de contexte (R04)** : `.agents/<prénom>.md` sur la branche (GitHub), `<localdir>/.agents/` (local), `<localdir>/.myteam/agents/` hors branches (git local).
3. **Qui valide les MR** : R02/R10 disent « l'agent principal », alors que D04/G02 l'étendent à tout rôle ayant `mr_review`.
4. **Deux règles de priorité différentes** : R12 « la plus **spécifique** l'emporte » ; T10 « la plus **récente** l'emporte » (et instructions/décisions > règles). Un conflit entre une règle globale récente et une règle d'agent ancienne n'est pas défini.
5. **R06 (s'arrêter sur la tâche) vs R19 (reprendre sans attendre)** : l'exception « sauf décision en attente » n'existe que dans le code (E03), pas dans le texte de R19.
6. **R14 (ne pas s'arrêter) vs R06 (ne pas improviser)** : la frontière entre erreur technique et décision structurante n'est pas définie.
7. **R15 vs T01** : la règle dit « le principal ou le QA vérifie » ; le code exige `bug_verify` libre et exclut l'assigné ; l'utilisateur peut contourner depuis l'IHM.
8. **R10 / G03** : la publication est décrite comme faite par l'agent principal, mais en mode git elle est exécutée par l'application (G08).
9. **D02 vs D01** : les droits par défaut sont dupliqués dans `rights.py` et `static/index.html` (risque de divergence).
