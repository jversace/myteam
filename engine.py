"""Moteur d'agents côté serveur : dialogues, actions, tâches, onboarding, releases.

Tout s'exécute dans des threads du serveur : le travail continue navigateur fermé.
L'état est modifié via store.mutate (lecture-modification-écriture atomique)."""
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
from datetime import datetime

import github
import gitlocal
import checks
import locks
import project_rules as prules
import system_actions as sysact
import rights
import rulebook
from ai import call_ai, System
from rights import by_id, mod_ids, now, uid, roles_of
from store import DATA, Err, load, mutate

MAX_STEPS, MAX_DEPTH, MAX_CREATE = 6, 3, 8
SAY_ROUNDS = 4                                      # échanges successifs d'un dialogue direct tant que l'agent n'a pas fini (reprise du retour non traité)   # tours par échange, profondeur des messages, créations par échange
MAX_TEMP = 6                          # tentatives automatiques après erreur temporaire avant de remonter le problème
BACKOFF = [0, 300, 900, 1800, 3600]   # délais minimaux entre relances (s) ; ensuite : toutes les heures
STALL_SECONDS, SUP_COOLDOWN, STALE_RUNNING = 600, 600, 900   # inactivité, délai entre deux interventions, exécution « fantôme »
MAX_REVIEWS, MAX_RELAUNCH = 3, 3      # refus de vérification, relances par l'agent principal
SUP_INTERVAL = int(os.environ.get("APM_SUPERVISE_SECONDS", "60"))   # 0 = suivi périodique désactivé
TIMERS = set()                        # reprises planifiées : (utilisateur, projet, tâche)
TICKING = set()                       # passages du suivi en cours : (utilisateur, projet)
MAX_OTHER = 2                         # nouvelles tentatives automatiques après une erreur inattendue du service
MAX_ROUNDS, MAX_ROUND_STEPS, MAX_AUTO = 6, 8, 8   # tours par tâche, étapes par tour, lancements automatiques par tâche
NUDGE = ("Tu n’as pas déclaré la tâche terminée. Si le travail demandé est réellement fait, conclus avec task_status « à vérifier » et un compte rendu précis. "
         "Sinon poursuis-le maintenant avec les actions ; si tu es bloqué, écris à l’agent concerné (message) ou pose la question à l’utilisateur (ask_user). "
         "Ne t’arrête pas sur une erreur : analyse-la, corrige et continue.")
MISSING_WARN = 3                                    # vérifications « MR introuvable sur GitHub » avant d'avertir l'utilisateur
MAX_RESUME = 3                                      # reprises automatiques d'une tâche après une interruption (message, workflow…) avant d'attendre l'utilisateur
OPEN_CONFLICT, RECONCILE_EVERY = ("ouvert", "escalade", "pause"), 300   # statuts « actifs » d'un conflit ; délai entre deux vérifications (s)
CONFLICT_OPTS = ["L’auteur de la branche résout", "L’agent principal résout", "Je m’en occupe (mettre en pause)"]
MR_ACTIVE, MR_SYNC_EVERY = ("ouverte", "conflit"), 120   # statuts d'une MR qui bloque ; délai entre deux vérifications GitHub d'une même MR (s)
MANUAL_TIMEOUT = 24 * 3600   # attente maximale d'une réponse manuelle
WAIT = {}                    # échanges manuels en attente : id -> {u, ev, text}
F = "`" * 3
# Règles par défaut : socle commun + module par mode, avec sujets (voir rulebook.py). Les agents reçoivent rulebook.locked_core()
# (bloc partagé) puis rulebook.agent_rules(mode, sujets remplacés) ; RULES = tout, pour l'analyse de cohérence.
RULES = rulebook.all_lines()
ACTION_HEAD = "Actions (un objet JSON par bloc %saction ... %s) :" % (F, F)
ACTION_LINES = [
    '{"do":"list_files","path":"<dossier, vide pour la racine>","agent":"<prénom ou base, facultatif>"} : lister un dossier du dépôt (ta branche, ou en lecture seule celle d’un autre agent)',
    '{"do":"read_file","path":"...","agent":"<prénom ou base, facultatif>","from_line":1,"to_line":80} : lire un fichier du dépôt (ta branche, ou en lecture seule celle d’un autre agent) ; 12 000 caractères maximum par lecture : au-delà, lis la suite avec from_line/to_line ou offset/limit (en caractères)',
    '{"do":"write_file","path":"...","content":"<contenu complet>","message":"<message de commit>"} : créer ou remplacer un fichier sur ta branche (refusé si le contenu est bien plus court que le fichier existant : c’est probablement une troncature ; utilise edit_file, ou confirm_shrink:true si c’est voulu)',
    '{"do":"edit_file","path":"...","edits":[{"old":"<texte exact existant>","new":"<remplacement>"}],"message":"<message de commit>"} : modifier un fichier existant par remplacements ; « old » doit apparaître exactement une fois (sinon allonge-le avec du contexte, ou all:true) ; tout ou rien ; pour insérer, reprends une ancre dans « old » et ajoute le texte dans « new »',
    '{"do":"write_chunk","path":"...","index":1,"total":3,"content":"<morceau>","message":"<message de commit>"} : écrire un gros fichier en plusieurs morceaux (1 à 50, dans n’importe quel ordre) ; dès que tous sont reçus, l’application reconstitue et écrit le fichier, puis te donne sa taille et son empreinte sha256 ; sha256 facultatif pour contrôle',
    '{"do":"delete_file","path":"...","message":"..."} : supprimer un fichier sur ta branche',
    '{"do":"lock_file","paths":["<fichier>"],"note":"<pourquoi, facultatif>"} : (répertoire local) verrouiller des fichiers avant de les modifier ; attend que les autres agents les libèrent (jusqu’à 90 s), tout ou rien',
    '{"do":"unlock_file","paths":["<fichier>"]} : (répertoire local) libérer des fichiers verrouillés (sans « paths » : tous les tiens)',
    '{"do":"get_locks"} : (répertoire local) fichiers verrouillés, par qui, et agents en attente (sans coût)',
    '{"do":"stop_agent","agent":"<prénom>","cancel":false,"reason":"..."} : (agent principal) arrêter le travail d’un agent et libérer tous ses verrous ; cancel=true annule aussi ses modifications sur les fichiers qu’il avait verrouillés (retour à l’état d’avant)',
    '{"do":"force_unlock","path":"<fichier>","reason":"..."} : (agent principal) libérer de force le verrou d’un fichier (détenteur inactif ou plantage) ; si le détenteur travaille encore, utilise stop_agent',
    '{"do":"propose_check","name":"tests","command":"python -m unittest discover -s tests","when":"both","blocking":true,"explain":"<explication SIMPLE, sans jargon : ce que ça vérifie, ce que ça modifie, pourquoi>","reason":"<pourquoi maintenant>"} : proposer un contrôle automatique (tests, lint) exécuté par l’application à ta soumission (when : validation | integration | both) ; l’utilisateur l’approuve, rien ne s’exécute avant ; proposer le même name avec une autre commande = demander une modification',
    '{"do":"status"} : (git local) fichiers modifiés non commités de ton worktree',
    '{"do":"commit","message":"<message clair>"} : (git local) enregistrer tout ton worktree dans un commit (local)',
    '{"do":"diff"} : (git local) tes changements par rapport à la branche cible',
    '{"do":"search","pattern":"<texte>"} : (git local) chercher un texte dans les fichiers de ton worktree (évite de lire fichier par fichier)',
    '{"do":"sync"} : (git local) intégrer la branche cible (celle de la release, sinon develop) dans ta branche ; commit d’abord ; en cas de conflit, résous les marqueurs puis commit ; {"do":"sync","abort":true} pour renoncer',
    '{"do":"git_log","n":20} : (git local) derniers commits de ta branche',
    '{"do":"git_show","rev":"<branche ou commit>","path":"<fichier, facultatif>"} : (git local) lire un commit ou une version d’un fichier (ex. la branche d’un autre agent)',
    '{"do":"pull"} : récupérer dans ta branche les changements de la branche de travail de la release (fusion ; en cas de conflit, la règle du projet s’applique)',
    '{"do":"merge_request","title":"...","body":"...","task":"<id de la tâche, facultatif>"} : soumettre ta branche à la branche de travail de la release (l’agent principal la valide)',
    '{"do":"mr_review","id":"<id de la MR>","verdict":"approved|rejected","comment":"..."} : (agent principal, ou tout agent dont le rôle a le droit « valider les merge requests » ; jamais ta propre MR) valider (fusion) ou refuser une merge request',
    '{"do":"resolve_merge","conflict":"<id>","files":[{"path":"...","content":"<contenu final complet, ou null pour supprimer>"}],"message":"..."} : résoudre un conflit de fusion qui t’est confié (un élément par fichier en conflit)',
    '{"do":"message","to":"<prénom>","text":"..."} : écrire à un agent joignable (sa réponse te revient)',
    '{"do":"get_progress","agent":"<prénom ou all>"} : avancement d’un agent ou de toute l’équipe (sans coût)',
    '{"do":"run_workflow","workflow":"<nom du workflow>","task":"<id de la tâche cible, facultatif>","note":"..."} : déclencher un workflow du projet (s’il autorise le déclenchement par un agent)',
    '{"do":"wf_step","outcome":"ok|ko","comment":"..."} : conclure l’étape de workflow qui t’est confiée (ok = réalisée)',
    '{"do":"get_blockers"} : liste FACTUELLE des blocages du projet (conflits, dépendances, erreurs, décisions en attente) — à consulter avant toute question technique',
    '{"do":"context","text":"..."} : réécrire tes notes personnelles (fichier de contexte)',
    '{"do":"task","title":"...","desc":"...","assignee":"<prénom>","parent":"<id de la tâche parente, vide = 1er niveau>","modules":["<module>"],"depends_on":["<id d’une tâche à terminer avant>"],"why":"<justification>"} : créer une tâche (sous-tâche libre sous tes tâches ; 1er niveau selon tes droits)',
    '{"do":"task_status","id":"<id>","status":"à faire|en cours|en attente|à vérifier","note":"<compte rendu>"} : statut d’une de tes tâches (« à vérifier » = fini de ton côté ; la clôture se fait par vérification)',
    '{"do":"task_review","id":"<id>","verdict":"accepted|rejected","comment":"<ce qui manque, si rejected>"} : (agent principal / QA) vérifier une tâche « à vérifier » : accepted seulement si le travail est réellement fait',
    '{"do":"relaunch","task":"<id>","note":"<consigne>"} : (agent principal) relancer l’agent d’une tâche bloquée',
    '{"do":"module","name":"...","desc":"...","why":"..."} : créer un module (selon tes droits)',
    '{"do":"link_modules","kind":"task|bug","id":"<id>","modules":["<module>"]} : rattacher une tâche ou un bug à des modules (liste vide = global)',
    '{"do":"bug","title":"...","desc":"...","sev":"mineur|majeur|critique","modules":["<module>"]} : déclarer un bug (selon tes droits)',
    '{"do":"bug_fix","bug":"<id>","title":"...","desc":"...","assignee":"<prénom>"} : créer une sous-tâche de correction d’un bug',
    '{"do":"bug_status","id":"<id>","status":"en correction|corrigé|vérifié|rouvert"} : faire évoluer un bug (vérifié/rouvert : droit de vérification)',
    '{"do":"ask_user","question":"...","options":["A","B"],"recommendation":"...","impact":"...","structural":true,"task":"<id de la tâche bloquée, ou vide>","replaces":[<n° des décisions en attente auxquelles cette question répond aussi>]} : poser une question à l’utilisateur (ambiguïté, décision structurante) ; si elle répond aussi à une question déjà en attente, indique son numéro dans replaces : l’ancienne est annulée et remplacée',
    '{"do":"instruction","text":"...","scope":"global|role:<rôle>|agent:<prénom>|module:<module>","subject":"<sujet facultatif>"} : (agent principal) enregistrer une nouvelle instruction de l’utilisateur ; subject (langue, ambiguite, interruption, coordination, fichier_contexte, fichiers) fait remplacer la règle par défaut correspondante',
    '{"do":"instr_rewrite","items":[...],"obsolete":[...]} : (uniquement lors d’une consolidation demandée par l’application) liste consolidée des instructions',
    '{"do":"spec","text":"<spécifications complètes en markdown>"} : (agent principal) remplacer les spécifications']
ACTIONS = ACTION_HEAD + "\n" + "\n".join(ACTION_LINES)           # liste complète (référence) : les agents reçoivent la liste filtrée, voir actions_text


def _aname(line):
    return re.match(r'\{"do":"(\w+)"', line).group(1)


# Actions toujours proposées (bloc de prompt partagé) ; les autres dépendent des droits, du mode et du projet (voir action_applies).
ACTIONS_ALWAYS = ("list_files", "read_file", "write_file", "edit_file", "write_chunk", "delete_file", "get_blockers", "context", "task_status", "ask_user")
ACTIONS_HIDDEN = ("instr_rewrite",)                # décrite dans la demande de consolidation, jamais dans le prompt courant
GIT_ONLY = ("status", "commit", "diff", "search", "sync", "git_log", "git_show")


def local_agent(p, a):
    """Vrai si l'agent travaille sur le répertoire local du projet plutôt que sur sa branche GitHub."""
    return a.get("source") == "local"


GIT_LOCK = threading.RLock()                           # initialisation du dépôt, création des worktrees : jamais deux en même temps
GIT_OK = {}                                            # (utilisateur, projet, dossier, remote, base) -> dépôt validé (contrôles préalables passés)
GIT_INTEGRATION = "develop"                            # branche d'intégration quand aucune release n'est ouverte
INTEGRATION_LOCK = threading.RLock()                   # une intégration (merge, contrôles, push) à la fois, sans bloquer le travail des agents
CHECK_TRIES = 2                                        # échecs bloquants à la soumission avant transmission au validateur
MAX_CHECK_RUNS = 40
GIT_KINDS = ("status", "diff", "commit", "search", "sync", "git_log", "git_show")


def git_mode(p):
    """Mode « git local » : répertoire local versionné, un worktree par agent (docs/SPEC-git-local.md). Réglage de projet `nogit` :
    absent ou vrai = sans git (verrous de fichiers, comportement historique) ; faux = git local. Désactivé par défaut."""
    return bool(str(p.get("localdir") or "").strip()) and p.get("nogit") is False


def git_agent(p, a):
    return local_agent(p, a) and git_mode(p)


def agent_mode(p, a):
    """Mode de travail de l'agent pour le choix du module de règles : git, local ou github."""
    return rulebook.mode_of(local_agent(p, a), git_agent(p, a))


def action_applies(p, a, name):
    """L'action `name` est-elle utile à cet agent ? Selon son mode (github / local / git), ses droits, son statut d'agent principal et
    le projet (autres agents joignables, workflows). Une action non proposée reste traitée par le code, qui la refuse avec un motif."""
    mn = p.get("mainId") == a.get("id")
    mode = agent_mode(p, a)
    lv = lambda cap: rights.level(p, a, cap, mn)
    others = [x for x in p.get("agents", []) if x.get("id") != a.get("id")]
    if name in ACTIONS_ALWAYS:
        return True
    if name in ACTIONS_HIDDEN:
        return False
    if name in ("lock_file", "unlock_file", "get_locks"):
        return mode == "local"
    if name == "force_unlock":
        return mn and any(local_agent(p, x) and not git_agent(p, x) for x in p.get("agents", []))
    if name in ("stop_agent", "relaunch", "instruction", "spec"):
        return mn
    if name in GIT_ONLY:
        return mode == "git"
    if name == "propose_check":
        return mode == "git" and lv("check") != rights.NO
    if name in ("pull", "merge_request"):
        return mode == "github"
    if name == "resolve_merge":
        return mode != "local"
    if name == "mr_review":
        return mode != "local" and lv("mr_review") == rights.FREE
    if name == "message":
        return any(can_talk(a, x, mn) for x in others)
    if name == "get_progress":
        return bool(others)
    if name in ("run_workflow", "wf_step"):
        return any(w.get("enabled", True) for w in p.get("workflows", []))
    if name == "task":
        return lv("subtask") != rights.NO or lv("task_top") != rights.NO
    if name == "task_review":
        return mn or lv("bug_verify") == rights.FREE
    if name == "module":
        return lv("module") != rights.NO
    if name == "link_modules":
        return lv("link") != rights.NO
    if name == "bug":
        return lv("bug") != rights.NO
    if name == "bug_fix":
        return mn or lv("bug") != rights.NO or lv("subtask") != rights.NO
    if name == "bug_status":
        return lv("bug_update") != rights.NO or lv("bug_verify") != rights.NO
    return True                                        # action inconnue de cette liste : proposée par prudence


def actions_common():
    """Actions proposées à tous les agents : identiques pour tous, donc dans le bloc de prompt partagé (mis en cache)."""
    return ACTION_HEAD + "\n" + "\n".join(l for l in ACTION_LINES if _aname(l) in ACTIONS_ALWAYS)


def actions_extra(p, a):
    """Autres actions utiles à cet agent (bloc propre à l'agent)."""
    L = [l for l in ACTION_LINES if _aname(l) not in ACTIONS_ALWAYS and action_applies(p, a, _aname(l))]
    return ("Autres actions à ta disposition :\n" + "\n".join(L)) if L else ""


def actions_text(p, a):
    """Liste complète des actions de cet agent (fichier de contexte)."""
    return ACTION_HEAD + "\n" + "\n".join(l for l in ACTION_LINES if action_applies(p, a, _aname(l)))


def agent_rules_lines(p, a):
    """Règles par défaut propres à l'agent : règles modifiables non remplacées par une règle de l'utilisateur + module du mode."""
    return rulebook.agent_rules(agent_mode(p, a), rights.overridden_subjects(p, a))


def git_target(p):
    """Branche vers laquelle les agents se synchronisent : la branche de travail de la release en cours, sinon la branche d'intégration."""
    return (p.get("cycle") or {}).get("branch") or GIT_INTEGRATION


def git_strategy(p):
    """Stratégie de branches du projet : « task » (une par tâche, défaut en mode git) ou « agent » (une par agent, historique)."""
    return "agent" if p.get("branches") == "agent" else "task"


def git_task_of(p, a):
    """Tâche en cours de l'agent (la plus récente), sinon None."""
    ts = [t for t in p.get("tasks", []) if t.get("who") == a["id"] and t.get("status") == "en cours"]
    return max(ts, key=lambda t: t.get("started", 0)) if ts else None


def git_agent_branch(a):
    return (a.get("branch") or "").strip() or "agent/" + gitlocal.slug(a["name"], "agent", 30)


def git_branch_hint(p, a):
    """Nom de la branche de l'agent pour les messages (sans appeler git)."""
    if git_strategy(p) == "agent":
        return git_agent_branch(a)
    t = git_task_of(p, a)
    return gitlocal.task_branch(a["name"], t["id"], t["title"]) if t else "task/%s/<tâche> (créée automatiquement à chaque tâche)" % gitlocal.slug(a["name"], "agent", 20)


def git_branch(lg, p, a, tgt, notes=None):
    """Branche voulue pour l'agent, ou None = garder celle de son worktree.
    - « agent » : sa branche historique ;
    - « task » : tant que son ancienne branche (a["branch"]) porte du travail non fusionné dans la cible, il la garde (branche HÉRITÉE : elle doit être
      fusionnée avant de passer aux branches par tâche) ; ensuite, une branche `task/<prénom>/<id>-<titre>` par tâche en cours."""
    if git_strategy(p) == "agent":
        return git_agent_branch(a)
    old = (a.get("branch") or "").strip()
    if old and old not in (lg.base, tgt, GIT_INTEGRATION) and lg.unmerged(old, tgt):
        lg.ensure_local(old, tgt)
        ts = [t for t in p.get("tasks", []) if t.get("who") == a["id"] and t.get("status") == "en cours"]
        if len(ts) == 1 and not any(m.get("from") == old and m.get("status") in MR_ACTIVE for m in p.get("mrs", [])):
            new = gitlocal.task_branch(a["name"], ts[0]["id"], ts[0]["title"])        # une seule tâche en cours, aucune MR ouverte : l'ancienne branche devient sa branche
            if not lg.branch_exists(new):
                lg.rename_branch(old, new)
                if notes is not None:
                    notes.append("Branche héritée %s renommée en %s (une seule tâche en cours : son travail non fusionné lui appartient)." % (old, new))
                return new
        return old
    t = git_task_of(p, a)
    return gitlocal.task_branch(a["name"], t["id"], t["title"]) if t else None


def git_repo(u, pid, p):
    """Dépôt git local du projet : contrôles préalables puis initialisation, une seule fois (sous verrou) ; Err 409 si le dossier est inutilisable."""
    repo = p.get("repo") or {}
    lg = gitlocal.LocalGit(str(p["localdir"]).strip(), remote=(repo.get("url") or "").strip(), token=repo.get("token") or "",
                           base=repo.get("branch") or "main", integration=GIT_INTEGRATION)
    key = (u, pid, lg.root, lg.remote, lg.base)
    with GIT_LOCK:
        if key not in GIT_OK:
            pf = gitlocal.preflight(lg.root, lg.remote)
            if not pf["ok"]:
                raise Err(409, "Répertoire local inutilisable pour git : %s" % " ".join(pf["problems"]))
            GIT_OK[key] = lg.init()
            lg.recover()
    return lg


def git_workspace(u, pid, p, a):
    """(dépôt, chemin du worktree de l'agent). Le worktree est créé au premier usage, depuis la cible, sur la branche voulue (`git_branch`) ; quand une
    nouvelle tâche démarre, il change de branche (travail non commité enregistré en « WIP », ancienne branche supprimée si elle est fusionnée).
    Sans tâche en cours, il reste sur sa branche ; sans worktree, il prend la branche libre `task/<prénom>/libre`."""
    lg = git_repo(u, pid, p)
    tgt, notes = git_target(p), []
    with GIT_LOCK:
        lg.ensure_local(tgt, GIT_INTEGRATION if tgt != GIT_INTEGRATION else lg.base)
        cur = lg.worktrees().get(os.path.realpath(lg._wt_path(a["name"])))
        br = git_branch(lg, p, a, tgt, notes) or cur or gitlocal.task_branch(a["name"], "libre")
        if br in (lg.base, tgt, GIT_INTEGRATION):
            raise Err(403, "Ta branche (%s) est une branche de base ou de travail : l'écriture y est interdite." % br)
        path, prev = lg.switch(a["name"], br, tgt, agent=a["name"])
        if prev and prev.startswith("task/") and prev != br and lg.merged(prev, tgt):
            lg.delete_branch(prev)
    for n in notes:
        log(u, pid, "Système", a["name"], n)
    return lg, path


def git_source(lg, p):
    """Référence à intégrer lors d'une synchronisation : la cible du remote si elle existe (après fetch), sinon la cible locale."""
    tgt = git_target(p)
    if lg.remote:
        try:
            lg.fetch()
        except Err:
            pass
        if lg.has("origin/" + tgt):
            return "origin/" + tgt
    return tgt


def is_main(p, a):
    return p.get("mainId") == a["id"]


def role(p, a):
    return by_id(roles_of(p), a.get("role")) or {"name": "—", "desc": ""}


def mod_names(p, ids):
    return [m["name"] for i in ids or [] for m in p.get("modules", []) if m["id"] == i]


def snap(u, pid):
    st = load(u)
    p = by_id(st.get("projects", []), pid)
    if not p:
        raise Err(404, "Projet introuvable")
    p["_svc"] = st.get("services", [])
    return p


def mp(u, pid, fn):
    def f(st):
        p = by_id(st.get("projects", []), pid)
        if p is None:
            raise Err(404, "Projet introuvable")
        return fn(p)
    return mutate(u, f)


def log(u, pid, frm, to, text):
    mp(u, pid, lambda p: p.setdefault("log", []).append({"d": now(), "from": frm, "to": to, "text": text}))


def work(u, pid, aid, d):
    def f(p):
        a = by_id(p["agents"], aid)
        if a:
            a["work"] = max(0, a.get("work", 0) + d)
    mp(u, pid, f)


def record_commit(u, pid, by, msg, sha):
    mp(u, pid, lambda p: p.setdefault("commits", []).append({"m": msg, "by": by, "d": now(), "ts": time.time(), "sha": sha[:7]}))


def ctx_text(p, a):
    r, mn = role(p, a), is_main(p, a)
    tk = a.get("talk") or []
    talk = "tous" if (mn or not tk) else ("aucun" if "none" in tk else ", ".join(g["name"] for g in p["agents"] if g["id"] in tk))
    return "\n".join([
        "# Contexte de l’agent %s — projet %s" % (a["name"], p["name"]), "",
        "> Ce fichier fait foi : respecte-le rigoureusement et mets-le à jour (action « context »).", "",
        "## Règles de l’équipe", "\n".join("- " + x for x in rulebook.locked_core() + agent_rules_lines(p, a)),
        ("\n## Règles du projet\n" + prules.shared_rules_text(p)) if prules.shared_rules_text(p) else "",
        ("\n" + prules.subject_rules_text(p, a, "##")) if prules.subject_rules_text(p, a, "##") else "",
        ("\n" + prules.agent_rules_text(p, a, "##")) if (a.get("rules") or "").strip() else "", ("\n" + prules.workflows_text(p, a, "##")) if prules.workflows_text(p, a) else "",
        "\n## Mon rôle\n%s — %s" % (r["name"], r["desc"]),
        ("\n## Mon espace de travail\nWorktree git local personnel (branche %s) : pas de verrou, tes modifications ne comptent qu'après commit." % git_branch_hint(p, a)) if git_agent(p, a) else ("\n## Mon espace de travail\nRépertoire local du projet : %s (partagé : verrouille les fichiers avant de les modifier). Pas de branche, pas de pull ni de merge request." % (p.get("localdir") or "non défini")) if local_agent(p, a) else "\n## Ma branche\n" + a["branch"] + ("\n\nAgent principal : écrit à tous les agents, crée les tâches, tient SPEC.md à jour, réalise les merge requests et le tag à chaque release." if mn else ""),
        "\n## Droits\n%s\n- Agents joignables : %s" % (rights.rights_text(p, a, mn), talk),
        "\n## " + actions_text(p, a), "\n## Notes de l’agent (à tenir à jour)\n" + (a.get("notes") or "(vides)")])


def can_talk(a, t, mn):
    """Un agent peut écrire à tous les autres, sauf restriction explicite (liste d'agents, ou « none » = personne)."""
    tk = a.get("talk") or []
    return bool(mn or ("none" not in tk and (not tk or t["id"] in tk)))


def teamwork_text(p, a):
    oth = [t for t in p.get("tasks", []) if t.get("who") and t["who"] != a["id"] and t["status"] != "terminée" and not t.get("system")][:40]
    if not oth:
        return ""
    who = lambda t: (by_id(p["agents"], t["who"]) or {}).get("name", "?")
    dep = lambda t: (" — dépend de : " + ", ".join((by_id(p["tasks"], i) or {}).get("title", "?") for i in t["deps"])) if t.get("deps") else ""
    return "# Travail des autres agents (pour te coordonner)\n" + "\n".join("- %s : « %s » (%s)%s" % (who(t), t["title"], t["status"], dep(t)) for t in oth)


def checks_prompt(p):
    cs = [c for c in p.get("checks", []) if c.get("active", True)]
    if not cs:
        return "Aucun contrôle automatique n’est configuré : si le projet a des tests ou un lint, propose-les (propose_check).\n"
    return "Contrôles automatiques actifs (exécutés par l’application à ta soumission) :\n" + "\n".join(
        "- %s : %s (%s, %s%s)" % (c["name"], c["command"], {"validation": "à la validation", "integration": "à l’intégration", "both": "à la validation et à l’intégration"}[c.get("when", "both")],
                                 "bloquant" if c.get("blocking") and c.get("baseline") == "green" else "informatif" + (" — pas encore bloquant" if c.get("blocking") else ""), ", modifiable par proposition")
        for c in cs) + "\n"


def repo_text(p, a):
    if git_agent(p, a):
        return ("Tu travailles dans un WORKTREE GIT LOCAL qui t’est personnel (branche %s, créée depuis %s) : aucun autre agent n’y écrit, donc AUCUN verrou.\n"
                + ("Une branche est créée automatiquement pour chaque tâche (task/<prénom>/<tâche>) ; si tu as encore du travail non fusionné sur ton ancienne branche, tu la gardes jusqu’à sa fusion.\n" if git_strategy(p) == "task" else "") +
                "Tes modifications ne sont enregistrées qu’avec l’action commit (message clair) ; elles restent locales. Utilise status, diff (contre %s), search, git_log, git_show.\n"
                "Avant de commencer, et régulièrement, intègre les changements des autres avec sync (commit d’abord) ; en cas de conflit, résous-le dans les fichiers puis commit. "
                + checks_prompt(p).replace("%", "%%") +
                "Quand ton travail est prêt : task_status « à vérifier » (l’application fait le commit restant, se synchronise avec la cible et soumet la merge request, que l’agent principal valide) ou merge_request. "
                "Si ta merge request entre en conflit avec la cible, elle te revient : sync, résous, commit, puis merge_request de nouveau.") % (
                    git_branch_hint(p, a), git_target(p), git_target(p))
    if local_agent(p, a):
        return ("Tu travailles sur le RÉPERTOIRE LOCAL du projet : %s (pas de branche, pas de pull ni de merge request).\n"
                "Ce répertoire est partagé avec d’autres agents : verrouille un fichier (lock_file) avant de le modifier et libère-le (unlock_file) dès que tu as fini. "
                "Un fichier verrouillé par un autre agent est inaccessible en écriture tant qu’il ne l’a pas libéré (ta demande attend).\n"
                "Fichiers verrouillés actuellement :\n%s") % (p.get("localdir") or "non défini", locks.text(p, a["id"]))
    rp, cy, pol = p.get("repo") or {}, p.get("cycle"), p.get("conflict_policy") or "ask"
    ptxt = {"ask": "l’utilisateur décide", "author": "l’auteur de la branche résout"}.get(pol) or "l’agent %s résout" % (by_id(p["agents"], pol[6:]) or {}).get("name", "?")
    return "Branche principale : %s (réservée à la release finale).\n%s\nTa branche : %s.\nConflits de fusion : %s." % (
        rp.get("branch", "main"),
        ("Release en cours : %s, branche de travail %s (pull et merge requests)." % (cy["version"], cy["branch"])) if cy
        else "Aucune release en cours : tu ne peux pas soumettre de merge request tant que l’utilisateur n’en a pas démarré une.", a["branch"], ptxt)


def sys_prompt(p, a):
    mn, r = is_main(p, a), role(p, a)
    others = [x for x in p["agents"] if x["id"] != a["id"]]
    can = lambda x: can_talk(a, x, mn)
    who = lambda t: (by_id(p["agents"], t.get("who")) or {}).get("name", "—")
    tasks = [t for t in p.get("tasks", []) if mn or t.get("who") == a["id"] or t.get("by") == a["name"]][:80]
    ts = "\n".join("%s | %s | %s | %s | parent : %s%s" % (t["id"], t["title"], t["status"], who(t), t.get("parent") or "—",
                   (" | modules : " + ",".join(mod_names(p, t.get("modules")))) if t.get("modules") else "") + ((" | dépend de : " + ",".join(t["deps"])) if t.get("deps") else "") for t in tasks)
    bs = "\n".join("%s | %s | %s | %s%s" % (b["id"], b["t"], b["sev"], b["status"], (" | modules : " + ",".join(mod_names(p, b.get("modules")))) if b.get("modules") else "")
                   for b in p.get("bugs", []) if b["status"] not in rights.BUG_CLOSED)[:3000]
    pend = [d for d in p.get("decisions", []) if d["status"] == "attente"]
    ins, news = rights.instruction_lines(p, a), rights.instruction_lines(p, a, only_new=True)
    news += prules.news_lines(p, a)
    if p.get("spec_n", 0) > a.get("seen_seq", 0):
        news.append("- Les spécifications ont été mises à jour.")
    repo = p.get("repo") or {}
    parts = [
        "# Règles de l’équipe\n" + "\n".join(rulebook.locked_core()),
        ("# Règles du projet\n" + prules.shared_rules_text(p)) if prules.shared_rules_text(p) else "",
        prules.agent_rules_text(p, a), prules.workflows_text(p, a),
        ("# Instructions de l’utilisateur (à respecter)\n" + "\n".join(ins)) if ins else "",
        ("# Nouveautés depuis ta dernière activité\n" + "\n".join(news)) if news else "",
        "# Toi\nPrénom : %s\nRôle : %s — %s\n%s\n%s" % (
            a["name"], r["name"], r["desc"],
            "Espace de travail : worktree git local (branche %s)" % git_branch_hint(p, a) if git_agent(p, a) else "Espace de travail : répertoire local du projet" if local_agent(p, a) else "Branche git : %s (branche de base : %s)" % (a["branch"], repo.get("branch", "main")),
            "Tu es l’AGENT PRINCIPAL : tu peux écrire à tous les agents et tu tiens les spécifications à jour. Quand l’utilisateur te donne un élément durable (consigne, contrainte, décision), enregistre-le avec l’action instruction et mets les spécifications à jour si besoin. Tu assures aussi le suivi : tu vérifies chaque tâche « à vérifier » (task_review : accepted seulement si le travail demandé est réellement fait — lis les fichiers de l’agent avec read_file et \"agent\" —, sinon rejected avec ce qui manque), tu interroges et relances (relaunch) les agents qui semblent bloqués, et tu remontes à l’utilisateur (ask_user) tout problème autre qu’un simple manque de tokens (ceux-là sont relancés automatiquement)." if mn else ""),
        "# Tes droits\n" + rights.rights_text(p, a, mn),
        "# Dépôt et intégration\n" + repo_text(p, a),
        "# Équipe\n" + ("\n".join("- %s (%s%s, branche %s)%s" % (x["name"], role(p, x)["name"], ", agent principal" if is_main(p, x) else "", x["branch"], "" if can(x) else " [non joignable]") for x in others) or "(seul)"),
        teamwork_text(p, a),
        "# Projet %s\n%s" % (p["name"], p.get("desc", "")),
        "# Modules\n" + ("\n".join("- %s : %s" % (m["name"], m.get("desc", "")) for m in p.get("modules", [])) or "(aucun)"),
        "# Tâches (id | titre | statut | agent | parent | modules)\n" + (ts or "(aucune)"),
        ("# Bugs ouverts (id | titre | gravité | statut | modules)\n" + bs) if bs else "",
        ("# Décisions en attente de l’utilisateur\n" + "\n".join("- %s (%s)" % (d.get("question") or (d["payload"].get("title") or d["payload"].get("name")), d["byName"]) for d in pend)) if pend else "",
        ("# Spécifications actuelles\n" + p["spec"][:6000]) if p.get("spec") else "",
        "# Notes personnelles (ton fichier de contexte)\n" + (a.get("notes") or "(vides)"), actions_common(), focus_text(p, a),
        "# Règles de l’équipe propres à ton mode de travail\n" + "\n".join(agent_rules_lines(p, a)), prules.subject_rules_text(p, a), actions_extra(p, a)]
    # Blocs du plus stable au plus changeant (prompt caching Anthropic : les deux premiers sont mis en cache) :
    # 1) commun à tous les agents, 2) propre à l'agent mais rarement modifié, 3) change à presque chaque tour.
    shared = [parts[0], parts[18], parts[1], parts[11], parts[12], parts[16]]
    per_agent = [parts[20], parts[21], parts[22], parts[2], parts[3], parts[4], parts[6], parts[7], parts[8], parts[9]]
    volatile = [parts[19], parts[5], parts[10], parts[13], parts[14], parts[15], parts[17]]
    return System.of(["\n\n".join(x for x in grp if x) for grp in (shared, per_agent, volatile)])


ACT_RE = re.compile(r"```action\s*([\s\S]*?)```")


def parse_acts(t):
    """Extrait les blocs ```action de la réponse d'un agent : (texte sans les blocs, actions, erreurs).
    Aucun bloc ne se perd en silence : JSON illisible, bloc non refermé (réponse tronquée) ou élément qui n'est pas un objet
    produisent une erreur factuelle renvoyée à l'agent. Un bloc peut contenir une liste d'actions."""
    acts, errs = [], []
    for i, m in enumerate(ACT_RE.finditer(t), 1):
        try:
            d = json.loads(m.group(1))
        except ValueError as e:
            errs.append("Bloc action n° %d invalide (JSON illisible : %s) : il n'a PAS été exécuté. Renvoie-le en JSON valide "
                        "(guillemets et retours à la ligne échappés dans les chaînes, une seule action par bloc)." % (i, e))
            continue
        for x in (d if isinstance(d, list) else [d]):
            if isinstance(x, dict):
                acts.append(x)
            else:
                errs.append("Bloc action n° %d : chaque action doit être un objet JSON {\"do\": …} : élément ignoré." % i)
    rest = ACT_RE.sub("", t)
    if "```action" in rest:
        errs.append("Un bloc action n'est pas refermé (réponse tronquée ?) : il n'a PAS été exécuté. Renvoie-le complet, "
                    "en plusieurs écritures de fichier plus courtes si le contenu est long.")
        rest = rest[:rest.index("```action")]
    return rest.strip(), acts, errs


def safe_path(path, write=False):
    path = str(path or "").strip().strip("/")
    if ".." in path.split("/") or not re.fullmatch(r"[\w .@+\-/]*", path):
        raise Err(400, "Chemin invalide : « %s »" % path)
    if write and (path == "" or path.startswith((".git/", ".github/", ".agents/"))):
        raise Err(403, "Écriture interdite sur « %s »" % path)
    return path


def _overflow(e):
    m = (e.msg or "").lower()
    return e.code in (400, 413, 502) and any(k in m for k in (
        "too long", "context length", "maximum context", "context window", "token limit", "too many tokens", "reduce the length", "exceeds the"))


OLD_RESULT = "[Application] Résultats de tes actions"


READ_MAX = 12000       # taille maximale d'un résultat de read_file
SHRINK_GUARD = (2000, 0.5)   # write_file refusé si le fichier existant fait >= 2000 caractères et que le nouveau contenu en fait moins de la moitié
CHUNK_TTL, CHUNK_MAX, CHUNK_BUF = 1800, 50, {}
READS, REREAD_WARN = {}, 3      # relectures identiques d'un fichier par un agent, sans écriture entre-temps : avertissement dès la 3e
CHUNK_LOCK = threading.Lock()


def read_view(t, x):
    """Extrait lu d'un fichier selon x : `from_line`/`to_line` (1-based, inclus) ou `offset`/`limit` (caractères) ; au plus READ_MAX caractères.
    Retourne (texte, étiquette, suite) : étiquette = précision sur l'extrait ; suite = message pour lire la suite (ou '')."""
    def num(k):
        try:
            return int(x.get(k))
        except (TypeError, ValueError):
            return None
    n, fl, tl, off, lim = len(t), num("from_line"), num("to_line"), num("offset"), num("limit")
    if fl is not None or tl is not None:
        lines = t.splitlines(True)
        a = max(fl or 1, 1)
        b = min(tl if tl and tl >= a else len(lines), len(lines))
        body = "".join(lines[a - 1:b])
        if len(body) > READ_MAX:
            body = body[:READ_MAX]
            b = a + body.count("\n") - (0 if body.endswith("\n") else 1)
            nxt = "[…extrait tronqué à %d caractères : suite avec from_line=%d]" % (READ_MAX, b + 1)
        else:
            nxt = "[suite : from_line=%d]" % (b + 1) if b < len(lines) else ""
        return body, " — lignes %d à %d sur %d" % (a, max(b, a), len(lines)), nxt
    start = max(off or 0, 0)
    size = min(max(lim or READ_MAX, 1), READ_MAX)
    body = t[start:start + size]
    end = start + len(body)
    if start == 0 and off is None and lim is None:
        lab = ""
    else:
        lab = " — caractères %d à %d sur %d" % (start, end, n)
    if end < n:
        nxt = "[…tronqué à %d caractères sur %d : lis la suite avec offset=%d, ou la zone voulue avec from_line/to_line ; pour modifier, utilise edit_file]" % (end - start, n, end)
    else:
        nxt = ""
    return body, lab, nxt


HIST_BUDGET = 36000         # caractères de retours d'actions gardés intacts dans l'historique (≈ 9 000 tokens)


def shrink_history(h, keep=2, cap=400, budget=HIST_BUDGET):
    """Abrège les anciens retours d'actions (contenu de fichiers, listes…) de l'historique envoyé à l'IA. L'historique stocké n'est pas modifié.
    Restent intacts : les `keep` derniers messages, puis, du plus récent au plus ancien, les retours qui tiennent dans `budget` caractères.
    Un retour dont tous les fichiers ont été relus depuis (même en-tête « Fichier … ») est abrégé d'office : relire en boucle ne remplit pas le budget.
    (Avant : seuls les 2 derniers messages restaient intacts, si bien que lire trois fichiers en abrégeait un et poussait l'agent à le relire sans fin.)"""
    n, used, seen, out = len(h), 0, set(), list(h)
    for i in range(n - 1, -1, -1):
        m = h[i]
        if m["role"] != "user" or not m["content"].startswith(OLD_RESULT) or len(m["content"]) <= cap:
            continue
        heads = set(re.findall(r"^Fichier .* :$", m["content"], re.M))
        if i >= n - keep or (not (heads and heads <= seen) and used + len(m["content"]) <= budget):
            used += len(m["content"])
            seen |= heads
        else:
            out[i] = {"role": "user", "content": m["content"][:cap] + "\n[…ancien résultat abrégé : si tu as besoin d’un passage, relis seulement la zone utile (read_file from_line/to_line) ; ne réécris jamais un fichier entier à partir de ce texte abrégé]"}
    return out


def ask(u, pid, aid, text, frm, depth=0, stats=None, relog=True, max_steps=None, fresh=False):
    """Envoie un message à un agent et exécute ses actions jusqu'à sa réponse finale.
    `fresh` : appel sans mémoire (historique de l'agent ni lu ni écrit) — pour les analyses qui doivent ne voir que l'état courant.
    `stats` (facultatif) reçoit {actions, pending} : nombre d'actions exécutées, et retour non traité si les étapes sont épuisées."""
    p = snap(u, pid)
    a = by_id(p["agents"], aid)
    if not a:
        raise Err(404, "Agent introuvable")
    stats = stats if stats is not None else {}
    stats.update(actions=0, pending="")
    if relog:
        log(u, pid, frm, a["name"], text)
    if not fresh and frm != "Application":
        mark_interrupted(u, pid, aid, frm)
    work(u, pid, aid, 1)
    try:
        msg, final, ctx, overflow = "[%s] %s" % (frm, text), "", {"n": 0, "t0": time.time()}, 0
        for _ in range(max_steps or MAX_STEPS):
            p = snap(u, pid)
            a = by_id(p["agents"], aid)
            if not a:
                break
            s = by_id(p["_svc"], a.get("service"))
            manual = a.get("mode") == "manual"
            if not s and not manual:
                raise Err(400, "Service IA révoqué pour %s" % a["name"])
            h = [] if fresh else list(a.get("history", []))[-30:]
            while h and h[0]["role"] != "user":
                h.pop(0)
            h = shrink_history(h)
            locks.check_stop(u, pid, aid, ctx["t0"])           # arrêté par l'agent principal / l'utilisateur : on ne fait plus rien
            if not manual:
                check_budget(p)
            sp = sys_prompt(p, a)
            mp(u, pid, lambda pp: by_id(pp["agents"], aid).__setitem__("seen_seq", pp.get("seq", 0)))
            try:
                out = manual_exchange(u, pid, a, s, sp, h, msg) if manual else call_ai(s, sp, h + [{"role": "user", "content": msg}])
            except Err as e:
                if manual or overflow >= 2 or not _overflow(e):
                    raise
                overflow += 1                              # contexte trop long : on compacte l'historique et on réessaie

                def compact(pp):
                    ag = by_id(pp["agents"], aid)
                    h2 = [{"role": m["role"], "content": m["content"][:1500]} for m in ag.get("history", [])[-4:]]
                    while h2 and h2[0]["role"] != "user":
                        h2.pop(0)
                    ag["history"] = h2
                mp(u, pid, compact)
                log(u, pid, "Système", a["name"], "Contexte trop long : l’historique de %s est compacté, nouvel essai." % a["name"])
                continue
            if getattr(out, "usage", None) and s:
                record_usage(u, pid, aid, s, out.usage)
            locks.check_stop(u, pid, aid, ctx["t0"])           # arrêté pendant l'appel IA : les actions de cette réponse ne sont pas exécutées
            clean, acts, errs = parse_acts(out)
            keep = msg if len(msg) < 3000 else msg[:3000] + "\n[…tronqué]"
            keep_out = out if len(out) < 6000 else out[:6000] + "\n[…tronqué dans l’historique]"

            def add(pp):
                ag = by_id(pp["agents"], aid)
                if ag and not fresh:
                    ag["history"] = (ag.get("history", []) + [{"role": "user", "content": keep}, {"role": "assistant", "content": keep_out}])[-30:]
            mp(u, pid, add)
            if clean:
                log(u, pid, a["name"], frm, clean)
                final = clean
            fb = list(errs)                                    # blocs illisibles : l'agent en est informé (sinon il attend un retour qui ne viendra jamais)
            if errs:
                log(u, pid, "Système", a["name"], "%d bloc(s) action de %s illisible(s) : erreur renvoyée à l’agent." % (len(errs), a["name"]))
            stats["actions"] += len(acts)
            for x in acts:
                note, back = do_act(u, pid, aid, x, depth, ctx)
                if note:
                    log(u, pid, "Système", a["name"], note)
                if back:
                    fb.append(back)
            if not fb:
                break
            msg = "[Application] Résultats de tes actions :\n\n" + "\n\n".join(fb)
        else:
            stats["pending"] = msg                         # étapes épuisées : le dernier retour n'a pas été traité
        return final
    finally:
        work(u, pid, aid, -1)


def num(x):
    try:
        return max(0.0, float(str(x).replace(",", ".")))
    except (TypeError, ValueError):
        return 0.0


def record_usage(u, pid, aid, svc, usage):
    """Comptabilise les tokens d'un appel API et son coût estimé (tarifs saisis dans le service, en $ par million de tokens)."""
    tin, tout = int(usage.get("in") or 0), int(usage.get("out") or 0)
    cr, cw = int(usage.get("cache_read") or 0), int(usage.get("cache_write") or 0)
    # tokens lus dans le cache : 10 % du tarif d'entrée ; écrits dans le cache : 125 % (tarifs Anthropic du cache de 5 min)
    cost = round((max(tin - cr - cw, 0) + cr * 0.1 + cw * 1.25) * num(svc.get("pin")) / 1e6 + tout * num(svc.get("pout")) / 1e6, 6)

    def bump(d):
        d["calls"] = d.get("calls", 0) + 1
        d["in"] = d.get("in", 0) + tin
        d["out"] = d.get("out", 0) + tout
        if cr:
            d["cached"] = d.get("cached", 0) + cr
        d["cost"] = round(d.get("cost", 0) + cost, 6)

    def f(pp):
        a = by_id(pp["agents"], aid)
        bump(pp.setdefault("usage", {}))
        if a:
            bump(a.setdefault("usage", {}))
        led = pp.setdefault("ledger", [])
        led.append({"d": now(), "agent": a["name"] if a else "?", "model": svc.get("model", ""), "in": tin, "out": tout, "cached": cr, "cost": cost})
        del led[:-300]
    mp(u, pid, f)


def reset_usage(u, pid):
    def f(pp):
        pp["usage"], pp["ledger"] = {}, []
        for a in pp["agents"]:
            a["usage"] = {}
    mp(u, pid, f)


def manual_exchange(u, pid, a, svc, system, h, msg):
    """Mode « chat manuel » : l'application prépare le prompt, l'utilisateur le colle dans le chatbot
    de son choix puis colle la réponse (aucune API, aucun accès automatisé au site du chatbot)."""
    hist = "\n".join("%s : %s" % ("Toi" if x["role"] == "assistant" else "Application", x["content"][:600]) for x in h[-6:])
    full = ("Tu vas tenir le rôle d'un agent dans une application de gestion de projets de développement. "
            "Voici tes instructions permanentes, à respecter pendant toute la conversation :\n\n<<<INSTRUCTIONS\n%s\nINSTRUCTIONS>>>\n\n%sMessage à traiter :\n%s"
            % (system, ("Historique récent :\n%s\n\n" % hist) if hist else "", msg))
    short = "(Suite de la même conversation, mêmes instructions.)\n\n%s\n\nRappel : si tu dois agir, termine par des blocs d'action." % msg
    item = {"id": uid(), "agent": a["id"], "name": a["name"], "service": (svc or {}).get("name", ""),
            "full": full, "short": short, "new": not a.get("session"), "d": now()}
    WAIT[item["id"]] = {"u": u, "ev": threading.Event(), "text": None}
    mp(u, pid, lambda pp: pp.setdefault("pending", []).append(item))
    log(u, pid, "Système", a["name"], "Prompt prêt pour %s : copiez-le dans le chatbot puis collez sa réponse (bandeau en haut du projet)." % a["name"])
    try:
        WAIT[item["id"]]["ev"].wait(MANUAL_TIMEOUT)
        text = WAIT[item["id"]]["text"]
    finally:
        WAIT.pop(item["id"], None)
        mp(u, pid, lambda pp: pp.__setitem__("pending", [x for x in pp.get("pending", []) if x["id"] != item["id"]]))
    if text is None:
        raise Err(408, "Échange manuel annulé ou expiré pour %s" % a["name"])
    mp(u, pid, lambda pp: by_id(pp["agents"], a["id"]).__setitem__("session", True))
    return text


def manual_answer(u, xid, text):
    w = WAIT.get(xid)
    if not w or w["u"] != u:
        raise Err(404, "Échange introuvable ou déjà traité")
    w["text"] = text
    w["ev"].set()


def manual_cancel(u, xid):
    w = WAIT.get(xid)
    if not w or w["u"] != u:
        raise Err(404, "Échange introuvable ou déjà traité")
    w["ev"].set()


def check_budget(p):
    bud = num(p.get("budget"))
    if bud and (p.get("usage") or {}).get("cost", 0) >= bud:
        raise Err(402, "Plafond de budget atteint (%.2f $) : les agents en mode API sont en pause. Relevez le plafond dans l’onglet Coût." % bud)


def _dec_key(d):
    """Clé « sujet » d'une décision : deux décisions en attente de même clé portent sur la même chose."""
    for k in ("conflict", "mr_review", "wf_run", "review", "ruleconf"):
        if d.get(k):
            return (k, d[k])
    if d.get("escalation") and d.get("task"):
        return ("escalation", d["task"])
    if d.get("kind") != "question":
        p = d.get("payload") or {}
        return (d["kind"], rights.norm(p.get("title") or p.get("name")))
    return None


def _dec_words(s):
    return set(re.findall(r"\w{4,}", str(s or "").lower()))


def _supersedes(new, old, replaces):
    """Vrai si la décision `new` (la plus récente) remplace la décision en attente `old`."""
    if old["id"] == new["id"] or old["status"] != "attente":
        return False
    if old.get("n") in replaces:
        return True
    kn, ko = _dec_key(new), _dec_key(old)
    if kn or ko:
        return kn is not None and kn == ko
    a, b = _dec_words(new.get("question")), _dec_words(old.get("question"))
    if not a or not b:
        return False
    sim = len(a & b) / len(a | b)
    same_task = bool(new.get("task")) and new.get("task") == old.get("task")
    return sim >= (0.3 if same_task else 0.5)


def add_decision(pp, d, replaces=()):
    """Ajoute une décision en attente ; celles, plus anciennes, auxquelles elle répond aussi sont annulées et remplacées
    (même sujet : conflit, merge request, workflow, validation, proposition identique ; question très proche ;
    ou numéros indiqués explicitement par l'agent dans `replaces`). Retourne la liste des numéros remplacés."""
    reps = set()
    for r in replaces or ():
        m = re.search(r"\d+", str(r))
        if m:
            reps.add(int(m.group()))
    olds = [x for x in pp.get("decisions", []) if _supersedes(d, x, reps)]
    pp.setdefault("decisions", []).append(d)
    for x in olds:
        x.update(status="remplacée", note="Remplacée par la décision n° %s." % d.get("n", "?"), replaced_by=d["id"],
                 done=now(), n_done=rights.next_seq(pp))
        t = by_id(pp.get("tasks", []), x.get("task"))
        if t and t.get("blocked") == x["id"]:
            t.pop("blocked", None)
            t["status"], t["touched"] = "à faire", time.time()
    return [x.get("n") for x in olds]


def propose(u, pid, a, kind, data):
    """Enregistre une proposition dans la boîte « Décisions » (None si une proposition identique attend déjà)."""
    def f(pp):
        key = rights.norm(data.get("title") or data.get("name"))
        if any(d["status"] == "attente" and d["kind"] == kind and d["by"] == a["id"]
               and rights.norm(d["payload"].get("title") or d["payload"].get("name")) == key for d in pp.get("decisions", [])):
            return None
        d = {"id": uid(), "n": rights.next_seq(pp), "kind": kind, "by": a["id"], "byName": a["name"], "d": now(),
             "status": "attente", "payload": data, "why": data.get("why", "")}
        add_decision(pp, d)
        return d
    return mp(u, pid, f)


def gate(u, pid, p, a, cap, kind, data, ctx):
    """Création soumise aux droits : libre (créée), à valider (proposition) ou interdite."""
    ctx["n"] += 1
    if ctx["n"] > MAX_CREATE:
        m = "Trop de créations dans un même échange (maximum %d) : poursuis plus tard." % MAX_CREATE
        return m, m
    lvl, title = rights.level(p, a, cap, is_main(p, a)), data.get("title") or data.get("name")
    if lvl == rights.NO:
        m = "Création refusée : ton rôle ne t’autorise pas à %s.%s" % (rights.CAP_TXT[cap][0].lower() + rights.CAP_TXT[cap][1:], " Demande-le à l’agent principal." if kind == "task" else "")
        return m, m
    if lvl == rights.APPROVE:
        d = propose(u, pid, a, kind, data)
        return ("Proposition enregistrée dans « Décisions » : %s" % title) if d else ("Proposition déjà en attente : %s" % title), None
    msg = mp(u, pid, lambda pp: rights.create_from(pp, kind, data, a["name"]))
    spawn(tick, u, pid)                                   # une tâche créée peut être lancée aussitôt
    return msg, None


def progress_text(p, who):
    w = str(who or "all").lower()
    ags = [g for g in p["agents"] if w in ("all", "tous", "") or g["name"].lower() == w]
    if not ags:
        return "Agent « %s » inconnu." % who
    L = []
    for g in ags:
        ts = [t for t in p.get("tasks", []) if t.get("who") == g["id"]]
        cnt = ", ".join("%d %s" % (sum(1 for t in ts if t["status"] == s), s) for s in rights.STATUS if any(t["status"] == s for t in ts)) or "aucune tâche"
        cur = " ; ".join(t["title"] for t in ts if t["status"] == "en cours")[:200] or "—"
        last = next((m["d"] for m in reversed(p.get("log", [])) if m["from"] == g["name"]), "—")
        L.append("- %s (%s) : %s ; en cours : %s ; dernière activité : %s" % (g["name"], role(p, g)["name"], cnt, cur, last))
    pend = sum(1 for d in p.get("decisions", []) if d["status"] == "attente")
    return "Avancement :\n" + "\n".join(L) + (("\nDécisions en attente de l’utilisateur : %d" % pend) if pend else "")


def do_act(u, pid, aid, x, depth, ctx=None):
    """Exécute une action d'agent. Retourne (note pour l'historique, retour pour l'agent ou None)."""
    ctx = ctx if ctx is not None else {"n": 0, "t0": time.time()}
    locks.check_stop(u, pid, aid, ctx.get("t0", 0))
    p = snap(u, pid)
    a = by_id(p["agents"], aid)
    mn, kind = is_main(p, a), x.get("do")
    byname = lambda n: next((g for g in p["agents"] if g["name"].lower() == str(n or "").lower()), None)
    refuse = lambda m: (m, m)
    lv = lambda cap: rights.level(p, a, cap, mn)
    x.pop("_raw", None)                                    # drapeau interne (lecture intégrale) : jamais fourni par un agent
    try:
        if kind == "message":
            t = byname(x.get("to"))
            if not t or t["id"] == a["id"]:
                return refuse("Message refusé : agent « %s » inconnu." % x.get("to"))
            if not can_talk(a, t, mn):
                return refuse("Message refusé : %s ne peut pas écrire à %s." % (a["name"], t["name"]))
            if depth >= MAX_DEPTH:
                return refuse("Message non délivré (profondeur maximale d’échanges atteinte).")
            r = ask(u, pid, t["id"], str(x.get("text", "")), a["name"], depth + 1)
            return "", "[Réponse de %s] %s" % (t["name"], r or "(pas de réponse écrite)")
        if kind in ("list_files", "read_file", "write_file", "delete_file", "edit_file", "write_chunk"):
            return file_act(u, pid, p, a, x, kind, ctx.get("t0", 0))
        if kind == "propose_check":
            return propose_check(u, pid, p, a, x, mn, ctx)
        if kind in GIT_KINDS:
            if not git_agent(p, a):
                return refuse("L’action %s n’existe qu’en mode git local (répertoire local versionné, un worktree par agent)." % kind)
            return git_act(u, pid, p, a, x, kind, ctx.get("t0", 0))
        if kind in ("lock_file", "unlock_file", "get_locks", "stop_agent", "force_unlock"):
            return lock_act(u, pid, p, a, x, kind, mn, ctx.get("t0", 0))
        if kind == "get_progress":
            return "", progress_text(p, x.get("agent"))
        if kind == "get_blockers":
            if sync_mrs(u, pid, force=True):               # jamais de « blocage fantôme » : les MR closes sur GitHub sont retirées avant la réponse
                spawn(tick, u, pid)
            return "", blockers_text(snap(u, pid))
        if kind == "context":
            def f(pp):
                by_id(pp["agents"], aid)["notes"] = str(x.get("text", ""))
            mp(u, pid, f)
            p = snap(u, pid)
            a = by_id(p["agents"], aid)
            msg = "chore(%s) : mise à jour du contexte" % a["name"]
            if git_agent(p, a):                            # git local : le contexte vit hors des branches (.myteam/agents/, ignoré par git)
                try:
                    return "Contexte mis à jour : %s" % git_context_write(u, pid, p, a), None
                except Err as e:
                    return refuse("Contexte enregistré dans l’application, fichier non écrit : %s" % e.msg)
            try:
                rp = github.Repo(p)
                sha = rp.put_file(a["branch"], ".agents/%s.md" % a["name"], ctx_text(p, a), msg)
                record_commit(u, pid, a["name"], msg, sha)
                return "Contexte mis à jour et poussé sur %s." % a["branch"], None
            except Err:
                return "Contexte mis à jour (dépôt non configuré : fichier non poussé).", None
        if kind == "task_status":
            st = x.get("status")
            if st not in rights.STATUS:
                return refuse("Statut invalide : %s." % st)
            if st == "terminée":
                st = "à vérifier"          # seule une vérification (task_review) ou l'utilisateur clôture une tâche

            def f(pp):
                t = by_id(pp["tasks"], x.get("id"))
                if not t or (not mn and t.get("who") != aid):
                    return None
                t["status"], t["touched"] = st, time.time()
                if st != "en attente":
                    t.pop("blocked", None)
                if x.get("note"):
                    t["result"], t["result_note"] = str(x["note"]), True
                return "Tâche « %s » : %s.%s" % (t["title"], st, " Elle sera vérifiée avant clôture." if st == "à vérifier" else "")
            r = mp(u, pid, f)
            return refuse("Changement de statut refusé.") if r is None else (r, None)
        if kind == "task_review":
            t = by_id(p["tasks"], x.get("id"))
            if not t or t["status"] != "à vérifier":
                return refuse("Cette tâche n’est pas en attente de vérification.")
            if not (mn or lv("bug_verify") == rights.FREE):
                return refuse("Vérification refusée pour ton rôle.")
            if t.get("who") == aid:
                return refuse("Tu ne peux pas vérifier ta propre tâche.")
            verdict, comment = x.get("verdict"), str(x.get("comment", "")).strip()
            if verdict not in ("accepted", "rejected") or (verdict == "rejected" and not comment):
                return refuse("Verdict invalide : accepted, ou rejected avec la liste précise de ce qui manque.")
            return apply_review(u, pid, t["id"], a["name"], verdict, comment), None
        if kind == "relaunch":
            if not mn:
                return refuse("Relance réservée à l’agent principal.")
            t = by_id(p["tasks"], x.get("task"))
            if not t or not t.get("who") or t["status"] in ("terminée", "à vérifier") or t.get("running"):
                return refuse("Relance impossible pour cette tâche.")
            if t.get("relaunch_n", 0) >= MAX_RELAUNCH:
                return refuse("Trop de relances (%d) : remonte le problème à l’utilisateur avec ask_user." % MAX_RELAUNCH)
            mp(u, pid, lambda pp: by_id(pp["tasks"], t["id"]).__setitem__("relaunch_n", t.get("relaunch_n", 0) + 1))
            spawn(run_task, u, pid, t["id"], False, "[Agent principal] " + (str(x.get("note", "")).strip() or "Reprends la tâche."))
            return "Relance de « %s » demandée." % t["title"], None
        if kind == "task":
            parent = x.get("parent") if by_id(p["tasks"], x.get("parent")) else ""
            as_ = byname(x.get("assignee"))
            data = {"title": x.get("title", ""), "desc": x.get("desc", ""), "parent": parent, "who": as_["id"] if as_ else "",
                    "modules": mod_ids(p, x.get("modules")), "why": str(x.get("why", "")),
                    "deps": [i for i in (x.get("depends_on") or []) if by_id(p["tasks"], i)]}
            if parent:
                par = by_id(p["tasks"], parent)
                if not mn and not rights.owns(p, a, par):
                    return refuse("Création refusée : tu n’es pas responsable de la tâche parente « %s »." % par["title"])
                if rights.depth(p, par) >= rights.MAX_TASK_DEPTH:
                    return refuse("Profondeur maximale des sous-tâches atteinte (%d niveaux)." % rights.MAX_TASK_DEPTH)
            return gate(u, pid, p, a, "subtask" if parent else "task_top", "task", data, ctx)
        if kind == "module":
            return gate(u, pid, p, a, "module", "module", {"name": x.get("name", ""), "desc": x.get("desc", ""), "why": str(x.get("why", ""))}, ctx)
        if kind == "bug":
            return gate(u, pid, p, a, "bug", "bug", {"title": x.get("title", ""), "desc": x.get("desc", ""), "sev": x.get("sev"),
                                                     "modules": mod_ids(p, x.get("modules")), "why": str(x.get("why", ""))}, ctx)
        if kind == "bug_fix":
            if not mn and lv("bug") == rights.NO and lv("subtask") == rights.NO:
                return refuse("Création de sous-tâche de correction refusée pour ton rôle.")
            if not by_id(p["bugs"], x.get("bug")):
                return refuse("Bug introuvable : %s." % x.get("bug"))
            ctx["n"] += 1
            if ctx["n"] > MAX_CREATE:
                return refuse("Trop de créations dans un même échange (maximum %d)." % MAX_CREATE)
            as_ = byname(x.get("assignee"))
            return mp(u, pid, lambda pp: "Sous-tâche de correction créée : %s (bug « %s » : en correction)" % (
                rights.add_fix_task(pp, by_id(pp["bugs"], x["bug"]), x.get("title", ""), x.get("desc", ""), as_["id"] if as_ else "", a["name"])["title"],
                by_id(pp["bugs"], x["bug"])["t"])), None
        if kind == "bug_status":
            st = x.get("status")
            if st not in ("en correction", "corrigé", "vérifié", "rouvert"):
                return refuse("Statut de bug invalide : %s." % st)
            if lv("bug_verify" if st in ("vérifié", "rouvert") else "bug_update") == rights.NO:
                return refuse("Changement de statut de bug refusé pour ton rôle (%s)." % st)

            def f(pp):
                b = by_id(pp["bugs"], x.get("id"))
                if not b:
                    return None
                b["status"] = st
                return "Bug « %s » : %s." % (b["t"], st)
            r = mp(u, pid, f)
            return refuse("Bug introuvable : %s." % x.get("id")) if r is None else (r, None)
        if kind == "link_modules":
            if lv("link") == rights.NO:
                return refuse("Rattachement aux modules refusé pour ton rôle.")
            coll = "tasks" if x.get("kind") == "task" else "bugs"
            it = by_id(p[coll], x.get("id"))
            if not it or (coll == "tasks" and not mn and not rights.owns(p, a, it)):
                return refuse("Rattachement refusé ou élément introuvable : %s." % x.get("id"))
            ids = mod_ids(p, x.get("modules"))
            mp(u, pid, lambda pp: by_id(pp[coll], x["id"]).__setitem__("modules", ids))
            return "Modules de « %s » : %s." % (it.get("title") or it.get("t"), ", ".join(mod_names(p, ids)) or "aucun (global)"), None
        if kind == "ask_user":
            q = str(x.get("question", "")).strip()
            if not q:
                return refuse("Question vide.")
            tid = x.get("task") if by_id(p["tasks"], x.get("task")) else ""

            def f(pp):
                d = {"id": uid(), "n": rights.next_seq(pp), "kind": "question", "by": aid, "byName": a["name"], "d": now(), "status": "attente",
                     "question": q, "options": [str(o) for o in (x.get("options") or [])][:4], "recommendation": str(x.get("recommendation", "")),
                     "impact": str(x.get("impact", "")), "structural": bool(x.get("structural")), "task": tid}
                add_decision(pp, d, x.get("replaces") if isinstance(x.get("replaces"), list) else ())
                t = by_id(pp["tasks"], tid)
                if t:
                    t["status"], t["blocked"] = "en attente", d["id"]
            mp(u, pid, f)
            return "Question transmise à l’utilisateur : %s%s" % (q, " (tâche en attente de décision)" if tid else ""), None
        if kind == "pull" and git_agent(p, a):
            return refuse("En mode git local : utilise sync pour intégrer la branche cible dans ta branche.")
        if kind in ("pull", "merge_request") and local_agent(p, a) and not git_agent(p, a):
            return refuse("Tu travailles sur le répertoire local du projet : il n’y a ni pull ni merge request (partage par verrous de fichiers).")
        if kind == "pull":
            st, msg = pull(u, pid, aid)
            return msg, msg
        if kind == "merge_request":
            st, msg = submit_mr(u, pid, aid, str(x.get("title", "")), str(x.get("body", "")), x.get("task") if by_id(p["tasks"], x.get("task")) else "")
            return msg, msg
        if kind == "mr_review":
            m = by_id(p.get("mrs", []), x.get("id"))
            if not m or m.get("status") != "ouverte":
                return refuse("Cette merge request n’est pas en attente de validation.")
            if not (mn or lv("mr_review") == rights.FREE):
                return refuse("Validation de merge request refusée pour ton rôle (droit « valider les merge requests » absent).")
            if m.get("by_id") == aid:
                return refuse("Tu ne peux pas valider ta propre merge request.")
            verdict, comment = x.get("verdict"), str(x.get("comment", "")).strip()
            if verdict not in ("approved", "rejected") or (verdict == "rejected" and not comment):
                return refuse("Verdict invalide : approved, ou rejected avec ce qu’il faut corriger.")
            return apply_mr_review(u, pid, m["id"], a["name"], verdict, comment), None
        if kind == "resolve_merge":
            c = by_id(p.get("conflicts", []), x.get("conflict"))
            if not c or c["status"] != "ouvert" or c.get("resolver") != aid:
                return refuse("Aucun conflit ouvert ne t’est confié sous cet identifiant.")
            res = {}
            for f in x.get("files") or []:
                if isinstance(f, dict) and isinstance(f.get("path"), str) and (f.get("content") is None or isinstance(f.get("content"), str)):
                    res[safe_path(f["path"])] = f.get("content")
            if c.get("git"):                                # git local : on enregistre la résolution, c'est l'application qui fusionne (contrôles compris)
                m = by_id(p.get("mrs", []), c.get("mr"))
                if not m or m["status"] not in MR_ACTIVE:
                    return refuse("La merge request de ce conflit n’est plus ouverte.")
                with INTEGRATION_LOCK:
                    info = git_repo(u, pid, p).conflicts(m["from"], m["to"])
                missing = [i["path"] for i in info if i["path"] not in res]
                if missing:
                    return refuse("Résolution incomplète : il manque %s." % ", ".join(missing))
                if any("<<<<<<<" in (v or "") or ">>>>>>>" in (v or "") for v in res.values()):
                    return refuse("Ne laisse pas de marqueurs de conflit (<<<<<<<) dans les fichiers.")
                msg = str(x.get("message") or "Résolution du conflit avec %s" % m["to"])[:300]
                mp(u, pid, lambda pp: (by_id(pp["mrs"], m["id"]).update(resolution=res, resolution_msg=msg, resolution_by=a["name"]),
                                       by_id(pp["conflicts"], c["id"]).update(status="résolu", resolved_by=a["name"], done=now())))
                spawn(after_conflict, u, pid, c["id"])
                return "Conflit résolu : l’application fusionne la merge request #%s avec ta résolution (contrôles compris)." % m["n"], None
            owner, rp = by_id(p["agents"], c["agent"]), github.Repo(p)
            missing = [i["path"] for i in rp.conflict_info(owner["branch"], rp.work) if i["path"] not in res]
            if missing:
                return refuse("Résolution incomplète : il manque %s." % ", ".join(missing))
            if any("<<<<<<<" in (v or "") or ">>>>>>>" in (v or "") for v in res.values()):
                return refuse("Ne laisse pas de marqueurs de conflit (<<<<<<<) dans les fichiers.")
            sha = rp.merge_with_resolution(owner["branch"], rp.work, res, str(x.get("message") or "Résolution du conflit avec %s" % rp.work))
            record_commit(u, pid, a["name"], "merge : résolution du conflit (%s)" % ", ".join(res), sha)
            mp(u, pid, lambda pp: by_id(pp["conflicts"], c["id"]).update(status="résolu", resolved_by=a["name"], done=now()))
            spawn(after_conflict, u, pid, c["id"])
            return "Conflit résolu : fusion commitée sur %s." % owner["branch"], None
        if kind == "run_workflow":
            wf = prules.find_workflow(p, str(x.get("workflow", "")))
            if not wf:
                return refuse("Workflow introuvable : %s." % x.get("workflow"))
            if not wf.get("enabled", True) or not wf.get("allow_agent", True):
                return refuse("Le workflow « %s » ne peut pas être déclenché par un agent." % wf["name"])
            tid = x.get("task") if by_id(p["tasks"], x.get("task")) else ""
            workflow_start(u, pid, wf["id"], "agent %s" % a["name"], requester=aid, task=tid, note=str(x.get("note", ""))[:500])
            return "Workflow « %s » déclenché." % wf["name"], None
        if kind == "wf_step":
            wc = a.get("wf_ctx")
            if not wc:
                return refuse("Aucune étape de workflow ne t’est confiée en ce moment.")
            if x.get("outcome") not in ("ok", "ko"):
                return refuse("outcome doit valoir ok ou ko.")
            mp(u, pid, lambda pp: (by_id(pp.get("wf_runs", []), wc["run"]) or {}).__setitem__("cur", {"i": wc["step"], "outcome": x["outcome"], "comment": str(x.get("comment", ""))[:500]}))
            return "Étape de workflow conclue : %s." % x["outcome"], None
        if kind == "rules_report":
            if (p.get("rules_report") or {}).get("analyzing") != aid:
                return refuse("Aucune analyse des règles ne t’est demandée.")
            issues = []
            for it in (x.get("issues") or [])[:20]:
                if isinstance(it, dict) and str(it.get("text", "")).strip():
                    issues.append({"level": it.get("severity") if it.get("severity") in ("erreur", "avertissement", "info") else "info", "where": str(it.get("where", ""))[:120],
                                   "text": str(it["text"])[:500], "suggestion": str(it.get("suggestion", ""))[:400]})
            mp(u, pid, lambda pp: pp.setdefault("rules_report", {}).update(ai=issues, ai_ts=now(), ai_note="", analyzing=""))
            return "Analyse des règles enregistrée : %d point(s) relevé(s)." % len(issues), None
        if kind == "instr_rewrite":
            if (p.get("instr_consol") or {}).get("analyzing") != aid:
                return refuse("Aucune consolidation des instructions ne t’est demandée.")
            actifs = {i["n"]: i for i in p.get("instructions", []) if i.get("active", True)}
            items, obs = [], set()
            for it in (x.get("items") or [])[:40]:
                if isinstance(it, dict) and str(it.get("text", "")).strip():
                    sc = str(it.get("scope") or "global")
                    sc = sc if sc == "global" or sc.split(":")[0] in ("role", "agent", "module") else "global"
                    reps = [int(m.group()) for r in (it.get("replaces") or []) for m in [re.search(r"\d+", str(r))] if m and int(m.group()) in actifs]
                    items.append({"text": str(it["text"]).strip()[:1500], "scope": sc, "replaces": reps, "subject": rights.clean_subject(it.get("subject"))})
            for r in x.get("obsolete") or []:
                m = re.search(r"\d+", str(r))
                if m and int(m.group()) in actifs:
                    obs.add(int(m.group()))
            res = {"added": 0, "off": set()}

            def f(pp):
                if obs:
                    for i in pp.get("instructions", []):
                        if i.get("active", True) and i.get("n") in obs:
                            i.update(active=False)
                            res["off"].add(i["n"])
                for it in items:
                    ni = {"id": uid(), "by": a["name"] + " (consolidation)", "scope": it["scope"], "text": it["text"]}
                    if it["subject"]:
                        ni["subject"] = it["subject"]
                    res["off"].update(rights.add_instruction(pp, ni, it["replaces"]))
                    res["added"] += 1
                pp.setdefault("instr_consol", {}).update(analyzing="", ts=now(), note="%d instruction(s) consolidée(s), %d désactivée(s)." % (res["added"], len(res["off"])))
            mp(u, pid, f)
            gone = "\n".join("  × n°%s : %s" % (n, actifs[n]["text"][:160]) for n in sorted(res["off"]) if n in actifs)
            return "Consolidation des instructions : %d ajoutée(s), %d désactivée(s).%s" % (res["added"], len(res["off"]), ("\n" + gone) if gone else ""), None
        if kind == "instruction":
            if not mn:
                return refuse("Enregistrement d’instruction refusé : réservé à l’agent principal (écris-lui un message).")
            text, sc = str(x.get("text", "")).strip(), str(x.get("scope") or "global")
            if not text:
                return refuse("Instruction vide.")
            if sc != "global" and sc.split(":")[0] not in ("role", "agent", "module"):
                sc = "global"
            ins = {"id": uid(), "by": a["name"], "scope": sc, "text": text}
            if rights.clean_subject(x.get("subject")):
                ins["subject"] = str(x["subject"]).strip()
            res = rule_submit(u, pid, ins, x.get("replaces") or (), a)
            if "pending" in res:
                return "Instruction NON appliquée pour l’instant : elle touche le même sujet qu’une règle plus spécifique ; une question a été posée à l’utilisateur (décision n° %s)." % res["pending"], None
            out = res.get("off", [])
            return "Instruction enregistrée (%s) : %s%s" % (sc, text, (" — remplace n° " + ", ".join(map(str, out))) if out else ""), None
        if kind == "spec":
            if not mn:
                return refuse("Mise à jour des spécifications refusée : réservée à l’agent principal.")

            def f(pp):
                pp["spec"], pp["spec_n"] = str(x.get("text", "")), rights.next_seq(pp)
            mp(u, pid, f)
            try:
                rp = github.Repo(p)
                rp.ensure_branch(a["branch"])
                sha = rp.put_file(a["branch"], "SPEC.md", str(x.get("text", "")), "docs : mise à jour des spécifications")
                record_commit(u, pid, a["name"], "docs : mise à jour des spécifications", sha)
            except Err:
                pass
            return "Spécifications mises à jour.", None
    except Err as e:
        return refuse("Action « %s » en échec : %s" % (kind, e.msg))
    except Exception as e:                                 # bogue inattendue : l'agent reçoit un retour (sinon il attend sans fin) et la trace reste dans la console
        print("Erreur interne dans l'action %s de %s : %r" % (kind, a["name"], e), flush=True)
        return refuse("Action « %s » en échec (erreur interne de l’application : %s). Réessaie ou signale-le à l’agent principal." % (kind, type(e).__name__))
    return refuse("Action inconnue : %s" % kind)


def decide(u, pid, did, decision, note="", fields=None, answer=""):
    """Décision de l'utilisateur : accepter/refuser une proposition ou répondre à une question d'agent."""
    p = snap(u, pid)
    d = by_id(p.get("decisions", []), did)
    if not d or d["status"] != "attente":
        raise Err(404, "Décision introuvable ou déjà traitée")
    ag = by_id(p["agents"], d["by"])
    title = d.get("question") or d["payload"].get("title") or d["payload"].get("name")
    if decision == "dismiss":                              # question sans objet (ex. cause inventée par un agent) : on la classe
        def dis(pp):
            dd = by_id(pp["decisions"], did)
            dd.update(status="ignorée", note=str(note or "Classée sans suite par l’utilisateur."), done=now(), n_done=rights.next_seq(pp))
            t = by_id(pp["tasks"], dd.get("task"))
            if t and t.get("blocked") == did:
                t.pop("blocked", None)
                t["status"], t["touched"] = "à faire", time.time()
        mp(u, pid, dis)
        return log(u, pid, "Vous", d["byName"], "Décision classée sans suite : %s" % title)
    if d["kind"] == "question":
        ans = str(answer or "").strip()
        if not ans:
            raise Err(400, "Réponse vide")
        opts, tid = d.get("options") or [], d.get("task") or ""
        nt = str(note or "").strip() if ans in opts else ""       # précision saisie avec le clic sur une option
        full = ans + (" — précision de l’utilisateur : " + nt if nt else "")

        def f(pp):
            dd = by_id(pp["decisions"], did)
            dd.update(status="répondue", answer=full, done=now(), n_done=rights.next_seq(pp))
            rc = dd.get("ruleconf")
            if rc:                                         # question « règle en conflit » : 0 remplacer, 1 garder la spécifique, 2 sujets différents
                k = opts.index(ans) if ans in opts else -1
                ins = dict(rc["ins"])
                if k == 0:
                    rights.add_instruction(pp, ins, list(rc.get("replaces") or []) + list(rc.get("old") or []))
                    for o in rc.get("old_text") or []:             # règle en texte d'un agent : retirée de ses règles
                        ag2 = by_id(pp["agents"], o["agent"])
                        if ag2:
                            ag2["rules"] = rulebook.remove_rule(ag2.get("rules"), o["raw"])
                elif k == 2:
                    ins.pop("subject", None)
                    rights.add_instruction(pp, ins, rc.get("replaces") or [])
            if dd.get("structural"):
                rights.add_instruction(pp, {"id": uid(), "by": "Décision", "scope": "global", "key": "dec:" + rights.norm(dd["question"]),
                                            "text": "Décision (%s) : %s → %s" % (dd["byName"], dd["question"], full)})
            t = by_id(pp["tasks"], tid)
            if t and t.get("blocked") == did:
                t.pop("blocked", None)
                t["touched"] = time.time()
                if not dd.get("escalation"):
                    t["status"] = "en cours"
        mp(u, pid, f)
        log(u, pid, "Vous", ag["name"] if ag else "Suivi", "Réponse à la question « %s » : %s" % (title, full))
        if d.get("review"):                                   # validation de fin de tâche par l'utilisateur
            if ans == opts[0]:
                log(u, pid, "Système", "Vous", apply_review(u, pid, tid, "Vous", "accepted", "Validée par l’utilisateur" + (" — " + nt if nt else "")))
            else:
                log(u, pid, "Système", "Vous", apply_review(u, pid, tid, "Vous", "rejected", (nt or "Refusée par l’utilisateur") if ans == opts[1] else ans))
        elif d.get("wf_run"):                                 # suite d'un workflow mis en pause
            rid, i = d["wf_run"], d.get("wf_step", 0)

            def g(pp):
                run = by_id(pp.get("wf_runs", []), rid)
                if not run or run["status"] != "en cours":
                    return None
                run.pop("waiting", None)
                if ans == "Arrêter le workflow":
                    run.update(status="arrêté", note="arrêté par l’utilisateur", ended=now())
                    return "stop"
                if (d.get("wf_after") == "ko" and ans == "Relancer cette étape") or d.get("wf_after") == "sysconfirm":
                    run["step"] = i
                    if d.get("wf_after") == "sysconfirm":
                        run["confirmed"] = i               # confirmée : l'étape système s'exécute sans redemander
                    return "next"
                if d.get("wf_after") != "ko":
                    run.setdefault("results", []).append({"i": i + 1, "outcome": "ok", "comment": full[:300], "d": now()})
                run["step"] = i + 1
                return "next"
            if mp(u, pid, g) == "next":
                wf_continue(u, pid, rid)
        elif d.get("conflict"):                               # qui résout le conflit de fusion
            cid = d["conflict"]
            if by_id(snap(u, pid).get("conflicts", []), cid):
                if ans == opts[2]:
                    gitc = bool(by_id(snap(u, pid)["conflicts"], cid).get("git"))
                    mp(u, pid, lambda pp: by_id(pp["conflicts"], cid).update(status="pause", **({"mode": "manual"} if gitc else {})))
                    log(u, pid, "Système", "Vous", ("Conflit mis en pause : résolvez-le dans le dépôt local (fusionnez la cible dans la branche de l’auteur, ou fusionnez la merge request vous-même) puis utilisez « Vérifier » sur le conflit."
                                                    if gitc else "Conflit mis en pause : résolvez-le sur GitHub puis utilisez « Réessayer » sur la merge request."))
                else:
                    mgr = by_id(p["agents"], p.get("mainId"))
                    c = by_id(snap(u, pid)["conflicts"], cid)
                    resolve_conflict(u, pid, cid, mgr["id"] if (ans == opts[1] and mgr) else c["agent"])
        elif d.get("ruleconf"):                             # règle en conflit : la réponse est appliquée plus haut ; la règle arrive aux agents via leurs instructions
            pass
        elif d.get("mr_review"):                              # validation de merge request par l'utilisateur
            log(u, pid, "Système", "Vous", apply_mr_review(u, pid, d["mr_review"], "Vous", "approved" if ans == opts[0] else "rejected", nt if ans == opts[0] else (nt or "Refusée par l’utilisateur")))
        elif d.get("escalation"):                             # décision sur une tâche bloquée
            if len(opts) > 2 and ans == opts[1]:
                mp(u, pid, lambda pp: by_id(pp["tasks"], tid).__setitem__("status", "en attente"))
            elif len(opts) > 2 and ans == opts[2]:
                mp(u, pid, lambda pp: by_id(pp["tasks"], tid).update(status="terminée", abandoned=True, result="Abandonnée sur décision de l’utilisateur." + (" " + nt if nt else "")))
            else:
                mp(u, pid, lambda pp: by_id(pp["tasks"], tid).update(relaunch_n=0, sup_n=0))
                run_task(u, pid, tid, note="[Décision de l’utilisateur] " + (("Reprends la tâche." + (" Précision : " + nt if nt else "")) if ans == opts[0] else full))
        elif tid and ag:                                      # l'agent reprend lui-même sa tâche, sans nouvelle demande
            run_task(u, pid, tid, note="[Décision de l’utilisateur] Réponse à ta question « %s » : %s\nReprends la tâche en tenant compte de cette décision." % (title, full))
        elif ag:
            ask(u, pid, ag["id"], "Réponse de l’utilisateur à ta question « %s » : %s" % (title, full), "Vous")
        return
    if decision == "accept" and d["kind"] == "check":      # l'approbation porte sur le texte EXACT de la commande : aucun champ modifiable
        pay = dict(d["payload"])

        def gc(pp):
            msg, cid = checks_apply(pp, d, pay)
            by_id(pp["decisions"], did).update(status="acceptée", done=now(), n_done=rights.next_seq(pp))
            return msg, cid
        msg, cid = mp(u, pid, gc)
        log(u, pid, "Système", d["byName"], "Proposition acceptée — %s" % msg)
        spawn(check_baseline, u, pid, cid)
        return
    if decision == "accept":
        pay = dict(d["payload"])
        pay.update({k: v for k, v in (fields or {}).items() if k in ("title", "desc", "name") and str(v).strip()})

        def g(pp):
            msg = rights.create_from(pp, d["kind"], pay, d["byName"])
            by_id(pp["decisions"], did).update(status="acceptée", payload=pay, done=now(), n_done=rights.next_seq(pp))
            return msg
        log(u, pid, "Système", d["byName"], "Proposition acceptée — %s" % mp(u, pid, g))
        return
    mp(u, pid, lambda pp: by_id(pp["decisions"], did).update(status="refusée", note=str(note or ""), done=now(), n_done=rights.next_seq(pp)))
    log(u, pid, "Vous", d["byName"], "Proposition « %s » refusée.%s" % (title, (" Motif : " + note) if note else ""))
    if ag:
        ask(u, pid, ag["id"], "L’utilisateur a refusé ta proposition « %s ».%s" % (title, (" Motif : " + note) if note else ""), "Vous")


def instr_consolidate(u, pid):
    """Consolide les instructions actives : un agent (sans mémoire) fusionne les doublons et supprime les instructions
    contredites ou obsolètes (la plus récente l'emporte) ; résultat appliqué par l'action instr_rewrite."""
    p = snap(u, pid)
    ins = [i for i in p.get("instructions", []) if i.get("active", True)]
    an = next((g for g in [by_id(p["agents"], p.get("mainId"))] + p["agents"] if g and g.get("mode") != "manual"), None)
    if not ins or not an:
        return mp(u, pid, lambda pp: pp.setdefault("instr_consol", {}).update(analyzing="", ts=now(), note="Aucune instruction à consolider." if not ins else "Impossible : aucun agent en mode API."))
    mp(u, pid, lambda pp: pp.setdefault("instr_consol", {}).update(analyzing=an["id"], note="Consolidation en cours…"))
    log(u, pid, "Système", an["name"], "Consolidation des instructions demandée à %s (%d instructions actives)." % (an["name"], len(ins)))
    liste = "\n".join("- n°%s [%s]%s %s" % (i.get("n"), i.get("scope", "global"), (" {sujet : %s}" % i["subject"]) if i.get("subject") else "", i["text"]) for i in ins)
    msg = ("[Consolidation des instructions] Voici TOUTES les instructions actives de l'utilisateur, de la plus ancienne à la plus récente. Elles se sont accumulées : certaines sont des doublons, "
           "d'autres sont contredites ou rendues obsolètes par une plus récente (la plus récente l'emporte), d'autres ne sont que des décisions ponctuelles déjà appliquées. "
           "Produis la liste MINIMALE et sans contradiction qui conserve toute l'intention actuelle de l'utilisateur, sans rien inventer ni perdre de contrainte encore valable. "
           "Réponds par UNE action instr_rewrite {\"items\":[{\"text\":\"...\",\"scope\":\"global|role:<rôle>|agent:<prénom>|module:<module>\",\"subject\":\"<sujet, à conserver s’il y en avait un>\",\"replaces\":[<n° remplacés>]}],\"obsolete\":[<n° simplement obsolètes, à supprimer sans remplaçant>]}. "
           "Une instruction non citée dans replaces ni obsolete reste inchangée.\n\n" + liste)
    try:
        ask(u, pid, an["id"], msg, "Consolidation des instructions", relog=False, fresh=True)
    except Err as e:
        return mp(u, pid, lambda pp: pp.setdefault("instr_consol", {}).update(analyzing="", note="Consolidation impossible : %s" % e.msg))
    mp(u, pid, lambda pp: pp.setdefault("instr_consol", {}).update(analyzing="", note=pp["instr_consol"].get("note") if pp["instr_consol"].get("note", "").endswith("désactivée(s).") else "Aucun changement proposé par l’agent."))


def rule_submit(u, pid, ins, replaces=(), agent=None):
    """Enregistre une règle de l'utilisateur (instruction). Si elle porte le même sujet qu'une règle plus spécifique encore active
    et visant aussi les mêmes agents (ex. règle globale récente vs règle d'un agent), elle ne la remplace pas d'office : une
    question est posée à l'utilisateur (remplacer / garder la spécifique / sujets différents) et la règle attend sa réponse.
    Retourne {"off": [n° désactivés]} ou {"pending": n° de la décision}."""
    res = {}

    def f(pp):
        olds = rights.rule_conflicts(pp, ins)
        if not olds:
            res["off"] = rights.add_instruction(pp, ins, replaces)
            return
        ns = [x["n"] for x in olds if x["kind"] == "ins"]
        txt = [{"agent": x["agent"], "raw": x["raw"]} for x in olds if x["kind"] == "agent"]
        lab = ", ".join(("n°%s [%s]" % (x["n"], x["scope"])) if x["kind"] == "ins" else "[%s] « %s »" % (x["scope"], x["text"][:60]) for x in olds)
        d = {"id": uid(), "n": rights.next_seq(pp), "kind": "question", "by": (agent or {}).get("id") or pp.get("mainId") or "",
             "byName": (agent or {}).get("name") or "Vous", "d": now(), "status": "attente", "task": "", "structural": False,
             "question": "Nouvelle règle [%s] sur le sujet « %s » : « %s ». Elle est plus récente mais plus large que la règle %s, qui l’emporte aujourd’hui pour ses agents. Que faire ?" % (
                 ins.get("scope", "global"), rulebook.SUBJECTS.get(ins.get("subject"), ins.get("subject")), str(ins.get("text", ""))[:300], lab),
             "options": ["Remplacer la règle %s par la nouvelle" % lab, "Garder la règle spécifique (elle reste prioritaire)", "Les deux : ce sont des sujets différents"],
             "recommendation": "", "impact": "Sans réponse, la nouvelle règle n’est pas appliquée.",
             "ruleconf": {"ins": dict(ins), "replaces": [r for r in (replaces or ())], "old": ns, "old_text": txt}}
        add_decision(pp, d)
        res["pending"] = d["n"]
    mp(u, pid, f)
    return res


def instr_add(u, pid, scope, text, replaces=(), subject=""):
    text = text.strip()
    if not text:
        raise Err(400, "Instruction vide")
    sc = scope if scope == "global" or scope.split(":")[0] in ("role", "agent", "module") else "global"
    ins = {"id": uid(), "by": "Vous", "scope": sc, "text": text}
    if rights.clean_subject(subject):
        ins["subject"] = subject
    res = rule_submit(u, pid, ins, replaces)
    if "pending" in res:
        return log(u, pid, "Système", "Vous", "Règle en attente de votre réponse (décision n° %s) : elle touche le même sujet qu’une règle plus spécifique : %s" % (res["pending"], text))
    log(u, pid, "Système", "Vous", "Instruction enregistrée (%s) : %s%s" % (sc, text, (" — remplace n° " + ", ".join(map(str, res["off"]))) if res.get("off") else ""))


def git_act(u, pid, p, a, x, kind, t0=0):
    """Actions d'un agent en mode git local : fichiers de SON worktree (aucun verrou : une branche = un agent), puis commit, diff,
    recherche, synchronisation avec la cible, historique. Tout git est exécuté par l'application (gitlocal), jamais par l'agent."""
    refuse = lambda m: (m, m)
    try:
        lg, wt = git_workspace(u, pid, p, a)
    except Err as e:
        return refuse("Espace git indisponible : %s" % e.msg)
    pw = dict(p, localdir=wt)                              # locks.* confine déjà tout accès à un dossier : ici, le worktree de l'agent
    tgt = git_target(p)
    ident = lambda: {"agent": a["name"], "service": str(a.get("service") or "")}
    if kind in ("list_files", "read_file", "write_file", "delete_file"):
        if str(x.get("agent") or "").strip():
            return refuse("En mode git local, tu ne vois que ton propre worktree : pour le travail d'un autre agent, utilise git_show (rev = sa branche).")
        path = safe_path(x.get("path"), write=kind in ("write_file", "delete_file"))
        if kind == "list_files":
            items = locks.list_dir(pw, path)
            if items is None:
                return refuse("Dossier introuvable : /%s" % path)
            lines = "\n".join("- %s%s" % (i["name"], "/" if i["type"] == "dir" else " (%s o)" % i["size"]) for i in items)
            return "Liste de /%s (%d éléments, worktree git)" % (path, len(items)), "Contenu de /%s dans ton worktree :\n%s" % (path, lines or "(vide)")
        if kind == "read_file":
            t = locks.read_text(pw, path)
            if t is None:
                return refuse("Fichier introuvable : %s" % path)
            if x.get("_raw"):
                return None, t
            body, lab, nxt = read_view(t, x)
            return "Lecture de %s (%d caractères)" % (path, len(t)), "Fichier %s (ton worktree)%s :\n%s\n%s\n%s%s" % (
                path, lab, F, body, F, ("\n" + nxt) if nxt else "")
        content = str(x.get("content", "")) if kind == "write_file" else None
        if content is not None and len(content) > 200000:
            raise Err(413, "Fichier trop volumineux (200 000 caractères maximum)")
        with locks.IO:
            locks.check_stop(u, pid, a["id"], t0)
            if kind == "write_file":
                locks.write_text(pw, path, content)
            elif not locks.delete(pw, path):
                return refuse("Fichier introuvable : %s" % path)
        return "Fichier %s dans ton worktree : %s (non commité : utilise l’action commit)" % ("écrit" if kind == "write_file" else "supprimé", path), None
    if kind == "status":
        st = lg.status(wt)
        return "État du worktree (%d)" % len(st), "Branche %s, cible %s. %s" % (
            lg.current_branch(wt) or "?", tgt, ("Modifications non commitées :\n" + "\n".join("%s %s" % (i["code"], i["path"]) for i in st)) if st else "Rien à committer.")
    if kind == "commit":
        locks.check_stop(u, pid, a["id"], t0)
        msg = str(x.get("message") or "").strip() or "Mise à jour"
        sha = lg.commit(wt, msg, task=str(x.get("task") or ""), **ident())
        if not sha:
            return "Rien à committer", "Rien à committer : aucun changement dans ton worktree."
        record_commit(u, pid, a["name"], msg, sha)
        return "Commit %s : %s" % (sha[:7], msg), "Commit %s enregistré sur ta branche (local : il n’est poussé nulle part)." % sha[:7]
    if kind == "diff":
        d = lg.diff(wt, tgt, budget=15000)
        return "Diff avec %s" % tgt, d or "Aucune différence avec %s." % tgt
    if kind == "search":
        r = lg.search(wt, x.get("pattern"))
        return "Recherche « %s » (%d résultat(s))" % (str(x.get("pattern"))[:40], len(r)), "\n".join(r) or "Aucun résultat."
    if kind == "git_log":
        return "Historique", lg.log(wt, int(x.get("n") or 20) if str(x.get("n") or "20").isdigit() else 20) or "(vide)"
    if kind == "git_show":
        return "Lecture d'une version", lg.show(wt, str(x.get("rev") or "HEAD"), x.get("path") or None)
    if kind == "sync":
        locks.check_stop(u, pid, a["id"], t0)
        if x.get("abort"):
            lg.abort(wt)
            return "Synchronisation annulée", "Synchronisation annulée : ton worktree est revenu à son dernier commit."
        if lg.status(wt):
            return refuse("Synchronisation refusée : tu as des modifications non commitées. Fais d’abord commit.")
        src = git_source(lg, p)
        r = lg.sync(wt, src, a["name"])
        if r["state"] == "conflict":
            return "Synchronisation avec %s : conflit" % tgt, ("Conflit avec %s dans : %s.\nOuvre chaque fichier : les marqueurs <<<<<<< ======= >>>>>>> montrent les deux versions. "
                                                           "Retire les marqueurs avec edit_file (write_file seulement pour un petit fichier), puis fais commit. Pour renoncer : {\"do\":\"sync\",\"abort\":true}.") % (tgt, ", ".join(r["files"]))
        return "Synchronisation avec %s : %s" % (tgt, r["state"]), {"uptodate": "Ta branche contient déjà %s.", "merged": "%s a été intégrée à ta branche (aucun conflit)."}[r["state"]] % tgt
    return refuse("Action inconnue : %s" % kind)


def local_act(u, pid, p, a, x, kind, t0=0):
    """Fichiers du RÉPERTOIRE LOCAL du projet. Toute écriture exige le verrou du fichier : s'il est libre il est pris le temps de l'écriture,
    s'il est tenu par un autre agent la demande attend qu'il soit libéré (voir locks.acquire)."""
    path = safe_path(x.get("path"), write=kind in ("write_file", "delete_file"))
    refuse = lambda m: (m, m)
    if kind == "list_files":
        items = locks.list_dir(p, path)
        if items is None:
            return refuse("Dossier introuvable : /%s" % path)
        lines = "\n".join("- %s%s" % (i["name"], "/" if i["type"] == "dir" else " (%s o)" % i["size"]) for i in items)
        return "Liste de /%s (%d éléments, répertoire local)" % (path, len(items)), "Contenu de /%s dans le répertoire local :\n%s" % (path, lines or "(vide)")
    if kind == "read_file":
        t = locks.read_text(p, path)
        if t is None:
            return refuse("Fichier introuvable : %s" % path)
        if x.get("_raw"):
            return None, t
        l = locks.holder_of(p, path)
        warn = ("\n⚠ Ce fichier est verrouillé par %s (modifications en cours) : son contenu peut changer." % l["name"]) if l and l["by"] != a["id"] else ""
        body, lab, nxt = read_view(t, x)
        return "Lecture de %s (%d caractères)" % (path, len(t)), "Fichier %s (répertoire local)%s :\n%s\n%s\n%s%s%s" % (path, lab, F, body, F, ("\n" + nxt) if nxt else "", warn)
    content = None
    if kind == "write_file":
        content = str(x.get("content", ""))
        if len(content) > 200000:
            raise Err(413, "Fichier trop volumineux (200 000 caractères maximum)")
    held = locks.held_by(p, path, a["id"])
    if not held:
        ok, m = locks.acquire(u, pid, a["id"], [path], note="écriture", since=t0, log=lambda m: log(u, pid, "Système", "Vous", m))
        if not ok:
            return refuse("Écriture refusée : " + m)
    try:
        with locks.IO:                                     # jamais en même temps qu'une restauration (annulation du travail de l'agent)
            locks.check_stop(u, pid, a["id"], t0)
            if not locks.held_by(snap(u, pid), path, a["id"]):
                return refuse("Écriture refusée : ton verrou sur %s a été libéré (expiré ou libéré par l’agent principal). Redemande-le avec lock_file puis recommence." % path)
            msg = str(x.get("message") or "") or ("update %s" % path if kind == "write_file" else "delete %s" % path)
            if kind == "write_file":
                locks.write_text(p, path, content)
            elif not locks.delete(p, path):
                return refuse("Fichier introuvable : %s" % path)
    finally:
        if not held:
            locks.release(u, pid, a["id"], [path])         # verrou « éphémère » : pris pour cette écriture seulement
    if held:
        locks.touch(u, pid, a["id"])
    record_commit(u, pid, a["name"], msg, "local")
    return "Fichier %s dans le répertoire local : %s%s" % ("écrit" if kind == "write_file" else "supprimé", path, " (verrou conservé)" if held else ""), None


def raw_read(u, pid, p, a, path, t0):
    """Contenu intégral d'un fichier de l'agent (quel que soit son espace de travail) : (texte, None), ou (None, motif) s'il n'existe pas."""
    note, t = repo_act(u, pid, p, a, {"do": "read_file", "path": path, "_raw": True}, "read_file", t0)
    return (t, None) if note is None else (None, t)


def locked_rmw(u, pid, p, a, path, t0, fn):
    """Exécute `fn()` (lecture puis écriture du fichier) en tenant le verrou du fichier pendant TOUTE l'opération quand l'agent travaille sur le répertoire
    local partagé : personne ne peut modifier le fichier entre la lecture et l'écriture. Verrou déjà tenu par l'agent : conservé ; pris ici : rendu à la fin.
    Aux autres modes (worktree git personnel, GitHub) : pas de verrou, `fn()` directement."""
    if git_agent(p, a) or not local_agent(p, a):
        return fn()
    held = locks.held_by(p, path, a["id"])
    if not held:
        ok, m = locks.acquire(u, pid, a["id"], [path], note="écriture", since=t0, log=lambda m: log(u, pid, "Système", "Vous", m))
        if not ok:
            return (lambda m: (m, m))("Écriture refusée : " + m)
    try:
        r = fn()
    finally:
        if not held:
            locks.release(u, pid, a["id"], [path])
    if not held and isinstance(r, tuple) and isinstance(r[0], str):
        r = (r[0].replace(" (verrou conservé)", ""), r[1])           # verrou éphémère : rendu, pas conservé
    return r


def file_act(u, pid, p, a, x, kind, t0=0):
    """Actions fichiers : lecture/écriture/suppression (repo_act) et, au-dessus, `edit_file` (remplacements ciblés) et `write_chunk`
    (écriture en morceaux reconstitués ici), plus le garde-fou contre l'écriture d'un fichier tronqué."""
    refuse = lambda m: (m, m)
    if kind == "read_file" and not str(x.get("agent") or "").strip():
        r = repo_act(u, pid, p, a, x, kind, t0)
        k = (str(u), pid, a["id"], str(x.get("path")), tuple(str(x.get(c)) for c in ("from_line", "to_line", "offset", "limit")))
        now_t = time.time()
        with CHUNK_LOCK:
            for kk in [kk for kk, v in READS.items() if now_t - v[1] > CHUNK_TTL]:
                READS.pop(kk, None)
            n = READS.get(k, (0, 0))[0] + 1
            READS[k] = (n, now_t)
        if n >= REREAD_WARN and r[1] is not None and r[0] is not r[1]:
            r = (r[0], r[1] + "\n⚠ C’est la lecture n°%d de ce même extrait sans écriture entre-temps. Relire en boucle ne sert à rien : passe à l’étape suivante. "
                 "Pour modifier, utilise edit_file avec un passage exact copié ci-dessus (pas besoin du fichier entier) ; pour un autre passage, lis seulement la zone utile (from_line/to_line) ; "
                 "pour garder une information d’un tour à l’autre, note-la avec l’action context. Si tu es réellement bloqué, dis-le (ask_user)." % n)
        return r
    if kind in ("edit_file", "write_chunk", "write_file", "delete_file"):
        r = _file_write(u, pid, p, a, x, kind, t0)
        if r[1] is None or kind == "write_chunk" and r[0] != r[1]:      # écriture réussie : les relectures de ce fichier repartent de zéro
            with CHUNK_LOCK:
                for kk in [kk for kk in READS if kk[:3] == (str(u), pid, a["id"]) and kk[3] == str(x.get("path"))]:
                    READS.pop(kk, None)
        return r
    return repo_act(u, pid, p, a, x, kind, t0)


def _file_write(u, pid, p, a, x, kind, t0=0):
    refuse = lambda m: (m, m)
    if kind == "edit_file":
        return edit_act(u, pid, p, a, x, t0)
    if kind == "write_chunk":
        return chunk_act(u, pid, p, a, x, t0)
    if kind == "write_file" and not x.get("confirm_shrink"):
        path = safe_path(x.get("path"), write=True)

        def guarded():
            old, _ = raw_read(u, pid, p, a, path, t0)
            new = str(x.get("content", ""))
            if old is None or len(old) < SHRINK_GUARD[0] or len(new) >= len(old) * SHRINK_GUARD[1]:
                return repo_act(u, pid, p, a, x, kind, t0)
            return refuse("Écriture refusée : %s fait %d caractères et ton contenu seulement %d. write_file remplace le fichier ENTIER : ce contenu semble tronqué ou incomplet "
                          "(les anciens résultats de lecture sont abrégés dans ta mémoire). Rien n’a été écrit. Modifie une partie avec edit_file (relis d’abord la zone avec read_file "
                          "from_line/to_line), écris un gros fichier avec write_chunk, ou, si le raccourcissement est voulu, renvoie l’action avec \"confirm_shrink\":true." % (path, len(old), len(new)))
        return locked_rmw(u, pid, p, a, path, t0, guarded)
    return repo_act(u, pid, p, a, x, kind, t0)


def edit_act(u, pid, p, a, x, t0=0):
    """`edit_file` : applique des remplacements de texte à un fichier existant, tout ou rien. Chaque `old` doit apparaître exactement une fois (ou `all`)."""
    refuse = lambda m: (m, m)
    path = safe_path(x.get("path"), write=True)
    edits = x.get("edits")
    if edits is None and "old" in x:
        edits = [{"old": x.get("old"), "new": x.get("new", ""), "all": x.get("all")}]
    if not isinstance(edits, list) or not edits or len(edits) > 50 or not all(isinstance(e, dict) and str(e.get("old", "")) for e in edits):
        return refuse("Édition refusée : « edits » doit être une liste de 1 à 50 objets {\"old\":\"texte exact existant\",\"new\":\"remplacement\"}.")
    def run():
        cur, why = raw_read(u, pid, p, a, path, t0)
        if cur is None:
            return refuse("Édition refusée : " + why)
        crlf = "\r\n" in cur
        for i, e in enumerate(edits, 1):
            old, new = str(e["old"]), str(e.get("new", ""))
            if crlf and "\r\n" not in old:                      # fichier Windows : l'agent écrit des fins de ligne « \n »
                old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            n = cur.count(old)
            if n == 0:
                return refuse("Édition %d refusée : le texte « old » est introuvable dans %s (copie-le exactement, espaces et fins de ligne compris : relis la zone avec read_file from_line/to_line). Aucune modification n’a été faite." % (i, path))
            if n > 1 and not e.get("all"):
                return refuse("Édition %d refusée : le texte « old » apparaît %d fois dans %s. Allonge-le avec le contexte voisin pour qu’il soit unique, ou ajoute \"all\":true. Aucune modification n’a été faite." % (i, n, path))
            cur = cur.replace(old, new) if e.get("all") else cur.replace(old, new, 1)
        if len(cur) > 200000:
            raise Err(413, "Fichier trop volumineux (200 000 caractères maximum)")
        r = repo_act(u, pid, p, a, {"do": "write_file", "path": path, "content": cur, "message": str(x.get("message") or "") or "edit %s" % path}, "write_file", t0)
        if r[1] is None:
            return "%s — %d modification(s)" % (r[0], len(edits)), None
        return r
    return locked_rmw(u, pid, p, a, path, t0, run)


def chunk_act(u, pid, p, a, x, t0=0):
    """`write_chunk` : reçoit un fichier en morceaux numérotés (1..total, dans n'importe quel ordre), garde les morceaux en mémoire, puis, dès qu'ils sont tous là,
    reconstitue le fichier, vérifie l'empreinte `sha256` si elle est fournie et l'écrit comme write_file. Rien n'est écrit tant qu'il manque un morceau."""
    refuse = lambda m: (m, m)
    path = safe_path(x.get("path"), write=True)
    try:
        idx, total = int(x.get("index")), int(x.get("total"))
    except (TypeError, ValueError):
        return refuse("Morceau refusé : « index » et « total » doivent être des entiers.")
    if not 1 <= total <= CHUNK_MAX or not 1 <= idx <= total:
        return refuse("Morceau refusé : index %s / total %s invalides (1 ≤ index ≤ total ≤ %d)." % (x.get("index"), x.get("total"), CHUNK_MAX))
    key, now = (str(u), pid, a["id"], path), time.time()
    with CHUNK_LOCK:
        for k in [k for k, v in CHUNK_BUF.items() if now - v["ts"] > CHUNK_TTL]:
            CHUNK_BUF.pop(k, None)                         # morceaux orphelins (agent arrêté, application redémarrée…)
        b = CHUNK_BUF.get(key)
        if b is None or b["total"] != total:
            b = CHUNK_BUF[key] = {"total": total, "parts": {}, "ts": now, "message": "", "sha": ""}
        b["parts"][idx], b["ts"] = str(x.get("content", "")), now
        b["message"] = str(x.get("message") or b["message"])
        b["sha"] = str(x.get("sha256") or b["sha"]).strip().lower()
        missing = [i for i in range(1, total + 1) if i not in b["parts"]]
        if missing:
            return "Morceau %d/%d de %s reçu" % (idx, total, path), "Morceau %d/%d reçu (%d caractères). Manquants : %s. Rien n’est encore écrit." % (idx, total, len(b["parts"][idx]), ", ".join(map(str, missing)))
        full = "".join(b["parts"][i] for i in range(1, total + 1))
        CHUNK_BUF.pop(key, None)
        msg, want = b["message"], b["sha"]
    if len(full) > 200000:
        raise Err(413, "Fichier trop volumineux (200 000 caractères maximum)")
    got = hashlib.sha256(full.encode("utf-8")).hexdigest()
    if want and want != got:
        return refuse("Reconstitution de %s refusée : empreinte sha256 différente (attendue %s…, obtenue %s…). Rien n’a été écrit : renvoie tous les morceaux." % (path, want[:12], got[:12]))
    r = file_act(u, pid, p, a, {"do": "write_file", "path": path, "content": full, "message": msg or "update %s" % path, "confirm_shrink": x.get("confirm_shrink")}, "write_file", t0)
    if r[1] is None:
        return "%s — reconstitué de %d morceaux (%d caractères, sha256 %s…)" % (r[0], total, len(full), got[:12]), "Fichier %s reconstitué à partir de %d morceaux : %d caractères, sha256 %s." % (path, total, len(full), got)
    return r


def repo_act(u, pid, p, a, x, kind, t0=0):
    tgt0 = str(x.get("agent") or "").strip()
    other = next((g for g in p["agents"] if g["name"].lower() == tgt0.lower()), None) if tgt0 and kind in ("list_files", "read_file") else None
    if git_agent(p, a):
        return git_act(u, pid, p, a, x, kind, t0)
    if (local_agent(p, a) and not (tgt0 and kind in ("list_files", "read_file"))) or (other and local_agent(p, other)):
        return local_act(u, pid, p, a, x, kind, t0)
    rp, b = github.Repo(p), a["branch"]
    path = safe_path(x.get("path"), write=kind in ("write_file", "delete_file"))
    if kind in ("write_file", "delete_file") and b in (rp.base, rp.work):
        m = "Écriture refusée : ta branche est la branche de base (%s)." % b
        return m, m
    tgt = str(x.get("agent") or "").strip()
    if tgt and kind in ("list_files", "read_file"):      # lecture seule sur la branche d'un autre agent (ou « base »)
        g = next((g for g in p["agents"] if g["name"].lower() == tgt.lower()), None)
        if g:
            b = g["branch"]
        elif tgt.lower() == "base":
            b = rp.base
        else:
            m = "Agent inconnu : %s." % tgt
            return m, m
    else:
        rp.ensure_branch(b)
    if kind == "list_files":
        items = rp.list_dir(b, path)
        if items is None:
            m = "Dossier introuvable : /%s" % path
            return m, m
        lines = "\n".join("- %s%s" % (i["name"], "/" if i["type"] == "dir" else " (%s o)" % i["size"]) for i in items)
        return "Liste de /%s (%d éléments)" % (path, len(items)), "Contenu de /%s sur %s :\n%s" % (path, b, lines or "(vide)")
    if kind == "read_file":
        t = rp.read_file(b, path)
        if t is None:
            m = "Fichier introuvable : %s" % path
            return m, m
        if x.get("_raw"):
            return None, t
        body, lab, nxt = read_view(t, x)
        return "Lecture de %s (%d caractères)" % (path, len(t)), "Fichier %s%s :\n%s\n%s\n%s%s" % (path, lab, F, body, F, ("\n" + nxt) if nxt else "")
    msg = str(x.get("message") or "")
    if kind == "write_file":
        content = str(x.get("content", ""))
        if len(content) > 200000:
            raise Err(413, "Fichier trop volumineux (200 000 caractères maximum)")
        msg = msg or "update %s" % path
        sha = rp.put_file(b, path, content, msg)
        record_commit(u, pid, a["name"], msg, sha)
        return "Fichier écrit sur %s : %s (%s)" % (b, path, sha[:7]), None
    msg = msg or "delete %s" % path
    sha = rp.delete_file(b, path, msg)
    record_commit(u, pid, a["name"], msg, sha)
    return "Fichier supprimé sur %s : %s (%s)" % (b, path, sha[:7]), None


def lock_report(p):
    items = [i for i in locks.diag(p)]
    return "Verrous de fichiers (répertoire local) :\n" + ("\n".join("- [%s] %s %s" % (i["level"], i["who"], i["text"]) for i in items) if items else "(aucun verrou, personne en attente)")


def lock_act(u, pid, p, a, x, kind, mn, t0=0):
    refuse = lambda m: (m, m)
    byname = lambda n: next((g for g in p["agents"] if g["name"].lower() == str(n or "").lower()), None)
    sysl = lambda m: log(u, pid, "Système", "Vous", m)
    if kind == "get_locks":
        return "", lock_report(p)
    if kind in ("lock_file", "unlock_file"):
        if git_agent(p, a):
            return refuse("Pas de verrou en mode git local : ton worktree est à toi seul (une branche = un agent).")
        if not local_agent(p, a):
            return refuse("Les verrous ne concernent que le répertoire local du projet ; tu travailles sur ta branche GitHub (pas de verrou nécessaire).")
        paths = x.get("paths") if isinstance(x.get("paths"), list) else ([x["path"]] if x.get("path") else [])
        if kind == "lock_file":
            paths = [safe_path(i, write=True) for i in paths if isinstance(i, str)]
            ok, m = locks.acquire(u, pid, a["id"], paths, note=str(x.get("note", "")), since=t0, log=sysl)
            return (m, None) if ok else refuse(m)
        freed = locks.release(u, pid, a["id"], [safe_path(i) for i in paths if isinstance(i, str)])
        return ("Verrou(s) libéré(s) : %s." % ", ".join(freed)) if freed else "Aucun verrou à libérer.", None
    if not mn:
        return refuse("Action réservée à l’agent principal.")
    if kind == "stop_agent":
        t = byname(x.get("agent"))
        if not t or t["id"] == a["id"]:
            return refuse("Agent inconnu (ou toi-même) : %s." % x.get("agent"))
        m = stop_agent(u, pid, t["id"], bool(x.get("cancel")), a["name"], str(x.get("reason", ""))[:300])
        return m, m
    path = safe_path(x.get("path"))
    l = locks.holder_of(p, path)
    if not l:
        return refuse("Aucun verrou sur %s." % path)
    h = by_id(p["agents"], l["by"]) or {}
    if (h.get("work") or 0) > 0 or any(t.get("who") == l["by"] and t.get("running") for t in p["tasks"]):
        return refuse("%s travaille encore : arrête-le d’abord avec stop_agent (ou attends) plutôt que de lui retirer son verrou en cours de route." % l["name"])
    locks.force_release(u, pid, l["id"])
    m = "Verrou sur « %s » (détenu par %s) libéré de force par %s%s." % (path, l["name"], a["name"], (" — " + str(x.get("reason"))[:200]) if x.get("reason") else "")
    return m, m


def git_stop_note(u, pid, p, a, cancel):
    """Arrêt d'un agent en mode git : l'annulation écarte ses modifications NON commitées (ses commits restent) ; un simple arrêt les conserve."""
    try:
        lg = git_repo(u, pid, p)
        with GIT_LOCK:
            wt = lg._wt_path(a["name"])
            if os.path.realpath(wt) not in lg.worktrees():
                return ""
            if cancel:
                files = lg.discard(wt)
                return " Worktree git remis à son dernier commit (%d fichier(s) non commité(s) écarté(s)%s)." % (len(files), (" : " + ", ".join(files[:8])) if files else "")
            st = lg.status(wt)
            return (" Modifications non commitées conservées dans son worktree (%d fichier(s)) : enregistrées en « WIP » au prochain changement de tâche, ou commitées à sa reprise." % len(st)) if st else ""
    except Err as e:
        return " Worktree git non traité : %s." % e.msg


def stop_agent(u, pid, aid, cancel=False, by="Vous", reason=""):
    """Arrête le travail d'un agent : ses actions suivantes sont refusées, ses verrous sont libérés ; avec `cancel`, les fichiers qu'il avait
    verrouillés sont remis dans leur état d'avant. Sa tâche passe « en attente » (à relancer). Un appel IA déjà parti n'est pas interrompu,
    mais ses actions ne sont pas exécutées."""
    p = snap(u, pid)
    a = by_id(p["agents"], aid)
    if not a:
        raise Err(404, "Agent introuvable")
    reason = reason or "décision de %s" % by
    locks.request_stop(u, pid, aid, cancel, by, reason)
    for pe in p.get("pending", []):                         # échange manuel en attente : annulé
        w = WAIT.get(pe["id"])
        if pe["agent"] == aid and w and w["u"] == u:
            w["ev"].set()
    freed, restored, lost = locks.release_agent(u, pid, aid, restore=cancel)
    gnote = git_stop_note(u, pid, p, a, cancel) if git_agent(p, a) else ""

    def f(pp):
        ag = by_id(pp["agents"], aid)
        live = (ag.get("work") or 0) > 0
        for t in pp["tasks"]:
            if t.get("who") == aid and (t.get("running") or t["status"] == "en cours"):
                t["status"], t["touched"] = "en attente", time.time()
                t["stopped"] = {"by": by, "reason": reason, "cancel": bool(cancel), "d": now()}
                for k in ("retry_at", "fail"):
                    t.pop(k, None)
                if not live:
                    t["running"] = False
        pp["pending"] = [x for x in pp.get("pending", []) if x["agent"] != aid]
    mp(u, pid, f)
    txt = "%s %s par %s (%s). Verrous libérés : %s.%s%s" % (
        a["name"], "annulé (travail défait)" if cancel else "arrêté", by, reason, ", ".join(freed) or "aucun",
        (" Fichiers remis dans leur état d’avant : %s." % ", ".join(restored)) if restored else "",
        (" Non restaurables (pas de sauvegarde) : %s." % ", ".join(lost)) if lost else "") + gnote
    log(u, pid, "Système", "Vous", txt + " Sa tâche est « en attente » : relancez-la quand vous voulez.")
    log(u, pid, "Système", a["name"], "Tu as été %s : %s. Tes verrous ont été libérés ; attends une nouvelle consigne." % ("arrêté, ton travail est annulé" if cancel else "arrêté", reason))
    return txt


def lock_release(u, pid, lid):
    l = locks.force_release(u, pid, lid)
    log(u, pid, "Système", "Vous", ("Verrou sur « %s » (détenu par %s) libéré à votre demande ; l’agent n’est pas arrêté." % (l["path"], l["name"])) if l else "Verrou introuvable (déjà libéré).")


def locks_clear(u, pid):
    log(u, pid, "Système", "Vous", "%d verrou(x) libéré(s) à votre demande." % locks.clear_all(u, pid))


def localdir_check(u, pid):
    p = snap(u, pid)
    try:
        m = locks.check_dir(p)
    except Err as e:
        m = "Répertoire local inutilisable : " + e.msg
    if git_mode(p):                                        # contrôles préalables git (dossier inutilisable = alerte, agents concernés refusés)
        repo = p.get("repo") or {}
        with GIT_LOCK:
            for k in [k for k in GIT_OK if k[:2] == (u, pid)]:
                del GIT_OK[k]                              # revalide au prochain usage
        pf = gitlocal.preflight(str(p.get("localdir") or "").strip(), (repo.get("url") or "").strip())
        m += "\nMode git local : " + ("dossier utilisable (%s)%s." % ({"git": "dépôt git existant", "empty": "dossier vide : sera initialisé ou cloné", "plain": "dossier non versionné : sera initialisé"}.get(pf["state"], pf["state"]),
                                                                     (" — avertissement : " + " ".join(pf["warnings"])) if pf["warnings"] else "")
                                                     if pf["ok"] else "INUTILISABLE — " + " ".join(pf["problems"]) + " Les agents concernés sont arrêtés sur ce dossier tant que le problème subsiste.")
    log(u, pid, "Système", "Vous", m)


def git_context_write(u, pid, p, a):
    """Écrit le fichier de contexte de l'agent dans `<répertoire local>/.myteam/agents/<prénom>.md` (hors des branches, ignoré par git) :
    il reste stable d'une tâche à l'autre. Retourne le chemin."""
    lg = git_repo(u, pid, p)
    path = os.path.join(lg.root, ".myteam", "agents", gitlocal.slug(a["name"], "agent", 40) + ".md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with locks.IO, open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(ctx_text(p, a))
    return path


def provision(u, pid, aid):
    p = snap(u, pid)
    a = by_id(p["agents"], aid)
    if git_agent(p, a):                                    # git local : worktree de l'agent + fichier de contexte hors des branches
        try:
            lg, wt = git_workspace(u, pid, p, a)
            return "Git local : worktree prêt (branche %s) ; contexte : %s." % (lg.current_branch(wt) or "?", git_context_write(u, pid, p, a))
        except Err as e:
            return "Répertoire local inutilisable pour git : %s" % e.msg
    if local_agent(p, a):                                  # répertoire local : pas de branche ; le fichier de contexte est écrit dans .agents/
        try:
            with locks.IO:
                locks.write_text(p, ".agents/%s.md" % a["name"], ctx_text(p, a), internal=True)
            return "Répertoire local : fichier .agents/%s.md écrit." % a["name"]
        except Err as e:
            return "Répertoire local inutilisable : %s" % e.msg
    try:
        rp = github.Repo(p)
    except Err:
        return "Dépôt GitHub/token non configuré au niveau du projet : branche et fichier de contexte non créés."
    if a["branch"] in (rp.base, rp.work):
        return "La branche de l’agent est la branche de base ou d’intégration : refusé, choisissez une branche dédiée."
    try:
        created = rp.ensure_branch(a["branch"])
        msg = "chore(%s) : contexte agent" % a["name"]
        sha = rp.put_file(a["branch"], ".agents/%s.md" % a["name"], ctx_text(p, a), msg)
        record_commit(u, pid, a["name"], msg, sha)
        return "Branche %s %s, fichier .agents/%s.md poussé." % (a["branch"], "créée" if created else "déjà existante", a["name"])
    except Err as e:
        return "Provisionnement échoué : %s" % e.msg


def onboard(u, pid, aid):
    a = by_id(snap(u, pid)["agents"], aid)
    if a:
        r = provision(u, pid, aid)
        log(u, pid, "Système", a["name"], "Onboarding de %s : règles de l’équipe et du projet transmises (prompt système + fichier de contexte). %s" % (a["name"], r))


def sync_all(u, pid):
    """Pour chaque agent : crée sa branche si besoin et pousse son fichier de contexte (règles à jour).
    Renvoie un compte rendu [{agent, ok, text}] affiché dans l'onglet Équipe ; chaque ligne est aussi tracée dans l'historique."""
    out = []
    for a in snap(u, pid)["agents"]:
        r = provision(u, pid, a["id"])
        log(u, pid, "Système", a["name"], "Synchronisation : " + r)
        out.append({"agent": a["name"], "branch": a.get("branch", ""), "ok": r.startswith(("Branche ", "Répertoire local :")), "text": r})
    return out


def ask_through(u, pid, aid, text, frm, relog=True):
    """`ask` qui ne perd pas le dernier retour d'actions : si les étapes sont épuisées en plein travail (`stats["pending"]`), le retour
    non traité est renvoyé à l'agent (au plus SAY_ROUNDS échanges). Retourne True si l'agent a fini, False s'il restait du travail."""
    stats = {}
    ask(u, pid, aid, text, frm, relog=relog, stats=stats)
    for _ in range(SAY_ROUNDS - 1):
        if not stats.get("pending"):
            return True
        ask(u, pid, aid, stats["pending"].removeprefix("[Application] ") + "\nPoursuis.", "Application", relog=False, stats=stats)
    return not stats.get("pending")


def say(u, pid, aid, text, attempt=0, relog=True):
    try:
        if not ask_through(u, pid, aid, text, "Vous", relog=relog):
            a = by_id(snap(u, pid)["agents"], aid) or {"name": "?"}
            log(u, pid, "Système", a["name"], "%s travaille encore (limite de tours atteinte pour cet échange) : écrivez-lui « continue » pour qu’il reprenne." % a["name"])
        for t in snap(u, pid)["tasks"]:               # une tâche passée « à vérifier » pendant la conversation
            if t["status"] == "à vérifier" and not t.get("reviewing"):
                spawn(review_task, u, pid, t["id"])
        resume_interrupted(u, pid)                    # tâche interrompue par ce dialogue : reprise sans qu'il faille la redemander
    except Err as e:
        a = by_id(snap(u, pid)["agents"], aid) or {"name": "?"}
        if classify(e) in ("quota", "temp") and attempt < 3:
            w = backoff(attempt, e.retry)
            log(u, pid, "Système", a["name"], "Service momentanément indisponible (%s) : nouvelle tentative automatique vers %s." % (
                "quota atteint" if e.code == 429 else "erreur temporaire", time.strftime("%H:%M", time.localtime(time.time() + w))))
            tm = threading.Timer(w, lambda: say(u, pid, aid, text, attempt + 1, False))
            tm.daemon = True
            tm.start()
        else:
            log(u, pid, "Système", a["name"], "Erreur : " + e.msg)


def spawn(fn, *a, **k):
    """Lance un travail dans un thread ; une exception est tracée au lieu de disparaître."""
    def run():
        try:
            fn(*a, **k)
        except Exception as e:
            print("Erreur travail d'arrière-plan (%s) : %s" % (getattr(fn, "__name__", "?"), e), flush=True)
    threading.Thread(target=run, daemon=True).start()


def classify(e):
    """Nature d'un échec d'appel : quota, temp (réseau / 5xx), budget, auth, model ou other."""
    m = e.msg or ""
    if e.code == 429:
        return "quota"
    if e.code == 402:
        return "budget"
    if "Clé API" in m or "(401)" in m or "(403)" in m:
        return "auth"
    if "Modèle ou URL introuvable" in m:
        return "model"
    if "injoignable" in m or re.search(r"\(5\d\d\)", m):
        return "temp"
    return "other"


def backoff(n, retry_after=None):
    """Délai avant la tentative n+1 : au moins le délai du fournisseur, de plus en plus long, puis toutes les heures."""
    return int(min(3600, max(int(retry_after or 60), BACKOFF[min(n, len(BACKOFF) - 1)], 5)))


def schedule(u, pid, tid, wait):
    key = (u, pid, tid)
    if key in TIMERS:
        return
    TIMERS.add(key)

    def go():
        TIMERS.discard(key)
        run_task(u, pid, tid, retry=True)
    t = threading.Timer(wait, go)
    t.daemon = True
    t.start()


def dep_context(p, t):
    L = []
    for i in t.get("deps") or []:
        d = by_id(p["tasks"], i)
        if d:
            g = by_id(p["agents"], d.get("who")) or {}
            L.append("- « %s » (%s, branche %s) : %s" % (d["title"], g.get("name", "?"), g.get("branch", "?"), (d.get("result") or "pas de compte rendu")[:600]))
    return ("\nTâches dont tu dépends (terminées ; leur travail est dans la branche de travail) :\n" + "\n".join(L)) if L else ""


def coordination_hint(p, a):
    if len(p["agents"]) < 2:
        return ""
    return ("\nCoordination : avant de commencer, consulte « Travail des autres agents » ; si ta tâche dépend du travail d’un autre agent ou l’impacte "
            "(interface, contrat d’API, schéma de données, convention), écris-lui d’abord (message) pour convenir de l’approche, puis préviens-le quand tu modifies ce qu’il utilise.")


def _task_tail(t, n=1500):
    return ((t.get("desc") or "").strip()[:n]) or "(pas de description : voir le titre)"


def focus_text(p, a):
    """Section du prompt système (bloc changeant) : la ou les tâches EN COURS de l'agent avec leur consigne d'origine. L'historique envoyé à l'IA est
    borné (30 messages) et peut perdre le message de départ de la tâche : ce rappel, reconstruit à chaque tour depuis l'état, ne dépend pas de lui."""
    L = []
    for t in p.get("tasks", []):
        if t.get("who") != a["id"] or t.get("system") or not (t["status"] == "en cours" or (t.get("interrupted") and t["status"] == "en attente")):
            continue
        s = "- %s : « %s » (statut %s)\n  Consigne d’origine : %s" % (t["id"], t["title"], t["status"], _task_tail(t))
        if t.get("modules"):
            s += "\n  Modules : " + ", ".join(mod_names(p, t["modules"]))
        if t.get("result") and not t.get("running"):
            s += "\n  Dernier compte rendu : " + str(t["result"])[:500]
        if t.get("interrupted"):
            s += "\n  Tu as été interrompu par %s (%s) : réponds à cette sollicitation, puis REPRENDS cette tâche là où tu t’étais arrêté." % (t["interrupted"].get("by", "?"), t["interrupted"].get("d", ""))
        L.append(s)
    return ("# Ta tâche en cours (à reprendre après toute interruption)\n" + "\n".join(L)) if L else ""


def mark_interrupted(u, pid, aid, frm):
    """Une sollicitation (utilisateur, workflow, suivi, autre agent) arrive pendant que l'agent a une tâche en cours : on le note sur la tâche
    pour qu'elle soit rappelée à l'agent (focus_text, boucle de run_task) et reprise automatiquement si plus rien ne la fait avancer."""
    def f(pp):
        for t in pp.get("tasks", []):
            if t.get("who") == aid and t["status"] == "en cours" and not t.get("system"):
                t["interrupted"] = {"by": frm, "d": now(), "ts": time.time()}
    mp(u, pid, f)


def resume_text(p, t, intr=None):
    intr = intr or t.get("interrupted") or {}
    return ("[Reprise après interruption] Tu as été sollicité par %s ; cette sollicitation est traitée. Reprends maintenant ta tâche « %s » (id %s) là où tu t’étais arrêté "
            "(relis ton historique et l’état de ta branche).\nConsigne d’origine : %s\nQuand le travail demandé est réellement fait, conclus avec task_status « à vérifier » et un compte rendu précis ; "
            "si tu es bloqué, pose la question avec ask_user.") % (intr.get("by", "?"), t["title"], t["id"], _task_tail(t))


def resumable(p, t):
    """Tâche interrompue que plus rien ne fait avancer : à relancer (bornée par MAX_RESUME ; jamais si une décision, un blocage, un arrêt ou un conflit l'explique)."""
    if not t.get("interrupted") or t.get("running") or t.get("retry_at") or t.get("fail") or t.get("stopped") or t.get("blocked") or t.get("reviewing"):
        return False
    if t["status"] not in ("en cours", "en attente") or t.get("resume_n", 0) >= MAX_RESUME:
        return False
    if any(d["status"] == "attente" and d.get("task") == t["id"] for d in p.get("decisions", [])):
        return False
    a = by_id(p["agents"], t.get("who"))
    return bool(a) and a.get("mode") != "manual" and not a.get("work") and not deps_unmet(p, t) and not open_conflict_for(p, a["id"])


def resume_interrupted(u, pid):
    """Relance les tâches interrompues qui n'avancent plus (appelée en fin de dialogue / d'étape de workflow et à chaque passage du suivi)."""
    p = snap(u, pid)
    if p.get("autopilot") == "off":
        return 0
    n, busy = 0, set()
    for t in p.get("tasks", []):
        if t.get("who") in busy or not resumable(p, t):
            continue
        busy.add(t["who"])
        a = by_id(p["agents"], t["who"])
        note = resume_text(p, t)

        def f(pp, i=t["id"]):
            tt = by_id(pp["tasks"], i)
            tt["resume_n"] = tt.get("resume_n", 0) + 1
            if tt["status"] == "en attente":
                tt["status"] = "à faire"
        mp(u, pid, f)
        log(u, pid, "Système", a["name"], "Tâche « %s » interrompue par %s : reprise automatique (%d/%d)." % (t["title"], t["interrupted"].get("by", "?"), t.get("resume_n", 0) + 1, MAX_RESUME))
        spawn(run_task, u, pid, t["id"], False, note)
        n += 1
    return n


def run_task(u, pid, tid, retry=False, note=""):
    """Exécute une tâche en PLUSIEURS tours : l'agent est relancé tant qu'il n'a ni conclu, ni posé de question, ni signalé de blocage."""
    p = snap(u, pid)
    t = by_id(p["tasks"], tid)
    a = t and by_id(p["agents"], t.get("who"))
    if not a or (retry and not t.get("retry_at")) or t.get("running"):
        return
    unmet = deps_unmet(p, t)
    if unmet and sync_mrs(u, pid, force=True):             # une MR déjà close sur GitHub ne doit pas retenir la tâche
        p = snap(u, pid)
        t = by_id(p["tasks"], tid)
        unmet = deps_unmet(p, t)
    if unmet:
        mp(u, pid, lambda pp: by_id(pp["tasks"], tid).__setitem__("waiting", unmet))
        return log(u, pid, "Système", a["name"], "Tâche « %s » non lancée : elle attend %s." % (t["title"], ", ".join("« %s »" % x for x in unmet)))
    if open_conflict_for(p, a["id"]):
        reconcile_conflicts(u, pid, a["id"], force=True)   # le conflit est peut-être déjà levé (résolu à la main, branche mise à jour)
        p = snap(u, pid)
        t = by_id(p["tasks"], tid)
        if open_conflict_for(p, a["id"]):
            c = next(c for c in p["conflicts"] if c["agent"] == a["id"] and c["status"] in OPEN_CONFLICT)
            return log(u, pid, "Système", a["name"], "Tâche « %s » non lancée : %s" % (t["title"], describe_conflict(p, c)))

    def start(pp):
        tt = by_id(pp["tasks"], tid)
        if tt.get("running"):                              # déjà lancée par un autre passage
            return False
        tt.update(running=True, status="en cours", started=time.time(), touched=time.time())
        for k in ("retry_at", "fail", "blocked", "waiting", "resume", "result_note", "stopped", "interrupted"):
            tt.pop(k, None)
        by_id(pp["agents"], a["id"])["state"] = "actif"
        return True
    if not mp(u, pid, start):
        return
    exhausted = False
    replies = []
    try:
        pull_note = ""
        if git_agent(p, a):                                # git local : synchronisation programmatique (0 token) avec la cible avant de commencer
            try:
                sn, sb = git_act(u, pid, p, a, {}, "sync", time.time())
                pull_note = "\n(Synchronisation automatique : %s)" % (sb or sn)
            except Err as e:
                pull_note = "\n(Synchronisation impossible : %s)" % e.msg
        elif local_agent(p, a):                            # répertoire local partagé : ni pull, ni branche
            pull_note = "\n(Répertoire local partagé : verrouille chaque fichier avant de le modifier, libère-le dès que tu as fini.)"
        else:
            try:                                           # pull régulier : on repart de la branche de travail à jour
                state, pm = pull(u, pid, a["id"], origin="pull", task=tid)
                if state == "conflict":
                    mp(u, pid, lambda pp: by_id(pp["tasks"], tid).update(running=False, status="en attente"))
                    return
                pull_note = "\n(Pull automatique : %s)" % pm
            except Err as e:
                pull_note = "\n(Pull impossible : %s)" % e.msg
        p = snap(u, pid)
        msg = "Exécute cette tâche (id %s) : %s%s%s%s%s%s%s\nRéponds avec ton livrable. Quand tu as terminé, mets le statut à « à vérifier » avec task_status et un compte rendu précis de ce qui est fait ; s’il reste du travail ou un blocage, ne dis pas que c’est terminé : décris ce qui manque ou pose la question avec ask_user." % (
            tid, t["title"], ("\n" + t["desc"]) if t.get("desc") else "",
            ("\nModules : " + ", ".join(mod_names(p, t.get("modules")))) if t.get("modules") else "", dep_context(p, t), coordination_hint(p, a),
            ("\n" + note) if note else "", pull_note)
        msg += ("\nQuand ton travail est prêt, fais commit (message clair) puis passe la tâche à « à vérifier »." if git_agent(p, a)
                else "\nQuand ton travail est prêt, libère tes verrous (unlock_file) puis passe la tâche à « à vérifier »." if local_agent(p, a)
                else "\nQuand ton travail est prêt, soumets une merge request vers la branche de travail de la release avec l’action merge_request, puis passe la tâche à « à vérifier ».")
        idle = 0
        for _ in range(MAX_ROUNDS):
            stats = {}
            r = ask(u, pid, a["id"], msg, "Application", stats=stats, max_steps=MAX_ROUND_STEPS)
            if r:
                replies.append(r)
            tt = by_id(snap(u, pid)["tasks"], tid)
            if not tt:
                return
            mp(u, pid, lambda pp: (by_id(pp["tasks"], tid) or {}).__setitem__("touched", time.time()))
            if tt["status"] != "en cours":                 # conclu (à vérifier), en attente d'une décision, etc.
                break
            intr = tt.get("interrupted")
            if intr:                                       # sollicité pendant la tâche (utilisateur, workflow, autre agent) : on la lui rappelle, sans compter comme tour « sans action »
                mp(u, pid, lambda pp: (by_id(pp["tasks"], tid) or {}).pop("interrupted", None))
                back = (stats["pending"].removeprefix("[Application] ") + "\n\n") if stats.get("pending") else ""   # retour d'actions non traité : il n'est pas perdu
                msg, idle = back + resume_text(snap(u, pid), tt, intr), 0
            elif stats.get("pending"):                       # étapes épuisées en plein travail : on continue
                msg, idle = stats["pending"].removeprefix("[Application] ") + "\nPoursuis ta tâche.", 0
            elif stats.get("actions"):
                msg, idle = NUDGE, 0
            else:
                idle += 1                                  # deux tours sans action ni conclusion : on considère le travail rendu
                if idle >= 2:
                    break
                msg = NUDGE
        else:
            exhausted = True                               # l'agent travaille toujours : reprise automatique au prochain passage
        r = replies[-1] if replies else ""

        def fin(pp):
            tt = by_id(pp["tasks"], tid)
            if not tt:
                return
            tt["running"], tt["touched"] = False, time.time()
            tt.pop("retries", None)
            if not tt.get("result_note"):
                tt["result"] = r
            if tt["status"] == "en cours":
                if exhausted:
                    tt["status"], tt["resume"] = "à faire", True
                else:
                    tt["status"] = "à vérifier"            # jamais « terminée » d'office : l'agent principal vérifie
        mp(u, pid, fin)
        if by_id(snap(u, pid)["tasks"], tid)["status"] == "à vérifier":
            try:
                if local_agent(snap(u, pid), a) and not git_agent(snap(u, pid), a):
                    raise Err(0, "")                       # répertoire local sans git : pas de merge request
                st, mm = submit_mr(u, pid, a["id"], "Tâche : %s" % t["title"], "", tid)
                if st in ("submitted", "conflict", "checks"):
                    log(u, pid, "Système", a["name"], mm)
                if st in ("conflict", "checks"):             # rien n'a été soumis : la tâche retourne à l'agent (borné), pas de vérification d'un travail non intégrable
                    if task_resubmit(u, pid, tid, a, st, mm):
                        return
            except Err as e:
                if e.msg:
                    log(u, pid, "Système", a["name"], "Merge request non soumise : %s" % e.msg)
            spawn(review_task, u, pid, tid)
    except locks.Stopped as e:                             # arrêté / annulé par l'agent principal ou l'utilisateur : ni nouvelle tentative, ni échec
        def stp(pp):
            tt = by_id(pp["tasks"], tid)
            if tt:
                tt["running"], tt["touched"] = False, time.time()
                if tt["status"] == "en cours":
                    tt["status"] = "en attente"
                tt.setdefault("stopped", {"by": "?", "reason": e.msg, "cancel": False, "d": now()})
                tt["result"] = "Arrêtée : " + e.msg
        mp(u, pid, stp)
    except Err as e:
        kind = classify(e)

        def ko(pp):
            tt = by_id(pp["tasks"], tid)
            if not tt:
                return None
            tt["running"], tt["status"], tt["touched"] = False, "à faire", time.time()
            n = tt.get("retries", 0)
            if kind == "quota" or (kind == "temp" and n < MAX_TEMP) or (kind == "other" and n < MAX_OTHER):
                w = backoff(n, e.retry)
                tt["retries"], tt["retry_at"] = n + 1, time.time() + w
                tt["result"] = "Suspendu (%s) — nouvelle tentative automatique vers %s (tentative %d)." % (
                    {"quota": "quota atteint", "temp": "service momentanément indisponible"}.get(kind, "erreur inattendue du service"),
                    time.strftime("%H:%M", time.localtime(tt["retry_at"])), n + 1)
                by_id(pp["agents"], a["id"])["state"] = "suspendu"
                return w
            tt["fail"] = {"kind": kind, "msg": e.msg, "ts": time.time()}
            tt["result"] = "Erreur : " + e.msg
            return None
        w = mp(u, pid, ko)
        if w:
            schedule(u, pid, tid, w)
    except Exception as e:                                 # bogue inattendue : la tâche ne doit jamais rester « en cours » pour toujours
        def crash(pp):
            tt = by_id(pp["tasks"], tid)
            if tt:
                tt.update(running=False, status="à faire", touched=time.time(), fail={"kind": "internal", "msg": "%s : %s" % (type(e).__name__, str(e)[:250]), "ts": time.time()},
                          result="Erreur interne : %s" % str(e)[:250])
        mp(u, pid, crash)
        log(u, pid, "Système", a["name"], "Erreur interne pendant la tâche « %s » : %s : %s" % (t["title"], type(e).__name__, e))
    finally:                                               # un verrou ne survit jamais à la tâche (réussie, échouée, arrêtée ou plantée)
        try:
            freed = locks.release(u, pid, a["id"])
            if freed:
                log(u, pid, "Système", a["name"], "Fin de tâche : verrou(x) libéré(s) : %s." % ", ".join(freed))
        except Exception:
            pass


def task_resubmit(u, pid, tid, a, st, msg, max_tries=3):
    """Une soumission refusée (conflit, contrôles en échec) renvoie la tâche à son agent ; au-delà de `max_tries`, l'utilisateur est sollicité.
    Retourne True si la tâche a été renvoyée ou escaladée (pas de vérification)."""
    def f(pp):
        t = by_id(pp["tasks"], tid)
        if not t or t["status"] != "à vérifier":
            return False
        n = t.get("submit_fail", 0) + 1
        t["submit_fail"], t["touched"] = n, time.time()
        if n > max_tries:
            add_escalation(pp, t, "sa soumission a échoué %d fois (%s)." % (n - 1, "conflit de fusion" if st == "conflict" else "contrôles automatiques en échec"))
            return True
        t["status"] = "à faire"
        return "retry"
    r = mp(u, pid, f)
    if r == "retry":
        spawn(run_task, u, pid, tid, False, "[Soumission refusée] %s" % msg)
    return bool(r)


def add_escalation(pp, t, reason, review=False):
    """Question à l'utilisateur : tâche bloquée (relancer / pause / abandonner) ou validation de fin de tâche."""
    ag = by_id(pp["agents"], t.get("who")) or {}
    d = {"id": uid(), "n": rights.next_seq(pp), "kind": "question", "by": ag.get("id", ""), "byName": "Suivi", "d": now(),
         "status": "attente", "task": t["id"], "structural": False, "impact": "", "recommendation": ""}
    if review:
        d.update(question="La tâche « %s » (%s) est soumise à vérification et aucun agent ne peut la vérifier. %s Le travail demandé est-il vraiment fait ?" % (
            t["title"], ag.get("name", "?"), reason), options=["Oui, la valider", "Non, à refaire"], review=t["id"])
    else:
        d.update(question="La tâche « %s » (%s) est bloquée : %s Que faites-vous ?" % (t["title"], ag.get("name", "?"), reason),
                 options=["Relancer la tâche", "Mettre en pause (je m’en occupe)", "Abandonner la tâche"], escalation=True)
        d["recommendation"] = "Relancer la tâche"
        t["blocked"], t["status"] = d["id"], "en attente"
    t["touched"] = time.time()
    add_decision(pp, d)
    return d


def apply_review(u, pid, tid, by, verdict, comment):
    """Verdict de vérification : clôture la tâche (accepted) ou la renvoie à l'agent (rejected)."""
    def f(pp):
        t = by_id(pp["tasks"], tid)
        if not t:
            return "Tâche introuvable.", False
        t["touched"] = time.time()
        if verdict == "accepted":
            t.update(status="terminée", reviewed={"by": by, "d": now(), "note": comment})
            for k in ("review_note", "reviews", "relaunch_n", "fail", "retries", "review_fail", "review_asked", "review_err", "auto_n", "auto_escalated", "resume", "submit_fail", "interrupted", "resume_n"):
                t.pop(k, None)
            b = rights.on_task_done(pp, t)
            return "Tâche « %s » vérifiée par %s : terminée.%s" % (t["title"], by, (" Bug « %s » corrigé : à vérifier par le QA." % b["t"]) if b else ""), False
        n = t.get("reviews", 0) + 1
        t["reviews"], t["review_note"] = n, comment
        if n >= MAX_REVIEWS:
            add_escalation(pp, t, "son travail a été refusé %d fois à la vérification (dernier motif : %s)." % (n, comment))
            return "Tâche « %s » refusée %d fois à la vérification : question transmise à l’utilisateur." % (t["title"], n), False
        t["status"] = "à faire"
        return "Tâche « %s » refusée à la vérification par %s : %s" % (t["title"], by, comment), True
    msg, again = mp(u, pid, f)
    if verdict == "accepted":
        spawn(tick, u, pid)                               # des tâches dépendantes peuvent maintenant démarrer
    if again:
        spawn(run_task, u, pid, tid, False, "[Vérification par %s] Travail refusé : %s\nCorrige ce qui manque, puis soumets à nouveau la tâche (task_status « à vérifier »)." % (by, comment))
    return msg


def evidence(p, t, a):
    m = next((m for m in p.get("mrs", []) if m.get("id") and m.get("task") == t["id"]), None)
    return _evidence(p, t, a) + (("\nMerge request #%s : %s%s." % (m["n"], m["status"], (" (" + m["note"] + ")") if m.get("note") else "")) if m else "\nAucune merge request liée à cette tâche.")


def _evidence(p, t, a):
    since = t.get("started", 0)
    cs = [c for c in p.get("commits", []) if c.get("by") == a["name"] and c.get("ts", 0) >= since and not c["m"].startswith("chore(")]
    lines = "\n".join("  - %s (%s)" % (c["m"], c.get("sha", "")) for c in cs[-10:])
    return "Preuves : %d commit(s) de %s sur la branche %s depuis le début de la tâche%s" % (
        len(cs), a["name"], a["branch"], (" :\n" + lines) if lines else ". Aucune écriture dans le dépôt n’a été enregistrée.")


def pick_reviewer(p, t):
    ok = lambda g: g and g.get("mode") != "manual" and g["id"] != t.get("who")
    mg = by_id(p["agents"], p.get("mainId"))
    if ok(mg):
        return mg
    return next((g for g in p["agents"] if ok(g) and rights.level(p, g, "bug_verify") == rights.FREE), None)


def pick_mr_reviewer(p, author_id):
    """Validateur d'une merge request : l'agent principal, sinon un agent dont le rôle a le droit mr_review ; jamais l'auteur."""
    ok = lambda g: g and g.get("mode") != "manual" and g["id"] != author_id
    mg = by_id(p["agents"], p.get("mainId"))
    if ok(mg):
        return mg
    return next((g for g in p["agents"] if ok(g) and rights.level(p, g, "mr_review") == rights.FREE), None)


def diff_text(rp, base, head, budget=12000, per_file=3000):
    """Diff (compare GitHub) de `head` par rapport à `base`, prêt à être placé dans le message d'un validateur : évite les tours de
    lecture `read_file`. Tronqué par fichier (`per_file`) et au total (`budget`) ; les fichiers sans patch (binaires, trop gros)
    et ceux qui dépassent le budget sont listés pour être lus à la demande. Chaîne vide si la comparaison est impossible."""
    try:
        files = rp.compare(base, head).get("files", [])
    except Err:
        return ""
    out, used, omitted = [], 0, []
    for f in files:
        name, patch = f.get("filename", "?"), f.get("patch")
        head_line = "--- %s (%s, +%s/-%s)" % (name, f.get("status", "?"), f.get("additions", "?"), f.get("deletions", "?"))
        if not patch:
            omitted.append("%s (pas de diff : binaire ou trop gros)" % name)
            continue
        if len(patch) > per_file:
            patch = patch[:per_file] + "\n[…diff tronqué : lis le fichier avec read_file pour la suite]"
        if used + len(patch) > budget:
            omitted.append("%s (budget de diff atteint)" % name)
            continue
        used += len(patch)
        out.append(head_line + "\n" + patch)
    if omitted:
        out.append("Fichiers non détaillés (lis-les avec read_file si besoin) : " + " ; ".join(omitted))
    return "\n".join(out)


def review_task(u, pid, tid):
    """Fait vérifier par l'agent principal (ou un agent habilité) qu'une tâche soumise est réellement terminée."""
    if snap(u, pid).get("supervision") == "off":
        return

    def claim(pp):
        t = by_id(pp["tasks"], tid)
        if not t or t["status"] != "à vérifier" or t.get("reviewing"):
            return False
        t["reviewing"] = True
        return True
    if not mp(u, pid, claim):
        return
    try:
        p = snap(u, pid)
        t = by_id(p["tasks"], tid)
        a = by_id(p["agents"], t.get("who")) or {"name": "?", "branch": "?"}
        rv = pick_reviewer(p, t)
        if rv is None:                                  # personne ne peut vérifier : l'utilisateur tranche
            def ask_user(pp):
                tt = by_id(pp["tasks"], tid)
                if not tt.get("review_asked"):
                    tt["review_asked"] = True
                    add_escalation(pp, tt, "Compte rendu de l’agent : %s." % (tt.get("result") or "aucun")[:300], review=True)
            mp(u, pid, ask_user)
            return
        dif = ""
        if a.get("branch") and a.get("source") != "local":          # diff fourni directement : le vérificateur n'a pas à relire les fichiers un par un
            try:
                rp = github.Repo(p)
                mr = next((m for m in p.get("mrs", []) if m.get("id") and m.get("task") == tid), None)
                if mr:
                    dif = diff_text(rp, mr["to"], mr["from"])
                elif rp.work != a["branch"]:
                    dif = diff_text(rp, rp.work, a["branch"])
            except Err:
                dif = ""
        msg = ("[Application — vérification] La tâche « %s » (id %s), réalisée par %s, est soumise à vérification.\nDescription demandée : %s\n"
               "Compte rendu de l’agent : %s\n%s\n%s"
               "Vérifie que le travail demandé est EFFECTIVEMENT fait : %s, compare avec la description et les spécifications. "
               "Conclus obligatoirement par task_review : accepted seulement si tout est fait, sinon rejected avec la liste précise de ce qui manque.") % (
            t["title"], tid, a["name"], t.get("desc") or "(aucune)", (t.get("result") or "(aucun)")[:1500], evidence(p, t, a),
            ("Changements de la branche de %s (le diff peut inclure d’autres tâches) :\n%s\n" % (a["name"], dif)) if dif else "",
            "le diff ci-dessus est fourni ; n’utilise read_file (avec \"agent\":\"%s\") que pour un fichier tronqué ou absent du diff" % a["name"] if dif
            else "lis les fichiers concernés avec read_file en précisant \"agent\":\"%s\"" % a["name"])
        ask(u, pid, rv["id"], msg, "Vérification")
        if by_id(snap(u, pid)["tasks"], tid)["status"] == "à vérifier":
            def nofinal(pp):
                tt = by_id(pp["tasks"], tid)
                tt["review_fail"] = tt.get("review_fail", 0) + 1
                if tt["review_fail"] >= 2 and not tt.get("review_asked"):
                    tt["review_asked"] = True
                    add_escalation(pp, tt, "Le vérificateur n’a pas rendu de verdict.", review=True)
            mp(u, pid, nofinal)
        mp(u, pid, lambda pp: by_id(pp["tasks"], tid).pop("review_err", None))
    except Err as e:                                    # nouvelle tentative au prochain passage du suivi
        def err(pp):
            tt = by_id(pp["tasks"], tid)
            tt["review_err"] = tt.get("review_err", 0) + 1
            return tt["review_err"]
        if mp(u, pid, err) == 1:
            log(u, pid, "Système", "Vous", "Vérification de la tâche impossible pour le moment : %s (nouvel essai automatique)." % e.msg)
    finally:
        mp(u, pid, lambda pp: (by_id(pp["tasks"], tid) or {}).pop("reviewing", None))


# ---------------------------------------------------------------- règles du projet, communication, workflows
def rules_changed(u, pid):
    """Les règles ont été modifiées : elles sont communiquées aux agents concernés (message, nouveautés du prompt, fichier de contexte),
    puis le contrôle de cohérence est relancé."""
    p = snap(u, pid)
    new = prules.snapshot(p)
    entries = prules.diff(p.get("rules_snap"), new)
    lint = prules.lint(p)

    def f(pp):
        for e in entries:
            pp.setdefault("rules_log", []).append({"n": rights.next_seq(pp), "d": now(), "scope": e["scope"], "text": e["text"]})
        if "rules_log" in pp:
            del pp["rules_log"][:-60]
        pp["rules_snap"] = new
        pp.setdefault("rules_report", {}).update(static=lint, ts=now())
    mp(u, pid, f)
    for a in p["agents"]:
        mine = [e["text"] for e in entries if prules.concerned(e["scope"], a)]
        if mine:
            res = provision(u, pid, a["id"])
            log(u, pid, "Système", a["name"], "Règles mises à jour : %s%s" % (" ".join(mine), "" if res.startswith("Dépôt GitHub/token non configuré") else " (" + res + ")"))
    if entries:
        spawn(rules_analysis, u, pid)
    return entries


def rules_check(u, pid):
    """Contrôle de cohérence à la demande (sans communication aux agents)."""
    lint = prules.lint(snap(u, pid))
    mp(u, pid, lambda pp: pp.setdefault("rules_report", {}).update(static=lint, ts=now()))
    rules_analysis(u, pid, force=True)


def analysis_builtin(p):
    """Règles intégrées à soumettre à l'analyse : seulement les modules des modes utilisés et les règles non remplacées pour tous les agents."""
    ags = p.get("agents", [])
    ov = set.intersection(*[rights.overridden_subjects(p, a) for a in ags]) if ags else set()
    return rulebook.analysis_lines({agent_mode(p, a) for a in ags}, ov)


def rules_analysis(u, pid, force=False):
    """Un agent (le principal, sinon un agent en mode API) analyse la cohérence des règles ; résultat dans p.rules_report.ai.
    L'IA ne voit que ce que le code ne sait pas trancher : les règles à sujet sont arbitrées par l'application (priorité), et
    l'analyse n'est pas relancée si le texte à analyser n'a pas changé depuis la dernière (sauf demande explicite)."""
    p = snap(u, pid)
    nothing = not prules.has_material(p)
    text = prules.dump(p, analysis_builtin(p))
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    rr0 = p.get("rules_report") or {}
    if not nothing and not force and rr0.get("ai_hash") == h and rr0.get("ai") is not None and not rr0.get("analyzing"):
        return mp(u, pid, lambda pp: pp.setdefault("rules_report", {}).update(ai_note="Aucun changement à analyser depuis la dernière analyse (aucun appel à l’IA)."))
    an = next((g for g in [by_id(p["agents"], p.get("mainId"))] + p["agents"] if g and g.get("mode") != "manual"), None)
    if nothing or not an:
        return mp(u, pid, lambda pp: pp.setdefault("rules_report", {}).update(ai=[], ai_ts=now(), analyzing="", ai_note="Aucune règle à analyser." if nothing else "Analyse automatique impossible : aucun agent en mode API."))
    mp(u, pid, lambda pp: pp.setdefault("rules_report", {}).update(analyzing=an["id"], ai_note="Analyse en cours…"))
    log(u, pid, "Système", an["name"], "Analyse de cohérence des règles demandée à %s." % an["name"])
    msg = ("[Analyse des règles] Analyse la COHÉRENCE des règles ci-dessous : contradictions entre elles (y compris avec les règles intégrées), règles incomplètes ou ambiguës, "
           "workflows irréalisables, dépendances manquantes. Réponds par UNE action rules_report "
           "{\"issues\":[{\"severity\":\"erreur|avertissement|info\",\"where\":\"...\",\"text\":\"...\",\"suggestion\":\"...\"}]} (liste vide si tout est cohérent). "
           "Ne signale que ce qui découle du texte ci-dessous, sans rien inventer. Ce texte est l'état ACTUEL et complet : ignore toute version antérieure des règles. "
           "Les instructions et décisions de l'utilisateur sont plus récentes que les règles et les complètent ou les remplacent : un point qu'elles précisent n'est PLUS ambigu, ne le signale pas. "
           "Entre deux instructions de même sujet qui se contredisent, la plus spécifique (agent > rôle ou module > global) l'emporte, puis à portée égale la plus RÉCENTE (n° le plus élevé) : ce n'est pas une incohérence, signale seulement l'autre comme « instruction obsolète n°X » (niveau info). Les règles intégrées « par défaut » cèdent devant toute règle de l'utilisateur de même sujet.\n\n" + text)
    try:
        reply = ask(u, pid, an["id"], msg, "Analyse des règles", relog=False, fresh=True)
    except Err as e:
        return mp(u, pid, lambda pp: pp.setdefault("rules_report", {}).update(analyzing="", ai_note="Analyse impossible : %s" % e.msg))

    def fin(pp):
        rr = pp.setdefault("rules_report", {})
        if rr.get("analyzing"):                            # pas de rapport structuré
            rr.update(analyzing="", ai=[{"level": "info", "where": "Analyse", "text": (reply or "(aucune réponse)")[:600], "suggestion": ""}], ai_ts=now(), ai_note="Réponse non structurée de l’agent.")
        else:
            rr["ai_hash"] = h                              # rapport structuré reçu : inutile de refaire la même analyse
    mp(u, pid, fin)


def _wf_end(u, pid, rid, status, why):
    run = by_id(snap(u, pid).get("wf_runs", []), rid)
    if not run or run["status"] != "en cours":
        return
    mp(u, pid, lambda pp: (by_id(pp.get("wf_runs", []), rid) or {}).update(status=status, note=why, ended=now()))
    log(u, pid, "Système", "Vous", "Workflow « %s » : %s%s." % (run["name"], status, (" — " + why) if why else ""))


def workflow_start(u, pid, wfid, trigger, requester="", task="", note=""):
    """Crée un déroulement de workflow et le lance en arrière-plan. Retourne son identifiant."""
    p = snap(u, pid)
    wf = by_id(p.get("workflows", []), wfid)
    if not wf:
        raise Err(404, "Workflow introuvable")
    if not wf.get("steps"):
        raise Err(400, "Le workflow « %s » n’a aucune étape." % wf.get("name"))
    task = task if by_id(p["tasks"], task) else ""
    runs = p.get("wf_runs", [])
    if any(r["wf"] == wfid and r["status"] == "en cours" and r.get("task", "") == task for r in runs):
        raise Err(409, "Le workflow « %s » est déjà en cours%s." % (wf["name"], " sur cette tâche" if task else ""))
    if sum(1 for r in runs if r["wf"] == wfid and time.time() - r.get("ts", 0) < 3600) >= prules.WF_MAX_PER_HOUR:
        raise Err(429, "Trop de déclenchements du workflow « %s » dans l’heure (maximum %d)." % (wf["name"], prules.WF_MAX_PER_HOUR))
    rid = uid()
    run = {"id": rid, "wf": wfid, "name": wf["name"], "trigger": trigger, "requester": requester, "task": task, "note": note, "ts": time.time(),
           "started": now(), "status": "en cours", "step": 0, "results": [], "count": 0}

    def f(pp):
        pp.setdefault("wf_runs", []).append(run)
        del pp["wf_runs"][:-40]
    mp(u, pid, f)
    t = by_id(p["tasks"], task)
    log(u, pid, "Système", "Vous", "Workflow « %s » démarré (%s)%s." % (wf["name"], trigger, (" sur la tâche « %s »" % t["title"]) if t else ""))
    spawn(wf_continue, u, pid, rid)
    return rid


def wf_user_run(u, pid, wfid, task, note):
    try:
        workflow_start(u, pid, wfid, "utilisateur", task=task, note=note)
    except Err as e:
        log(u, pid, "Système", "Vous", "Workflow non lancé : %s" % e.msg)


def wf_resume(u, pid, rid):
    """Reprend un déroulement échoué ou arrêté à l'étape où il s'est interrompu."""
    run = by_id(snap(u, pid).get("wf_runs", []), rid)
    if not run or run["status"] == "en cours" or run["status"] == "terminé":
        return
    mp(u, pid, lambda pp: (by_id(pp["wf_runs"], rid) or {}).update(status="en cours", note=""))
    wf_continue(u, pid, rid)


def wf_after(u, pid, rid, i, st, outcome, comment):
    """Enregistre le résultat d'une étape et décide de la suite : next (étape suivante ou saut), stop, ou wait (question à l'utilisateur)."""
    ko, policy = outcome != "ok", st.get("on_ko") or ("ask_user" if st.get("kind") == "system" else "stop")

    def f(pp):
        run = by_id(pp["wf_runs"], rid)
        run["results"].append({"i": i + 1, "outcome": outcome, "comment": str(comment)[:300], "d": now()})
        run.pop("cur", None)
        if not ko and st.get("on_ok") == "goto":
            try:
                g = int(st.get("goto_ok")) - 1
            except (TypeError, ValueError):
                g = -1
            if g >= 0:
                run["step"] = g
                return "next"
        if not ko or policy == "continue":
            run["step"] = i + 1
            return "next"
        if policy == "goto":
            try:
                g = int(st.get("goto")) - 1
            except (TypeError, ValueError):
                g = -1
            if g >= 0:
                run["step"] = g
                return "next"
        if policy == "ask_user":
            d = {"id": uid(), "n": rights.next_seq(pp), "kind": "question", "by": "", "byName": "Workflow", "d": now(), "status": "attente", "task": "", "structural": False,
                 "question": "Le workflow « %s » a échoué à l’étape %d (%s). Que faire ?" % (run["name"], i + 1, str(comment)[:200] or "sans détail"),
                 "options": ["Continuer malgré l’échec", "Relancer cette étape", "Arrêter le workflow"], "recommendation": "Relancer cette étape", "impact": "",
                 "wf_run": rid, "wf_step": i, "wf_after": "ko"}
            add_decision(pp, d)
            run["waiting"] = d["id"]
            return "wait"
        run.update(status="arrêté", note="étape %d en échec : %s" % (i + 1, str(comment)[:200]), ended=now())
        return "stop"
    r = mp(u, pid, f)
    if r == "stop":
        run = by_id(snap(u, pid)["wf_runs"], rid)
        log(u, pid, "Système", "Vous", "Workflow « %s » arrêté : %s." % (run["name"], run["note"]))
    return r


def wf_do_step(u, pid, p, wf, run, i, st):
    """Exécute une étape. Retourne (issue ok|ko, commentaire, attente)."""
    kind = st.get("kind", "ask")
    if kind == "system":                                   # étape exécutée par l'application, sans IA (system_actions.py)
        return sysact.run_step(sys.modules[__name__], u, pid, p, wf, run, i, st)
    if kind == "start_tasks":
        n = dispatch(u, pid, snap(u, pid), time.time(), force=True)
        return "ok", "%d tâche(s) démarrée(s)" % n, False
    if kind == "ask_user":
        def gate(pp):
            r = by_id(pp["wf_runs"], run["id"])
            d = {"id": uid(), "n": rights.next_seq(pp), "kind": "question", "by": "", "byName": "Workflow", "d": now(), "status": "attente", "task": "", "structural": False,
                 "question": "Workflow « %s », étape %d : %s" % (wf["name"], i + 1, st.get("text", "")), "options": ["Continuer", "Arrêter le workflow"], "recommendation": "", "impact": "",
                 "wf_run": run["id"], "wf_step": i, "wf_after": "gate"}
            add_decision(pp, d)
            r["waiting"] = d["id"]
        mp(u, pid, gate)
        return "ok", "", True
    a = prules.resolve_actor(p, st, run)
    t = by_id(p["tasks"], run.get("task"))
    if st.get("who") == "assignee" and not t:
        return "ko", "aucune tâche cible pour cette étape (indiquez-la au déclenchement)", False
    if not a:
        return "ko", "agent introuvable pour cette étape (%s)" % (st.get("who") or "principal"), False
    prev = "\n".join("  %d. %s%s" % (r["i"], r["outcome"], (" — " + r["comment"]) if r.get("comment") else "") for r in run.get("results", []))
    tinfo = ("\nTâche concernée : « %s » (id %s, statut %s, agent %s)%s" % (t["title"], t["id"], t["status"], (by_id(p["agents"], t.get("who")) or {}).get("name", "—"),
             (" ; compte rendu : " + t["result"][:600]) if t.get("result") else "")) if t else ""
    msg = ("« %s » — étape %d/%d (déclenché par %s)%s%s\nInstruction : %s\n%sConclus cette étape avec l’action wf_step {\"outcome\":\"ok|ko\",\"comment\":\"...\"} "
           "(ok si l’étape est réalisée, ko sinon). Utilise les autres actions (task_review, task_status, message, relaunch…) pour faire ce qui est demandé.") % (
        wf["name"], i + 1, len(wf["steps"]), run["trigger"], ("\nNote du demandeur : " + run["note"]) if run.get("note") else "", tinfo, st.get("text", ""),
        ("Résultats des étapes précédentes :\n" + prev + "\n") if prev else "")
    mp(u, pid, lambda pp: by_id(pp["agents"], a["id"]).__setitem__("wf_ctx", {"run": run["id"], "step": i}))
    try:
        for _ in range(2):
            ask(u, pid, a["id"], msg, "Workflow")
            cur = (by_id(snap(u, pid).get("wf_runs", []), run["id"]) or {}).get("cur")
            if cur and cur.get("i") == i:
                return cur["outcome"], cur.get("comment", ""), False
            msg = "Conclus l’étape avec l’action wf_step {\"outcome\":\"ok|ko\",\"comment\":\"...\"}."
        return "ok", "sans verdict explicite de l’agent", False
    finally:
        mp(u, pid, lambda pp: (by_id(pp["agents"], a["id"]) or {}).pop("wf_ctx", None))
        try:
            resume_interrupted(u, pid)                     # l'étape de workflow a interrompu une tâche en cours de cet agent : reprise
        except Exception as ex:
            print("Erreur de reprise après étape de workflow (%s/%s) : %s" % (u, pid, ex), flush=True)


def wf_continue(u, pid, rid):
    """Exécute les étapes d'un déroulement jusqu'à sa fin, un arrêt, ou une question posée à l'utilisateur."""
    for _ in range(prules.WF_MAX_STEPS + 2):
        p = snap(u, pid)
        run = by_id(p.get("wf_runs", []), rid)
        if not run or run["status"] != "en cours" or run.get("waiting"):
            return
        wf = by_id(p.get("workflows", []), run["wf"])
        if not wf:
            return _wf_end(u, pid, rid, "échoué", "le workflow a été supprimé")
        i = run["step"]
        if i >= len(wf.get("steps", [])):
            return _wf_end(u, pid, rid, "terminé", "")
        if run.get("count", 0) >= prules.WF_MAX_STEPS:
            return _wf_end(u, pid, rid, "arrêté", "limite de %d étapes exécutées atteinte (boucle ?)" % prules.WF_MAX_STEPS)
        mp(u, pid, lambda pp: by_id(pp["wf_runs"], rid).__setitem__("count", run.get("count", 0) + 1))
        try:
            outcome, comment, wait = wf_do_step(u, pid, p, wf, run, i, wf["steps"][i])
        except Err as e:
            return _wf_end(u, pid, rid, "échoué", "étape %d : %s" % (i + 1, e.msg))
        if wait:
            return
        if outcome == "end":                                # action système « terminer le workflow »
            res, _, txt = comment.partition("|")
            mp(u, pid, lambda pp: by_id(pp["wf_runs"], rid)["results"].append({"i": i + 1, "outcome": "ok", "comment": txt[:300], "d": now()}))
            return _wf_end(u, pid, rid, "terminé" if res == "ok" else "échoué", txt or "terminé par une étape système")
        if wf_after(u, pid, rid, i, wf["steps"][i], outcome, comment) != "next":
            return


def wf_timers(u, pid):
    """Déclenche les workflows dont la minuterie est échue (heure locale du serveur)."""
    p = snap(u, pid)
    nowdt = datetime.now()
    for w in p.get("workflows", []):
        due, new = prules.due(w, (p.get("wf_state") or {}).get(w["id"], {}), nowdt)
        if new is not None:
            mp(u, pid, lambda pp, i=w["id"], n=new: pp.setdefault("wf_state", {}).setdefault(i, {}).update(n))
        if due:
            try:
                workflow_start(u, pid, w["id"], "minuterie")
            except Err as e:
                log(u, pid, "Système", "Vous", "Workflow « %s » : déclenchement par minuterie impossible (%s)." % (w.get("name"), e.msg))


def dispatch(u, pid, p, t0, force=False):
    """Lancement automatique : les tâches prêtes (assignées, à faire, dépendances satisfaites) démarrent sans intervention, une par agent."""
    if p.get("autopilot") == "off" and not force:
        return 0
    bud = num(p.get("budget"))
    if bud and (p.get("usage") or {}).get("cost", 0) >= bud:
        return 0
    busy = {t["who"] for t in p["tasks"] if t["status"] == "en cours" and t.get("who")} | {a["id"] for a in p["agents"] if a.get("work")}
    parents = {t.get("parent") for t in p["tasks"] if t.get("parent")}
    launched = []
    for t in p["tasks"]:
        a = by_id(p["agents"], t.get("who"))
        if not a or t["status"] != "à faire" or a.get("mode") == "manual" or a["id"] in busy or t.get("system") or t["id"] in parents:
            continue
        if t.get("fail") or t.get("retry_at") or t.get("blocked") or t.get("running") or deps_unmet(p, t) or open_conflict_for(p, a["id"]):
            continue
        if t.get("auto_n", 0) >= MAX_AUTO:
            if not t.get("auto_escalated"):
                def esc(pp, i=t["id"]):
                    tt = by_id(pp["tasks"], i)
                    tt["auto_escalated"] = True
                    add_escalation(pp, tt, "elle a été relancée automatiquement %d fois sans aboutir." % MAX_AUTO)
                mp(u, pid, esc)
            continue
        busy.add(a["id"])
        launched.append(t)
    for t in launched:
        mp(u, pid, lambda pp, i=t["id"]: by_id(pp["tasks"], i).__setitem__("auto_n", by_id(pp["tasks"], i).get("auto_n", 0) + 1))
        spawn(run_task, u, pid, t["id"], False, "Tu avais déjà commencé cette tâche : reprends là où tu t’es arrêté (relis ton historique et l’état de ta branche)." if t.get("resume") else "")
    return len(launched)


def tick(u, pid):
    """Passage du suivi (un seul à la fois par projet)."""
    key = (u, pid)
    if key in TICKING:
        return
    TICKING.add(key)
    try:
        try:
            sync_mrs(u, pid)                               # MR closes sur GitHub retirées des blocages (sans IA, même si le suivi est désactivé)
        except Exception as ex:
            print("Erreur de synchronisation des merge requests (%s/%s) : %s" % (u, pid, ex), flush=True)
        try:                                               # verrous expirés ou orphelins libérés (même si le suivi est désactivé)
            pl = snap(u, pid)
            if pl.get("locks") or pl.get("lock_wait"):
                busy = {a["id"] for a in pl["agents"] if a.get("work")} | {t["who"] for t in pl["tasks"] if t.get("running") and t.get("who")}
                for n in locks.maintain(u, pid, busy):
                    log(u, pid, "Système", "Vous", n)
        except Exception as ex:
            print("Erreur de maintenance des verrous (%s/%s) : %s" % (u, pid, ex), flush=True)
        _tick(u, pid)
        wf_timers(u, pid)                                  # minuteries des workflows (indépendantes du suivi)
    finally:
        TICKING.discard(key)


def _tick(u, pid):
    """Relances après quota, vérifications en attente, lancement des tâches prêtes, tâches bloquées (agent principal, puis utilisateur)."""
    p = snap(u, pid)
    if p.get("supervision") == "off":
        return
    t0 = time.time()

    def stamp(pp):
        for tt in pp["tasks"]:
            if tt["status"] == "en cours" and "touched" not in tt:
                tt["touched"] = t0
    mp(u, pid, stamp)
    p = snap(u, pid)
    ghosts = [t["id"] for t in p["tasks"] if t.get("running") and not (by_id(p["agents"], t.get("who")) or {}).get("work") and t0 - t.get("touched", t0) > STALE_RUNNING]
    if ghosts:                                             # exécution « fantôme » : le thread s'est arrêté sans rendre la main
        def unstick(pp):
            for i in ghosts:
                by_id(pp["tasks"], i).update(running=False, status="à faire", fail={"kind": "internal", "msg": "exécution interrompue sans résultat", "ts": t0})
        mp(u, pid, unstick)
        p = snap(u, pid)
    for t in p["tasks"]:
        if t.get("retry_at"):
            schedule(u, pid, t["id"], max(0, t["retry_at"] - t0))
        if t["status"] == "à vérifier" and not t.get("reviewing"):
            spawn(review_task, u, pid, t["id"])
    if any(c["status"] in OPEN_CONFLICT for c in p.get("conflicts", [])):
        reconcile_conflicts(u, pid)                        # un conflit levé entre-temps ne doit pas bloquer les agents
    resume_interrupted(u, pid)                             # tâches interrompues (message, workflow, suivi) que plus rien ne fait avancer
    p = snap(u, pid)
    dispatch(u, pid, p, t0)
    mg = by_id(p["agents"], p.get("mainId"))
    can_ask = bool(mg) and mg.get("mode") != "manual"
    report, esc = [], []
    for t in p["tasks"]:
        a = by_id(p["agents"], t.get("who"))
        if not a or t.get("retry_at") or t.get("running") or t.get("blocked") or t.get("reviewing") or a.get("work"):
            continue
        if t["status"] == "à faire" and t.get("fail"):
            kind, cause = t["fail"]["kind"], "erreur : " + t["fail"]["msg"][:200]
        elif t["status"] == "en cours" and t.get("started") and t0 - t.get("touched", t0) > STALL_SECONDS:
            kind, cause = "stall", "aucune activité depuis %d min" % ((t0 - t["touched"]) // 60)
        else:
            continue
        if kind in ("budget", "auth", "model", "internal") or not can_ask or t.get("sup_n", 0) >= 2:
            esc.append((t, cause))
        elif t0 - t.get("sup_ts", 0) >= SUP_COOLDOWN:
            report.append((t, a, cause))
    for t, cause in esc:
        mp(u, pid, lambda pp, i=t["id"], c=cause: add_escalation(pp, by_id(pp["tasks"], i), c))
        log(u, pid, "Système", "Vous", "Tâche « %s » bloquée (%s) : votre décision est demandée (onglet Décisions)." % (t["title"], cause))
    if report:
        msg = ("[Suivi automatique] Des tâches semblent bloquées (les simples manques de tokens sont relancés automatiquement et ne figurent pas ici) :\n%s\n"
               "Pour chacune : consulte d’abord get_blockers (faits), puis interroge l’agent concerné (message) pour comprendre ; si le problème est résolu ou contournable, relance-le avec relaunch ; "
               "sinon (décision à prendre, information manquante, erreur répétée), remonte-le à l’utilisateur avec ask_user en indiquant la tâche. "
               "Si un verrou de fichier est en cause (get_locks), libère-le avec force_unlock ou arrête l’agent avec stop_agent.") % "\n".join(
            "- %s « %s » — agent %s — %s" % (t["id"], t["title"], a["name"], cause) for t, a, cause in report)
        try:
            ask(u, pid, mg["id"], msg, "Suivi automatique")

            def mark(pp):
                for t, _, _ in report:
                    tt = by_id(pp["tasks"], t["id"])
                    if tt:
                        tt["sup_n"], tt["sup_ts"] = tt.get("sup_n", 0) + 1, time.time()
            mp(u, pid, mark)
        except Err as e:
            if time.time() - snap(u, pid).get("sup_err_ts", 0) > SUP_COOLDOWN:
                log(u, pid, "Système", "Vous", "Suivi : impossible d’interroger l’agent principal (%s)." % e.msg)
                mp(u, pid, lambda pp: pp.__setitem__("sup_err_ts", time.time()))


def supervisor_loop():
    while True:
        time.sleep(SUP_INTERVAL)
        for f in (DATA / "users").glob("*.json"):
            for p in load(f.stem).get("projects", []):
                try:
                    tick(f.stem, p["id"])
                except Exception as e:
                    print("Erreur du suivi (%s/%s) : %s" % (f.stem, p.get("id"), e), flush=True)


def start_supervisor():
    if SUP_INTERVAL > 0:
        threading.Thread(target=supervisor_loop, daemon=True).start()


def open_conflict_for(p, aid):
    return any(c["agent"] == aid and c["status"] in OPEN_CONFLICT for c in p.get("conflicts", []))


def deps_unmet(p, t):
    """Titres des dépendances non satisfaites : tâche non terminée, ou sa merge request encore en cours."""
    out = []
    for i in t.get("deps") or []:
        d = by_id(p["tasks"], i)
        if d and (d["status"] != "terminée" or any(m.get("task") == i and m.get("status") in ("ouverte", "conflit") for m in p.get("mrs", []))):
            out.append(d["title"])
    return out


def git_cycle_start(u, pid, p, version):
    """Release en mode git local : `release/<version>` créée depuis `develop` (alignée sur le remote si possible) et poussée ; les merge requests git
    ouvertes qui visaient `develop` sont redirigées vers la release."""
    branch = "release/" + re.sub(r"[^\w.\-]+", "-", version)
    note = ""
    try:
        lg = git_repo(u, pid, p)
        with INTEGRATION_LOCK:
            lg.ensure_local(GIT_INTEGRATION, lg.base)
            if lg.remote:
                try:
                    lg.update_target(GIT_INTEGRATION)
                except Err as e:
                    if e.code == 409:
                        raise
                    note += " Remote injoignable : la release part de la branche locale."
            lg.ensure_local(branch, GIT_INTEGRATION)
            if lg.remote:
                try:
                    lg.push(branch)
                except Err as e:
                    note += " Poussée de %s impossible : %s" % (branch, e.msg)
    except Err as e:
        return log(u, pid, "Système", "Vous", "Impossible de démarrer la release : %s" % e.msg)

    def f(pp):
        pp["cycle"] = {"version": version, "branch": branch, "started": now()}
        n = 0
        for m in pp.get("mrs", []):
            if m.get("git") and m.get("status") in MR_ACTIVE and m.get("to") == GIT_INTEGRATION:
                m["to"], n = branch, n + 1
        return n
    n = mp(u, pid, f)
    log(u, pid, "Système", "Vous", "Release %s démarrée (git local) : branche de travail %s créée depuis %s. Les agents s’y synchronisent (sync) et y soumettent leurs merge requests ; %s reste réservée à la release finale.%s%s" % (
        version, branch, GIT_INTEGRATION, p.get("repo", {}).get("branch") or "main", (" %d merge request(s) ouverte(s) redirigée(s) vers la release." % n) if n else "", note))


def cycle_start(u, pid, version):
    """Démarre une release : crée la branche de travail release/<version> depuis la branche de base."""
    p = snap(u, pid)
    version = version.strip()
    if not version:
        return log(u, pid, "Système", "Vous", "Version vide : release non démarrée.")
    if p.get("cycle"):
        return log(u, pid, "Système", "Vous", "Une release est déjà en cours (%s) : publiez-la d’abord." % p["cycle"]["version"])
    if git_mode(p):
        return git_cycle_start(u, pid, p, version)
    try:
        rp = github.Repo(p)
        branch = "release/" + re.sub(r"[^\w.\-]+", "-", version)
        rp.ensure_branch(branch, source=rp.base)
    except Err as e:
        return log(u, pid, "Système", "Vous", "Impossible de démarrer la release : %s" % e.msg)
    mp(u, pid, lambda pp: pp.__setitem__("cycle", {"version": version, "branch": branch, "started": now()}))
    log(u, pid, "Système", "Vous", "Release %s démarrée : branche de travail %s créée depuis %s. Les agents s’y synchronisent (pull) et y soumettent leurs merge requests ; %s reste réservée à la release finale." % (version, branch, rp.base, rp.base))


def pull(u, pid, aid, origin="pull", task="", title="", body=""):
    """Fusionne la branche de travail dans la branche de l'agent. Retourne (état, message) : uptodate | merged | conflict | skip."""
    p = snap(u, pid)
    a = by_id(p["agents"], aid)
    try:
        rp = github.Repo(p)
    except Err:
        return "skip", "Dépôt non configuré : pas de pull."
    if rp.work == rp.base:
        return "skip", "Aucune release en cours : pas de branche de travail à récupérer."
    if a["branch"] in (rp.base, rp.work):
        return "skip", "Ta branche ne peut pas être la branche de base ou la branche de travail."
    rp.ensure_branch(a["branch"])
    r = rp.merge_branch(a["branch"], rp.work, "pull : %s dans %s" % (rp.work, a["branch"]))
    if r["state"] in ("uptodate", "merged"):
        for c in p.get("conflicts", []):                    # la fusion passe : les conflits encore « ouverts » sont obsolètes
            if c["agent"] == aid and c["status"] in OPEN_CONFLICT:
                close_conflict(u, pid, c["id"], "résolu", "levé : le pull de la branche de travail réussit")
        if r["state"] == "uptodate":
            return "uptodate", "Ta branche est déjà à jour avec %s." % rp.work
        record_commit(u, pid, a["name"], "pull : %s" % rp.work, r.get("sha", ""))
        return "merged", "Pull réussi : %s est fusionnée dans ta branche." % rp.work
    ex = next((c for c in p.get("conflicts", []) if c["agent"] == aid and c["status"] in OPEN_CONFLICT), None)
    if ex:                                                  # pas de second enregistrement pour le même conflit
        return "conflict", describe_conflict(p, ex)
    return "conflict", open_conflict(u, pid, aid, origin, task=task, title=title, body=body)


def describe_conflict(p, c):
    """Explication factuelle d'un conflit ouvert (ce qui bloque, qui doit agir) — jamais d'« environnement » à nettoyer."""
    res = by_id(p["agents"], c.get("resolver")) or {}
    suite = {"ouvert": ("une résolution est en cours par %s : attends sa fin" % res["name"]) if res else "il attend la décision de l’utilisateur (onglet Décisions)",
             "escalade": "il attend la décision de l’utilisateur (onglet Décisions) ou une résolution manuelle",
             "pause": "il est en pause : l’utilisateur le résout à la main puis le fait revérifier"}.get(c["status"], "")
    return ("Conflit de fusion n°%s sur %s (statut : %s) : %s. Il n’existe aucun état local ni environnement à nettoyer : tout se règle via l’application ; "
            "aucune action de ta part n’est possible tant qu’il n’est pas levé." % (c.get("n", "?"), ", ".join(c.get("files", [])) or "des fichiers non détaillés", c["status"], suite))


def _close_in(pp, cid, status, why):
    c = by_id(pp.get("conflicts", []), cid)
    if not c:
        return None
    c.update(status=status, done=now(), note=why)
    for d in pp.get("decisions", []):
        if d.get("conflict") == cid and d["status"] == "attente":
            d.update(status="répondue", answer=why, done=now(), n_done=rights.next_seq(pp))
    t = by_id(pp["tasks"], c.get("task"))
    if t and t["status"] == "en attente" and status != "résolu":
        t["status"] = "à faire"
    return c


def close_conflict(u, pid, cid, status, why):
    """Clôt un conflit (résolu ou abandonné) et débloque ce qui en dépendait : questions liées, tâche en attente."""
    return mp(u, pid, lambda pp: _close_in(pp, cid, status, why))


def reconcile_conflicts(u, pid, aid=None, force=False, only=None):
    """Revérifie les conflits actifs : si plus aucun fichier n'est en conflit (résolu à la main, branche mise à jour), ils sont clos
    et la suite reprend. Retourne les identifiants levés."""
    p = snap(u, pid)
    try:
        rp = github.Repo(p)
    except Err:
        rp = None                                          # pas de GitHub : seuls les conflits git (locaux) peuvent être revérifiés
    healed = []
    for c in p.get("conflicts", []):
        if c["status"] not in OPEN_CONFLICT or (aid and c["agent"] != aid) or (only and c["id"] != only):
            continue
        if not force and time.time() - c.get("checked", 0) < RECONCILE_EVERY:
            continue
        mp(u, pid, lambda pp, i=c["id"]: (by_id(pp["conflicts"], i) or {}).__setitem__("checked", time.time()))
        owner = by_id(p["agents"], c["agent"])
        if not owner:
            close_conflict(u, pid, c["id"], "abandonné", "agent supprimé")
            continue
        if c.get("git"):
            m = by_id(p.get("mrs", []), c.get("mr"))
            if not m or m["status"] not in MR_ACTIVE:
                close_conflict(u, pid, c["id"], "abandonné", "merge request close")
            elif c.get("mode") != "worktree" and git_conflict_healed(u, pid, p, c):    # l'auteur, lui, clôt le conflit en soumettant à nouveau
                close_conflict(u, pid, c["id"], "résolu", "levé automatiquement : plus aucun fichier en conflit")
                healed.append(c["id"])
                after_conflict(u, pid, c["id"])
            continue
        if rp is None:
            continue
        try:
            if rp.conflict_info(owner["branch"], rp.work):
                continue                                    # toujours un vrai conflit
            r = rp.merge_branch(owner["branch"], rp.work, "pull : %s dans %s" % (rp.work, owner["branch"]))
        except Err:
            continue                                        # branche introuvable, réseau… : on réessaiera
        if r["state"] == "conflict":
            continue                                        # conflit non détaillable : on ne peut rien conclure
        close_conflict(u, pid, c["id"], "résolu", "levé automatiquement : plus aucun fichier en conflit")
        healed.append(c["id"])
        after_conflict(u, pid, c["id"])
    return healed


def conflict_action(u, pid, cid, action):
    """Actions de l'utilisateur sur un conflit : check (revérifier), resolved (déclarer résolu), retry (relancer la résolution), abandon."""
    p = snap(u, pid)
    c = by_id(p.get("conflicts", []), cid)
    if not c:
        raise Err(404, "Conflit introuvable")
    if action == "check":
        if reconcile_conflicts(u, pid, force=True, only=cid):
            return
        return log(u, pid, "Système", "Vous", "Le conflit n°%s est toujours présent sur %s." % (c["n"], ", ".join(c["files"]) or "des fichiers"))
    if action == "resolved":
        close_conflict(u, pid, cid, "résolu", "déclaré résolu par l’utilisateur")
        return after_conflict(u, pid, cid)
    if action == "abandon":
        close_conflict(u, pid, cid, "abandonné", "abandonné par l’utilisateur")
        return log(u, pid, "Système", "Vous", "Conflit n°%s abandonné : l’agent n’est plus bloqué (un nouveau conflit sera signalé au prochain pull s’il persiste)." % c["n"])
    if action == "retry":
        owner = by_id(p["agents"], c["agent"])
        mgr = by_id(p["agents"], p.get("mainId"))
        rv = pick_resolver(p, owner) or mgr or owner
        mp(u, pid, lambda pp: by_id(pp["conflicts"], cid).update(status="ouvert", tries=0))
        return resolve_conflict(u, pid, cid, rv["id"])
    raise Err(400, "Action inconnue : %s" % action)


def agent_reset(u, pid, aid, history=True, notes=False, branch=False, onboard_after=True):
    """Remet un agent à zéro : conversation, travail en cours, attentes manuelles, conflits, et (option) sa branche ; puis onboarding."""
    p = snap(u, pid)
    a = by_id(p["agents"], aid)
    if not a:
        raise Err(404, "Agent introuvable")
    done = []
    for pe in p.get("pending", []):                         # échanges manuels en attente : annulés
        w = WAIT.get(pe["id"])
        if pe["agent"] == aid and w and w["u"] == u:
            w["ev"].set()
    rp = None
    if branch and local_agent(p, a):
        done.append("agent sur le répertoire local : pas de branche à réinitialiser")
    elif branch:
        try:
            rp = github.Repo(p)
            if a["branch"] in (rp.base, rp.work):
                done.append("branche non touchée (c’est la branche de base ou de travail)")
                rp = None
        except Err:
            rp = None
            done.append("dépôt non configuré : branche non touchée")

    def f(pp):
        ag = by_id(pp["agents"], aid)
        ag["work"], ag["state"], ag["seen_seq"] = 0, "actif", 0
        ag.pop("session", None)
        if history:
            ag["history"] = []
        if notes:
            ag["notes"] = ""
        for t in pp["tasks"]:
            if t.get("who") == aid and (t.get("running") or t["status"] == "en cours"):
                t.update(running=False, status="à faire", touched=time.time())
                for k in ("retry_at", "fail", "blocked", "waiting", "retries"):
                    t.pop(k, None)
        pp["pending"] = [x for x in pp.get("pending", []) if x["agent"] != aid]
        for c in pp.get("conflicts", []):
            if c["agent"] == aid and c["status"] in OPEN_CONFLICT:
                _close_in(pp, c["id"], "abandonné", "agent réinitialisé")
        if rp:
            for m in pp.get("mrs", []):
                if m.get("by_id") == aid and m.get("status") in ("ouverte", "conflit"):
                    m["status"], m["note"] = "annulée", "agent réinitialisé"
    mp(u, pid, f)
    freed = locks.release(u, pid, aid)
    if freed:
        done.append("verrous libérés : %s" % ", ".join(freed))
    if rp:
        try:
            rp.reset_branch(a["branch"], rp.work)
            done.append("branche %s remise sur %s" % (a["branch"], rp.work))
        except Err as e:
            done.append("branche non réinitialisée (%s)" % e.msg)
    log(u, pid, "Système", a["name"], "Agent %s réinitialisé (%s)." % (a["name"], ", ".join((["conversation effacée"] if history else []) + (["notes effacées"] if notes else []) + ["travail en cours remis à faire", "attentes et conflits clos"] + done)))
    if onboard_after:
        onboard(u, pid, aid)


def diagnose(p):
    """Causes FACTUELLES de blocage, sans IA : [{who, level, text, conflict?}]. level : bloquant | attente | info."""
    out, t0 = [], time.time()
    name = lambda i: (by_id(p["agents"], i) or {}).get("name", "—")
    hm = lambda ts: time.strftime("%H:%M", time.localtime(ts))
    for c in p.get("conflicts", []):
        if c["status"] in OPEN_CONFLICT:
            out.append({"who": name(c["agent"]), "level": "bloquant", "conflict": c["id"], "text": describe_conflict(p, c)})
    for m in p.get("mrs", []):
        if m.get("id") and m.get("status") in ("ouverte", "conflit"):
            out.append({"who": m.get("by", "?"), "level": "attente", "text": "Merge request #%s « %s » : %s." % (m.get("n", "?"), m.get("title", ""), ("en attente de validation" if m["status"] == "ouverte" else "en conflit") + (" — introuvable sur GitHub : vérifiez le token du projet ou marquez-la terminée" if m.get("missing_warned") else ""))})
    for t in p["tasks"]:
        who = name(t.get("who"))
        if t.get("retry_at"):
            out.append({"who": who, "level": "attente", "text": "« %s » : reprise automatique vers %s (tentative %s) — quota ou service momentanément indisponible." % (t["title"], hm(t["retry_at"]), t.get("retries", 1))})
        if t.get("fail"):
            out.append({"who": who, "level": "bloquant", "text": "« %s » : échec (%s) — %s" % (t["title"], t["fail"].get("kind", "?"), str(t["fail"].get("msg", ""))[:200])})
        if t["status"] == "à faire" and t.get("who") and deps_unmet(p, t):
            out.append({"who": who, "level": "attente", "text": "« %s » attend : %s." % (t["title"], ", ".join(deps_unmet(p, t)))})
        if t["status"] == "en attente" and t.get("blocked"):
            d = by_id(p.get("decisions", []), t["blocked"])
            out.append({"who": who, "level": "attente", "text": "« %s » attend votre décision : %s" % (t["title"], (d or {}).get("question", "")[:200])})
        if t["status"] == "à vérifier":
            out.append({"who": who, "level": "attente", "text": "« %s » : %s." % (t["title"], "vérification en cours" if t.get("reviewing") else "en attente de vérification")})
        if t.get("running") and not (by_id(p["agents"], t.get("who")) or {}).get("work") and t0 - t.get("touched", t0) > STALE_RUNNING:
            out.append({"who": who, "level": "bloquant", "text": "« %s » : exécution figée (sera réinitialisée automatiquement)." % t["title"]})
    for t in p["tasks"]:
        if t.get("stopped") and t["status"] == "en attente":
            out.append({"who": name(t.get("who")), "level": "attente", "text": "« %s » arrêtée par %s (%s) : à relancer." % (t["title"], t["stopped"].get("by", "?"), t["stopped"].get("reason", ""))})
    out += locks.diag(p)
    pend = [d for d in p.get("decisions", []) if d["status"] == "attente"]
    if pend:
        out.append({"who": "Vous", "level": "attente", "text": "%d décision(s) vous attendent (onglet Décisions)." % len(pend)})
    if p.get("pending"):
        out.append({"who": "Vous", "level": "attente", "text": "%d réponse(s) manuelle(s) de chatbot attendue(s) (bandeau en haut du projet)." % len(p["pending"])})
    bud = num(p.get("budget"))
    if bud and (p.get("usage") or {}).get("cost", 0) >= bud:
        out.append({"who": "Projet", "level": "bloquant", "text": "Plafond de budget atteint : les agents en mode API sont en pause (onglet Coût)."})
    for key, label in (("autopilot", "lancement automatique des tâches"), ("supervision", "suivi automatique")):
        if p.get(key) == "off":
            out.append({"who": "Projet", "level": "info", "text": "Le %s est désactivé." % label})
    return out or [{"who": "Projet", "level": "info", "text": "Aucun blocage détecté."}]


def blockers_text(p):
    return "Blocages actuels (faits) :\n" + "\n".join("- [%s] %s : %s" % (i["level"], i["who"], i["text"]) for i in diagnose(p))


def pick_resolver(p, owner):
    """Qui résout un conflit selon la règle du projet : l'auteur, un agent désigné, ou personne (l'utilisateur décide)."""
    pol = p.get("conflict_policy") or "ask"
    g = owner if pol == "author" else (by_id(p["agents"], pol[6:]) if pol.startswith("agent:") else None)
    return g if g and g.get("mode") != "manual" else None


def conflict_decision(pp, c, reason=""):
    owner = by_id(pp["agents"], c["agent"]) or {"name": "?"}
    d = {"id": uid(), "n": rights.next_seq(pp), "kind": "question", "by": c["agent"], "byName": "Fusion", "d": now(), "status": "attente",
         "question": "Conflit de fusion entre la branche %s (%s) et %s sur : %s. %sQui doit le résoudre ?" % (
             c["branch"], owner["name"], c.get("target", ""), ", ".join(c["files"]) or "(fichiers non identifiés)", (reason + " ") if reason else ""),
         "options": list(CONFLICT_OPTS), "recommendation": CONFLICT_OPTS[0], "structural": False, "task": "", "conflict": c["id"],
         "impact": "La fusion et les tâches qui en dépendent restent bloquées tant que le conflit n’est pas résolu."}
    add_decision(pp, d)
    return d


def open_conflict(u, pid, aid, origin, mr_id="", task="", title="", body=""):
    """Enregistre un conflit de fusion et applique la règle du projet (agent chargé de le résoudre, ou décision de l'utilisateur)."""
    p = snap(u, pid)
    a, rp = by_id(p["agents"], aid), github.Repo(p)
    files = [i["path"] for i in rp.conflict_info(a["branch"], rp.work)]
    resolver = pick_resolver(p, a) if files else None

    def f(pp):
        c = {"id": uid(), "n": rights.next_seq(pp), "agent": aid, "branch": a["branch"], "target": rp.work, "files": files, "origin": origin,
             "mr": mr_id, "task": task, "title": title, "body": body, "status": "ouvert", "resolver": "", "tries": 0, "d": now()}
        pp.setdefault("conflicts", []).append(c)
        m = by_id(pp.get("mrs", []), mr_id)
        if m:
            m["status"] = "conflit"
        if not resolver:
            conflict_decision(pp, c, "" if files else "Le conflit n’a pas pu être détaillé automatiquement.")
        return c["id"]
    cid = mp(u, pid, f)
    if resolver:
        spawn(resolve_conflict, u, pid, cid, resolver["id"])
        return "Conflit de fusion sur %d fichier(s) (%s) : la résolution est confiée à %s (règle du projet). Attends sa fin avant de poursuivre." % (len(files), ", ".join(files), resolver["name"])
    return "Conflit de fusion sur %s : une décision est demandée à l’utilisateur (onglet Décisions). Attends-la avant de poursuivre." % (", ".join(files) or "des fichiers")


def escalate_conflict(u, pid, cid, reason):
    def f(pp):
        c = by_id(pp["conflicts"], cid)
        c["status"] = "escalade"
        conflict_decision(pp, c, reason)
    mp(u, pid, f)
    log(u, pid, "Système", "Vous", "Conflit de fusion : %s Votre décision est demandée (onglet Décisions)." % reason)


def resolve_conflict(u, pid, cid, rid):
    """Demande à un agent de résoudre un conflit (l'agent répond par l'action resolve_merge) ; échec : l'utilisateur décide."""
    p = snap(u, pid)
    c = by_id(p.get("conflicts", []), cid)
    if not c or c["status"] not in ("ouvert", "escalade", "pause"):
        return
    if c.get("git"):
        return git_resolve_conflict(u, pid, cid, rid)
    owner, rp = by_id(p["agents"], c["agent"]), github.Repo(p)
    for attempt in range(2):
        info = rp.conflict_info(owner["branch"], rp.work)
        if not info:                                       # plus aucun vrai conflit : on poursuit
            mp(u, pid, lambda pp: by_id(pp["conflicts"], cid).__setitem__("status", "résolu"))
            return after_conflict(u, pid, cid)
        if len(info) > 8 or sum(len(i[k] or "") for i in info for k in ("base", "into", "source")) > 60000:
            return escalate_conflict(u, pid, cid, "Le conflit est trop volumineux pour une résolution automatique.")
        mp(u, pid, lambda pp: by_id(pp["conflicts"], cid).update(resolver=rid, status="ouvert", tries=attempt + 1))
        show = lambda v: ("```\n%s\n```" % v) if v is not None else "(absent)"
        material = "\n\n".join("### %s\n- Version commune (base) :\n%s\n- Version de la branche de %s (%s) :\n%s\n- Version de la branche de travail (%s) :\n%s" % (
            i["path"], show(i["base"]), owner["name"], owner["branch"], show(i["into"]), rp.work, show(i["source"])) for i in info)
        msg = ("[Conflit de fusion] La branche %s (agent %s) ne peut pas être fusionnée avec %s : conflit (id %s) sur %d fichier(s). "
               "Fusionne intelligemment les deux versions de chaque fichier, sans marqueur de conflit, puis réponds par UNE action resolve_merge "
               "{\"conflict\":\"%s\",\"files\":[{\"path\":...,\"content\":<contenu final complet>}],\"message\":...} qui couvre tous les fichiers.\n\n%s") % (
            owner["branch"], owner["name"], rp.work, cid, len(info), cid, material)
        try:
            ask(u, pid, rid, msg, "Conflit")
        except Err as e:
            return escalate_conflict(u, pid, cid, "Le résolveur n’a pas pu être sollicité (%s)." % e.msg)
        if by_id(snap(u, pid)["conflicts"], cid)["status"] == "résolu":
            return
    escalate_conflict(u, pid, cid, "La résolution automatique a échoué à deux reprises.")


def after_conflict(u, pid, cid):
    """Suite d'un conflit résolu : fusion de la merge request, nouvelle soumission, ou reprise de la tâche."""
    c = by_id(snap(u, pid).get("conflicts", []), cid)
    if not c:
        return
    log(u, pid, "Système", "Vous", "Conflit résolu sur la branche %s." % c["branch"])
    if c.get("mr"):
        log(u, pid, "Système", "Vous", merge_mr(u, pid, c["mr"]))
    elif c["origin"] == "mr":
        log(u, pid, "Système", "Vous", submit_mr(u, pid, c["agent"], c.get("title", ""), c.get("body", ""), c.get("task", ""))[1])
    elif c.get("task"):
        spawn(run_task, u, pid, c["task"], False, "[Fusion] Le conflit de fusion est résolu et ta branche est à jour : reprends la tâche.")


def propose_check(u, pid, p, a, x, mn, ctx):
    """Un agent PROPOSE un contrôle automatique (jamais activé sans l'approbation de l'utilisateur, qui lit une explication non technique)."""
    refuse = lambda m: (m, m)
    ctx["n"] += 1
    if ctx["n"] > MAX_CREATE:
        return refuse("Trop de créations dans un même échange (maximum %d) : poursuis plus tard." % MAX_CREATE)
    if rights.level(p, a, "check", mn) == rights.NO:
        return refuse("Ton rôle ne t’autorise pas à proposer des contrôles automatiques.")
    name, cmd, explain = str(x.get("name") or "").strip()[:60], str(x.get("command") or "").strip(), str(x.get("explain") or "").strip()
    if not name or not cmd or len(explain) < 20:
        return refuse("Proposition incomplète : name, command et explain sont obligatoires. explain = une explication SIMPLE, sans jargon, pour quelqu’un qui n’est pas technicien : "
                      "ce que la commande vérifie, si elle modifie quelque chose, et pourquoi tu la proposes.")
    when = x.get("when") if x.get("when") in checks.WHEN else "both"
    risk = checks.analyze(cmd)
    if risk["refused"]:
        return refuse("Commande refusée d’office (%s). Propose une commande qui reste dans le dossier de travail du projet, sans privilèges ni accès réseau direct." % " ; ".join(risk["flags"]))
    cur = next((c for c in p.get("checks", []) if c.get("active", True) and c["name"].strip().lower() == name.lower()), None)
    pay = {"name": name, "command": cmd, "when": when, "blocking": bool(x.get("blocking")), "timeout": min(int(x.get("timeout") or checks.DEFAULT_TIMEOUT), checks.MAX_TIMEOUT)
           if str(x.get("timeout") or "").isdigit() or not x.get("timeout") else checks.DEFAULT_TIMEOUT, "retry_once": bool(x.get("retry_once")), "explain": explain,
           "reason": str(x.get("reason") or "").strip(), "why": str(x.get("reason") or "").strip(), "id": cur["id"] if cur else "", "prev": cur["command"] if cur else "",
           "risk": {"level": risk["level"], "flags": risk["flags"]}}
    if cur and all(cur.get(k) == pay[k] for k in ("command", "when", "blocking", "retry_once")):
        return refuse("Ce contrôle est déjà actif avec cette commande : rien à proposer.")

    def f(pp):
        olds = [d for d in pp.get("decisions", []) if d["kind"] == "check" and d["status"] == "attente" and rights.norm(d["payload"].get("name")) == rights.norm(name)]
        if any(d["payload"].get("command") == cmd and d["payload"].get("when") == when and d["payload"].get("blocking") == pay["blocking"] for d in olds):
            return None
        d = {"id": uid(), "n": rights.next_seq(pp), "kind": "check", "by": a["id"], "byName": a["name"], "d": now(), "status": "attente", "payload": pay, "why": pay["why"]}
        for o in olds:                                       # une version révisée remplace la proposition en attente du même contrôle
            o.update(status="remplacée", note="Remplacée par la décision n° %s." % d["n"], replaced_by=d["id"], done=now(), n_done=rights.next_seq(pp))
        pp.setdefault("decisions", []).append(d)
        return d
    d = mp(u, pid, f)
    if not d:
        return "Proposition identique déjà en attente : %s" % name, None
    return "Proposition de contrôle enregistrée dans « Décisions » : %s (vigilance %s)" % (name, risk["level"]), \
           "Proposition enregistrée : l’utilisateur la lira (ton explication, la commande, le niveau de vigilance) et l’approuvera ou non ; rien ne s’exécute avant. Tu seras prévenu s’il la refuse."


def checks_active(p, when):
    return [c for c in p.get("checks", []) if c.get("active", True) and c.get("when", "both") in (when, "both")]


def checks_summary(results):
    return [{k: e.get(k) for k in ("name", "ok", "blocking", "declared_blocking", "flaky")} for e in results]


def checks_text(summary):
    """Résumé des contrôles pour le validateur (sans commandes ni sorties)."""
    if not summary:
        return "Contrôles automatiques : aucun contrôle configuré (aucune vérification automatique n’a été faite)."
    L = []
    for e in summary:
        tag = "bloquant" if e.get("blocking") else "informatif" + (" — pas encore bloquant : l’état de référence n’est pas vert" if e.get("declared_blocking") else "")
        L.append("  - %s %s (%s%s)" % ("✔" if e.get("ok") else "✖", e.get("name"), tag, ", instable : réussi au 2e essai" if e.get("flaky") else ""))
    return "Contrôles automatiques :\n" + "\n".join(L)


def checks_fail_text(results, only_blocking=True):
    """Sorties (fin) des contrôles en échec, pour l'agent qui doit corriger."""
    out, used = [], 0
    for e in results:
        if e["ok"] or (only_blocking and not e["blocking"]):
            continue
        t = checks.tail(e["tail"], 1200)
        out.append("✖ %s (code %s%s) :\n%s" % (e["name"], e["rc"], ", délai dépassé" if e["timed_out"] else "", t))
        used += len(t)
        if used > 3500:
            break
    return "\n\n".join(out)


def run_checks(u, pid, p, cwd, when, who=""):
    """Exécute les contrôles actifs de `when` dans `cwd`. Un contrôle n'est BLOQUANT que s'il est déclaré bloquant ET que son état de référence est vert
    (il a déjà réussi sur la cible). À l'intégration, une réussite établit l'état de référence. Retourne {results, blocking_failed, failed}."""
    res = []
    for c in checks_active(p, when):
        r = checks.run(c["command"], cwd, c.get("timeout"))
        flaky = False
        if not r["ok"] and c.get("retry_once"):
            r2 = checks.run(c["command"], cwd, c.get("timeout"))
            flaky, r = r2["ok"], r2
        res.append({"id": c["id"], "name": c["name"], "ok": r["ok"], "rc": r["rc"], "secs": r["secs"], "timed_out": r["timed_out"], "tail": r["tail"], "flaky": flaky,
                    "declared_blocking": bool(c.get("blocking")), "blocking": bool(c.get("blocking")) and c.get("baseline") == "green", "when": when})

    def f(pp):
        runs = pp.setdefault("check_runs", [])
        for e in res:
            runs.append({"d": now(), "who": who, "when": when, "name": e["name"], "ok": e["ok"], "secs": e["secs"], "blocking": e["blocking"], "flaky": e["flaky"], "tail": e["tail"][-600:]})
            cc = by_id(pp.get("checks", []), e["id"])
            if cc and when == "integration" and e["ok"] and cc.get("baseline") != "green":
                cc["baseline"] = "green"
        del runs[:-MAX_CHECK_RUNS]
    if res:
        mp(u, pid, f)
    return {"results": res, "blocking_failed": [e for e in res if not e["ok"] and e["blocking"]], "failed": [e for e in res if not e["ok"]]}


def checks_apply(pp, d, pay):
    """Applique une proposition de contrôle APPROUVÉE par l'utilisateur (texte exact de la commande approuvé). Retourne (message, id du contrôle)."""
    cmd = str(pay["command"]).strip()
    fields = {"name": str(pay["name"]).strip()[:60], "command": cmd, "when": pay.get("when") if pay.get("when") in checks.WHEN else "both",
              "blocking": bool(pay.get("blocking")), "timeout": int(pay.get("timeout") or checks.DEFAULT_TIMEOUT), "retry_once": bool(pay.get("retry_once"))}
    pp.setdefault("checks", [])
    cur = by_id(pp["checks"], pay.get("id")) if pay.get("id") else None
    hist = pp.setdefault("check_history", [])
    if cur:
        hist.append({"id": uid(), "check": cur["id"], "name": cur["name"], "action": "modification", "old": dict(cur), "new": cmd, "by": d["byName"], "approved": "Vous",
                     "d": now(), "reason": pay.get("reason", "")})
        if cur["command"] != cmd:
            cur["baseline"] = None                           # commande différente : l'état de référence est à rétablir
        cur.update(fields, active=True, ver=cur.get("ver", 1) + 1)
        c = cur
    else:
        c = {"id": uid(), **fields, "baseline": None, "active": True, "ver": 1}
        pp["checks"].append(c)
        hist.append({"id": uid(), "check": c["id"], "name": c["name"], "action": "ajout", "old": None, "new": cmd, "by": d["byName"], "approved": "Vous", "d": now(),
                     "reason": pay.get("reason", "")})
    del hist[:-100]
    return "Contrôle « %s » approuvé et activé (version %d)." % (c["name"], c["ver"]), c["id"]


def check_baseline(u, pid, cid):
    """Établit l'état de référence d'un contrôle : exécution sur la branche cible (worktree d'intégration). Vert = il pourra être bloquant."""
    p = snap(u, pid)
    c = by_id(p.get("checks", []), cid)
    if not c or not git_mode(p):
        return
    tgt = git_target(p)
    try:
        lg = git_repo(u, pid, p)
        with INTEGRATION_LOCK:
            lg.ensure_local(tgt, GIT_INTEGRATION if tgt != GIT_INTEGRATION else lg.base)
            r = checks.run(c["command"], lg.integration_path(tgt), c.get("timeout"))
    except Err as e:
        return log(u, pid, "Système", "Vous", "État de référence de « %s » non établi : %s" % (c["name"], e.msg))
    mp(u, pid, lambda pp: (by_id(pp.get("checks", []), cid) or {}).update(baseline="green" if r["ok"] else "red"))
    log(u, pid, "Système", "Vous", "Contrôle « %s » : état de référence %s sur %s.%s" % (
        c["name"], "VERT" if r["ok"] else "ROUGE", tgt,
        " Il pourra bloquer les soumissions." if r["ok"] and c.get("blocking") else "" if r["ok"] else " Il reste informatif (non bloquant) tant qu’il échoue sur la cible. Fin de la sortie :\n" + checks.tail(r["tail"], 600)))


def _check_hist(pp, c, action, old, by="Vous", reason=""):
    pp.setdefault("check_history", []).append({"id": uid(), "check": c["id"], "name": c["name"], "action": action, "old": old, "new": c.get("command"), "by": by, "approved": "Vous",
                                                "d": now(), "reason": reason})
    del pp["check_history"][:-100]


def check_remove(u, pid, cid):
    def f(pp):
        c = by_id(pp.get("checks", []), cid)
        if not c:
            raise Err(404, "Contrôle introuvable")
        _check_hist(pp, c, "retrait", dict(c))
        pp["checks"] = [x for x in pp["checks"] if x["id"] != cid]
        return c["name"]
    log(u, pid, "Système", "Vous", "Contrôle « %s » retiré." % mp(u, pid, f))


def check_restore(u, pid, hid):
    """Restaure la version d'un contrôle avant une modification ou un retrait (l'utilisateur décide : pas de nouvelle approbation)."""
    def f(pp):
        h = by_id(pp.get("check_history", []), hid)
        if not h or not h.get("old"):
            raise Err(404, "Version précédente introuvable")
        old = dict(h["old"], baseline=None)
        cur = by_id(pp.setdefault("checks", []), old["id"])
        if cur:
            _check_hist(pp, cur, "restauration", dict(cur))
            cur.update(old, active=True, ver=cur.get("ver", 1) + 1)
        else:
            pp["checks"].append(dict(old, active=True))
            _check_hist(pp, old, "restauration", None)
        return old["name"]
    name = mp(u, pid, f)
    log(u, pid, "Système", "Vous", "Contrôle « %s » restauré à sa version précédente." % name)
    spawn(check_baseline, u, pid, [h for h in snap(u, pid)["check_history"] if h["id"] == hid][0]["check"])


def git_submit(u, pid, aid, title="", body="", task=""):
    """Soumission d'un agent en mode git local (même retour que `submit_mr`) : commit programmatique du reste, synchronisation avec la cible
    (conflit = à résoudre dans son worktree), puis merge request interne validée par l'agent principal. 0 token jusqu'à la validation."""
    p = snap(u, pid)
    a = by_id(p["agents"], aid)
    try:
        lg, wt = git_workspace(u, pid, p, a)
    except Err as e:
        return "skip", "Espace git indisponible : %s" % e.msg
    tgt, br = git_target(p), lg.current_branch(wt)
    if not br:
        return "skip", "Ton worktree n’est sur aucune branche : rien à soumettre."
    title = title or "Merge request de %s" % a["name"]
    sha = lg.commit(wt, title, agent=a["name"], service=str(a.get("service") or ""), task=task)
    if sha:
        record_commit(u, pid, a["name"], title, sha)
    ex = next((m for m in p.get("mrs", []) if m.get("git") and m.get("from") == br and m.get("status") in MR_ACTIVE), None)
    if ex and sync_mrs(u, pid, force=True, only=ex["id"]):          # elle est peut-être déjà fusionnée
        p = snap(u, pid)
        ex = next((m for m in p.get("mrs", []) if m.get("git") and m.get("from") == br and m.get("status") in MR_ACTIVE), None)
    r = lg.sync(wt, git_source(lg, p), a["name"])
    if r["state"] == "conflict":
        return "conflict", ("Conflit avec %s dans : %s. Les marqueurs <<<<<<< ======= >>>>>>> sont dans tes fichiers : retire les marqueurs de chaque fichier avec edit_file (write_file seulement pour un petit fichier), "
                            "fais commit, puis soumets à nouveau (merge_request). Pour renoncer : {\"do\":\"sync\",\"abort\":true}.") % (tgt, ", ".join(r["files"]))
    if r["state"] == "merged":
        record_commit(u, pid, a["name"], "sync : %s" % tgt, lg.sha("HEAD", cwd=wt) or "")
    if not lg.ahead_behind(br, tgt)["ahead"]:
        return "skip", "Rien à soumettre : ta branche n’a aucun commit en avance sur %s." % tgt
    ck, escal = run_checks(u, pid, p, wt, "validation", a["name"]), ""   # contrôles automatiques approuvés, dans le worktree de l'agent (0 token)
    if ck["blocking_failed"]:
        def bump(pp):
            tr = pp.setdefault("check_tries", {})
            tr[br] = tr.get(br, 0) + 1
            return tr[br]
        n = mp(u, pid, bump)
        if n <= CHECK_TRIES:
            return "checks", "Contrôles automatiques en échec (tentative %d/%d) : corrige, fais commit, puis soumets à nouveau.\n\n%s" % (n, CHECK_TRIES, checks_fail_text(ck["results"]))
        escal = "contrôles toujours en échec après %d tentatives : transmis au validateur tels quels." % CHECK_TRIES
    else:
        mp(u, pid, lambda pp: pp.get("check_tries", {}).pop(br, None))
    csum = checks_summary(ck["results"])
    if ex:
        if ex["status"] == "conflit":                                # l'agent a résolu : la MR repart en validation
            mp(u, pid, lambda pp: by_id(pp["mrs"], ex["id"]).update(status="ouverte", note="Reprise après résolution du conflit.", merge_tries=ex.get("merge_tries", 0), tip=lg.sha(br),
                                                                    checks=csum, checks_note=escal))
            for c in p.get("conflicts", []):                 # l'auteur a résolu dans son worktree : le conflit suivi est clos
                if c.get("mr") == ex["id"] and c["status"] in OPEN_CONFLICT:
                    close_conflict(u, pid, c["id"], "résolu", "l’auteur a résolu le conflit et soumis à nouveau")
            spawn(review_mr, u, pid, ex["id"])
            return "submitted", "Merge request #%s reprise après résolution du conflit : l’agent principal va la valider." % ex["n"]
        return "exists", "Une merge request est déjà ouverte (#%s) : elle sera validée." % ex["n"]
    mid = uid()

    def add(pp):
        n = 1 + max([m.get("n") or 0 for m in pp.get("mrs", [])] or [0])
        pp.setdefault("mrs", []).append({"id": mid, "n": n, "git": True, "from": br, "to": tgt, "t": "#%d %s" % (n, title), "title": title, "by": a["name"],
                                          "by_id": aid, "task": task, "d": now(), "status": "ouverte", "note": "", "tip": lg.sha(br), "checks": csum, "checks_note": escal})
        return n
    n = mp(u, pid, add)
    spawn(review_mr, u, pid, mid)
    return "submitted", "Merge request #%d soumise vers %s : l’agent principal va la valider." % (n, tgt)


def git_push(u, pid, aid=None):
    """Pousse sur le remote, à la demande de l'utilisateur, la branche courante de chaque agent en mode git (ou d'un seul). Sans effet sans remote."""
    p = snap(u, pid)
    if not git_mode(p):
        return log(u, pid, "Système", "Vous", "Mode git local non activé : rien à pousser.")
    try:
        lg = git_repo(u, pid, p)
    except Err as e:
        return log(u, pid, "Système", "Vous", "Poussée impossible : %s" % e.msg)
    if not lg.remote:
        return log(u, pid, "Système", "Vous", "Aucun remote défini pour le projet : rien à pousser.")
    tgt, out = git_target(p), []
    for a in p["agents"]:
        if (aid and a["id"] != aid) or not git_agent(p, a):
            continue
        br = lg.worktrees().get(os.path.realpath(lg._wt_path(a["name"])))
        if not br or br in (lg.base, tgt, GIT_INTEGRATION):
            continue
        try:
            with GIT_LOCK:
                lg.push(br)
            out.append("%s : %s poussée" % (a["name"], br))
        except Err as e:
            out.append("%s : échec pour %s (%s)" % (a["name"], br, e.msg))
    log(u, pid, "Système", "Vous", "Poussée des branches des agents : " + (" ; ".join(out) if out else "aucune branche à pousser."))


def open_git_conflict(u, pid, m, files):
    """Conflit d'intégration d'une MR git : enregistré dans `conflicts` et traité selon la RÈGLE DU PROJET (`conflict_policy`) :
    `author` = l'auteur résout dans son worktree (sync, résolution, nouvelle soumission) ; `agent:<id>` = l'agent désigné fournit la résolution
    (action resolve_merge) que l'application applique ; `ask` = une décision est demandée à l'utilisateur. Au-delà de 3 essais : décision de l'utilisateur."""
    p = snap(u, pid)
    owner = by_id(p["agents"], m["by_id"]) or {"id": m["by_id"], "name": m.get("by", "?")}
    tries = m.get("merge_tries", 0) + 1
    resolver = pick_resolver(p, owner) if files else None

    def f(pp):
        mm = by_id(pp["mrs"], m["id"])
        mm.update(status="conflit", merge_tries=tries, conflict_files=files)
        mm.pop("resolution", None)
        c = next((x for x in pp.get("conflicts", []) if x.get("mr") == m["id"] and x["status"] in OPEN_CONFLICT), None)
        if c:
            c["files"] = files
            return c["id"], False
        c = {"id": uid(), "n": rights.next_seq(pp), "agent": m["by_id"], "branch": m["from"], "target": m["to"], "files": files, "origin": "mr", "mr": m["id"],
             "task": m.get("task", ""), "title": m.get("title", ""), "body": "", "status": "ouvert", "resolver": "", "tries": 0, "d": now(), "git": True, "mode": ""}
        pp.setdefault("conflicts", []).append(c)
        return c["id"], True
    cid, new = mp(u, pid, f)
    where = "Merge request #%s en conflit avec %s (fichiers : %s)" % (m["n"], m["to"], ", ".join(files) or "non détaillés")
    if tries > 3:
        escalate_conflict(u, pid, cid, "La merge request #%s est toujours en conflit après %d essais." % (m["n"], tries - 1))
        return "%s : toujours en conflit après %d essais ; intervention manuelle nécessaire, votre décision est demandée (onglet Décisions)." % (where, tries - 1)
    c = by_id(snap(u, pid)["conflicts"], cid)
    if not new and c["status"] == "pause":
        return "%s : conflit en pause, à traiter manuellement (puis « Vérifier » sur le conflit)." % where
    if not new and c["status"] == "escalade":
        return "%s : en attente de la décision de l’utilisateur (onglet Décisions)." % where
    if resolver:
        spawn(resolve_conflict, u, pid, cid, resolver["id"])
        return "%s : la résolution est confiée à %s (règle du projet)." % (where, resolver["name"] if resolver["id"] != m["by_id"] else m["by"] + " (auteur)")
    if new:
        mp(u, pid, lambda pp: conflict_decision(pp, by_id(pp["conflicts"], cid), ""))
    return "%s : une décision est demandée à l’utilisateur (onglet Décisions)." % where


def git_resolve_conflict(u, pid, cid, rid):
    """Résolution d'un conflit d'intégration git confiée à `rid`. L'auteur résout dans SON worktree (asynchrone : sa nouvelle soumission clôt le conflit) ;
    tout autre agent fournit la résolution (resolve_merge), appliquée ensuite par l'application sous contrôles d'intégration."""
    p = snap(u, pid)
    c = by_id(p.get("conflicts", []), cid)
    if not c or c["status"] not in OPEN_CONFLICT:
        return
    m = by_id(p.get("mrs", []), c.get("mr"))
    if not m or m["status"] not in MR_ACTIVE:
        return close_conflict(u, pid, cid, "abandonné", "merge request close")
    if rid == c["agent"]:
        mp(u, pid, lambda pp: by_id(pp["conflicts"], cid).update(resolver=rid, status="ouvert", mode="worktree", tries=1))
        spawn(ask, u, pid, rid, "[Intégration] Ta merge request #%s est en conflit avec %s sur : %s. Lance sync (les marqueurs apparaissent dans les fichiers), "
              "résous-les en retirant les marqueurs de chaque fichier avec edit_file (write_file seulement pour un petit fichier), fais commit, puis soumets à nouveau avec merge_request." % (m["n"], m["to"], ", ".join(c["files"])), "Intégration MR")
        return
    lg = git_repo(u, pid, p)
    owner = by_id(p["agents"], c["agent"]) or {"name": m.get("by", "?")}
    for attempt in range(2):
        with INTEGRATION_LOCK:
            info = lg.conflicts(m["from"], m["to"])
        if not info:                                       # plus aucun vrai conflit : la fusion peut reprendre
            close_conflict(u, pid, cid, "résolu", "levé : plus aucun fichier en conflit")
            return after_conflict(u, pid, cid)
        if len(info) > 8 or sum(len(i[k] or "") for i in info for k in ("base", "into", "source")) > 60000:
            return escalate_conflict(u, pid, cid, "Le conflit est trop volumineux pour une résolution automatique.")
        mp(u, pid, lambda pp: by_id(pp["conflicts"], cid).update(resolver=rid, status="ouvert", mode="direct", tries=attempt + 1))
        show = lambda v: ("```\n%s\n```" % v) if v is not None else "(absent)"
        material = "\n\n".join("### %s\n- Version commune (base) :\n%s\n- Version de la branche cible (%s) :\n%s\n- Version de la branche de %s (%s) :\n%s" % (
            i["path"], show(i["base"]), m["to"], show(i["into"]), owner["name"], m["from"], show(i["source"])) for i in info)
        msg = ("[Conflit de fusion] La merge request #%s (branche %s, agent %s) ne peut pas être fusionnée dans %s : conflit (id %s) sur %d fichier(s). "
               "Fusionne intelligemment les deux versions de chaque fichier, sans marqueur de conflit, puis réponds par UNE action resolve_merge "
               "{\"conflict\":\"%s\",\"files\":[{\"path\":...,\"content\":<contenu final complet>}],\"message\":...} qui couvre tous les fichiers.\n\n%s") % (
            m["n"], m["from"], owner["name"], m["to"], cid, len(info), cid, material)
        try:
            ask(u, pid, rid, msg, "Conflit")
        except Err as e:
            return escalate_conflict(u, pid, cid, "Le résolveur n’a pas pu être sollicité (%s)." % e.msg)
        if by_id(snap(u, pid)["conflicts"], cid)["status"] == "résolu":
            return
    escalate_conflict(u, pid, cid, "La résolution automatique a échoué à deux reprises.")


def git_conflict_healed(u, pid, p, c):
    """Vrai si le conflit d'une MR git n'existe plus (fusion de nouveau possible) ; l'état est lu dans git, sans IA."""
    m = by_id(p.get("mrs", []), c.get("mr"))
    try:
        lg = git_repo(u, pid, p)
        with INTEGRATION_LOCK:
            return bool(m) and not lg.conflicts(m["from"], m["to"])
    except Err:
        return False


def git_push_release(u, pid):
    """À la demande : pousse la base, `develop`, la branche de la release en cours et les tags (échec de la poussée à la publication, remote revenu…).
    Chaque branche est réalignée sur le remote (fusion, jamais de force) avant d'être poussée."""
    p = snap(u, pid)
    if not git_mode(p):
        return log(u, pid, "Système", "Vous", "Mode git local non activé : rien à pousser.")
    try:
        lg = git_repo(u, pid, p)
    except Err as e:
        return log(u, pid, "Système", "Vous", "Poussée impossible : %s" % e.msg)
    if not lg.remote:
        return log(u, pid, "Système", "Vous", "Aucun remote défini pour le projet : rien à pousser.")
    out = []
    with INTEGRATION_LOCK:
        for b in [lg.base, GIT_INTEGRATION] + ([p["cycle"]["branch"]] if p.get("cycle") else []):
            if not lg.has("refs/heads/" + b):
                continue
            try:
                out.append("%s %s" % (b, "poussée" if lg.push_target(b) == "pushed" else "à jour"))
            except Err as e:
                out.append("%s : échec (%s)" % (b, e.msg))
        try:
            lg.push_tags()
            out.append("tags poussés")
        except Err as e:
            out.append("tags : échec (%s)" % e.msg)
    log(u, pid, "Système", "Vous", "Poussée de la base, de develop et des tags : " + " ; ".join(out) + ".")


def git_mr_view(u, pid, p, m):
    """(commits, fichiers, diff) d'une merge request git, pour le message du validateur."""
    c = git_repo(u, pid, p).compare(m["to"], m["from"], budget=15000)
    commits = "\n".join("  - %s" % x for x in c["commits"][-15:]) or "  (aucun)"
    if "checks" in m:                                      # MR git : résultat des contrôles automatiques lancés par l'application à la soumission
        commits += "\n" + checks_text(m.get("checks")) + (("\n  ⚠ " + m["checks_note"]) if m.get("checks_note") else "")
    return commits, ", ".join(c["files"][:30]) or "(aucun)", c["diff"]


def git_mr_state(u, pid, p, m):
    """État d'une MR git DÉDUIT de git (sans IA, sans réseau) : merged | closed (branche supprimée) | open."""
    lg = git_repo(u, pid, p)
    ref = m["from"] if lg.has("refs/heads/" + m["from"]) else (m.get("tip") if m.get("tip") and lg.has(m["tip"]) else None)   # branche supprimée : on suit son dernier commit
    if not ref:
        return "closed"
    if lg.merged(ref, m["to"]):
        return "merged"
    return "closed" if ref != m["from"] else "open"


def git_merge_mr(u, pid, mid):
    """Intègre une MR git validée dans sa cible (merge programmatique, sérialisé), puis pousse si un remote est défini. Conflit : la MR repasse
    à son auteur (sync, résolution, nouvelle soumission) ; au-delà de 3 essais, intervention manuelle (bouton « Réessayer »)."""
    p = snap(u, pid)
    m = by_id(p.get("mrs", []), mid)
    pre, ckf = [], []
    try:
        lg = git_repo(u, pid, p)
        with INTEGRATION_LOCK:
            if not lg.has(m["from"]):
                r = {"state": "closed"}
            else:
                if lg.remote:
                    try:
                        lg.update_target(m["to"])
                    except Err as e:
                        if e.code == 409:                          # divergence/conflit avec le remote : on n'intègre pas
                            raise
                        pre.append("remote injoignable (%s)" % e.msg.splitlines()[0][:120])    # réseau : la fusion locale reste possible
                lg.ensure_local(m["to"], GIT_INTEGRATION if m["to"] != GIT_INTEGRATION else lg.base)
                before = lg.sha(m["to"])
                r = lg.integrate(m["from"], m["to"], message=m.get("resolution_msg") or "Merge %s dans %s (MR #%s : %s)" % (m["from"], m["to"], m["n"], m.get("title", "")),
                                 resolutions=m.get("resolution"))
                if r["state"] == "merged" and checks_active(p, "integration"):          # contrôles sur le RÉSULTAT de la fusion, avant tout push
                    ckr = run_checks(u, pid, p, lg.integration_path(m["to"]), "integration", m["by"])
                    ckf = ckr["failed"]
                    if ckr["blocking_failed"]:
                        lg.rollback(m["to"], before)                                    # cible locale remise comme avant : rien n'est poussé
                        r = {"state": "checks", "ck": ckr}
    except Err as e:
        return "Merge request #%s : fusion impossible pour le moment : %s" % (m["n"], e.msg)
    if r["state"] == "closed":
        ready = mp(u, pid, lambda pp: _close_mr_in(pp, mid, "annulée", "Branche %s introuvable." % m["from"]) if (by_id(pp["mrs"], mid) or {}).get("status") in MR_ACTIVE else None)
        return "Merge request #%s annulée : la branche %s n’existe plus." % (m["n"], m["from"])
    if r["state"] == "checks":
        tries = m.get("merge_tries", 0) + 1
        csum = checks_summary(r["ck"]["results"])
        mp(u, pid, lambda pp: by_id(pp["mrs"], mid).update(status="conflit", merge_tries=tries, conflict_files=[], note="Contrôles automatiques en échec après intégration.", checks=csum))
        if tries > 3:
            return "Merge request #%s : contrôles toujours en échec après %d essais ; intervention manuelle nécessaire (bouton « Réessayer »)." % (m["n"], tries - 1)
        spawn(ask, u, pid, m["by_id"], "[Intégration] Les contrôles automatiques échouent sur le résultat de la fusion de ta merge request #%s avec %s (fusion annulée, rien n’est poussé). "
              "Lance sync, corrige, fais commit, puis soumets à nouveau avec merge_request.\n\n%s" % (m["n"], m["to"], checks_fail_text(r["ck"]["results"])), "Intégration MR")
        return "Merge request #%s : contrôles automatiques en échec après intégration (fusion annulée) : renvoyée à %s." % (m["n"], m["by"])
    if m.get("resolution"):                                # la résolution fournie a servi (ou ne couvre plus le conflit) : on ne la rejoue pas
        mp(u, pid, lambda pp: (by_id(pp["mrs"], mid) or {}).pop("resolution", None))
    if r["state"] == "conflict":
        return open_git_conflict(u, pid, m, r["files"])
    notes, pushed_from = list(pre), False
    if ckf:
        notes.append("contrôles informatifs en échec : %s" % ", ".join(e["name"] for e in ckf))
    if r.get("sha"):
        record_commit(u, pid, m["by"], "Merge MR #%s : %s" % (m["n"], m.get("title", "")), r["sha"])
    with INTEGRATION_LOCK:
        if lg.remote:                                          # décision 2 : branche de l'agent poussée à la validation ; la cible l'est toujours
            for what, fn in (("de la branche %s" % m["from"], lambda: lg.push(m["from"])), ("de %s" % m["to"], lambda: lg.push_target(m["to"]))):
                try:
                    fn()
                    pushed_from = pushed_from or what.startswith("de la branche")
                except Err as e:
                    notes.append("poussée %s impossible : %s" % (what, e.msg))
            if pushed_from and m["from"].startswith("task/") and not p.get("keepbranches") and not [n for n in notes if "poussée de " + m["to"] in n]:
                try:                                           # décision 2 : branche distante supprimée après fusion, sauf option « conserver »
                    lg.push(m["from"], delete=True)
                except Err as e:
                    notes.append("suppression de la branche distante %s impossible : %s" % (m["from"], e.msg))
    if m["from"].startswith("task/"):
        with GIT_LOCK:
            lg.delete_branch(m["from"])                    # sans effet tant que le worktree de l'agent l'occupe : nettoyée au changement de tâche

    def f(pp):
        cur = by_id(pp.get("mrs", []), mid)
        if not cur or cur["status"] not in MR_ACTIVE:
            return None
        return _close_mr_in(pp, mid, "fusionnée", "Fusionnée dans %s (git local)." % m["to"])
    ready = mp(u, pid, f)
    spawn(tick, u, pid)
    return "Merge request #%s fusionnée dans %s.%s%s" % (m["n"], m["to"], (" Tâches désormais débloquées : %s." % ", ".join("« %s »" % x for x in (ready or []))) if ready else "",
                                                          (" Attention : " + " ; ".join(notes) + ".") if notes else "")


def submit_mr(u, pid, aid, title="", body="", task=""):
    """Soumet la branche de l'agent à la branche de travail. Retourne (état, message) : submitted | exists | conflict | skip."""
    p = snap(u, pid)
    a = by_id(p["agents"], aid)
    if git_agent(p, a):
        return git_submit(u, pid, aid, title, body, task)
    try:
        rp = github.Repo(p)
    except Err:
        return "skip", "Dépôt GitHub/token non configuré : pas de merge request."
    if rp.work == rp.base:
        return "skip", "Aucune release en cours : l’utilisateur doit d’abord en démarrer une (%s est réservée à la release finale)." % rp.base
    if a["branch"] in (rp.base, rp.work):
        return "skip", "Ta branche ne peut pas être la branche de base ou la branche de travail."
    if open_conflict_for(p, aid):
        reconcile_conflicts(u, pid, aid, force=True)
        p = snap(u, pid)
        if open_conflict_for(p, aid):
            return "skip", describe_conflict(p, next(c for c in p["conflicts"] if c["agent"] == aid and c["status"] in OPEN_CONFLICT))
    ex = next((m for m in p.get("mrs", []) if m.get("id") and m.get("from") == a["branch"] and m.get("status") in MR_ACTIVE), None)
    if ex and sync_mrs(u, pid, force=True, only=ex["id"]):  # elle est peut-être déjà close sur GitHub
        p = snap(u, pid)
        ex = next((m for m in p.get("mrs", []) if m.get("id") and m.get("from") == a["branch"] and m.get("status") in MR_ACTIVE), None)
    if ex:
        return "exists", "Une merge request est déjà ouverte (#%s) : elle sera validée." % ex["n"]
    state, msg = pull(u, pid, aid, origin="mr", task=task, title=title, body=body)
    if state == "conflict":
        return "conflict", msg
    if not rp.compare(rp.work, a["branch"]).get("ahead_by"):
        return "skip", "Rien à soumettre : ta branche n’a aucun commit en avance sur %s." % rp.work
    title = title or "Merge request de %s" % a["name"]
    r = rp.pr_open(a["branch"], title, body)
    if r.get("skipped"):
        return "skip", "Rien à fusionner."
    mid = uid()
    mp(u, pid, lambda pp: pp.setdefault("mrs", []).append({"id": mid, "n": r["number"], "from": a["branch"], "to": rp.work, "t": "#%d %s" % (r["number"], title),
                                                           "title": title, "by": a["name"], "by_id": aid, "task": task, "d": now(), "status": "ouverte", "note": ""}))
    spawn(review_mr, u, pid, mid)
    return "submitted", "Merge request #%d soumise vers %s : l’agent principal va la valider." % (r["number"], rp.work)


def add_mr_decision(pp, m):
    add_decision(pp, {
        "id": uid(), "n": rights.next_seq(pp), "kind": "question", "by": m.get("by_id", ""), "byName": "Fusion", "d": now(), "status": "attente", "task": "",
        "question": "La merge request #%s « %s » (%s → %s) attend une validation et aucun agent ne peut la faire. La fusionner ?" % (m["n"], m.get("title", ""), m["from"], m["to"]),
        "options": ["Oui, fusionner", "Non, la refuser"], "recommendation": "Oui, fusionner", "impact": "", "structural": False, "mr_review": m["id"]})


def review_mr(u, pid, mid):
    """Fait valider une merge request par l'agent principal (ou un agent habilité, sinon l'utilisateur)."""
    if snap(u, pid).get("supervision") == "off":
        return

    def claim(pp):
        m = by_id(pp.get("mrs", []), mid)
        if not m or m.get("status") != "ouverte" or m.get("reviewing"):
            return False
        m["reviewing"] = True
        return True
    if not mp(u, pid, claim):
        return
    try:
        p = snap(u, pid)
        m = by_id(p["mrs"], mid)
        rv = pick_mr_reviewer(p, m["by_id"])
        if rv is None:
            def ask_user(pp):
                mm = by_id(pp["mrs"], mid)
                if not mm.get("review_asked"):
                    mm["review_asked"] = True
                    add_mr_decision(pp, mm)
            return mp(u, pid, ask_user)
        if m.get("git"):
            commits, files, dif = git_mr_view(u, pid, p, m)
            how = ("le diff ci-dessus est fourni (relis avec git_show, rev \"%s\" et path, seulement un fichier tronqué ou absent)" if dif
                   else "relis les changements (git_show avec rev \"%s\")") % m["from"]
        else:
            cmp = github.Repo(p).compare(m["to"], m["from"])
            commits = "\n".join("  - %s" % c["commit"]["message"].splitlines()[0] for c in cmp.get("commits", [])[-15:]) or "  (aucun)"
            files = ", ".join(f["filename"] for f in cmp.get("files", [])[:30]) or "(aucun)"
            dif = diff_text(github.Repo(p), m["to"], m["from"], budget=15000)
            how = ("le diff ci-dessus est fourni (relis avec read_file et \"agent\":\"%s\" seulement un fichier tronqué ou absent)" % m["by"] if dif
                   else "relis les changements (read_file avec \"agent\":\"%s\")" % m["by"])
        task = by_id(p["tasks"], m.get("task"))
        msg = ("[Application — validation de merge request] MR #%s « %s » de %s : branche %s vers %s.\nTâche liée : %s\nCommits :\n%s\nFichiers modifiés : %s\n%s"
               "Valide cette merge request : %s, vérifie qu’ils sont cohérents avec les spécifications et la branche de travail. "
               "Conclus par mr_review : approved (elle sera fusionnée) ou rejected avec ce qu’il faut corriger.") % (
            m["n"], m.get("title", ""), m["by"], m["from"], m["to"], task["title"] if task else "aucune", commits, files,
            ("Diff :\n%s\n" % dif) if dif else "",
            how)
        ask(u, pid, rv["id"], msg, "Validation MR")
        if by_id(snap(u, pid)["mrs"], mid)["status"] == "ouverte":
            def nofinal(pp):
                mm = by_id(pp["mrs"], mid)
                mm["review_fail"] = mm.get("review_fail", 0) + 1
                if mm["review_fail"] >= 2 and not mm.get("review_asked"):
                    mm["review_asked"] = True
                    add_mr_decision(pp, mm)
            mp(u, pid, nofinal)
    except Err as e:                                   # nouvelle tentative au prochain passage du suivi
        def err(pp):
            mm = by_id(pp["mrs"], mid)
            mm["review_err"] = mm.get("review_err", 0) + 1
            return mm["review_err"]
        if mp(u, pid, err) == 1:
            log(u, pid, "Système", "Vous", "Validation de la merge request impossible pour le moment : %s" % e.msg)
    finally:
        mp(u, pid, lambda pp: (by_id(pp.get("mrs", []), mid) or {}).pop("reviewing", None))


def apply_mr_review(u, pid, mid, by, verdict, comment):
    m = by_id(snap(u, pid).get("mrs", []), mid)
    if not m or m["status"] != "ouverte":
        return "Merge request introuvable ou déjà traitée."
    if verdict == "approved":
        mp(u, pid, lambda pp: by_id(pp["mrs"], mid).update(note="Validée par %s%s" % (by, (" : " + comment) if comment else "")))
        return merge_mr(u, pid, mid)
    mp(u, pid, lambda pp: by_id(pp["mrs"], mid).update(status="refusée", note=comment, done=now()))
    spawn(ask, u, pid, m["by_id"], "[Validation MR par %s] Ta merge request #%s est refusée : %s\nCorrige puis soumets-la à nouveau avec merge_request." % (by, m["n"], comment), "Validation MR")
    return "Merge request #%s refusée : %s" % (m["n"], comment)


def merge_mr(u, pid, mid):
    """Fusionne une merge request validée ; un conflit déclenche la règle du projet."""
    p = snap(u, pid)
    m = by_id(p.get("mrs", []), mid)
    if not m or m["status"] not in ("ouverte", "conflit"):
        return "Merge request introuvable ou déjà traitée."
    if m.get("git"):
        return git_merge_mr(u, pid, mid)
    if github.Repo(p).pr_merge_number(m["n"]) == "merged":
        def f(pp):
            by_id(pp["mrs"], mid).update(status="fusionnée", done=now())
            for c in pp.get("conflicts", []):
                if c.get("mr") == mid and c["status"] != "résolu":
                    c["status"] = "résolu"
            return [t["title"] for t in pp["tasks"] if t.get("deps") and t["status"] != "terminée" and not deps_unmet(pp, t)]
        ready = mp(u, pid, f)
        spawn(tick, u, pid)
        return "Merge request #%s fusionnée dans %s.%s" % (m["n"], m["to"], (" Tâches désormais débloquées : %s." % ", ".join("« %s »" % x for x in ready)) if ready else "")
    tries = m.get("merge_tries", 0) + 1
    mp(u, pid, lambda pp: by_id(pp["mrs"], mid).update(merge_tries=tries))
    if tries > 3:
        mp(u, pid, lambda pp: by_id(pp["mrs"], mid).update(status="conflit"))
        return "Merge request #%s : toujours non fusionnable après %d essais ; intervention manuelle nécessaire (bouton « Réessayer »)." % (m["n"], tries - 1)
    return open_conflict(u, pid, m["by_id"], "mr", mr_id=mid, task=m.get("task", ""))


def _close_mr_in(pp, mid, status, why):
    """Clôt une MR dans l'état (sans GitHub, sans IA) et libère ce qui l'attendait : conflit lié, question de validation, tâche en attente.
    Retourne les titres des tâches désormais débloquées."""
    m = by_id(pp.get("mrs", []), mid)
    m.update(status=status, done=now(), note=why)
    m.pop("reviewing", None)
    for c in pp.get("conflicts", []):
        if c.get("mr") == mid and c["status"] in OPEN_CONFLICT:
            _close_in(pp, c["id"], "abandonné" if status == "annulée" else "résolu", why)
    for d in pp.get("decisions", []):
        if d.get("mr_review") == mid and d["status"] == "attente":
            d.update(status="répondue", answer=why, done=now(), n_done=rights.next_seq(pp))
    return [t["title"] for t in pp["tasks"] if t.get("deps") and t["status"] != "terminée" and not deps_unmet(pp, t)]


def sync_mrs(u, pid, force=False, only=None):
    """Aligne les merge requests de l'application sur GitHub, SANS IA : une MR « ouverte » ou « en conflit » ici mais fusionnée ou fermée
    dans le dépôt central est retirée des blocages (fusionnée, ou annulée si abandonnée). Retourne [(n, nouveau statut)].
    Prudent : une erreur réseau ou une MR introuvable (accès, numéro) ne conclut rien ; le bouton « Marquer terminée » reste le secours."""
    p = snap(u, pid)
    todo = [m for m in p.get("mrs", []) if m.get("id") and m.get("n") and m.get("status") in MR_ACTIVE and (not only or m["id"] == only)]
    if not todo:
        return []
    rp, out = None, []
    for m in todo:
        if not force and time.time() - m.get("checked", 0) < MR_SYNC_EVERY:
            continue
        mp(u, pid, lambda pp, i=m["id"]: (by_id(pp["mrs"], i) or {}).__setitem__("checked", time.time()))
        try:
            if m.get("git"):
                st = git_mr_state(u, pid, p, m)              # déduit de git : fusionnée si la branche est dans la cible, annulée si elle n'existe plus
            else:
                rp = rp or github.Repo(p)
                st = rp.pr_state(int(m["n"]))
        except (Err, ValueError):
            continue
        if st == "missing":                                  # introuvable (accès, numéro) : rien n'est conclu, mais l'utilisateur doit le savoir (sinon blocage invisible)
            def miss(pp, i=m["id"]):
                cur = by_id(pp["mrs"], i)
                if not cur or cur["status"] not in MR_ACTIVE:
                    return 0
                cur["missing_n"] = cur.get("missing_n", 0) + 1
                if cur["missing_n"] >= MISSING_WARN and not cur.get("missing_warned"):
                    cur["missing_warned"] = True
                    return cur["missing_n"]
                return 0
            if mp(u, pid, miss):
                log(u, pid, "Système", "Vous", "Merge request #%s « %s » : introuvable sur GitHub après %d vérifications (token sans accès au dépôt, ou numéro invalide). Elle continue de bloquer les agents : vérifiez le token du projet, ou utilisez « Marquer terminée »." % (m["n"], m.get("title", ""), MISSING_WARN))
            continue
        if st == "open":
            mp(u, pid, lambda pp, i=m["id"]: [(by_id(pp["mrs"], i) or {}).pop(k, None) for k in ("missing_n", "missing_warned")])
            continue
        where = "dans git" if m.get("git") else "sur GitHub"
        status, why = (("fusionnée", "Fusionnée %s (détectée automatiquement)." % where) if st == "merged"
                       else ("annulée", "%s sans fusion (détectée automatiquement)." % ("Branche supprimée" if m.get("git") else "Fermée sur GitHub")))

        def f(pp, i=m["id"], status=status, why=why):
            cur = by_id(pp["mrs"], i)
            if not cur or cur["status"] not in MR_ACTIVE:     # déjà traitée entre-temps (validation, bouton…)
                return None
            return _close_mr_in(pp, i, status, why)
        ready = mp(u, pid, f)
        if ready is None:
            continue
        out.append((m["n"], status))
        log(u, pid, "Système", "Vous", "Merge request #%s « %s » : %s %s. Elle ne bloque plus les agents.%s" % (
            m["n"], m.get("title", ""), "fusionnée" if status == "fusionnée" else ("annulée" if m.get("git") else "fermée sans fusion"), where,
            (" Tâches désormais débloquées : %s." % ", ".join("« %s »" % x for x in ready)) if ready else ""))
    return out


def mr_sync(u, pid):
    """Bouton « Vérifier sur GitHub » : revérifie tout de suite les MR en attente."""
    if not sync_mrs(u, pid, force=True):
        log(u, pid, "Système", "Vous", "Vérification : aucune merge request à retirer (celles qui restent sont bien encore ouvertes dans le dépôt, ou le dépôt n’a pas pu être interrogé).")


def mr_done(u, pid, mid):
    """Bouton « Marquer terminée » (secours si la vérification automatique échoue) : la MR ne bloque plus ni les agents ni la release."""
    def f(pp):
        m = by_id(pp.get("mrs", []), mid)
        if not m or m.get("status") not in MR_ACTIVE:
            return None
        return _close_mr_in(pp, mid, "terminée", "Marquée terminée par l’utilisateur.")
    ready = mp(u, pid, f)
    m = by_id(snap(u, pid).get("mrs", []), mid) or {}
    if ready is None:
        return log(u, pid, "Système", "Vous", "Merge request introuvable ou déjà close : rien à faire.")
    log(u, pid, "Système", "Vous", "Merge request #%s « %s » marquée terminée : elle ne bloque plus les agents ni la release.%s" % (
        m.get("n", "?"), m.get("title", ""), (" Tâches désormais débloquées : %s." % ", ".join("« %s »" % x for x in ready)) if ready else ""))


def mr_retry(u, pid, mid):
    mp(u, pid, lambda pp: (by_id(pp.get("mrs", []), mid) or {}).pop("merge_tries", None))
    log(u, pid, "Système", "Vous", merge_mr(u, pid, mid))


def git_release(u, pid, p, version, notes):
    """Publication d'une release en mode git local : la release est fusionnée dans la branche de base, le tag annoté est créé, la base est reportée dans
    `develop`, puis tout est poussé si un remote est défini. Refusée tant qu'une MR est ouverte ou qu'une tâche est « à vérifier » ; contrôles
    d'intégration sur la base avant le tag ; tout échec avant le tag laisse la base comme avant et la release en cours."""
    mg = by_id(p["agents"], p.get("mainId"))
    who, cy = (mg["name"] if mg else "Système"), p.get("cycle")
    if not cy:
        return log(u, pid, "Système", "Vous", "Aucune release en cours : démarrez-en une (« Démarrer une release ») avant de la publier.")
    if not version:
        return log(u, pid, "Système", "Vous", "Version vide : release non publiée.")
    sync_mrs(u, pid, force=True)
    p = snap(u, pid)
    opn = [m for m in p.get("mrs", []) if m.get("status") in MR_ACTIVE]
    if opn:
        return log(u, pid, "Système", "Vous", "Publication impossible : %d merge request(s) encore ouverte(s) (%s). Validez-les ou refusez-les d’abord." % (len(opn), ", ".join("#%s" % m["n"] for m in opn)))
    pend = [t for t in p.get("tasks", []) if t.get("status") == "à vérifier"]
    if pend:
        return log(u, pid, "Système", "Vous", "Publication impossible : %d tâche(s) encore à vérifier (%s)." % (len(pend), ", ".join("« %s »" % t["title"] for t in pend[:5])))
    try:
        lg = git_repo(u, pid, p)
    except Err as e:
        return log(u, pid, "Système", "Vous", "Publication impossible : %s" % e.msg)
    if lg.tag_exists(version):
        return log(u, pid, "Système", "Vous", "Publication impossible : le tag %s existe déjà. Choisissez une autre version." % version)
    base, rel = lg.base, cy["branch"]
    mp(u, pid, lambda pp: pp.__setitem__("releasing", True))
    out, warn, ok, sha = [], [], False, ""
    try:
        try:
            with INTEGRATION_LOCK:
                if lg.remote:
                    for b in (rel, base, GIT_INTEGRATION):
                        try:
                            lg.update_target(b)
                        except Err as e:
                            if e.code == 409:
                                raise
                            warn.append("remote injoignable (%s)" % e.msg.splitlines()[0][:100])
                            break
                before = lg.sha(base)
                if lg.merged(rel, base):
                    out.append("%s : rien à fusionner dans %s" % (rel, base))
                else:
                    r = lg.integrate(rel, base, message="Release %s" % version)
                    if r["state"] == "conflict":
                        raise Err(409, "conflit entre %s et %s (fichiers : %s)" % (rel, base, ", ".join(r["files"])))
                    if checks_active(p, "integration"):                       # contrôles sur la base fusionnée, avant le tag
                        ck = run_checks(u, pid, p, lg.integration_path(base), "integration", who)
                        if ck["blocking_failed"]:
                            lg.rollback(base, before)
                            raise Err(409, "contrôles automatiques en échec sur %s après fusion de la release :\n%s" % (base, checks_fail_text(ck["results"])))
                    out.append("%s fusionnée dans %s" % (rel, base))
                sha = lg.sha(base)
                try:
                    lg.tag(version, "Release %s%s" % (version, ("\n\n" + notes.strip()) if notes.strip() else ""), sha)
                except Err:
                    if before and sha != before:
                        lg.rollback(base, before)
                    raise
                out.append("Tag annoté %s créé sur %s." % (version, base))
                try:                                                          # retour de la base dans develop (avance rapide si possible)
                    if not lg.fast_forward(GIT_INTEGRATION, base):
                        if lg.integrate(base, GIT_INTEGRATION, message="Retour de %s dans %s (release %s)" % (base, GIT_INTEGRATION, version))["state"] == "conflict":
                            warn.append("le retour de %s dans %s est en conflit : à traiter à la main (develop est inchangée)" % (base, GIT_INTEGRATION))
                        else:
                            out.append("%s reportée dans %s." % (base, GIT_INTEGRATION))
                    else:
                        out.append("%s avancée jusqu’à %s." % (GIT_INTEGRATION, base))
                except Err as e:
                    warn.append("retour dans %s impossible : %s" % (GIT_INTEGRATION, e.msg))
                if lg.remote:
                    for what, fn in (("de %s" % base, lambda: lg.push_target(base)), ("du tag %s" % version, lambda: lg.push_tag(version)),
                                     ("de %s" % GIT_INTEGRATION, lambda: lg.push_target(GIT_INTEGRATION)), ("de %s" % rel, lambda: lg.push(rel))):
                        try:
                            fn()
                        except Err as e:
                            warn.append("poussée %s impossible : %s" % (what, e.msg))
                    if not [w for w in warn if w.startswith("poussée")]:
                        out.append("Branches et tag poussés sur le remote.")
                try:
                    lg.restore_root(base)
                except Err as e:
                    warn.append("dépôt racine non remis sur %s : %s" % (base, e.msg))
            ok = True
        except Err as e:
            out.append("ÉCHEC — %s (la release reste en cours)" % e.msg)
        url = ""
        if ok and lg.remote and not [w for w in warn if w.startswith("poussée")]:
            try:                                                              # release GitHub (best-effort : sans jeton ou hors GitHub, le tag suffit)
                url = github.Repo(p).release(version, "Release " + version, notes)
            except Err:
                pass
        if ok:
            def done(pp):
                n = 1 + max([m.get("n") or 0 for m in pp.get("mrs", [])] or [0])
                pp["mrs"].append({"id": uid(), "n": n, "git": True, "from": rel, "to": base, "t": "#%d release %s" % (n, version), "title": "Release %s" % version, "by": who,
                                  "d": now(), "status": "fusionnée", "note": "Release finale", "tip": sha})
                pp["releases"].append({"v": version, "n": notes, "d": now(), "url": url, "git": True, "sha": sha})
                pp.pop("cycle", None)
            mp(u, pid, done)
            out.append("La release est terminée.")
        if warn:
            out.append("Attention : " + " ; ".join(warn) + ".")
        log(u, pid, who, "Vous", "Release %s :\n%s" % (version, "\n".join(out)))
    finally:
        mp(u, pid, lambda pp: pp.__setitem__("releasing", False))


def release(u, pid, version, notes):
    """Release finale : fusionne la branche de travail dans la branche de base, crée le tag, termine le cycle."""
    p = snap(u, pid)
    mg = by_id(p["agents"], p.get("mainId"))
    who, cy = (mg["name"] if mg else "Système"), p.get("cycle")
    version = version.strip() or (cy or {}).get("version", "")
    if git_mode(p):
        return git_release(u, pid, p, version, notes)
    try:
        rp = github.Repo(p)
    except Err:
        mp(u, pid, lambda pp: pp["releases"].append({"v": version, "n": notes, "d": now()}))
        return log(u, pid, "Système", who, "Release %s enregistrée localement : dépôt/token non configuré, ni fusion ni tag." % version)
    if not cy:
        return log(u, pid, "Système", "Vous", "Aucune release en cours : démarrez-en une (« Démarrer une release ») avant de la publier.")
    sync_mrs(u, pid, force=True)                       # une MR déjà close sur GitHub ne doit pas empêcher la publication
    p = snap(u, pid)
    opn = [m for m in p.get("mrs", []) if m.get("status") in MR_ACTIVE]
    if opn:
        return log(u, pid, "Système", "Vous", "Publication impossible : %d merge request(s) encore ouverte(s) (%s). Validez-les ou refusez-les d’abord." % (len(opn), ", ".join("#%s" % m["n"] for m in opn)))
    mp(u, pid, lambda pp: pp.__setitem__("releasing", True))
    out = []
    try:
        try:
            r = rp.pr_merge(cy["branch"], "Release %s" % version, notes)
            if r.get("skipped"):
                out.append("%s : rien à fusionner dans %s" % (cy["branch"], rp.base))
            else:
                mp(u, pid, lambda pp: pp["mrs"].append({"id": uid(), "n": r["number"], "from": cy["branch"], "to": rp.base, "t": "#%d release %s" % (r["number"], version),
                                                        "title": "Release %s" % version, "by": who, "d": now(), "status": "fusionnée", "note": "Release finale"}))
                out.append("%s fusionnée dans %s (MR #%d)" % (cy["branch"], rp.base, r["number"]))
            url = rp.release(version, "Release " + version, notes)

            def done(pp):
                pp["releases"].append({"v": version, "n": notes, "d": now(), "url": url})
                pp.pop("cycle", None)
            mp(u, pid, done)
            out.append("Tag %s créé sur %s : la release est terminée." % (version, rp.base))
        except Err as e:
            out.append("ÉCHEC — %s (la release reste en cours)" % e.msg)
        log(u, pid, who, "Vous", "Release %s :\n%s" % (version, "\n".join(out)))
    finally:
        mp(u, pid, lambda pp: pp.__setitem__("releasing", False))


def start(u, j):
    """Lance un travail en arrière-plan (un thread par demande)."""
    t, pid = j.get("type"), j.get("projectId")
    snap(u, pid)
    jobs = {"say": lambda: say(u, pid, j.get("agentId"), str(j.get("text", ""))),
            "run_task": lambda: run_task(u, pid, j.get("taskId")),
            "onboard": lambda: onboard(u, pid, j.get("agentId")),
            "sync": lambda: sync_all(u, pid),
            "release": lambda: release(u, pid, str(j.get("version", "")), str(j.get("notes", ""))),
            "reset_usage": lambda: reset_usage(u, pid),
            "rules_changed": lambda: rules_changed(u, pid),
            "rules_check": lambda: rules_check(u, pid),
            "workflow_run": lambda: wf_user_run(u, pid, j.get("wfId"), j.get("task") or "", str(j.get("note", ""))),
            "wf_resume": lambda: wf_resume(u, pid, j.get("runId")),
            "wf_stop": lambda: _wf_end(u, pid, j.get("runId"), "arrêté", "arrêté par l’utilisateur"),
            "conflict_action": lambda: conflict_action(u, pid, j.get("conflictId"), str(j.get("action", ""))),
            "agent_reset": lambda: agent_reset(u, pid, j.get("agentId"), bool(j.get("history", True)), bool(j.get("notes", False)), bool(j.get("branch", False)), bool(j.get("onboard", True))),
            "cycle_start": lambda: cycle_start(u, pid, str(j.get("version", ""))),
            "lock_release": lambda: lock_release(u, pid, j.get("lockId")),
            "locks_clear": lambda: locks_clear(u, pid),
            "agent_stop": lambda: stop_agent(u, pid, j.get("agentId"), bool(j.get("cancel")), "Vous", str(j.get("reason", ""))[:300]),
            "localdir_check": lambda: localdir_check(u, pid),
            "git_push": lambda: git_push(u, pid, j.get("agentId") or None),
            "git_push_release": lambda: git_push_release(u, pid),
            "check_remove": lambda: check_remove(u, pid, j.get("checkId")),
            "check_restore": lambda: check_restore(u, pid, j.get("histId")),
            "check_baseline": lambda: check_baseline(u, pid, j.get("checkId")),
            "mr_retry": lambda: mr_retry(u, pid, j.get("mrId")),
            "mr_done": lambda: mr_done(u, pid, j.get("mrId")),
            "mr_sync": lambda: mr_sync(u, pid),
            "decide": lambda: decide(u, pid, j.get("decisionId"), str(j.get("decision", "")), str(j.get("note", "")), j.get("fields") or {}, str(j.get("answer", ""))),
            "instr_add": lambda: instr_add(u, pid, str(j.get("scope", "global")), str(j.get("text", "")), tuple(j.get("replaces") or ()), str(j.get("subject", ""))),
            "instr_consolidate": lambda: instr_consolidate(u, pid),
            "instr_del": lambda: mp(u, pid, lambda pp: pp.__setitem__("instructions", [i for i in pp.get("instructions", []) if i["id"] != j.get("instrId")]))}
    if t not in jobs:
        raise Err(400, "Type de travail inconnu")

    def run():
        try:
            jobs[t]()
            if t in ("decide", "lock_release", "locks_clear", "agent_stop", "mr_retry", "mr_done", "mr_sync", "cycle_start", "instr_add"):
                tick(u, pid)                              # la décision peut débloquer des tâches : on les lance sans attendre
        except Exception as e:  # ne jamais perdre un échec silencieusement
            try:
                log(u, pid, "Système", "Vous", "Erreur interne (%s) : %s" % (t, e))
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()


def recover():
    """Au démarrage : remet à zéro les indicateurs de travail et relance les reprises après quota."""
    for f in (DATA / "users").glob("*.json"):
        u, retries = f.stem, []

        def fix(st):
            for p in st.get("projects", []):
                p["releasing"] = False
                p["pending"] = []
                n = len(p.get("locks", []))                    # plus aucun agent ne travaille : aucun verrou ne doit survivre au redémarrage
                p["locks"], p["lock_wait"] = [], []
                if n:
                    p.setdefault("log", []).append({"d": now(), "from": "Système", "to": "Vous", "text": "Redémarrage du serveur : %d verrou(x) de fichiers libéré(s)." % n})
                for c in p.get("conflicts", []):
                    if c.get("status") == "ouvert":
                        c["status"] = "pause"
                for m in p.get("mrs", []):
                    m.pop("reviewing", None)
                for a in p.get("agents", []):
                    a["work"] = 0
                for t in p.get("tasks", []):
                    if t.get("retry_at"):
                        retries.append((p["id"], t["id"], max(0, t["retry_at"] - time.time())))
                    elif t.get("running"):
                        t["running"], t["status"], t["result"] = False, "à faire", "Interrompu par un redémarrage du serveur."
                    t.pop("reviewing", None)
        mutate(u, fix)
        locks.STOP.clear()
        shutil.rmtree(DATA / "lockbak", ignore_errors=True)
        for pid, tid, w in retries:
            schedule(u, pid, tid, w)
