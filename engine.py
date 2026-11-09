"""Moteur d'agents côté serveur : dialogues, actions, tâches, onboarding, releases.

Tout s'exécute dans des threads du serveur : le travail continue navigateur fermé.
L'état est modifié via store.mutate (lecture-modification-écriture atomique)."""
import json
import os
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
CONFLICT_OPTS = ["L’auteur de la branche résout", "L’agent principal résout", "Je m’en occupe (mettre en pause)"]
MANUAL_TIMEOUT = 24 * 3600   # attente maximale d'une réponse manuelle
WAIT = {}                    # échanges manuels en attente : id -> {u, ev, text}
F = "`" * 3
RULES = [
    "Tu es un agent IA membre d’une équipe de développement pilotée depuis une application web. Tu réponds en français, de façon concise et factuelle.",
    "Tu travailles uniquement sur ta branche git, jamais directement sur la branche de base. La release finale (fusion dans la branche de base et tag) est réalisée par l’agent principal.",
    "Tu lis et écris les fichiers du dépôt avec les actions list_files, read_file, write_file, delete_file (sur ta branche). write_file remplace le fichier entier : donne toujours le contenu complet. Messages de commit clairs. Ne touche ni à .github/ ni aux fichiers .agents/ des autres.",
    "Ton fichier de contexte (.agents/<ton prénom>.md sur ta branche) fait foi : respecte-le rigoureusement et mets-le à jour avec l’action « context » dès qu’une décision ou une convention durable apparaît.",
    "Tu respectes tes droits (section « Tes droits »). Quand une création est soumise à validation, ta proposition est enregistrée dans la boîte « Décisions » de l’utilisateur : continue ce qui n’en dépend pas, sans la recréer.",
    "En cas d’ambiguïté ou de décision structurante (architecture, périmètre, choix coûteux à défaire), n’improvise pas : utilise l’action ask_user avec 2 ou 3 options et ta recommandation, puis arrête-toi sur la tâche concernée.",
    "Les instructions de l’utilisateur et les spécifications sont partagées par toute l’équipe : respecte-les. L’agent principal les tient à jour ; si tu constates un manque ou une contradiction, écris-lui.",
    "Pour connaître l’avancement d’un autre agent, utilise d’abord get_progress (sans coût) ; n’interroge l’agent par message que pour obtenir une précision.",
    "Une tâche ou un bug peut être rattaché à un ou plusieurs modules, sans obligation (ex. le cahier des charges est global). Un bug se corrige par des sous-tâches de correction (bug_fix) ; quand elles sont terminées, il passe à « corrigé » et le QA le vérifie.",
    "Chaque release a une branche de travail (release/<version>) ; la branche principale est réservée à la release finale. Tu te synchronises avec la branche de travail par pull (fait automatiquement au démarrage d’une tâche et avant une merge request) ; quand ton travail est prêt, tu soumets une merge request (merge_request) vers cette branche ; l’agent principal la valide. Une tâche qui dépend d’une autre ne démarre que lorsque celle-ci est terminée et sa merge request fusionnée. En cas de conflit de fusion, la règle du projet décide qui le résout (l’utilisateur, l’auteur de la branche ou un agent désigné) ; si le conflit t’est confié, résous-le avec resolve_merge.",
    "Prends l’initiative de la coordination : avant de coder ce qui dépend du travail d’un autre agent ou qui l’impacte (interface, contrat d’API, schéma de données, convention), écris-lui avec message pour convenir de l’approche ; préviens-le quand tu modifies ce qu’il utilise ; réponds toujours aux messages que tu reçois. « Travail des autres agents » et get_progress te disent qui fait quoi.",
    "Ne t’arrête pas à la première erreur : analyse-la, corrige et continue (relis le fichier, change d’approche). Si tu es réellement bloqué, écris à l’agent qui peut t’aider ou pose la question avec ask_user ; ne laisse jamais une tâche en suspens sans le dire.",
    "Ne déclare jamais une tâche terminée si le travail demandé n’est pas réellement fait : mets-la à « à vérifier » (task_status) avec un compte rendu précis de ce qui est fait. L’agent principal (ou le QA) vérifie puis la clôture ; s’il la refuse, corrige ce qui manque. S’il reste du travail ou un blocage, dis-le ou pose la question avec ask_user.",
    "Pour agir, termine ta réponse par un ou plusieurs blocs d’action (voir ci-dessous). Tu reçois ensuite le résultat des lectures, des messages et des erreurs. Sans bloc, ta réponse reste du simple texte."]
ACTIONS = "Actions (un objet JSON par bloc %saction ... %s) :\n" % (F, F) + "\n".join([
    '{"do":"list_files","path":"<dossier, vide pour la racine>","agent":"<prénom ou base, facultatif>"} : lister un dossier du dépôt (ta branche, ou en lecture seule celle d’un autre agent)',
    '{"do":"read_file","path":"...","agent":"<prénom ou base, facultatif>"} : lire un fichier du dépôt (ta branche, ou en lecture seule celle d’un autre agent)',
    '{"do":"write_file","path":"...","content":"<contenu complet>","message":"<message de commit>"} : créer ou remplacer un fichier sur ta branche',
    '{"do":"delete_file","path":"...","message":"..."} : supprimer un fichier sur ta branche',
    '{"do":"pull"} : récupérer dans ta branche les changements de la branche de travail de la release (fusion ; en cas de conflit, la règle du projet s’applique)',
    '{"do":"merge_request","title":"...","body":"...","task":"<id de la tâche, facultatif>"} : soumettre ta branche à la branche de travail de la release (l’agent principal la valide)',
    '{"do":"mr_review","id":"<id de la MR>","verdict":"approved|rejected","comment":"..."} : (agent principal / QA) valider (fusion) ou refuser une merge request',
    '{"do":"resolve_merge","conflict":"<id>","files":[{"path":"...","content":"<contenu final complet, ou null pour supprimer>"}],"message":"..."} : résoudre un conflit de fusion qui t’est confié (un élément par fichier en conflit)',
    '{"do":"message","to":"<prénom>","text":"..."} : écrire à un agent joignable (sa réponse te revient)',
    '{"do":"get_progress","agent":"<prénom ou all>"} : avancement d’un agent ou de toute l’équipe (sans coût)',
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
    mp(u, pid, lambda p: p.setdefault("commits", []).append({"m": msg, "by": by, "d": now(), "ts": time.time(), "sha": sha[:7]}))


def ctx_text(p, a):
    r, mn = role(p, a), is_main(p, a)
    tk = a.get("talk") or []
    talk = "tous" if (mn or not tk) else ("aucun" if "none" in tk else ", ".join(g["name"] for g in p["agents"] if g["id"] in tk))
    return "\n".join([
        "# Contexte de l’agent %s — projet %s" % (a["name"], p["name"]), "",
        "> Ce fichier fait foi : respecte-le rigoureusement et mets-le à jour (action « context »).", "",
        "## Règles de l’équipe", "\n".join("- " + x for x in RULES),
        ("\n## Règles du projet\n" + p["rules"]) if p.get("rules") else "",
        "\n## Mon rôle\n%s — %s" % (r["name"], r["desc"]),
        "\n## Ma branche\n" + a["branch"] + ("\n\nAgent principal : écrit à tous les agents, crée les tâches, tient SPEC.md à jour, réalise les merge requests et le tag à chaque release." if mn else ""),
        "\n## Droits\n%s\n- Agents joignables : %s" % (rights.rights_text(p, a, mn), talk),
        "\n## " + ACTIONS, "\n## Notes de l’agent (à tenir à jour)\n" + (a.get("notes") or "(vides)")])


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


def repo_text(p, a):
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


def _overflow(e):
    m = (e.msg or "").lower()
    return e.code in (400, 413, 502) and any(k in m for k in (
        "too long", "context length", "maximum context", "context window", "token limit", "too many tokens", "reduce the length", "exceeds the"))


def ask(u, pid, aid, text, frm, depth=0, stats=None, relog=True, max_steps=None):
    """Envoie un message à un agent et exécute ses actions jusqu'à sa réponse finale.
    `stats` (facultatif) reçoit {actions, pending} : nombre d'actions exécutées, et retour non traité si les étapes sont épuisées."""
    p = snap(u, pid)
    a = by_id(p["agents"], aid)
    if not a:
        raise Err(404, "Agent introuvable")
    stats = stats if stats is not None else {}
    stats.update(actions=0, pending="")
    if relog:
        log(u, pid, frm, a["name"], text)
    work(u, pid, aid, 1)
    try:
        msg, final, ctx, overflow = "[%s] %s" % (frm, text), "", {"n": 0}, 0
        for _ in range(max_steps or MAX_STEPS):
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
            clean, acts = parse_acts(out)
            keep = msg if len(msg) < 3000 else msg[:3000] + "\n[…tronqué]"
            keep_out = out if len(out) < 6000 else out[:6000] + "\n[…tronqué dans l’historique]"

            def add(pp):
                ag = by_id(pp["agents"], aid)
                if ag:
                    ag["history"] = (ag.get("history", []) + [{"role": "user", "content": keep}, {"role": "assistant", "content": keep_out}])[-30:]
            mp(u, pid, add)
            if clean:
                log(u, pid, a["name"], frm, clean)
                final = clean
            fb = []
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
            if not can_talk(a, t, mn):
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
                pp.setdefault("decisions", []).append(d)
                t = by_id(pp["tasks"], tid)
                if t:
                    t["status"], t["blocked"] = "en attente", d["id"]
            mp(u, pid, f)
            return "Question transmise à l’utilisateur : %s%s" % (q, " (tâche en attente de décision)" if tid else ""), None
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
            if not (mn or lv("bug_verify") == rights.FREE):
                return refuse("Validation de merge request refusée pour ton rôle.")
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
        opts, tid = d.get("options") or [], d.get("task") or ""

        def f(pp):
            dd = by_id(pp["decisions"], did)
            dd.update(status="répondue", answer=ans, done=now(), n_done=rights.next_seq(pp))
            if dd.get("structural"):
                pp.setdefault("instructions", []).append({"id": uid(), "n": rights.next_seq(pp), "d": now(), "by": "Décision", "scope": "global",
                                                          "active": True, "text": "Décision (%s) : %s → %s" % (dd["byName"], dd["question"], ans)})
            t = by_id(pp["tasks"], tid)
            if t and t.get("blocked") == did:
                t.pop("blocked", None)
                t["touched"] = time.time()
                if not dd.get("escalation"):
                    t["status"] = "en cours"
        mp(u, pid, f)
        log(u, pid, "Vous", ag["name"] if ag else "Suivi", "Réponse à la question « %s » : %s" % (title, ans))
        if d.get("review"):                                   # validation de fin de tâche par l'utilisateur
            if ans == opts[0]:
                log(u, pid, "Système", "Vous", apply_review(u, pid, tid, "Vous", "accepted", "Validée par l’utilisateur"))
            else:
                log(u, pid, "Système", "Vous", apply_review(u, pid, tid, "Vous", "rejected", ans if ans != opts[1] else "Refusée par l’utilisateur"))
        elif d.get("conflict"):                               # qui résout le conflit de fusion
            cid = d["conflict"]
            if by_id(snap(u, pid).get("conflicts", []), cid):
                if ans == opts[2]:
                    mp(u, pid, lambda pp: by_id(pp["conflicts"], cid).__setitem__("status", "pause"))
                    log(u, pid, "Système", "Vous", "Conflit mis en pause : résolvez-le sur GitHub puis utilisez « Réessayer » sur la merge request.")
                else:
                    mgr = by_id(p["agents"], p.get("mainId"))
                    c = by_id(snap(u, pid)["conflicts"], cid)
                    resolve_conflict(u, pid, cid, mgr["id"] if (ans == opts[1] and mgr) else c["agent"])
        elif d.get("mr_review"):                              # validation de merge request par l'utilisateur
            log(u, pid, "Système", "Vous", apply_mr_review(u, pid, d["mr_review"], "Vous", "approved" if ans == opts[0] else "rejected", "" if ans == opts[0] else "Refusée par l’utilisateur"))
        elif d.get("escalation"):                             # décision sur une tâche bloquée
            if len(opts) > 2 and ans == opts[1]:
                mp(u, pid, lambda pp: by_id(pp["tasks"], tid).__setitem__("status", "en attente"))
            elif len(opts) > 2 and ans == opts[2]:
                mp(u, pid, lambda pp: by_id(pp["tasks"], tid).update(status="terminée", abandoned=True, result="Abandonnée sur décision de l’utilisateur."))
            else:
                mp(u, pid, lambda pp: by_id(pp["tasks"], tid).update(relaunch_n=0, sup_n=0))
                run_task(u, pid, tid, note="[Décision de l’utilisateur] " + ("Reprends la tâche." if ans == opts[0] else ans))
        elif tid and ag:                                      # l'agent reprend lui-même sa tâche, sans nouvelle demande
            run_task(u, pid, tid, note="[Décision de l’utilisateur] Réponse à ta question « %s » : %s\nReprends la tâche en tenant compte de cette décision." % (title, ans))
        elif ag:
            ask(u, pid, ag["id"], "Réponse de l’utilisateur à ta question « %s » : %s" % (title, ans), "Vous")
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
    for a in snap(u, pid)["agents"]:
        log(u, pid, "Système", a["name"], "Synchronisation : " + provision(u, pid, a["id"]))


def say(u, pid, aid, text, attempt=0, relog=True):
    try:
        ask(u, pid, aid, text, "Vous", relog=relog)
        for t in snap(u, pid)["tasks"]:               # une tâche passée « à vérifier » pendant la conversation
            if t["status"] == "à vérifier" and not t.get("reviewing"):
                spawn(review_task, u, pid, t["id"])
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


def run_task(u, pid, tid, retry=False, note=""):
    """Exécute une tâche en PLUSIEURS tours : l'agent est relancé tant qu'il n'a ni conclu, ni posé de question, ni signalé de blocage."""
    p = snap(u, pid)
    t = by_id(p["tasks"], tid)
    a = t and by_id(p["agents"], t.get("who"))
    if not a or (retry and not t.get("retry_at")) or t.get("running"):
        return
    unmet = deps_unmet(p, t)
    if unmet:
        mp(u, pid, lambda pp: by_id(pp["tasks"], tid).__setitem__("waiting", unmet))
        return log(u, pid, "Système", a["name"], "Tâche « %s » non lancée : elle attend %s." % (t["title"], ", ".join("« %s »" % x for x in unmet)))
    if open_conflict_for(p, a["id"]):
        return log(u, pid, "Système", a["name"], "Tâche « %s » non lancée : un conflit de fusion est en cours de résolution sur la branche de %s." % (t["title"], a["name"]))

    def start(pp):
        tt = by_id(pp["tasks"], tid)
        if tt.get("running"):                              # déjà lancée par un autre passage
            return False
        tt.update(running=True, status="en cours", started=time.time(), touched=time.time())
        for k in ("retry_at", "fail", "blocked", "waiting", "resume", "result_note"):
            tt.pop(k, None)
        by_id(pp["agents"], a["id"])["state"] = "actif"
        return True
    if not mp(u, pid, start):
        return
    exhausted = False
    replies = []
    try:
        pull_note = ""
        try:                                               # pull régulier : on repart de la branche de travail à jour
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
        msg += "\nQuand ton travail est prêt, soumets une merge request vers la branche de travail de la release avec l’action merge_request, puis passe la tâche à « à vérifier »."
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
            if stats.get("pending"):                       # étapes épuisées en plein travail : on continue
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
                st, mm = submit_mr(u, pid, a["id"], "Tâche : %s" % t["title"], "", tid)
                if st in ("submitted", "conflict"):
                    log(u, pid, "Système", a["name"], mm)
            except Err as e:
                log(u, pid, "Système", a["name"], "Merge request non soumise : %s" % e.msg)
            spawn(review_task, u, pid, tid)
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
    pp.setdefault("decisions", []).append(d)
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
            for k in ("review_note", "reviews", "relaunch_n", "fail", "retries", "review_fail", "review_asked", "review_err", "auto_n", "auto_escalated", "resume"):
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
        msg = ("[Application — vérification] La tâche « %s » (id %s), réalisée par %s, est soumise à vérification.\nDescription demandée : %s\n"
               "Compte rendu de l’agent : %s\n%s\n"
               "Vérifie que le travail demandé est EFFECTIVEMENT fait : lis les fichiers concernés avec read_file en précisant \"agent\":\"%s\", compare avec la description et les spécifications. "
               "Conclus obligatoirement par task_review : accepted seulement si tout est fait, sinon rejected avec la liste précise de ce qui manque.") % (
            t["title"], tid, a["name"], t.get("desc") or "(aucune)", (t.get("result") or "(aucun)")[:1500], evidence(p, t, a), a["name"])
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


def dispatch(u, pid, p, t0):
    """Lancement automatique : les tâches prêtes (assignées, à faire, dépendances satisfaites) démarrent sans intervention, une par agent."""
    if p.get("autopilot") == "off":
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
        _tick(u, pid)
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
               "Pour chacune : interroge l’agent concerné (message) pour comprendre ; si le problème est résolu ou contournable, relance-le avec relaunch ; "
               "sinon (décision à prendre, information manquante, erreur répétée), remonte-le à l’utilisateur avec ask_user en indiquant la tâche.") % "\n".join(
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
    return any(c["agent"] == aid and c["status"] in ("ouvert", "escalade", "pause") for c in p.get("conflicts", []))


def deps_unmet(p, t):
    """Titres des dépendances non satisfaites : tâche non terminée, ou sa merge request encore en cours."""
    out = []
    for i in t.get("deps") or []:
        d = by_id(p["tasks"], i)
        if d and (d["status"] != "terminée" or any(m.get("task") == i and m.get("status") in ("ouverte", "conflit") for m in p.get("mrs", []))):
            out.append(d["title"])
    return out


def cycle_start(u, pid, version):
    """Démarre une release : crée la branche de travail release/<version> depuis la branche de base."""
    p = snap(u, pid)
    version = version.strip()
    if not version:
        return log(u, pid, "Système", "Vous", "Version vide : release non démarrée.")
    if p.get("cycle"):
        return log(u, pid, "Système", "Vous", "Une release est déjà en cours (%s) : publiez-la d’abord." % p["cycle"]["version"])
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
    if r["state"] == "uptodate":
        return "uptodate", "Ta branche est déjà à jour avec %s." % rp.work
    if r["state"] == "merged":
        record_commit(u, pid, a["name"], "pull : %s" % rp.work, r.get("sha", ""))
        return "merged", "Pull réussi : %s est fusionnée dans ta branche." % rp.work
    return "conflict", open_conflict(u, pid, aid, origin, task=task, title=title, body=body)


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
    pp.setdefault("decisions", []).append(d)
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


def submit_mr(u, pid, aid, title="", body="", task=""):
    """Soumet la branche de l'agent à la branche de travail. Retourne (état, message) : submitted | exists | conflict | skip."""
    p = snap(u, pid)
    a = by_id(p["agents"], aid)
    try:
        rp = github.Repo(p)
    except Err:
        return "skip", "Dépôt GitHub/token non configuré : pas de merge request."
    if rp.work == rp.base:
        return "skip", "Aucune release en cours : l’utilisateur doit d’abord en démarrer une (%s est réservée à la release finale)." % rp.base
    if a["branch"] in (rp.base, rp.work):
        return "skip", "Ta branche ne peut pas être la branche de base ou la branche de travail."
    if open_conflict_for(p, aid):
        return "skip", "Un conflit de fusion est déjà en cours de résolution sur ta branche."
    ex = next((m for m in p.get("mrs", []) if m.get("id") and m.get("from") == a["branch"] and m.get("status") in ("ouverte", "conflit")), None)
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
    pp.setdefault("decisions", []).append({
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
        rv = pick_reviewer(p, {"who": m["by_id"]})
        if rv is None:
            def ask_user(pp):
                mm = by_id(pp["mrs"], mid)
                if not mm.get("review_asked"):
                    mm["review_asked"] = True
                    add_mr_decision(pp, mm)
            return mp(u, pid, ask_user)
        cmp = github.Repo(p).compare(m["to"], m["from"])
        commits = "\n".join("  - %s" % c["commit"]["message"].splitlines()[0] for c in cmp.get("commits", [])[-15:]) or "  (aucun)"
        files = ", ".join(f["filename"] for f in cmp.get("files", [])[:30]) or "(aucun)"
        task = by_id(p["tasks"], m.get("task"))
        msg = ("[Application — validation de merge request] MR #%s « %s » de %s : branche %s vers %s.\nTâche liée : %s\nCommits :\n%s\nFichiers modifiés : %s\n"
               "Valide cette merge request : relis les changements (read_file avec \"agent\":\"%s\"), vérifie qu’ils sont cohérents avec les spécifications et la branche de travail. "
               "Conclus par mr_review : approved (elle sera fusionnée) ou rejected avec ce qu’il faut corriger.") % (
            m["n"], m.get("title", ""), m["by"], m["from"], m["to"], task["title"] if task else "aucune", commits, files, m["by"])
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


def mr_retry(u, pid, mid):
    mp(u, pid, lambda pp: (by_id(pp.get("mrs", []), mid) or {}).pop("merge_tries", None))
    log(u, pid, "Système", "Vous", merge_mr(u, pid, mid))


def release(u, pid, version, notes):
    """Release finale : fusionne la branche de travail dans la branche de base, crée le tag, termine le cycle."""
    p = snap(u, pid)
    mg = by_id(p["agents"], p.get("mainId"))
    who, cy = (mg["name"] if mg else "Système"), p.get("cycle")
    version = version.strip() or (cy or {}).get("version", "")
    try:
        rp = github.Repo(p)
    except Err:
        mp(u, pid, lambda pp: pp["releases"].append({"v": version, "n": notes, "d": now()}))
        return log(u, pid, "Système", who, "Release %s enregistrée localement : dépôt/token non configuré, ni fusion ni tag." % version)
    if not cy:
        return log(u, pid, "Système", "Vous", "Aucune release en cours : démarrez-en une (« Démarrer une release ») avant de la publier.")
    opn = [m for m in p.get("mrs", []) if m.get("status") in ("ouverte", "conflit")]
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
            "cycle_start": lambda: cycle_start(u, pid, str(j.get("version", ""))),
            "mr_retry": lambda: mr_retry(u, pid, j.get("mrId")),
            "decide": lambda: decide(u, pid, j.get("decisionId"), str(j.get("decision", "")), str(j.get("note", "")), j.get("fields") or {}, str(j.get("answer", ""))),
            "instr_add": lambda: instr_add(u, pid, str(j.get("scope", "global")), str(j.get("text", ""))),
            "instr_del": lambda: mp(u, pid, lambda pp: pp.__setitem__("instructions", [i for i in pp.get("instructions", []) if i["id"] != j.get("instrId")]))}
    if t not in jobs:
        raise Err(400, "Type de travail inconnu")

    def run():
        try:
            jobs[t]()
            if t in ("decide", "mr_retry", "cycle_start", "instr_add"):
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
        for pid, tid, w in retries:
            schedule(u, pid, tid, w)
