"""Répertoire local du projet et verrous de fichiers entre agents.

Un agent peut travailler sur le répertoire local du projet (`p.localdir`) au lieu de sa branche GitHub.
Pour que deux agents ne s'écrasent pas, tout fichier local modifié est verrouillé (exclusif) :
`p.locks` = verrous tenus, `p.lock_wait` = agents en attente. Ces deux listes sont écrites par le serveur uniquement.

Mécanismes de secours (aucun verrou ne doit rester bloqué) :
- expiration après `TTL` secondes sans activité du détenteur ; verrou « orphelin » (détenteur sans travail en cours) libéré après `ORPHAN` s ;
- détection immédiate d'un verrou circulaire (la demande qui boucle la chaîne est refusée) ;
- tous les verrous sont libérés au démarrage du serveur et en fin de tâche ;
- arrêt (`stop`) ou annulation (`cancel` : fichiers verrouillés remis dans leur état d'avant) d'un agent par l'agent principal ou l'utilisateur ;
- libération forcée d'un verrou (agent principal ou utilisateur).
"""
import os
import re
import threading
import time
from pathlib import Path

from rights import by_id, now, uid
from store import DATA, Err, load, mutate

TTL = float(os.environ.get("APM_LOCK_TTL", "900"))          # inactivité du détenteur avant expiration (s)
ORPHAN = float(os.environ.get("APM_LOCK_ORPHAN", "120"))    # verrou d'un agent sans aucun travail en cours (s)
WAIT = float(os.environ.get("APM_LOCK_WAIT", "90"))         # attente maximale d'un verrou dans une action (s)
POLL, WAITER_STALE, MAX_PATHS, BAK_MAX = 1.0, 15.0, 10, 2_000_000
STOP = {}                                                   # (utilisateur, projet, agent) -> demande d'arrêt
IO = threading.RLock()                                      # écriture d'un fichier local / restauration : jamais en même temps
FORBIDDEN_WRITE = (".git/", ".github/", ".agents/")


class Stopped(Err):
    """L'agent a été arrêté (ou son travail annulé) par l'agent principal ou l'utilisateur."""
    def __init__(self, msg="Travail interrompu : tu as été arrêté par l’agent principal ou l’utilisateur."):
        Err.__init__(self, 499, msg)


# ---------------------------------------------------------------- répertoire local

def root(p):
    d = str((p.get("localdir") or "")).strip()
    if not d:
        raise Err(400, "Aucun répertoire local n’est défini pour ce projet (onglet Git & releases).")
    r = Path(d).expanduser()
    if not r.is_dir():
        raise Err(400, "Le répertoire local « %s » n’existe pas ou n’est pas un dossier." % d)
    return r.resolve()


def resolve(p, rel, write=False, internal=False):
    """Chemin absolu d'un chemin relatif DÉJÀ validé (engine.safe_path), borné au répertoire local (liens symboliques compris)."""
    rel = str(rel or "").strip().strip("/")
    if write and (rel == "" or (not internal and (rel + "/").startswith(FORBIDDEN_WRITE))):
        raise Err(403, "Écriture interdite sur « %s »" % rel)
    r = root(p)
    t = (r / rel).resolve()
    if t != r and r not in t.parents:
        raise Err(403, "Chemin hors du répertoire local : « %s »" % rel)
    return t


def list_dir(p, rel):
    t = resolve(p, rel)
    if not t.is_dir():
        return None
    out = []
    for e in sorted(t.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        if e.name == ".git":
            continue
        out.append({"name": e.name, "type": "dir" if e.is_dir() else "file", "size": e.stat().st_size if e.is_file() else 0})
    return out


def read_text(p, rel):
    t = resolve(p, rel)
    if not t.is_file():
        return None
    return t.read_bytes().decode("utf-8", errors="replace")


def write_text(p, rel, content, internal=False):
    t = resolve(p, rel, write=True, internal=internal)
    if t.is_dir():
        raise Err(400, "« %s » est un dossier." % rel)
    t.parent.mkdir(parents=True, exist_ok=True)
    tmp = t.with_name(t.name + ".apm-tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:      # écriture atomique : jamais de fichier à moitié écrit
        f.write(content)
    os.replace(tmp, t)


def delete(p, rel):
    t = resolve(p, rel, write=True)
    if not t.is_file():
        return False
    t.unlink()
    return True


# ---------------------------------------------------------------- verrous

def key(path):
    path = str(path or "").strip().strip("/")
    return path.lower() if os.name == "nt" else path


def _mp(u, pid, fn):
    def f(st):
        p = by_id(st.get("projects", []), pid)
        if p is None:
            raise Err(404, "Projet introuvable")
        return fn(p)
    return mutate(u, f)


def _bakdir(pid):
    return DATA / "lockbak" / pid


def _drop_bak(pid, lid):
    try:
        (_bakdir(pid) / lid).unlink()
    except OSError:
        pass


def _mins(ts):
    s = max(0, int(time.time() - ts))
    return "%d min %02d s" % (s // 60, s % 60) if s >= 60 else "%d s" % s


def held_by(p, path, aid):
    k = key(path)
    return next((l for l in p.get("locks", []) if l["key"] == k and l["by"] == aid), None)


def holder_of(p, path):
    k = key(path)
    return next((l for l in p.get("locks", []) if l["key"] == k), None)


def _expire(p, t0):
    """Retire les verrous expirés et les attentes périmées ; renvoie [(verrou, raison)]."""
    gone, keep = [], []
    for l in p.get("locks", []):
        if by_id(p.get("agents", []), l["by"]) is None:
            gone.append((l, "agent révoqué"))
        elif t0 - l.get("touched", t0) > TTL:
            gone.append((l, "expiré : aucune activité de %s depuis %d min" % (l["name"], (t0 - l["touched"]) // 60)))
        else:
            keep.append(l)
    p["locks"] = keep
    p["lock_wait"] = [w for w in p.get("lock_wait", []) if t0 - w.get("seen", 0) <= WAITER_STALE]
    return gone


def cycle(p, aid):
    """Chaîne d'attente circulaire passant par `aid` (liste de noms d'agents), ou None."""
    waits, locks = p.get("lock_wait", []), p.get("locks", [])
    edges = {}
    for w in waits:
        l = next((x for x in locks if x["key"] == w["key"]), None)
        if l and l["by"] != w["by"]:
            edges.setdefault(w["by"], set()).add(l["by"])
        for e in waits:                                  # file d'attente : on attend aussi ceux qui sont passés avant
            if e["key"] == w["key"] and e["by"] != w["by"] and e["since"] < w["since"]:
                edges.setdefault(w["by"], set()).add(e["by"])
    seen = set()

    def dfs(x, path):
        for y in edges.get(x, ()):
            if y == aid:
                return path + [y]
            if y not in seen:
                seen.add(y)
                r = dfs(y, path + [y])
                if r:
                    return r
        return None
    c = dfs(aid, [aid])
    return [(by_id(p.get("agents", []), i) or {}).get("name", "?") for i in c] if c else None


def check_stop(u, pid, aid, since):
    if not since:
        return
    s = STOP.get((u, pid, aid))
    if s and s["ts"] >= since:
        raise Stopped("Travail interrompu : %s (%s)." % ("annulé" if s["cancel"] else "arrêté", s.get("reason") or "décision de %s" % s["by"]))


def acquire(u, pid, aid, paths, task="", note="", wait=None, since=0.0, log=None):
    """Verrouille TOUS les chemins (ou aucun), en attendant au plus `wait` secondes que les autres agents les libèrent.
    Renvoie (ok, message). `log(texte)` trace les événements notables (verrou circulaire, expirations)."""
    paths = [str(x).strip().strip("/") for x in paths if str(x).strip().strip("/")][:MAX_PATHS]
    if not paths:
        return False, "Aucun chemin à verrouiller."
    keys = {key(x): x for x in paths}
    start, rid = time.time(), uid()
    deadline = start + (WAIT if wait is None else wait)
    me = {}

    def step(p):
        t0 = time.time()
        for l, why in _expire(p, t0):
            me.setdefault("gone", []).append((l, why))
        for l in p["locks"]:
            if l["by"] == aid:
                l["touched"] = t0                         # un agent qui attend est vivant : ses propres verrous ne doivent pas expirer
        a = by_id(p["agents"], aid)
        if a is None:
            raise Err(404, "Agent introuvable")
        waits = p.setdefault("lock_wait", [])
        busy = []
        for k, path in keys.items():
            l = next((x for x in p["locks"] if x["key"] == k), None)
            if l and l["by"] != aid:
                busy.append((path, l["name"], l["since"], l["by"]))
                continue
            ahead = [w for w in waits if w["key"] == k and w["by"] != aid and w["since"] < start]
            if ahead:
                w = min(ahead, key=lambda w: w["since"])
                busy.append((path, w["name"], w["since"], w["by"]))
        if not busy:
            p["lock_wait"] = [w for w in waits if w.get("rid") != rid]
            new = []
            for k, path in keys.items():
                l = next((x for x in p["locks"] if x["key"] == k and x["by"] == aid), None)
                if l:
                    l["touched"] = t0
                    continue
                l = {"id": uid(), "key": k, "path": path, "by": aid, "name": a["name"], "task": task, "note": str(note)[:200],
                     "since": t0, "touched": t0, "d": now(), "bak": "none"}
                p["locks"].append(l)
                new.append(l)
            me["new"] = new
            return "ok", None
        for path, hn, hs, hid in busy:
            k = key(path)
            w = next((x for x in waits if x.get("rid") == rid and x["key"] == k), None)
            if w:
                w["seen"] = t0
            else:
                waits.append({"id": uid(), "rid": rid, "key": k, "path": path, "by": aid, "name": a["name"], "since": start, "seen": t0, "task": task})
        c = cycle(p, aid)
        if c:
            p["lock_wait"] = [w for w in p["lock_wait"] if w.get("rid") != rid]
            return "deadlock", c
        return "busy", busy

    def clean():
        _mp(u, pid, lambda p: p.__setitem__("lock_wait", [w for w in p.get("lock_wait", []) if w.get("rid") != rid]))

    while True:
        kind, info = _mp(u, pid, step)
        for l, why in me.pop("gone", []):
            _drop_bak(pid, l["id"])
            if log:
                log("Verrou sur « %s » libéré (%s)." % (l["path"], why))
        if kind == "ok":
            _backup(u, pid, me.get("new", []))
            return True, "Verrouillé : %s." % ", ".join(paths)
        if kind == "deadlock":
            msg = ("Verrou circulaire détecté (%s) : tu attends un fichier détenu par quelqu’un qui attend l’un des tiens. Demande refusée pour éviter le blocage. "
                   "Libère les verrous dont tu n’as plus besoin (unlock_file), puis réessaie ; ou écris à l’agent concerné / à l’agent principal.") % " → ".join(info)
            if log:
                log("Verrou circulaire : %s. La demande de %s a été refusée." % (" → ".join(info), "l’agent"))
            return False, msg
        try:
            check_stop(u, pid, aid, since or start)
        except Stopped:
            clean()
            raise
        if time.time() >= deadline:
            clean()
            return False, "Fichier(s) toujours verrouillé(s) après %d s : %s. Fais autre chose en attendant, réessaie plus tard, ou écris au détenteur / à l’agent principal (qui peut libérer le verrou)." % (
                int(deadline - start), " ; ".join("%s détenu par %s depuis %s" % (b[0], b[1], _mins(b[2])) for b in info))
        time.sleep(min(POLL, max(0.05, deadline - time.time())))


def _backup(u, pid, new):
    """Sauvegarde le contenu d'origine de chaque fichier nouvellement verrouillé (pour l'annulation du travail d'un agent)."""
    if not new:
        return
    p = by_id(load(u).get("projects", []), pid)
    res = {}
    for l in new:
        try:
            t = resolve(p, l["path"])
            if t.is_file() and t.stat().st_size <= BAK_MAX:
                _bakdir(pid).mkdir(parents=True, exist_ok=True)
                (_bakdir(pid) / l["id"]).write_bytes(t.read_bytes())
                res[l["id"]] = "file"
            elif not t.exists():
                res[l["id"]] = "absent"
        except (OSError, Err):
            pass
    if res:
        _mp(u, pid, lambda pp: [l.__setitem__("bak", res[l["id"]]) for l in pp.get("locks", []) if l["id"] in res])


def touch(u, pid, aid):
    """Activité d'un agent : ses verrous restent vivants."""
    def f(p):
        t0 = time.time()
        for l in p.get("locks", []):
            if l["by"] == aid:
                l["touched"] = t0
    _mp(u, pid, f)


def release(u, pid, aid, paths=None):
    """Libère les verrous de l'agent (tous si `paths` est vide). Renvoie les chemins libérés."""
    ks = {key(x) for x in paths or []}
    out = []

    def f(p):
        keep = []
        for l in p.get("locks", []):
            if l["by"] == aid and (not ks or l["key"] in ks):
                out.append(l)
            else:
                keep.append(l)
        p["locks"] = keep
        p["lock_wait"] = [w for w in p.get("lock_wait", []) if not (w["by"] == aid and not ks)]
    _mp(u, pid, f)
    for l in out:
        _drop_bak(pid, l["id"])
    return [l["path"] for l in out]


def force_release(u, pid, lid):
    """Libération forcée d'un verrou précis (agent principal ou utilisateur). Renvoie le verrou, ou None."""
    out = []

    def f(p):
        out.extend(l for l in p.get("locks", []) if l["id"] == lid)
        p["locks"] = [l for l in p.get("locks", []) if l["id"] != lid]
    _mp(u, pid, f)
    for l in out:
        _drop_bak(pid, l["id"])
    return out[0] if out else None


def request_stop(u, pid, aid, cancel, by, reason):
    STOP[(u, pid, aid)] = {"ts": time.time(), "cancel": bool(cancel), "by": by, "reason": reason}


def release_agent(u, pid, aid, restore=False):
    """Libère tous les verrous d'un agent ; avec `restore`, remet chaque fichier verrouillé dans l'état d'avant le verrou.
    Renvoie (libérés, restaurés, non restaurables)."""
    freed, restored, lost = [], [], []
    with IO:
        p = by_id(load(u).get("projects", []), pid) or {}
        mine = [l for l in p.get("locks", []) if l["by"] == aid]
        if restore:
            for l in mine:
                try:
                    if l.get("bak") == "file":
                        t = resolve(p, l["path"], write=True)
                        t.parent.mkdir(parents=True, exist_ok=True)
                        t.write_bytes((_bakdir(pid) / l["id"]).read_bytes())
                        restored.append(l["path"])
                    elif l.get("bak") == "absent":
                        delete(p, l["path"])
                        restored.append(l["path"] + " (supprimé : n’existait pas avant)")
                    else:
                        lost.append(l["path"])
                except (OSError, Err):
                    lost.append(l["path"])
        freed = release(u, pid, aid)
    return freed, restored, lost


def maintain(u, pid, busy_agents):
    """Passage du suivi (indépendant de `supervision`) : expirations et verrous orphelins. Renvoie les phrases à journaliser."""
    notes, gone = [], []

    def f(p):
        t0 = time.time()
        gone.extend(_expire(p, t0))
        keep = []
        for l in p.get("locks", []):
            if l["by"] not in busy_agents and t0 - l.get("touched", t0) > ORPHAN:
                gone.append((l, "orphelin : %s n’a plus de travail en cours" % l["name"]))
            else:
                keep.append(l)
        p["locks"] = keep
    _mp(u, pid, f)
    for l, why in gone:
        _drop_bak(pid, l["id"])
        notes.append("Verrou sur « %s » (détenu par %s depuis %s) libéré automatiquement : %s." % (l["path"], l["name"], _mins(l["since"]), why))
    return notes


def diag(p):
    """Éléments factuels pour le diagnostic : [{who, level, text}]."""
    out, name = [], lambda i: (by_id(p.get("agents", []), i) or {}).get("name", "?")
    for l in p.get("locks", []):
        out.append({"who": l["name"], "level": "info", "text": "détient le verrou de « %s » depuis %s (dernière activité il y a %s)." % (l["path"], _mins(l["since"]), _mins(l.get("touched", l["since"])))})
    for w in p.get("lock_wait", []):
        l = holder_of(p, w["path"])
        out.append({"who": w["name"], "level": "attente", "text": "attend le fichier « %s »%s depuis %s." % (w["path"], (" détenu par %s" % l["name"]) if l else "", _mins(w["since"]))})
    seen = set()
    for w in p.get("lock_wait", []):
        c = cycle(p, w["by"])
        if c and frozenset(c) not in seen:
            seen.add(frozenset(c))
            out.append({"who": c[0], "level": "bloquant", "text": "verrou circulaire : %s. Utilise stop_agent / force_unlock (agent principal) ou le bouton de l’onglet Équipe." % " → ".join(c)})
    return out


def text(p, aid=None):
    """Résumé des verrous pour le prompt d'un agent."""
    L = ["- %s : %s%s" % (l["path"], l["name"], " (toi)" if l["by"] == aid else "") for l in p.get("locks", [])]
    return "\n".join(L) if L else "(aucun fichier verrouillé)"


def check_dir(p):
    r = root(p)
    n = sum(1 for _ in r.iterdir())
    return "Répertoire local « %s » accessible (%d élément(s) à la racine)." % (r, n)


def clear_all(u, pid):
    n = []
    def f(p):
        n.append(len(p.get("locks", [])))
        p["locks"], p["lock_wait"] = [], []
    _mp(u, pid, f)
    try:
        for x in _bakdir(pid).iterdir():
            x.unlink()
    except OSError:
        pass
    return n[0] if n else 0
