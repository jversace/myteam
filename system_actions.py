"""Étapes de workflow exécutées par le système, SANS IA (voir docs/PROPOSITION-ETAPES-SYSTEME.md).

Une étape `kind: "system"` porte une action du catalogue et ses paramètres :
  action  identifiant de l'action (ACTIONS)
  target  identifiant de la tâche / du bug / de la MR visé ; vide = tâche cible du déroulement (ou MR liée à cette tâche)
  value   statut, branche source, durée en secondes ou verdict selon l'action
  who     acteur : principal, assignee, requester, agent:<id>, role:<id> (même syntaxe que les étapes d'agent)
  text    texte (variables {task.title} {task.id} {task.assignee} {run.requester} {step.N.comment})
  confirm confirmation de l'utilisateur avant l'exécution ; par défaut oui pour les actions sensibles
Le résultat est toujours « ok » ou « ko » avec un commentaire ; une exception Err est un ko. Aucun appel d'IA, aucun token."""
import re
import time

import github
import project_rules as prules
import rights
from rights import by_id, now, uid
from store import Err

SOURCES = {"": "la branche de travail de la release en cours (sinon d’intégration)", "release": "la branche de travail de la release en cours (sinon d’intégration)",
           "develop": "la branche d’intégration (develop)", "main": "la branche principale"}
WAIT_MAX = 600

# id : (libellé, cible, valeur, acteur, texte, aide)
ACTIONS = {
    "set_task_status": ("Changer le statut d’une tâche", "task", "task_status", False, False, "valeur = statut"),
    "assign_task": ("Affecter une tâche à un agent", "task", None, True, False, "acteur = nom ou rôle"),
    "add_task_note": ("Ajouter une note au compte rendu d’une tâche", "task", None, False, True, "texte"),
    "launch_task": ("Lancer l’agent assigné sur une tâche", "task", None, False, True, "texte = consigne facultative"),
    "set_bug_status": ("Changer le statut d’un bug", "bug", "bug_status", False, False, "valeur = statut"),
    "assign_bug_fix": ("Affecter les corrections d’un bug à un agent", "bug", None, True, False, "acteur = nom ou rôle"),
    "sync_agent_branch": ("Intégrer une branche de référence dans la branche d’un agent", None, "source", True, False, "valeur = release, develop ou main"),
    "sync_mrs": ("Aligner les merge requests sur le dépôt (blockers fantômes)", None, None, False, False, ""),
    "mark_mr_done": ("Marquer une merge request terminée", "mr", None, False, False, ""),
    "notify_agent": ("Informer un agent (sans appel IA)", None, None, True, True, "texte"),
    "notify_user": ("Informer l’utilisateur", None, None, False, True, "texte"),
    "wait": ("Attendre", None, "seconds", False, False, "valeur = secondes (max %d)" % WAIT_MAX),
    "end_workflow": ("Terminer le workflow", None, "outcome", False, True, "valeur = ok ou ko"),
    # lot v2
    "if": ("Condition : réussit si elle est vraie, échoue sinon (aiguillage par « si réussite » / « si échec »)", None, "condition", True, False,
           "valeur = task_status=<statut>, bug_status=<statut>, subtasks_done, mr_active, agent_free, decision_pending ou release_open ; cible : tâche ou bug"),
    "create_subtask": ("Créer une sous-tâche", "task", None, True, True, "cible = tâche parente ; texte = titre ; acteur = agent assigné"),
    "create_bug_fix_subtask": ("Créer une sous-tâche de correction d’un bug", "bug", None, True, True, "texte = titre ; acteur = agent assigné"),
    "link_modules": ("Rattacher une tâche ou un bug à des modules", None, "modules", False, False, "cible = tâche ou bug ; valeur = noms de modules séparés par des virgules"),
    "set_task_dependency": ("Ajouter ou retirer une dépendance d’une tâche", "task", "dep", False, False, "valeur = add:<id de tâche> ou remove:<id de tâche>"),
    "close_subtasks": ("Changer le statut de toutes les sous-tâches d’une tâche", "task", "task_status", False, False, "valeur = statut"),
    "sync_all_agent_branches": ("Intégrer une branche de référence dans la branche de tous les agents", None, "source", False, False, "valeur = release, develop ou main"),
    "stop_agent": ("Arrêter le travail d’un agent", None, None, True, False, "acteur = nom ou rôle"),
    "run_workflow": ("Lancer un autre workflow", "task", "workflow", False, False, "valeur = nom du workflow ; cible = tâche concernée (facultatif)"),
}

CONDITIONS = ("task_status", "bug_status", "subtasks_done", "mr_active", "agent_free", "decision_pending", "release_open")


def default_ko(st):
    """Comportement par défaut d'un échec : question à l'utilisateur ; pour une condition, l'échec est une réponse « faux » : on continue."""
    return "continue" if st.get("action") == "if" else "ask_user"


def sensitive(st):
    """Action qui contourne un contrôle : confirmation de l'utilisateur par défaut (décochable par étape)."""
    return (st.get("action") in ("set_task_status", "close_subtasks") and st.get("value") == "terminée") or (st.get("action") == "set_bug_status" and st.get("value") == "vérifié")


def needs_confirm(st):
    c = st.get("confirm")
    return sensitive(st) if c is None or c == "" else str(c).lower() not in ("0", "false", "non", "no")


def render(text, p, run):
    """Variables d'un texte : {task.title} {task.id} {task.assignee} {run.requester} {step.N.comment}."""
    t = by_id(p.get("tasks", []), (run or {}).get("task"))
    ag = lambda i: (by_id(p.get("agents", []), i) or {}).get("name", "")

    def sub(m):
        k = m.group(1)
        if k == "task.title":
            return (t or {}).get("title", "")
        if k == "task.id":
            return (t or {}).get("id", "")
        if k == "task.assignee":
            return ag((t or {}).get("who"))
        if k == "run.requester":
            return ag((run or {}).get("requester"))
        m2 = re.match(r"step\.(\d+)\.comment$", k)
        if m2:
            r = next((x for x in (run or {}).get("results", []) if x["i"] == int(m2.group(1))), None)
            return (r or {}).get("comment", "")
        return m.group(0)
    return re.sub(r"\{([a-z]+(?:\.[a-z0-9]+)+)\}", sub, str(text or ""))


def describe(p, st):
    """Phrase décrivant l'étape (prompts des agents, liste des étapes, question de confirmation)."""
    act = st.get("action")
    if act not in ACTIONS:
        return "action système inconnue (%s)" % act
    lab, tk, vk, _, _, _ = ACTIONS[act]
    parts = []
    if tk:
        coll = {"task": "tasks", "bug": "bugs", "mr": "mrs"}[tk]
        it = by_id(p.get(coll, []), st.get("target"))
        parts.append(("%s « %s »" % ({"task": "tâche", "bug": "bug", "mr": "MR"}[tk], it.get("title") or it.get("t") or it.get("n") or "?")) if it else
                     ("%s %s" % ({"task": "tâche", "bug": "bug", "mr": "MR"}[tk], st.get("target")) if st.get("target") else "la cible du déroulement"))
    if vk:
        parts.append({"source": SOURCES.get(st.get("value") or "", "?"), "seconds": "%s s" % st.get("value", "?")}.get(vk, "« %s »" % st.get("value", "?")))
    if ACTIONS[act][3] and (act != "if" or str(st.get("value") or "").startswith("agent_free")):
        who = st.get("who") or "principal"
        a = prules.resolve_actor(p, st) if who not in ("assignee", "requester") else None
        parts.append({"principal": "l’agent principal", "assignee": "l’agent assigné", "requester": "l’agent demandeur"}.get(who) or (a or {}).get("name") or
                     ("un agent du rôle %s" % (by_id(rights.roles_of(p), who[5:]) or {}).get("name", "?") if who.startswith("role:") else "?"))
    if ACTIONS[act][4] and st.get("text"):
        parts.append("« %s »" % str(st["text"])[:80])
    return "%s%s" % (lab, (" — " + " ; ".join(parts)) if parts else "")


def lint_step(p, st, timer, wf=None):
    """[(niveau, message)] pour une étape système."""
    out, act = [], st.get("action")
    if act not in ACTIONS:
        return [("erreur", "Action système inconnue ou absente : %s." % (act or "—"))]
    _, tk, vk, wk, xk, _ = ACTIONS[act]
    if tk:
        coll = {"task": "tasks", "bug": "bugs", "mr": "mrs"}[tk]
        if st.get("target") and not by_id(p.get(coll, []), st["target"]):
            out.append(("erreur", "La cible désignée (%s) n’existe plus." % st["target"]))
        elif not st.get("target") and timer:
            out.append(("erreur", "L’étape vise la tâche cible du déroulement, mais le workflow se déclenche aussi par minuterie, qui n’en fournit aucune : l’étape échouera."))
        elif not st.get("target") and tk == "bug":
            out.append(("erreur", "Aucun bug désigné : indiquez l’identifiant du bug."))
    if vk == "task_status" and st.get("value") not in rights.STATUS:
        out.append(("erreur", "Statut de tâche invalide : %s." % (st.get("value") or "—")))
    if vk == "bug_status" and st.get("value") not in rights.BUG_STATUS:
        out.append(("erreur", "Statut de bug invalide : %s." % (st.get("value") or "—")))
    if vk == "source" and (st.get("value") or "") not in SOURCES:
        out.append(("erreur", "Branche source inconnue : %s (release, develop ou main)." % st["value"]))
    if vk == "seconds":
        try:
            ok = 1 <= int(st.get("value")) <= WAIT_MAX
        except (TypeError, ValueError):
            ok = False
        if not ok:
            out.append(("erreur", "Durée invalide : entre 1 et %d secondes." % WAIT_MAX))
    if vk == "outcome" and st.get("value") not in ("ok", "ko"):
        out.append(("erreur", "Verdict invalide : ok ou ko."))
    if vk == "condition":
        name, _, arg = str(st.get("value") or "").partition("=")
        if name not in CONDITIONS:
            out.append(("erreur", "Condition inconnue : %s." % (st.get("value") or "—")))
        elif name == "task_status" and arg not in rights.STATUS:
            out.append(("erreur", "Statut de tâche invalide dans la condition : %s." % arg))
        elif name == "bug_status" and arg not in rights.BUG_STATUS:
            out.append(("erreur", "Statut de bug invalide dans la condition : %s." % arg))
        elif name in ("task_status", "subtasks_done", "mr_active") and not st.get("target") and timer:
            out.append(("erreur", "La condition porte sur la tâche cible, que la minuterie ne fournit pas."))
        elif name == "bug_status" and not by_id(p.get("bugs", []), st.get("target")):
            out.append(("erreur", "Aucun bug valide désigné pour la condition."))
    if vk == "dep":
        m = re.match(r"^(add|remove):(\S+)$", str(st.get("value") or ""))
        if not m:
            out.append(("erreur", "Valeur attendue : add:<id de tâche> ou remove:<id de tâche>."))
        elif not by_id(p.get("tasks", []), m.group(2)) or m.group(2) == st.get("target"):
            out.append(("erreur", "Tâche de dépendance introuvable ou identique à la cible."))
    if vk == "workflow":
        w2 = prules.find_workflow(p, st.get("value") or "")
        if not w2:
            out.append(("erreur", "Workflow à lancer introuvable : %s." % (st.get("value") or "—")))
        elif wf and w2.get("id") == wf.get("id"):
            out.append(("erreur", "Un workflow ne peut pas se relancer lui-même."))
    if vk == "modules":
        names = [x.strip() for x in str(st.get("value") or "").split(",") if x.strip()]
        if not names or len(rights.mod_ids(p, names)) != len(names):
            out.append(("erreur", "Module(s) inconnu(s) : %s." % (st.get("value") or "—")))
        if not (by_id(p.get("tasks", []), st.get("target")) or by_id(p.get("bugs", []), st.get("target"))):
            out.append(("erreur", "Aucune tâche ni aucun bug désigné."))
    if act in ("create_subtask", "create_bug_fix_subtask", "link_modules") and not st.get("target") and timer and act != "create_bug_fix_subtask":
        out.append(("erreur", "L’étape vise la tâche cible, que la minuterie ne fournit pas."))
    if xk and act not in ("launch_task", "end_workflow") and not prules.norm(st.get("text")):
        out.append(("erreur", "Texte vide."))
    if wk:
        who = st.get("who") or "principal"
        if who == "assignee" and (timer or not st.get("target")):
            out.append(("erreur", "« Agent assigné » exige une tâche cible, que la minuterie ne fournit pas."))
        elif who == "principal" and not by_id(p.get("agents", []), p.get("mainId")):
            out.append(("erreur", "Aucun agent principal n’est défini."))
        elif who.startswith(("agent:", "role:")) and not prules.resolve_actor(p, st):
            out.append(("erreur", "L’agent ou le rôle désigné n’existe plus ou n’a aucun agent."))
    if sensitive(st) and not needs_confirm(st):
        out.append(("avertissement", "Action sensible exécutée sans confirmation : elle contourne la vérification."))
    if sensitive(st) and needs_confirm(st) and timer:
        out.append(("info", "La confirmation de l’utilisateur sera demandée à chaque déclenchement par minuterie."))
    return out


# ---------------------------------------------------------------- exécution
def _task(p, run, st):
    t = by_id(p["tasks"], st.get("target") or (run or {}).get("task"))
    if not t:
        raise Err(404, "tâche introuvable (indiquez-la dans l’étape ou au déclenchement)")
    return t


def _actor(p, run, st):
    a = prules.resolve_actor(p, {"who": st.get("who") or "principal"}, run)
    if not a:
        raise Err(404, "agent introuvable (%s)" % (st.get("who") or "principal"))
    return a


def run_step(E, u, pid, p, wf, run, i, st):
    """Exécute l'étape système i. Retourne (issue ok|ko|end, commentaire, attente)."""
    act = st.get("action")
    if act not in ACTIONS:
        return "ko", "action système inconnue : %s" % act, False
    if needs_confirm(st):
        if run.get("confirmed") != i:
            def gate(pp):
                r = by_id(pp["wf_runs"], run["id"])
                d = {"id": uid(), "n": rights.next_seq(pp), "kind": "question", "by": "", "byName": "Workflow", "d": now(), "status": "attente", "task": "", "structural": False,
                     "question": "Workflow « %s », étape %d (automatique, sans IA) : %s. Exécuter ?" % (wf["name"], i + 1, describe(p, st)),
                     "options": ["Exécuter", "Arrêter le workflow"], "recommendation": "", "impact": "", "wf_run": run["id"], "wf_step": i, "wf_after": "sysconfirm"}
                E.add_decision(pp, d)
                r["waiting"] = d["id"]
            E.mp(u, pid, gate)
            return "ok", "", True
        E.mp(u, pid, lambda pp: (by_id(pp["wf_runs"], run["id"]) or {}).pop("confirmed", None))
    try:
        return HANDLERS[act](E, u, pid, p, wf, run, st) + (False,)
    except Err as e:
        return "ko", e.msg, False


def _set_task_status(E, u, pid, p, wf, run, st):
    t, s = _task(p, run, st), st.get("value")
    if s not in rights.STATUS:
        raise Err(400, "statut invalide : %s" % s)
    if t["status"] == s:
        return "ok", "« %s » est déjà « %s »" % (t["title"], s)
    if s == "terminée":
        return "ok", E.apply_review(u, pid, t["id"], "Workflow", "accepted", "Clôturée par le workflow « %s »" % wf["name"])

    def f(pp):
        x = by_id(pp["tasks"], t["id"])
        x["status"], x["touched"] = s, time.time()
        if s != "en attente":
            x.pop("blocked", None)
    E.mp(u, pid, f)
    if s == "à faire":
        E.spawn(E.tick, u, pid)
    return "ok", "« %s » : %s" % (t["title"], s)


def _assign_task(E, u, pid, p, wf, run, st):
    t, a = _task(p, run, st), _actor(p, run, st)
    if t.get("running"):
        raise Err(409, "« %s » est en cours d’exécution par son agent" % t["title"])
    if t.get("status") == "terminée":
        raise Err(409, "« %s » est terminée" % t["title"])
    E.mp(u, pid, lambda pp: by_id(pp["tasks"], t["id"]).update(who=a["id"], touched=time.time()))
    E.spawn(E.tick, u, pid)
    return "ok", "« %s » affectée à %s" % (t["title"], a["name"])


def _add_task_note(E, u, pid, p, wf, run, st):
    t, txt = _task(p, run, st), render(st.get("text"), p, run).strip()
    if not txt:
        raise Err(400, "note vide")
    E.mp(u, pid, lambda pp: by_id(pp["tasks"], t["id"]).update(result=((t.get("result") or "") + "\n" + txt).strip()[:4000], result_note=True))
    return "ok", "note ajoutée à « %s »" % t["title"]


def _launch_task(E, u, pid, p, wf, run, st):
    t = _task(p, run, st)
    if not t.get("who") or t["status"] != "à faire" or t.get("running"):
        raise Err(409, "« %s » ne peut pas être lancée (agent assigné, statut « à faire » et tâche libre requis)" % t["title"])
    E.spawn(E.run_task, u, pid, t["id"], False, "[Workflow « %s »] %s" % (wf["name"], render(st.get("text"), p, run).strip() or "Démarre la tâche."))
    return "ok", "« %s » lancée" % t["title"]


def _bug(p, st):
    b = by_id(p.get("bugs", []), st.get("target"))
    if not b:
        raise Err(404, "bug introuvable : %s" % (st.get("target") or "—"))
    return b


def _set_bug_status(E, u, pid, p, wf, run, st):
    b, s = _bug(p, st), st.get("value")
    if s not in rights.BUG_STATUS:
        raise Err(400, "statut de bug invalide : %s" % s)
    E.mp(u, pid, lambda pp: by_id(pp["bugs"], b["id"]).update(status=s))
    return "ok", "bug « %s » : %s" % (b["t"], s)


def _assign_bug_fix(E, u, pid, p, wf, run, st):
    b, a = _bug(p, st), _actor(p, run, st)
    ids = [t["id"] for t in p["tasks"] if t.get("bug") == b["id"] and t["status"] != "terminée" and not t.get("running")]
    if not ids:
        raise Err(409, "aucune sous-tâche de correction à affecter pour « %s »" % b["t"])

    def f(pp):
        for i in ids:
            by_id(pp["tasks"], i).update(who=a["id"], touched=time.time())
    E.mp(u, pid, f)
    E.spawn(E.tick, u, pid)
    return "ok", "%d correction(s) de « %s » affectée(s) à %s" % (len(ids), b["t"], a["name"])


def _sync_agent_branch(E, u, pid, p, wf, run, st):
    return "ok", _sync_one(E, u, pid, p, _actor(p, run, st), st.get("value") or "")


def _sync_one(E, u, pid, p, a, src):
    """Intègre la branche de référence dans celle de l'agent ; retourne un compte rendu, ou lève Err (conflit, modifications non commitées…)."""
    if src not in SOURCES:
        raise Err(400, "branche source inconnue : %s" % src)
    if E.git_agent(p, a):                                   # git local : worktree de l'agent
        lg, wt = E.git_workspace(u, pid, p, a)
        if lg.status(wt):
            raise Err(409, "%s a des modifications non commitées : intégration impossible" % a["name"])
        ref = {"": E.git_source(lg, p), "release": E.git_source(lg, p), "develop": E.GIT_INTEGRATION, "main": lg.base}[src]
        r = lg.sync(wt, ref, a["name"])
        if r["state"] == "conflict":
            lg.abort(wt)
            raise Err(409, "conflit entre %s et la branche de %s dans : %s (intégration annulée)" % (ref, a["name"], ", ".join(r["files"][:8])))
        return "%s : %s" % (a["name"], "déjà à jour avec %s" % ref if r["state"] == "uptodate" else "%s intégrée" % ref)
    if E.local_agent(p, a):
        raise Err(409, "%s travaille dans un répertoire local sans git : pas de branche à synchroniser" % a["name"])
    rp = github.Repo(p)                                     # mode GitHub : fusion côté dépôt central
    ref = {"": rp.work, "release": rp.work, "develop": "develop", "main": rp.base}[src]
    rp.ensure_branch(a["branch"])
    r = rp.merge_branch(a["branch"], ref)
    if r["state"] == "conflict":
        raise Err(409, "conflit entre %s et la branche de %s" % (ref, a["name"]))
    return "%s : %s" % (a["name"], "déjà à jour avec %s" % ref if r["state"] == "uptodate" else "%s intégrée" % ref)


def _sync_mrs(E, u, pid, p, wf, run, st):
    out = E.sync_mrs(u, pid, force=True)
    return "ok", ("%d merge request(s) retirée(s) : %s" % (len(out), ", ".join("#%s %s" % x for x in out))) if out else "aucune merge request à retirer"


def _mark_mr_done(E, u, pid, p, wf, run, st):
    m = by_id(p.get("mrs", []), st.get("target")) if st.get("target") else next(
        (x for x in p.get("mrs", []) if x.get("id") and x.get("task") == (run or {}).get("task") and x.get("status") in E.MR_ACTIVE), None)
    if not m or m.get("status") not in E.MR_ACTIVE:
        raise Err(404, "aucune merge request active à marquer terminée")
    ready = E.mp(u, pid, lambda pp: E._close_mr_in(pp, m["id"], "terminée", "Marquée terminée par le workflow « %s »." % wf["name"]))
    return "ok", "MR #%s « %s » terminée%s" % (m.get("n", "?"), m.get("title", ""), (" ; débloquées : " + ", ".join(ready)) if ready else "")


def _notify_agent(E, u, pid, p, wf, run, st):
    a, txt = _actor(p, run, st), render(st.get("text"), p, run).strip()
    E.log(u, pid, "Système", a["name"], "[Workflow « %s »] %s" % (wf["name"], txt))
    return "ok", "%s informé(e)" % a["name"]


def _notify_user(E, u, pid, p, wf, run, st):
    E.log(u, pid, "Système", "Vous", "[Workflow « %s »] %s" % (wf["name"], render(st.get("text"), p, run).strip()))
    return "ok", "utilisateur informé"


def _wait(E, u, pid, p, wf, run, st):
    try:
        n = min(max(int(st.get("value")), 1), WAIT_MAX)
    except (TypeError, ValueError):
        raise Err(400, "durée invalide")
    time.sleep(n)
    return "ok", "attendu %d s" % n


def _end(E, u, pid, p, wf, run, st):
    return "end", "%s|%s" % (st.get("value") or "ok", render(st.get("text"), p, run).strip())


def _descendants(p, tid):
    out, todo = [], [tid]
    while todo:
        cur = todo.pop()
        for t in p["tasks"]:
            if t.get("parent") == cur and t not in out:
                out.append(t)
                todo.append(t["id"])
    return out


def eval_condition(p, run, st):
    """(vrai?, explication) d'une condition, en lecture seule."""
    name, _, arg = str(st.get("value") or "").partition("=")
    tid = st.get("target") or (run or {}).get("task")
    t = by_id(p.get("tasks", []), tid)
    if name in ("task_status", "subtasks_done", "mr_active") and not t:
        raise Err(404, "tâche introuvable pour la condition")
    if name == "task_status":
        return t["status"] == arg, "« %s » est « %s »" % (t["title"], t["status"])
    if name == "subtasks_done":
        ch = _descendants(p, t["id"])
        return bool(ch) and all(c["status"] == "terminée" for c in ch), "%d/%d sous-tâche(s) terminée(s)" % (sum(1 for c in ch if c["status"] == "terminée"), len(ch))
    if name == "mr_active":
        n = sum(1 for m in p.get("mrs", []) if m.get("id") and m.get("task") == t["id"] and m.get("status") in ("ouverte", "conflit"))
        return n > 0, "%d merge request(s) active(s)" % n
    if name == "bug_status":
        b = by_id(p.get("bugs", []), st.get("target"))
        if not b:
            raise Err(404, "bug introuvable pour la condition")
        return b["status"] == arg, "bug « %s » : %s" % (b["t"], b["status"])
    if name == "agent_free":
        a = prules.resolve_actor(p, {"who": st.get("who") or "principal"}, run)
        if not a:
            raise Err(404, "agent introuvable pour la condition")
        busy = [x["title"] for x in p["tasks"] if x.get("who") == a["id"] and x.get("running")]
        return not busy, "%s %s" % (a["name"], ("travaille sur « %s »" % busy[0]) if busy else "est libre")
    if name == "decision_pending":
        n = sum(1 for d in p.get("decisions", []) if d.get("status") == "attente")
        return n > 0, "%d décision(s) en attente" % n
    if name == "release_open":
        return bool(p.get("cycle")), "release en cours" if p.get("cycle") else "aucune release en cours"
    raise Err(400, "condition inconnue : %s" % st.get("value"))


def _if(E, u, pid, p, wf, run, st):
    ok, why = eval_condition(p, run, st)
    return ("ok" if ok else "ko"), ("vrai : " if ok else "faux : ") + why


def _create_subtask(E, u, pid, p, wf, run, st):
    parent, a = _task(p, run, st), _actor(p, run, st)
    if rights.depth(p, parent) >= rights.MAX_TASK_DEPTH:
        raise Err(409, "profondeur maximale des sous-tâches atteinte (%d niveaux)" % rights.MAX_TASK_DEPTH)
    title = render(st.get("text"), p, run).strip()
    t = E.mp(u, pid, lambda pp: rights.create_task(pp, title, "", parent=parent["id"], who=a["id"], modules=parent.get("modules", []), by="Workflow"))
    E.spawn(E.tick, u, pid)
    return "ok", "sous-tâche « %s » créée sous « %s » (%s)" % (t["title"], parent["title"], a["name"])


def _create_bug_fix_subtask(E, u, pid, p, wf, run, st):
    b, a = _bug(p, st), _actor(p, run, st)
    title = render(st.get("text"), p, run).strip()
    t = E.mp(u, pid, lambda pp: rights.add_fix_task(pp, by_id(pp["bugs"], b["id"]), title, "", a["id"], "Workflow"))
    E.spawn(E.tick, u, pid)
    return "ok", "correction « %s » créée pour le bug « %s » (%s)" % (t["title"], b["t"], a["name"])


def _link_modules(E, u, pid, p, wf, run, st):
    ids = rights.mod_ids(p, [x.strip() for x in str(st.get("value") or "").split(",") if x.strip()])
    coll = "tasks" if by_id(p["tasks"], st.get("target")) else "bugs"
    it = by_id(p[coll], st.get("target"))
    if not it:
        raise Err(404, "tâche ou bug introuvable : %s" % (st.get("target") or "—"))
    E.mp(u, pid, lambda pp: by_id(pp[coll], it["id"]).__setitem__("modules", ids))
    return "ok", "modules de « %s » : %s" % (it.get("title") or it.get("t"), ", ".join(E.mod_names(p, ids)) or "aucun (global)")


def _set_task_dependency(E, u, pid, p, wf, run, st):
    t = _task(p, run, st)
    op, _, did = str(st.get("value") or "").partition(":")
    d = by_id(p["tasks"], did)
    if op not in ("add", "remove") or not d or d["id"] == t["id"]:
        raise Err(400, "dépendance invalide : %s" % st.get("value"))
    if op == "add" and t["id"] in (d.get("deps") or []):
        raise Err(409, "dépendance circulaire : « %s » dépend déjà de « %s »" % (d["title"], t["title"]))

    def f(pp):
        x = by_id(pp["tasks"], t["id"])
        deps = [i for i in (x.get("deps") or []) if i != d["id"]] + ([d["id"]] if op == "add" else [])
        x["deps"] = deps
        if not deps:
            x.pop("deps", None)
    E.mp(u, pid, f)
    E.spawn(E.tick, u, pid)
    return "ok", "« %s » %s « %s »" % (t["title"], "dépend maintenant de" if op == "add" else "ne dépend plus de", d["title"])


def _close_subtasks(E, u, pid, p, wf, run, st):
    t, s = _task(p, run, st), st.get("value")
    if s not in rights.STATUS:
        raise Err(400, "statut invalide : %s" % s)
    todo = [c for c in _descendants(p, t["id"]) if c["status"] != s and not c.get("running")]
    if not todo:
        return "ok", "aucune sous-tâche à modifier"
    for c in todo:
        if s == "terminée":
            E.apply_review(u, pid, c["id"], "Workflow", "accepted", "Clôturée par le workflow « %s »" % wf["name"])
        else:
            E.mp(u, pid, lambda pp, i=c["id"]: by_id(pp["tasks"], i).update(status=s, touched=time.time()))
    return "ok", "%d sous-tâche(s) de « %s » : %s" % (len(todo), t["title"], s)


def _sync_all(E, u, pid, p, wf, run, st):
    src, done, bad = st.get("value") or "", [], []
    for a in p["agents"]:
        try:
            done.append(_sync_one(E, u, pid, p, a, src))
        except Err as e:
            if "sans git" in e.msg:
                continue                                    # agent en répertoire local sans git : rien à synchroniser
            bad.append("%s : %s" % (a["name"], e.msg))
    if bad:
        return "ko", "%d échec(s) : %s" % (len(bad), " ; ".join(bad))[:280]
    return "ok", "%d agent(s) synchronisé(s)" % len(done)


def _stop_agent(E, u, pid, p, wf, run, st):
    a = _actor(p, run, st)
    return "ok", E.stop_agent(u, pid, a["id"], False, "Workflow « %s »" % wf["name"], "étape système du workflow « %s »" % wf["name"])


def _run_workflow(E, u, pid, p, wf, run, st):
    w2 = prules.find_workflow(p, st.get("value") or "")
    if not w2 or w2.get("id") == wf.get("id"):
        raise Err(400, "workflow à lancer introuvable (ou identique) : %s" % st.get("value"))
    if not w2.get("enabled", True):
        raise Err(409, "le workflow « %s » est désactivé" % w2["name"])
    tid = st.get("target") or (run or {}).get("task") or ""
    E.workflow_start(u, pid, w2["id"], "workflow « %s »" % wf["name"], requester=(run or {}).get("requester", ""), task=tid)
    return "ok", "workflow « %s » lancé" % w2["name"]


HANDLERS = {"if": _if, "create_subtask": _create_subtask, "create_bug_fix_subtask": _create_bug_fix_subtask, "link_modules": _link_modules,
            "set_task_dependency": _set_task_dependency, "close_subtasks": _close_subtasks, "sync_all_agent_branches": _sync_all, "stop_agent": _stop_agent,
            "run_workflow": _run_workflow, "set_task_status": _set_task_status, "assign_task": _assign_task, "add_task_note": _add_task_note, "launch_task": _launch_task,
            "set_bug_status": _set_bug_status, "assign_bug_fix": _assign_bug_fix, "sync_agent_branch": _sync_agent_branch, "sync_mrs": _sync_mrs,
            "mark_mr_done": _mark_mr_done, "notify_agent": _notify_agent, "notify_user": _notify_user, "wait": _wait, "end_workflow": _end}


def simulate(p, wf, task=""):
    """Essai à blanc : décrit chaque étape sans rien modifier. Les conditions sont évaluées sur l'état actuel ; les sauts ne sont pas suivis."""
    run, L = {"task": task if by_id(p.get("tasks", []), task) else "", "requester": ""}, []
    timer = (wf.get("schedule") or {}).get("mode", "off") != "off"
    for i, st in enumerate(wf.get("steps", []), 1):
        kind = st.get("kind", "ask")
        if kind == "system":
            line = "[système] " + describe(p, st)
            if needs_confirm(st):
                line += " — confirmation de l’utilisateur demandée"
            if st.get("action") == "if":
                try:
                    ok, why = eval_condition(p, run, st)
                    line += " — actuellement %s (%s)" % ("VRAIE" if ok else "FAUSSE", why)
                except Err as e:
                    line += " — évaluation impossible (%s)" % e.msg
            probs = [m for lvl, m in lint_step(p, st, timer, wf) if lvl == "erreur"]
            if probs:
                line += " — PROBLÈME : " + " ".join(probs)
        elif kind == "start_tasks":
            line = "[système] démarrer les tâches prêtes"
        elif kind == "ask_user":
            line = "[utilisateur] question : %s" % st.get("text", "")[:100]
        else:
            line = "[agent, consomme des tokens] %s" % prules.step_text(p, st, i).split(". ", 1)[-1][:140]
        L.append("%d. %s" % (i, line))
    return L
