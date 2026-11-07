"""Moteur d'agents côté serveur : dialogues, actions, tâches, onboarding, releases.

Tout s'exécute dans des threads du serveur : le travail continue navigateur fermé.
L'état est modifié via store.mutate (lecture-modification-écriture atomique)."""
import json
import re
import secrets
import threading
import time
from datetime import datetime

import github
import rights
from ai import call_ai
from rights import by_id, mod_ids, now, uid
from store import DATA, Err, load, mutate

MAX_STEPS, MAX_DEPTH, MAX_CREATE = 6, 3, 8   # tours par échange, profondeur des messages, créations par échange
MANUAL_TIMEOUT = 24 * 3600   # attente maximale d'une réponse manuelle
WAIT = {}                    # échanges manuels en attente : id -> {u, ev, text}
F = "`" * 3
RULES = [
    "Tu es un agent IA membre d’une équipe de développement pilotée depuis une application web. Tu réponds en français, de façon concise et factuelle.",
    "Tu travailles uniquement sur ta branche git, jamais directement sur la branche de base. Les merge requests et le tag sont réalisés par l’agent principal lors des releases.",
    "Tu lis et écris les fichiers du dépôt avec les actions list_files, read_file, write_file, delete_file (sur ta branche). write_file remplace le fichier entier : donne toujours le contenu complet. Messages de commit clairs. Ne touche ni à .github/ ni aux fichiers .agents/ des autres.",
    "Ton fichier de contexte (.agents/<ton prénom>.md sur ta branche) fait foi : respecte-le rigoureusement et mets-le à jour avec l’action « context » dès qu’une décision ou une convention durable apparaît.",
    "Tu respectes tes droits (section « Tes droits »). Quand une création est soumise à validation, ta proposition est enregistrée dans la boîte « Décisions » de l’utilisateur : continue ce qui n’en dépend pas, sans la recréer.",
    "En cas d’ambiguïté ou de décision structurante (architecture, périmètre, choix coûteux à défaire), n’improvise pas : utilise l’action ask_user avec 2 ou 3 options et ta recommandation, puis arrête-toi sur la tâche concernée.",
    "Les instructions de l’utilisateur et les spécifications sont partagées par toute l’équipe : respecte-les. L’agent principal les tient à jour ; si tu constates un manque ou une contradiction, écris-lui.",
    "Pour connaître l’avancement d’un autre agent, utilise d’abord get_progress (sans coût) ; n’interroge l’agent par message que pour obtenir une précision.",
    "Une tâche ou un bug peut être rattaché à un ou plusieurs modules, sans obligation (ex. le cahier des charges est global). Un bug se corrige par des sous-tâches de correction (bug_fix) ; quand elles sont terminées, il passe à « corrigé » et le QA le vérifie.",
    "Pour agir, termine ta réponse par un ou plusieurs blocs d’action (voir ci-dessous). Tu reçois ensuite le résultat des lectures, des messages et des erreurs. Sans bloc, ta réponse reste du simple texte."]
ACTIONS = "Actions (un objet JSON par bloc %saction ... %s) :\n" % (F, F) + "\n".join([
    '{"do":"list_files","path":"<dossier, vide pour la racine>"} : lister un dossier du dépôt (ta branche)',
    '{"do":"read_file","path":"..."} : lire un fichier du dépôt (ta branche)',
    '{"do":"write_file","path":"...","content":"<contenu complet>","message":"<message de commit>"} : créer ou remplacer un fichier sur ta branche',
    '{"do":"delete_file","path":"...","message":"..."} : supprimer un fichier sur ta branche',
    '{"do":"message","to":"<prénom>","text":"..."} : écrire à un agent joignable (sa réponse te revient)',
    '{"do":"get_progress","agent":"<prénom ou all>"} : avancement d’un agent ou de toute l’équipe (sans coût)',
    '{"do":"context","text":"..."} : réécrire tes notes personnelles (fichier de contexte)',
    '{"do":"task","title":"...","desc":"...","assignee":"<prénom>","parent":"<id de la tâche parente, vide = 1er niveau>","modules":["<module>"],"why":"<justification>"} : créer une tâche (sous-tâche libre sous tes tâches ; 1er niveau selon tes droits)',
    '{"do":"task_status","id":"<id>","status":"à faire|en cours|en attente|terminée"} : statut d’une de tes tâches',
    '{"do":"module","name":"...","desc":"...","why":"..."} : créer un module (selon tes droits)',
    '{"do":"link_modules","kind":"task|bug","id":"<id>","modules":["<module>"]} : rattacher une tâche ou un bug à des modules (liste vide = global)',
    '{"do":"bug","title":"...","desc":"...","sev":"mineur|majeur|critique","modules":["<module>"]} : déclarer un bug (selon tes droits)',
    '{"do":"bug_fix","bug":"<id>","title":"...","desc":"...","assignee":"<prénom>"} : créer une sous-tâche de correction d’un bug',
    '{"do":"bug_status","id":"<id>","status":"en correction|corrigé|vérifié|rouvert"} : faire évoluer un bug (vérifié/rouvert : droit de vérification)',
    '{"do":"ask_user","question":"...","options":["A","B"],"recommendation":"...","impact":"...","structural":true,"task":"<id de la tâche bloquée, ou vide>"} : poser une question à l’utilisateur (ambiguïté, décision structurante)',
    '{"do":"instruction","text":"...","scope":"global|role:<rôle>|agent:<prénom>|module:<module>"} : (agent principal) enregistrer une nouvelle instruction de l’utilisateur',
    '{"do":"spec","text":"<spécifications complètes en markdown>"} : (agent principal) remplacer les spécifications'])


def is_main(p, a):
    return p.get("mainId") == a["id"]


def role(p, a):
    return by_id(p.get("roles", []), a.get("role")) or {"name": "—", "desc": ""}


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
    mp(u, pid, lambda p: p.setdefault("commits", []).append({"m": msg, "by": by, "d": now(), "sha": sha[:7]}))


def ctx_text(p, a):
    r, mn = role(p, a), is_main(p, a)
    talk = "tous" if mn else (", ".join(g["name"] for g in p["agents"] if g["id"] in a.get("talk", [])) or "aucun")
    return "\n".join([
        "# Contexte de l’agent %s — projet %s" % (a["name"], p["name"]), "",
        "> Ce fichier fait foi : respecte-le rigoureusement et mets-le à jour (action « context »).", "",
        "## Règles de l’équipe", "\n".join("- " + x for x in RULES),
        ("\n## Règles du projet\n" + p["rules"]) if p.get("rules") else "",
        "\n## Mon rôle\n%s — %s" % (r["name"], r["desc"]),
        "\n## Ma branche\n" + a["branch"] + ("\n\nAgent principal : écrit à tous les agents, crée les tâches, tient SPEC.md à jour, réalise les merge requests et le tag à chaque release." if mn else ""),
        "\n## Droits\n%s\n- Agents joignables : %s" % (rights.rights_text(p, a, mn), talk),
        "\n## " + ACTIONS, "\n## Notes de l’agent (à tenir à jour)\n" + (a.get("notes") or "(vides)")])


def sys_prompt(p, a):
    mn, r = is_main(p, a), role(p, a)
    others = [x for x in p["agents"] if x["id"] != a["id"]]
    can = lambda x: mn or x["id"] in a.get("talk", [])
    who = lambda t: (by_id(p["agents"], t.get("who")) or {}).get("name", "—")
    tasks = [t for t in p.get("tasks", []) if mn or t.get("who") == a["id"] or t.get("by") == a["name"]][:80]
    ts = "\n".join("%s | %s | %s | %s | parent : %s%s" % (t["id"], t["title"], t["status"], who(t), t.get("parent") or "—",
                   (" | modules : " + ",".join(mod_names(p, t.get("modules")))) if t.get("modules") else "") for t in tasks)
    bs = "\n".join("%s | %s | %s | %s%s" % (b["id"], b["t"], b["sev"], b["status"], (" | modules : " + ",".join(mod_names(p, b.get("modules")))) if b.get("modules") else "")
                   for b in p.get("bugs", []) if b["status"] not in rights.BUG_CLOSED)[:3000]
    pend = [d for d in p.get("decisions", []) if d["status"] == "attente"]
    ins, news = rights.instruction_lines(p, a), rights.instruction_lines(p, a, only_new=True)
    if p.get("spec_n", 0) > a.get("seen_seq", 0):
        news.append("- Les spécifications ont été mises à jour.")
    repo = p.get("repo") or {}
    parts = [
        "# Règles de l’équipe\n" + "\n".join(RULES),
        ("# Règles du projet\n" + p["rules"]) if p.get("rules") else "",
        ("# Instructions de l’utilisateur (à respecter)\n" + "\n".join(ins)) if ins else "",
        ("# Nouveautés depuis ta dernière activité\n" + "\n".join(news)) if news else "",
        "# Toi\nPrénom : %s\nRôle : %s — %s\nBranche git : %s (branche de base : %s)\n%s" % (
            a["name"], r["name"], r["desc"], a["branch"], repo.get("branch", "main"),
            "Tu es l’AGENT PRINCIPAL : tu peux écrire à tous les agents et tu tiens les spécifications à jour. Quand l’utilisateur te donne un élément durable (consigne, contrainte, décision), enregistre-le avec l’action instruction et mets les spécifications à jour si besoin." if mn else ""),
        "# Tes droits\n" + rights.rights_text(p, a, mn),
        "# Équipe\n" + ("\n".join("- %s (%s%s, branche %s)%s" % (x["name"], role(p, x)["name"], ", agent principal" if is_main(p, x) else "", x["branch"], "" if can(x) else " [non joignable]") for x in others) or "(seul)"),
        "# Projet %s\n%s" % (p["name"], p.get("desc", "")),
        "# Modules\n" + ("\n".join("- %s : %s" % (m["name"], m.get("desc", "")) for m in p.get("modules", [])) or "(aucun)"),
        "# Tâches (id | titre | statut | agent | parent | modules)\n" + (ts or "(aucune)"),
        ("# Bugs ouverts (id | titre | gravité | statut | modules)\n" + bs) if bs else "",
        ("# Décisions en attente de l’utilisateur\n" + "\n".join("- %s (%s)" % (d.get("question") or (d["payload"].get("title") or d["payload"].get("name")), d["byName"]) for d in pend)) if pend else "",
        ("# Spécifications actuelles\n" + p["spec"][:6000]) if p.get("spec") else "",
        "# Notes personnelles (ton fichier de contexte)\n" + (a.get("notes") or "(vides)"), ACTIONS]
    return "\n\n".join(x for x in parts if x)


def parse_acts(t):
    acts = []
    for m in re.finditer(r"```action\s*([\s\S]*?)```", t):
        try:
            acts.append(json.loads(m.group(1)))
        except ValueError:
            pass
    return re.sub(r"```action\s*([\s\S]*?)```", "", t).strip(), acts


def safe_path(path, write=False):
    path = str(path or "").strip().strip("/")
    if ".." in path.split("/") or not re.fullmatch(r"[\w .@+\-/]*", path):
        raise Err(400, "Chemin invalide : « %s »" % path)
    if write and (path == "" or path.startswith((".git/", ".github/", ".agents/"))):
        raise Err(403, "Écriture interdite sur « %s »" % path)
    return path


def ask(u, pid, aid, text, frm, depth=0):
    """Envoie un message à un agent et exécute ses actions jusqu'à sa réponse finale."""
    p = snap(u, pid)
    a = by_id(p["agents"], aid)
    if not a:
        raise Err(404, "Agent introuvable")
    log(u, pid, frm, a["name"], text)
    work(u, pid, aid, 1)
    try:
        msg, final, ctx = "[%s] %s" % (frm, text), "", {"n": 0}
        for _ in range(MAX_STEPS):
            p = snap(u, pid)
            a = by_id(p["agents"], aid)
            if not a:
                break
            s = by_id(p["_svc"], a.get("service"))
            manual = a.get("mode") == "manual"
            if not s and not manual:
                raise Err(400, "Service IA révoqué pour %s" % a["name"])
            h = list(a.get("history", []))[-30:]
            while h and h[0]["role"] != "user":
                h.pop(0)
            if not manual:
                check_budget(p)
            sp = sys_prompt(p, a)
            mp(u, pid, lambda pp: by_id(pp["agents"], aid).__setitem__("seen_seq", pp.get("seq", 0)))
            out = manual_exchange(u, pid, a, s, sp, h, msg) if manual else call_ai(s, sp, h + [{"role": "user", "content": msg}])
            if getattr(out, "usage", None) and s:
                record_usage(u, pid, aid, s, out.usage)
            clean, acts = parse_acts(out)
            keep = msg if len(msg) < 3000 else msg[:3000] + "\n[…tronqué]"

            def add(pp):
                ag = by_id(pp["agents"], aid)
                if ag:
                    ag["history"] = (ag.get("history", []) + [{"role": "user", "content": keep}, {"role": "assistant", "content": out}])[-30:]
            mp(u, pid, add)
            if clean:
                log(u, pid, a["name"], frm, clean)
                final = clean
            fb = []
            for x in acts:
                note, back = do_act(u, pid, aid, x, depth, ctx)
                if note:
                    log(u, pid, "Système", a["name"], note)
                if back:
                    fb.append(back)
            if not fb:
                break
            msg = "[Application] Résultats de tes actions :\n\n" + "\n\n".join(fb)
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
    cost = round(tin * num(svc.get("pin")) / 1e6 + tout * num(svc.get("pout")) / 1e6, 6)

    def bump(d):
        d["calls"] = d.get("calls", 0) + 1
        d["in"] = d.get("in", 0) + tin
        d["out"] = d.get("out", 0) + tout
        d["cost"] = round(d.get("cost", 0) + cost, 6)

    def f(pp):
        a = by_id(pp["agents"], aid)
        bump(pp.setdefault("usage", {}))
        if a:
            bump(a.setdefault("usage", {}))
        led = pp.setdefault("ledger", [])
        led.append({"d": now(), "agent": a["name"] if a else "?", "model": svc.get("model", ""), "in": tin, "out": tout, "cost": cost})
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


def propose(u, pid, a, kind, data):
    """Enregistre une proposition dans la boîte « Décisions » (None si une proposition identique attend déjà)."""
    def f(pp):
        key = rights.norm(data.get("title") or data.get("name"))
        if any(d["status"] == "attente" and d["kind"] == kind and d["by"] == a["id"]
               and rights.norm(d["payload"].get("title") or d["payload"].get("name")) == key for d in pp.get("decisions", [])):
            return None
        d = {"id": uid(), "n": rights.next_seq(pp), "kind": kind, "by": a["id"], "byName": a["name"], "d": now(),
             "status": "attente", "payload": data, "why": data.get("why", "")}
        pp.setdefault("decisions", []).append(d)
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
    return mp(u, pid, lambda pp: rights.create_from(pp, kind, data, a["name"])), None


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
    ctx = ctx if ctx is not None else {"n": 0}
    p = snap(u, pid)
    a = by_id(p["agents"], aid)
    mn, kind = is_main(p, a), x.get("do")
    byname = lambda n: next((g for g in p["agents"] if g["name"].lower() == str(n or "").lower()), None)
    refuse = lambda m: (m, m)
    lv = lambda cap: rights.level(p, a, cap, mn)
    try:
        if kind == "message":
            t = byname(x.get("to"))
            if not t or t["id"] == a["id"]:
                return refuse("Message refusé : agent « %s » inconnu." % x.get("to"))
            if not mn and t["id"] not in a.get("talk", []):
                return refuse("Message refusé : %s ne peut pas écrire à %s." % (a["name"], t["name"]))
            if depth >= MAX_DEPTH:
                return refuse("Message non délivré (profondeur maximale d’échanges atteinte).")
            r = ask(u, pid, t["id"], str(x.get("text", "")), a["name"], depth + 1)
            return "", "[Réponse de %s] %s" % (t["name"], r or "(pas de réponse écrite)")
        if kind in ("list_files", "read_file", "write_file", "delete_file"):
            return repo_act(u, pid, p, a, x, kind)
        if kind == "get_progress":
            return "", progress_text(p, x.get("agent"))
        if kind == "context":
            def f(pp):
                by_id(pp["agents"], aid)["notes"] = str(x.get("text", ""))
            mp(u, pid, f)
            p = snap(u, pid)
            a = by_id(p["agents"], aid)
            msg = "chore(%s) : mise à jour du contexte" % a["name"]
            try:
                rp = github.Repo(p)
                sha = rp.put_file(a["branch"], ".agents/%s.md" % a["name"], ctx_text(p, a), msg)
                record_commit(u, pid, a["name"], msg, sha)
                return "Contexte mis à jour et poussé sur %s." % a["branch"], None
            except Err:
                return "Contexte mis à jour (dépôt non configuré : fichier non poussé).", None
        if kind == "task_status":
            if x.get("status") not in rights.STATUS:
                return refuse("Statut invalide : %s." % x.get("status"))

            def f(pp):
                t = by_id(pp["tasks"], x.get("id"))
                if not t or (not mn and t.get("who") != aid):
                    return None
                t["status"] = x["status"]
                t.pop("blocked", None) if x["status"] != "en attente" else None
                b = rights.on_task_done(pp, t) if x["status"] == "terminée" else None
                return "Tâche « %s » : %s.%s" % (t["title"], x["status"], (" Bug « %s » corrigé : à vérifier par le QA." % b["t"]) if b else "")
            r = mp(u, pid, f)
            return refuse("Changement de statut refusé.") if r is None else (r, None)
        if kind == "task":
            parent = x.get("parent") if by_id(p["tasks"], x.get("parent")) else ""
            as_ = byname(x.get("assignee"))
            data = {"title": x.get("title", ""), "desc": x.get("desc", ""), "parent": parent, "who": as_["id"] if as_ else "",
                    "modules": mod_ids(p, x.get("modules")), "why": str(x.get("why", ""))}
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
                pp.setdefault("decisions", []).append(d)
                t = by_id(pp["tasks"], tid)
                if t:
                    t["status"], t["blocked"] = "en attente", d["id"]
            mp(u, pid, f)
            return "Question transmise à l’utilisateur : %s%s" % (q, " (tâche en attente de décision)" if tid else ""), None
        if kind == "instruction":
            if not mn:
                return refuse("Enregistrement d’instruction refusé : réservé à l’agent principal (écris-lui un message).")
            text, sc = str(x.get("text", "")).strip(), str(x.get("scope") or "global")
            if not text:
                return refuse("Instruction vide.")
            if sc != "global" and sc.split(":")[0] not in ("role", "agent", "module"):
                sc = "global"
            mp(u, pid, lambda pp: pp.setdefault("instructions", []).append(
                {"id": uid(), "n": rights.next_seq(pp), "d": now(), "by": a["name"], "scope": sc, "text": text, "active": True}))
            return "Instruction enregistrée (%s) : %s" % (sc, text), None
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
    return refuse("Action inconnue : %s" % kind)


def decide(u, pid, did, decision, note="", fields=None, answer=""):
    """Décision de l'utilisateur : accepter/refuser une proposition ou répondre à une question d'agent."""
    p = snap(u, pid)
    d = by_id(p.get("decisions", []), did)
    if not d or d["status"] != "attente":
        raise Err(404, "Décision introuvable ou déjà traitée")
    ag = by_id(p["agents"], d["by"])
    title = d.get("question") or d["payload"].get("title") or d["payload"].get("name")
    if d["kind"] == "question":
        ans = str(answer or "").strip()
        if not ans:
            raise Err(400, "Réponse vide")

        def f(pp):
            dd = by_id(pp["decisions"], did)
            dd.update(status="répondue", answer=ans, done=now(), n_done=rights.next_seq(pp))
            if dd.get("structural"):
                pp.setdefault("instructions", []).append({"id": uid(), "n": rights.next_seq(pp), "d": now(), "by": "Décision", "scope": "global",
                                                          "active": True, "text": "Décision (%s) : %s → %s" % (dd["byName"], dd["question"], ans)})
            t = by_id(pp["tasks"], dd.get("task"))
            if t and t.get("blocked") == did:
                t["status"] = "en cours"
                t.pop("blocked", None)
        mp(u, pid, f)
        log(u, pid, "Vous", ag["name"] if ag else "?", "Réponse à la question « %s » : %s" % (title, ans))
        if ag:
            ask(u, pid, ag["id"], "Réponse de l’utilisateur à ta question « %s » : %s%s" % (title, ans, ("\nTu peux reprendre la tâche %s." % d["task"]) if d.get("task") else ""), "Vous")
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


def instr_add(u, pid, scope, text):
    text = text.strip()
    if not text:
        raise Err(400, "Instruction vide")
    sc = scope if scope == "global" or scope.split(":")[0] in ("role", "agent", "module") else "global"
    mp(u, pid, lambda pp: pp.setdefault("instructions", []).append(
        {"id": uid(), "n": rights.next_seq(pp), "d": now(), "by": "Vous", "scope": sc, "text": text, "active": True}))
    log(u, pid, "Système", "Vous", "Instruction enregistrée (%s) : %s" % (sc, text))


def repo_act(u, pid, p, a, x, kind):
    rp, b = github.Repo(p), a["branch"]
    path = safe_path(x.get("path"), write=kind in ("write_file", "delete_file"))
    if kind in ("write_file", "delete_file") and b == rp.base:
        m = "Écriture refusée : ta branche est la branche de base (%s)." % b
        return m, m
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
        cut = len(t) > 12000
        return "Lecture de %s (%d caractères)" % (path, len(t)), "Fichier %s :\n%s\n%s\n%s%s" % (path, F, t[:12000], F, "\n[…tronqué à 12000 caractères]" if cut else "")
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


def provision(u, pid, aid):
    p = snap(u, pid)
    a = by_id(p["agents"], aid)
    try:
        rp = github.Repo(p)
    except Err:
        return "Dépôt GitHub/token non configuré au niveau du projet : branche et fichier de contexte non créés."
    if a["branch"] == rp.base:
        return "La branche de l’agent est la branche de base : refusé, choisissez une branche dédiée."
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
    for a in snap(u, pid)["agents"]:
        log(u, pid, "Système", a["name"], "Synchronisation : " + provision(u, pid, a["id"]))


def say(u, pid, aid, text):
    try:
        ask(u, pid, aid, text, "Vous")
    except Err as e:
        a = by_id(snap(u, pid)["agents"], aid) or {"name": "?"}
        log(u, pid, "Système", a["name"], ("Quota du service atteint : réessayez dans %s s." % (e.retry or 60)) if e.code == 429 else "Erreur : " + e.msg)


def schedule(u, pid, tid, wait):
    t = threading.Timer(wait, lambda: run_task(u, pid, tid, retry=True))
    t.daemon = True
    t.start()


def run_task(u, pid, tid, retry=False):
    p = snap(u, pid)
    t = by_id(p["tasks"], tid)
    a = t and by_id(p["agents"], t.get("who"))
    if not a or (retry and not t.get("retry_at")) or t.get("running"):
        return

    def start(pp):
        tt = by_id(pp["tasks"], tid)
        tt.update(running=True, status="en cours")
        tt.pop("retry_at", None)
        by_id(pp["agents"], a["id"])["state"] = "actif"
    mp(u, pid, start)
    msg = "Exécute cette tâche (id %s) : %s%s%s\nRéponds avec ton livrable, puis mets à jour le statut avec l’action task_status." % (
        tid, t["title"], ("\n" + t["desc"]) if t.get("desc") else "",
        ("\nModules : " + ", ".join(mod_names(p, t.get("modules")))) if t.get("modules") else "")
    try:
        r = ask(u, pid, a["id"], msg, "Application")

        def ok(pp):
            tt = by_id(pp["tasks"], tid)
            if tt:
                tt["result"] = r
                tt["running"] = False
                if tt["status"] == "en cours":
                    tt["status"] = "terminée"
                if tt["status"] == "terminée":
                    rights.on_task_done(pp, tt)
        mp(u, pid, ok)
    except Err as e:
        wait = max(5, min(int(e.retry or 60), 3600)) if e.code == 429 else 0

        def ko(pp):
            tt = by_id(pp["tasks"], tid)
            if not tt:
                return
            tt["running"], tt["status"] = False, "à faire"
            if wait:
                tt["retry_at"] = time.time() + wait
                tt["result"] = "Suspendu (quota atteint) — reprise automatique dans %d s." % wait
                by_id(pp["agents"], a["id"])["state"] = "suspendu"
            else:
                tt["result"] = "Erreur : " + e.msg
        mp(u, pid, ko)
        if wait:
            schedule(u, pid, tid, wait)


def release(u, pid, version, notes):
    p = snap(u, pid)
    mg = by_id(p["agents"], p.get("mainId"))
    who, base = (mg["name"] if mg else "Système"), (p.get("repo") or {}).get("branch", "main")
    try:
        rp = github.Repo(p)
    except Err:
        mp(u, pid, lambda pp: pp["releases"].append({"v": version, "n": notes, "d": now()}))
        return log(u, pid, "Système", who, "Release %s enregistrée localement : dépôt/token non configuré, ni fusion ni tag." % version)
    mp(u, pid, lambda pp: pp.__setitem__("releasing", True))
    out = []
    try:
        for b in dict.fromkeys(a["branch"] for a in p["agents"] if a.get("branch") and a["branch"] != base):
            try:
                r = rp.pr_merge(b, "Release %s : fusion de %s" % (version, b), notes)
                if r.get("skipped"):
                    out.append("%s : rien à fusionner" % b)
                else:
                    mp(u, pid, lambda pp: pp["mrs"].append({"from": b, "to": base, "t": "#%d %s" % (r["number"], b), "d": now()}))
                    out.append("%s : merge request #%d fusionnée" % (b, r["number"]))
            except Err as e:
                out.append("%s : ÉCHEC — %s" % (b, e.msg))
        try:
            url = rp.release(version, "Release " + version, notes)
            mp(u, pid, lambda pp: pp["releases"].append({"v": version, "n": notes, "d": now(), "url": url}))
            out.append("Tag %s créé sur %s" % (version, base))
        except Err as e:
            out.append("Tag : ÉCHEC — %s" % e.msg)
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
            "decide": lambda: decide(u, pid, j.get("decisionId"), str(j.get("decision", "")), str(j.get("note", "")), j.get("fields") or {}, str(j.get("answer", ""))),
            "instr_add": lambda: instr_add(u, pid, str(j.get("scope", "global")), str(j.get("text", ""))),
            "instr_del": lambda: mp(u, pid, lambda pp: pp.__setitem__("instructions", [i for i in pp.get("instructions", []) if i["id"] != j.get("instrId")]))}
    if t not in jobs:
        raise Err(400, "Type de travail inconnu")

    def run():
        try:
            jobs[t]()
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
                for a in p.get("agents", []):
                    a["work"] = 0
                for t in p.get("tasks", []):
                    if t.get("retry_at"):
                        retries.append((p["id"], t["id"], max(0, t["retry_at"] - time.time())))
                    elif t.get("running"):
                        t["running"], t["status"], t["result"] = False, "à faire", "Interrompu par un redémarrage du serveur."
        mutate(u, fix)
        for pid, tid, w in retries:
            schedule(u, pid, tid, w)
