"""Règles du projet : règles globales, règles par agent, workflows. Fonctions pures (sans accès au disque ni au réseau).

Un workflow est un enchaînement nommé d'étapes ; il se déclenche à la demande de l'utilisateur, à la demande
d'un agent (action run_workflow) ou par une minuterie (tous les jours à une heure donnée, ou à intervalle régulier)."""
import re
from datetime import datetime

STEP_KINDS = ("ask", "start_tasks", "ask_user")
ON_KO = ("stop", "continue", "ask_user", "goto")
WF_LATE = 6 * 3600          # une minuterie quotidienne manquée de plus de 6 h n'est pas rattrapée
WF_MAX_STEPS = 20           # garde-fou : nombre maximal d'étapes exécutées par déroulement (boucles)
WF_MAX_PER_HOUR = 10        # garde-fou : déroulements d'un même workflow par heure


def by_id(lst, i):
    return next((x for x in lst or [] if x.get("id") == i), None)


def norm(s):
    return " ".join(str(s or "").lower().split())


def find_workflow(p, key):
    """Un workflow par identifiant ou par nom (insensible à la casse)."""
    return by_id(p.get("workflows", []), key) or next((w for w in p.get("workflows", []) if norm(w.get("name")) == norm(key)), None)


def resolve_actor(p, st, run=None):
    """Agent chargé d'une étape : principal, assignee (agent de la tâche cible), requester, agent:<id>, role:<id>."""
    who, agents = st.get("who") or "principal", p.get("agents", [])
    main = by_id(agents, p.get("mainId"))
    if who == "principal":
        return main
    if who == "assignee":
        t = by_id(p.get("tasks", []), (run or {}).get("task"))
        return by_id(agents, (t or {}).get("who"))
    if who == "requester":
        return by_id(agents, (run or {}).get("requester")) or main
    if who.startswith("agent:"):
        return by_id(agents, who[6:])
    if who.startswith("role:"):
        cands = [a for a in agents if a.get("role") == who[5:]]
        return next((a for a in cands if a.get("mode") != "manual" and a["id"] != (run or {}).get("requester")), cands[0] if cands else None)
    return None


def trigger_text(w):
    sc, parts = w.get("schedule") or {}, ["sur demande de l’utilisateur"]
    if w.get("allow_agent", True):
        parts.append("sur demande d’un agent (run_workflow)")
    if sc.get("mode") == "daily":
        parts.append("tous les jours à %s%s" % (sc.get("time", "?"), (" (jours : %s)" % ",".join("lun mar mer jeu ven sam dim".split()[d] for d in sorted(sc.get("days") or []) if 0 <= d <= 6)) if sc.get("days") else ""))
    elif sc.get("mode") == "interval":
        parts.append("toutes les %s minutes" % sc.get("every", "?"))
    return ", ".join(parts)


def step_text(p, st, i):
    a = resolve_actor(p, st) if (st.get("who") or "principal") not in ("assignee", "requester") else None
    who = {"principal": "l’agent principal", "assignee": "l’agent assigné à la tâche cible", "requester": "l’agent qui a déclenché le workflow"}.get(st.get("who") or "principal") \
        or ((a or {}).get("name", "?") if (st.get("who") or "").startswith("agent:") else "un agent du rôle %s" % ((by_id(p.get("roles", []), (st.get("who") or "")[5:]) or {}).get("name", "?")))
    kind = st.get("kind", "ask")
    body = {"start_tasks": "démarrer les tâches prêtes (assignées, à faire, dépendances satisfaites)", "ask_user": "demander à l’utilisateur : %s" % st.get("text", "")}.get(kind, "%s : %s" % (who, st.get("text", "")))
    ko = {"stop": "arrêter", "continue": "continuer", "ask_user": "demander à l’utilisateur", "goto": "aller à l’étape %s" % st.get("goto", "?")}.get(st.get("on_ko") or "stop", "arrêter")
    return "%d. %s (si échec : %s)" % (i, body, ko)


def workflows_text(p, a, h="#"):
    """Description des workflows pour le prompt d'un agent."""
    wfs = [w for w in p.get("workflows", []) if w.get("enabled", True)][:10]
    if not wfs:
        return ""
    L = []
    for w in wfs:
        mine = [str(i) for i, st in enumerate(w.get("steps", []), 1) if (resolve_actor(p, st) or {}).get("id") == a["id"] or st.get("who") in ("assignee", "requester")]
        L.append("- « %s »%s — %s\n  Déclenchement : %s.%s\n  Étapes :\n%s" % (
            w.get("name", "?"), (" : " + w["desc"]) if w.get("desc") else "", "ouvert aux agents" if w.get("allow_agent", True) else "réservé à l’utilisateur et à la minuterie",
            trigger_text(w), ("\n  Tu interviens (ou peux intervenir) aux étapes : " + ", ".join(mine) + ".") if mine else "",
            "\n".join("    " + step_text(p, st, i)[:260] for i, st in enumerate(w.get("steps", []), 1))))
    return "%s Workflows du projet (enchaînements d’actions à respecter ; déclenche-en un avec run_workflow, conclus l’étape qui t’est confiée avec wf_step)\n%s" % (h, "\n".join(L))


def agent_rules_text(p, a, h="#"):
    return ("%s Règles spécifiques à toi (%s)\n%s" % (h, a["name"], a["rules"].strip())) if (a.get("rules") or "").strip() else ""


def snapshot(p):
    """État comparable des règles (pour détecter ce qui a changé entre deux communications)."""
    keep = ("name", "desc", "enabled", "allow_agent", "schedule", "steps")
    return {"global": (p.get("rules") or "").strip(),
            "agents": {a["id"]: (a.get("rules") or "").strip() for a in p.get("agents", [])},
            "wf": {w["id"]: {k: w.get(k) for k in keep} for w in p.get("workflows", [])}}


def diff(old, new):
    """Changements entre deux instantanés : [{scope: global|agent:<id>|workflow, text}]."""
    old = old or {"global": "", "agents": {}, "wf": {}}
    out = []
    if old.get("global", "") != new["global"]:
        out.append({"scope": "global", "text": "Les règles globales ont été %s." % ("supprimées" if not new["global"] else "modifiées")})
    for aid, txt in new["agents"].items():
        if old.get("agents", {}).get(aid, "") != txt:
            out.append({"scope": "agent:" + aid, "text": "Tes règles spécifiques ont été %s." % ("supprimées" if not txt else "modifiées")})
    for wid, w in new["wf"].items():
        o = old.get("wf", {}).get(wid)
        if o is None:
            out.append({"scope": "workflow", "text": "Workflow « %s » créé." % w["name"]})
        elif o != w:
            out.append({"scope": "workflow", "text": "Workflow « %s » modifié." % w["name"]})
    for wid, o in old.get("wf", {}).items():
        if wid not in new["wf"]:
            out.append({"scope": "workflow", "text": "Workflow « %s » supprimé." % o["name"]})
    return out


def concerned(scope, a):
    """Un changement concerne-t-il cet agent ? Règles globales et workflows : tous ; règles d'un agent : lui seul."""
    return scope in ("global", "workflow") or scope == "agent:" + a["id"]


def news_lines(p, a):
    seen = a.get("seen_seq", 0)
    return ["- Règles : %s" % e["text"] for e in p.get("rules_log", []) if e.get("n", 0) > seen and concerned(e["scope"], a)][-10:]


def _valid_time(s):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(s or ""))
    return bool(m) and int(m.group(1)) < 24 and int(m.group(2)) < 60


def lint(p):
    """Contrôle déterministe de cohérence : [{level: erreur|avertissement|info, where, text}]."""
    out = []
    add = lambda lvl, where, text: out.append({"level": lvl, "where": where, "text": text})
    agents, wfs = p.get("agents", []), p.get("workflows", [])
    main = by_id(agents, p.get("mainId"))
    seen = set()
    for w in wfs:
        where = "Workflow « %s »" % (w.get("name") or "(sans nom)")
        if not norm(w.get("name")):
            add("erreur", where, "Le workflow n’a pas de nom : les agents ne pourront pas le déclencher.")
        elif norm(w["name"]) in seen:
            add("erreur", where, "Deux workflows portent le même nom : les agents ne pourront pas les distinguer.")
        seen.add(norm(w.get("name")))
        steps, sc = w.get("steps", []), w.get("schedule") or {}
        mode = sc.get("mode", "off")
        timer = mode != "off"
        if not steps:
            add("erreur", where, "Aucune étape définie : le workflow ne fait rien.")
        if mode == "daily" and not _valid_time(sc.get("time")):
            add("erreur", where, "Heure de déclenchement invalide (« %s ») : attendu HH:MM." % sc.get("time", ""))
        if mode == "interval":
            try:
                every = int(sc.get("every") or 0)
            except (TypeError, ValueError):
                every = 0
            if every < 1:
                add("erreur", where, "Intervalle de déclenchement invalide.")
            elif every < 5:
                add("avertissement", where, "Intervalle très court (%d min) : coût élevé et risque de chevauchement." % every)
        if timer and not w.get("enabled", True):
            add("avertissement", where, "Une minuterie est définie mais le workflow est désactivé : elle ne se déclenchera pas.")
        for i, st in enumerate(steps, 1):
            wh = "%s, étape %d" % (where, i)
            kind = st.get("kind", "ask")
            who = st.get("who") or "principal"
            if kind not in STEP_KINDS:
                add("erreur", wh, "Type d’étape inconnu : %s." % kind)
                continue
            if kind in ("ask", "ask_user") and not norm(st.get("text")):
                add("erreur", wh, "Instruction vide.")
            if kind == "ask":
                actor = resolve_actor(p, st)
                if who == "principal" and not main:
                    add("erreur", wh, "Cette étape revient à l’agent principal, mais aucun agent principal n’est défini.")
                elif who.startswith("agent:") and not actor:
                    add("erreur", wh, "L’agent désigné n’existe plus.")
                elif who.startswith("role:") and not actor:
                    add("erreur", wh, "Aucun agent n’a le rôle désigné.")
                if who == "assignee" and timer:
                    add("erreur", wh, "Cette étape s’adresse à l’agent assigné à une tâche cible, mais le workflow se déclenche aussi par minuterie, qui ne fournit aucune tâche : l’étape échouera.")
                if who == "requester" and timer:
                    add("avertissement", wh, "« Agent demandeur » n’existe pas pour un déclenchement par minuterie : l’agent principal sera utilisé.")
                if actor and actor.get("mode") == "manual" and (timer or w.get("allow_agent", True)):
                    add("avertissement", wh, "%s est en chat manuel : cette étape attendra votre intervention (jusqu’à 24 h)." % actor.get("name", "L’agent"))
            if kind == "start_tasks" and p.get("autopilot") != "off":
                add("info", wh, "Le lancement automatique des tâches est déjà actif : cette étape ne sert qu’à forcer un démarrage immédiat.")
            ko = st.get("on_ko") or "stop"
            if ko not in ON_KO:
                add("erreur", wh, "Comportement en cas d’échec inconnu : %s." % ko)
            elif ko == "goto":
                try:
                    g = int(st.get("goto"))
                except (TypeError, ValueError):
                    g = 0
                if not 1 <= g <= len(steps):
                    add("erreur", wh, "Saut vers une étape inexistante (%s)." % st.get("goto", ""))
                elif g <= i:
                    add("avertissement", wh, "Retour vers l’étape %d : boucle possible (limitée à %d étapes exécutées)." % (g, WF_MAX_STEPS))
            elif ko == "ask_user" and timer:
                add("info", wh, "En cas d’échec, le workflow attendra votre réponse (déclenchement par minuterie).")
    pol = p.get("conflict_policy") or "ask"
    if pol.startswith("agent:"):
        ag = by_id(agents, pol[6:])
        if not ag:
            add("erreur", "Conflits de fusion", "La règle désigne un agent qui n’existe plus.")
        elif ag.get("mode") == "manual":
            add("avertissement", "Conflits de fusion", "L’agent désigné (%s) est en chat manuel : il ne sera pas sollicité automatiquement, la question vous sera posée." % ag["name"])
    if agents and not main:
        add("avertissement", "Équipe", "Aucun agent principal : la vérification des tâches, la validation des merge requests et le suivi ne fonctionneront pas.")
    if agents and not (p.get("rules") or "").strip():
        add("info", "Règles globales", "Aucune règle globale n’est définie (seules les règles intégrées s’appliquent).")
    return out


def due(w, st, now):
    """Une minuterie est-elle échue ? Retourne (échue, nouvel état à mémoriser ou None). `now` : datetime local."""
    sc, mode = w.get("schedule") or {}, (w.get("schedule") or {}).get("mode", "off")
    if not w.get("enabled", True) or mode == "off":
        return False, None
    ts = now.timestamp()
    if mode == "interval":
        try:
            every = max(1, int(sc.get("every") or 60)) * 60
        except (TypeError, ValueError):
            return False, None
        last = st.get("last_ts")
        if last is None:
            return False, {"last_ts": ts}                      # première observation : le compte à rebours démarre sans lancer
        return (True, {"last_ts": ts}) if ts - last >= every else (False, None)
    if mode == "daily" and _valid_time(sc.get("time")):
        days = sc.get("days") or []
        if days and now.weekday() not in days:
            return False, None
        hh, mm = (int(x) for x in sc["time"].split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        today = now.strftime("%Y-%m-%d")
        if now < target or st.get("last_day") == today:
            return False, None
        if (now - target).total_seconds() > WF_LATE:
            return False, {"last_day": today}                  # trop tard (serveur arrêté) : on saute aujourd'hui
        return True, {"last_day": today, "last_ts": ts}
    return False, None


def dump(p, builtin_rules):
    """Texte complet des règles, pour l'analyse de cohérence par un agent."""
    L = ["## Règles intégrées (non modifiables, appliquées à tous les agents)", "\n".join("- " + r for r in builtin_rules), "",
         "## Règles globales du projet (écrites par l'utilisateur)", (p.get("rules") or "(aucune)").strip(), "", "## Règles spécifiques par agent"]
    for a in p.get("agents", []):
        role = (by_id(p.get("roles", []), a.get("role")) or {}).get("name", "?")
        L.append("- %s (rôle : %s%s, mode : %s) : %s" % (a["name"], role, ", agent principal" if a["id"] == p.get("mainId") else "", a.get("mode", "api"), (a.get("rules") or "(aucune)").strip()))
    L += ["", "## Workflows"]
    for w in p.get("workflows", []):
        L.append("- « %s » (%s) : %s\n  Déclenchement : %s\n%s" % (w.get("name", "?"), "actif" if w.get("enabled", True) else "désactivé", w.get("desc", ""), trigger_text(w),
                                                                  "\n".join("  " + step_text(p, st, i) for i, st in enumerate(w.get("steps", []), 1))))
    L += ["", "## Réglages du projet",
          "- Autonomie des agents : %s ; règle des conflits de fusion : %s ; lancement automatique : %s ; suivi automatique : %s" % (
              p.get("autonomy", "balanced"), p.get("conflict_policy", "ask"), p.get("autopilot", "on"), p.get("supervision", "on")),
          "- Droits des rôles : " + "; ".join("%s : %s" % (r.get("name"), ", ".join("%s=%s" % kv for kv in (r.get("caps") or {}).items())) for r in p.get("roles", []))]
    return "\n".join(L)
