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
from ai import call_ai
from store import DATA, Err, load, mutate

MAX_STEPS, MAX_DEPTH = 6, 3
MANUAL_TIMEOUT = 24 * 3600   # attente maximale d'une réponse manuelle
WAIT = {}                    # échanges manuels en attente : id -> {u, ev, text}
F = "`" * 3
RULES = [
    "Tu es un agent IA membre d’une équipe de développement pilotée depuis une application web. Tu réponds en français, de façon concise et factuelle.",
    "Tu travailles uniquement sur ta branche git, jamais directement sur la branche de base. Les merge requests et le tag sont réalisés par l’agent principal lors des releases.",
    "Tu lis et écris les fichiers du dépôt avec les actions list_files, read_file, write_file, delete_file (sur ta branche). write_file remplace le fichier entier : donne toujours le contenu complet. Messages de commit clairs. Ne touche ni à .github/ ni aux fichiers .agents/ des autres.",
    "Ton fichier de contexte (.agents/<ton prénom>.md sur ta branche) fait foi : respecte-le rigoureusement et mets-le à jour avec l’action « context » dès qu’une décision ou une convention durable apparaît.",
    "Tu ne contactes que les agents autorisés. Seul l’agent principal crée des tâches et tient les spécifications. Tu ne déclares un bug que si ce droit t’est accordé.",
    "Pour agir, termine ta réponse par un ou plusieurs blocs d’action (voir ci-dessous). Tu reçois ensuite le résultat des lectures, des messages et des erreurs. Sans bloc, ta réponse reste du simple texte."]
ACTIONS = "Actions (un objet JSON par bloc %saction ... %s) :\n" % (F, F) + "\n".join([
    '{"do":"list_files","path":"<dossier, vide pour la racine>"} : lister un dossier du dépôt (ta branche)',
    '{"do":"read_file","path":"..."} : lire un fichier du dépôt (ta branche)',
    '{"do":"write_file","path":"...","content":"<contenu complet>","message":"<message de commit>"} : créer ou remplacer un fichier sur ta branche',
    '{"do":"delete_file","path":"...","message":"..."} : supprimer un fichier sur ta branche',
    '{"do":"message","to":"<prénom>","text":"..."} : écrire à un agent joignable (sa réponse te revient)',
    '{"do":"context","text":"..."} : réécrire tes notes personnelles (fichier de contexte)',
    '{"do":"task_status","id":"<id>","status":"à faire|en cours|terminée"} : statut d’une de tes tâches',
    '{"do":"bug","title":"...","desc":"...","sev":"mineur|majeur|critique","modules":["<module>"]} : déclarer un bug (si autorisé)',
    '{"do":"task","title":"...","desc":"...","assignee":"<prénom>","parent":"<id ou vide>","modules":["<module>"]} : créer une tâche (agent principal)',
    '{"do":"spec","text":"<spécifications complètes en markdown>"} : remplacer les spécifications (agent principal)'])


def now():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def uid():
    return secrets.token_hex(4)


def by_id(lst, i):
    return next((x for x in lst if x.get("id") == i), None)


def is_main(p, a):
    return p.get("mainId") == a["id"]


def role(p, a):
    return by_id(p.get("roles", []), a.get("role")) or {"name": "—", "desc": ""}


def mod_names(p, ids):
    return [m["name"] for i in ids or [] for m in p.get("modules", []) if m["id"] == i]


def mod_ids(p, vals):
    out = []
    for v in vals or []:
        m = next((m for m in p.get("modules", []) if m["id"] == v or m["name"].lower() == str(v).lower()), None)
        if m:
            out.append(m["id"])
    return out


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
        "\n## Droits\n- Déclarer des bugs : %s\n- Agents joignables : %s" % ("oui" if a.get("canBug") or mn else "non", talk),
        "\n## " + ACTIONS, "\n## Notes de l’agent (à tenir à jour)\n" + (a.get("notes") or "(vides)")])


def sys_prompt(p, a):
    mn, r = is_main(p, a), role(p, a)
    others = [x for x in p["agents"] if x["id"] != a["id"]]
    can = lambda x: mn or x["id"] in a.get("talk", [])
    ts = "\n".join("%s | %s | %s | %s%s" % (t["id"], t["title"], t["status"],
                   (by_id(p["agents"], t.get("who")) or {}).get("name", "—"),
                   (" | " + ",".join(mod_names(p, t.get("modules")))) if t.get("modules") else "")
                   for t in p.get("tasks", []) if mn or t.get("who") == a["id"])
    repo = p.get("repo") or {}
    parts = [
        "# Règles de l’équipe\n" + "\n".join(RULES),
        ("# Règles du projet\n" + p["rules"]) if p.get("rules") else "",
        "# Toi\nPrénom : %s\nRôle : %s — %s\nBranche git : %s (branche de base : %s)\n%s" % (
            a["name"], r["name"], r["desc"], a["branch"], repo.get("branch", "main"),
            "Tu es l’AGENT PRINCIPAL : tu peux écrire à tous les agents, créer des tâches, déclarer des bugs et tenir les spécifications à jour." if mn
            else "Droit de déclarer des bugs : " + ("oui" if a.get("canBug") else "non")),
        "# Équipe\n" + ("\n".join("- %s (%s%s, branche %s)%s" % (x["name"], role(p, x)["name"], ", agent principal" if is_main(p, x) else "", x["branch"], "" if can(x) else " [non joignable]") for x in others) or "(seul)"),
        "# Projet %s\n%s" % (p["name"], p.get("desc", "")),
        "# Modules\n" + ("\n".join("- %s : %s" % (m["name"], m.get("desc", "")) for m in p.get("modules", [])) or "(aucun)"),
        "# Tâches (id | titre | statut | agent | modules)\n" + (ts or "(aucune)"),
        ("# Spécifications actuelles\n" + (p.get("spec") or "(vides)")[:6000]) if mn else "",
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
        msg, final = "[%s] %s" % (frm, text), ""
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
            sp = sys_prompt(p, a)
            out = manual_exchange(u, pid, a, s, sp, h, msg) if manual else call_ai(s, sp, h + [{"role": "user", "content": msg}])
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
                note, back = do_act(u, pid, aid, x, depth)
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


def do_act(u, pid, aid, x, depth):
    """Exécute une action d'agent. Retourne (note pour l'historique, retour pour l'agent ou None)."""
    p = snap(u, pid)
    a = by_id(p["agents"], aid)
    mn, kind = is_main(p, a), x.get("do")
    byname = lambda n: next((g for g in p["agents"] if g["name"].lower() == str(n or "").lower()), None)
    refuse = lambda m: (m, m)
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
            def f(pp):
                t = by_id(pp["tasks"], x.get("id"))
                if not t or (not mn and t.get("who") != aid):
                    return "Changement de statut refusé."
                t["status"] = x.get("status")
                return "Tâche « %s » : %s." % (t["title"], x.get("status"))
            r = mp(u, pid, f)
            return (r, r) if r.endswith("refusé.") else (r, None)
        if kind == "bug":
            if not mn and not a.get("canBug"):
                return refuse("Déclaration de bug refusée : droit non accordé à %s." % a["name"])
            sev = x.get("sev") if x.get("sev") in ("mineur", "majeur", "critique") else "mineur"
            mp(u, pid, lambda pp: pp["bugs"].append({"id": uid(), "t": x.get("title", ""), "d": x.get("desc", ""), "sev": sev,
                                                     "status": "ouvert", "by": a["name"], "modules": mod_ids(p, x.get("modules"))}))
            return "Bug déclaré : %s" % x.get("title"), None
        if kind == "task":
            if not mn:
                return refuse("Création de tâche refusée : réservée à l’agent principal.")
            as_ = byname(x.get("assignee"))
            parent = x.get("parent") if by_id(p["tasks"], x.get("parent")) else ""
            mp(u, pid, lambda pp: pp["tasks"].append({"id": uid(), "parent": parent, "title": x.get("title", ""), "desc": x.get("desc", ""),
                                                      "who": as_["id"] if as_ else "", "status": "à faire", "modules": mod_ids(p, x.get("modules"))}))
            return "Tâche créée : %s%s" % (x.get("title"), (" → " + as_["name"]) if as_ else ""), None
        if kind == "spec":
            if not mn:
                return refuse("Mise à jour des spécifications refusée : réservée à l’agent principal.")
            mp(u, pid, lambda pp: pp.__setitem__("spec", str(x.get("text", ""))))
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
            "release": lambda: release(u, pid, str(j.get("version", "")), str(j.get("notes", "")))}
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
