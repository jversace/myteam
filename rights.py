"""Droits des agents (capacités par rôle, niveau d'autonomie du projet) et création des éléments de projet.

Les fonctions « create_* » travaillent sur le dictionnaire du projet à l'intérieur d'un store.mutate."""
import secrets
from datetime import datetime

from store import Err

CAPS = ["subtask", "task_top", "module", "bug", "bug_update", "bug_verify", "link"]
FREE, APPROVE, NO = "free", "approve", "no"
MAX_TASK_DEPTH = 4
STATUS = ("à faire", "en cours", "en attente", "terminée")
BUG_STATUS = ("ouvert", "en correction", "corrigé", "vérifié", "rouvert")
BUG_CLOSED = ("vérifié", "résolu")        # « résolu » : ancien statut conservé

_BASE = {"subtask": FREE, "task_top": NO, "module": NO, "bug": FREE, "bug_update": FREE, "bug_verify": NO, "link": FREE}
# Capacités par défaut des rôles standard (à garder synchronisées avec DEF_CAPS de static/index.html)
DEFAULT_CAPS = {
    "Chef de projet": {**_BASE, "task_top": APPROVE, "module": APPROVE, "bug_verify": FREE},
    "Architecte": {**_BASE, "task_top": APPROVE, "module": APPROVE, "bug_verify": FREE},
    "Développeur back-end": dict(_BASE),
    "Développeur front-end": dict(_BASE),
    "Testeur QA": {**_BASE, "task_top": APPROVE, "bug_verify": FREE},
    "DevOps": dict(_BASE),
    "Rédacteur technique": {**_BASE, "bug": NO, "bug_update": NO},
}
CAP_TXT = {"subtask": "Créer des sous-tâches (sous les tâches dont tu es responsable)",
           "task_top": "Créer des tâches de premier niveau", "module": "Créer des modules",
           "bug": "Déclarer des bugs", "bug_update": "Faire évoluer les bugs (en correction, corrigé)",
           "bug_verify": "Vérifier ou rouvrir un bug", "link": "Associer tâches et bugs à des modules"}
LV_TXT = {FREE: "libre", APPROVE: "soumis à validation (tu proposes, l'utilisateur décide)", NO: "interdit"}
AUTONOMY_TXT = {"prudent": "prudent (toute création est soumise à validation)",
                "balanced": "équilibré", "auto": "autonome (seules les décisions structurantes remontent)"}


def now():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def uid():
    return secrets.token_hex(4)


def by_id(lst, i):
    return next((x for x in lst if x.get("id") == i), None)


def norm(s):
    return " ".join(str(s or "").lower().split())


def mod_ids(p, vals):
    out = []
    for v in vals or []:
        m = next((m for m in p.get("modules", []) if m["id"] == v or m["name"].lower() == str(v).lower()), None)
        if m:
            out.append(m["id"])
    return out


def next_seq(pp):
    pp["seq"] = pp.get("seq", 0) + 1
    return pp["seq"]


def level(p, a, cap, main=False):
    """Niveau effectif d'une capacité pour un agent : libre, soumis à validation ou interdit."""
    r = by_id(p.get("roles", []), a.get("role")) or {}
    c = (a.get("caps") or {}).get(cap) or (r.get("caps") or {}).get(cap) or DEFAULT_CAPS.get(r.get("name"), _BASE).get(cap, NO)
    if main and cap in ("task_top", "module") and c == NO:
        c = APPROVE
    if cap == "bug" and a.get("canBug") and c == NO:
        c = FREE
    au = p.get("autonomy") or "balanced"
    if au == "prudent" and c == FREE and cap in ("subtask", "task_top", "module", "bug"):
        c = APPROVE
    if au == "auto" and c == APPROVE and cap in ("task_top", "module"):
        c = FREE
    return c


def rights_text(p, a, main=False):
    return "\n".join("- %s : %s" % (CAP_TXT[c], LV_TXT[level(p, a, c, main)]) for c in CAPS) \
        + "\n- Niveau d'autonomie du projet : " + AUTONOMY_TXT.get(p.get("autonomy") or "balanced", "équilibré")


def depth(pp, t):
    d = 1
    while t and t.get("parent"):
        t = by_id(pp["tasks"], t["parent"])
        d += 1 if t else 0
    return d


def owns(pp, a, t):
    """L'agent est responsable de la tâche (ou d'un de ses ancêtres), ou l'a créée."""
    while t:
        if t.get("who") == a["id"] or t.get("by") == a["name"]:
            return True
        t = by_id(pp["tasks"], t.get("parent"))
    return False


def create_task(pp, title, desc="", parent="", who="", modules=(), by="", why="", bug="", system=False):
    title = str(title or "").strip()
    if not title:
        raise Err(400, "titre de tâche vide")
    if parent and not by_id(pp["tasks"], parent):
        parent = ""
    if any(t.get("parent", "") == parent and norm(t["title"]) == norm(title) for t in pp["tasks"]):
        raise Err(409, "doublon : la tâche « %s » existe déjà à cet endroit" % title)
    t = {"id": uid(), "parent": parent, "title": title, "desc": str(desc or ""), "who": who, "status": "à faire",
         "modules": list(modules), "by": by, "why": str(why or "")}
    if bug:
        t["bug"] = bug
    if system:
        t["system"] = True
    pp["tasks"].append(t)
    return t


def create_module(pp, name, desc="", by=""):
    name = str(name or "").strip()
    if not name:
        raise Err(400, "nom de module vide")
    if any(norm(m["name"]) == norm(name) for m in pp["modules"]):
        raise Err(409, "doublon : le module « %s » existe déjà" % name)
    m = {"id": uid(), "name": name, "desc": str(desc or ""), "by": by}
    pp["modules"].append(m)
    return m


def corrections_task(pp, module_id):
    """Tâche conteneur « Corrections : <module> », créée par le système (sans validation)."""
    mod = by_id(pp["modules"], module_id) if module_id else None
    title = "Corrections : %s" % mod["name"] if mod else "Corrections (global)"
    t = next((x for x in pp["tasks"] if x.get("system") and x["title"] == title), None)
    return t or create_task(pp, title, "Conteneur des sous-tâches de correction de bugs.",
                            modules=[module_id] if mod else [], by="Système", system=True)


def create_bug(pp, title, desc="", sev="mineur", modules=(), by=""):
    title = str(title or "").strip()
    if not title:
        raise Err(400, "titre de bug vide")
    b = {"id": uid(), "t": title, "d": str(desc or ""), "sev": sev if sev in ("mineur", "majeur", "critique") else "mineur",
         "status": "ouvert", "by": by, "modules": list(modules)}
    pp["bugs"].append(b)
    for m in (list(modules) or [""]):
        corrections_task(pp, m)
    return b


def add_fix_task(pp, bug, title, desc="", who="", by=""):
    """Sous-tâche de correction d'un bug, rangée sous « Corrections : <module> » ; le bug passe en correction."""
    cont = corrections_task(pp, (bug.get("modules") or [""])[0])
    t = create_task(pp, title, desc, parent=cont["id"], who=who, modules=bug.get("modules", []), by=by, bug=bug["id"])
    if bug["status"] in ("ouvert", "rouvert"):
        bug["status"] = "en correction"
    return t


def on_task_done(pp, t):
    """Quand toutes les sous-tâches de correction d'un bug sont terminées, le bug passe à « corrigé »."""
    b = by_id(pp["bugs"], t.get("bug")) if t.get("bug") else None
    if not b or b["status"] not in ("ouvert", "en correction", "rouvert"):
        return None
    if all(x["status"] == "terminée" for x in pp["tasks"] if x.get("bug") == b["id"]):
        b["status"] = "corrigé"
        return b
    return None


def create_from(pp, kind, d, by):
    """Crée l'élément d'une proposition (tâche, module ou bug) ; retourne un message pour l'historique."""
    if kind == "task":
        t = create_task(pp, d.get("title"), d.get("desc", ""), d.get("parent", ""), d.get("who", ""), d.get("modules", []), by, d.get("why", ""))
        return "Tâche créée : %s%s" % (t["title"], " (sous-tâche)" if t["parent"] else "")
    if kind == "module":
        return "Module créé : %s" % create_module(pp, d.get("name"), d.get("desc", ""), by)["name"]
    if kind == "bug":
        return "Bug déclaré : %s" % create_bug(pp, d.get("title"), d.get("desc", ""), d.get("sev", "mineur"), d.get("modules", []), by)["t"]
    raise Err(400, "type de proposition inconnu : %s" % kind)


def instr_applies(p, a, i):
    sc = i.get("scope", "global")
    if sc.startswith("role:"):
        return norm(sc[5:]) == norm((by_id(p.get("roles", []), a.get("role")) or {}).get("name"))
    if sc.startswith("agent:"):
        return norm(sc[6:]) == norm(a.get("name"))
    return True


def instruction_lines(p, a, only_new=False):
    seen = a.get("seen_seq", 0)
    out = []
    for i in p.get("instructions", []):
        if i.get("active", True) and instr_applies(p, a, i) and (not only_new or i.get("n", 0) > seen):
            sc = i.get("scope", "global")
            out.append("- %s%s" % (("[%s] " % sc) if sc != "global" else "", i["text"]))
    return out[-40:]
