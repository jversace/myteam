"""Persistance JSON, révisions et fusion à trois voies de l'état d'un utilisateur.

Le navigateur et le moteur d'agents (serveur) modifient le même état : le navigateur envoie
son état complet (PUT), le serveur le fusionne avec l'état courant en s'appuyant sur le
dernier instantané que le navigateur a reçu (fusion à trois voies, élément par élément)."""
import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("APM_DATA", ROOT / "data"))
(DATA / "users").mkdir(parents=True, exist_ok=True)
USERS, SESSIONS = DATA / "users.json", DATA / "sessions.json"
LOCK = threading.RLock()
SER, REVN, SNAP = {}, {}, {}   # compteur d'écritures, n° d'instantané, instantanés par utilisateur


class Err(Exception):
    def __init__(self, code, msg, retry=None):
        Exception.__init__(self, msg)
        self.code, self.msg, self.retry = code, msg, retry


def rj(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def wj(p, obj):  # écriture atomique : fichier temporaire puis remplacement
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def state_path(u):
    return DATA / "users" / (u + ".json")


def _init(u):
    SER.setdefault(u, 0)
    REVN.setdefault(u, 0)
    SNAP.setdefault(u, {})


def load(u):
    with LOCK:
        return rj(state_path(u), {})


def mutate(u, fn):
    """Lecture-modification-écriture atomique de l'état (utilisé par le moteur d'agents)."""
    with LOCK:
        _init(u)
        st = rj(state_path(u), {})
        r = fn(st)
        SER[u] += 1
        wj(state_path(u), st)
        return r


def _snap(u, st, keep):
    REVN[u] += 1
    SNAP[u][REVN[u]] = json.dumps(st)
    for r in sorted(SNAP[u])[:-8]:
        if r != keep:
            del SNAP[u][r]
    return REVN[u]


def get(u):
    with LOCK:
        _init(u)
        st = rj(state_path(u), {})
        return _snap(u, st, 0), SER[u], st


def changed(u, ser):
    with LOCK:
        _init(u)
        return SER[u] != ser


def put(u, base_rev, mine):
    with LOCK:
        _init(u)
        theirs = rj(state_path(u), {})
        b = SNAP[u].get(base_rev)
        base = json.loads(b) if b else theirs   # repli : sans instantané, le navigateur l'emporte
        merged = merge3(base, theirs, mine)
        SER[u] += 1
        wj(state_path(u), merged)
        return _snap(u, merged, base_rev), SER[u], merged


def _id_list(*ls):
    return all(isinstance(l, list) and all(isinstance(x, dict) and "id" in x for x in l) for l in ls)


def merge3(base, theirs, mine):
    """base = dernier état reçu par le navigateur ; theirs = état serveur ; mine = état du navigateur.
    En cas de vrai conflit sur une même valeur, le navigateur (l'utilisateur) l'emporte."""
    if mine == base:
        return theirs
    if theirs == base or theirs == mine:
        return mine
    if isinstance(mine, dict) and isinstance(theirs, dict):
        b = base if isinstance(base, dict) else {}
        out = {}
        for k in list(mine) + [k for k in theirs if k not in mine]:
            if k in mine and k in theirs:
                out[k] = merge3(b.get(k), theirs[k], mine[k])
            elif k in mine:                       # absent côté serveur
                if not (k in b and mine[k] == b[k]):
                    out[k] = mine[k]
            elif not (k in b and theirs[k] == b[k]):   # absent côté navigateur
                out[k] = theirs[k]
        return out
    if isinstance(mine, list) and isinstance(theirs, list) and _id_list(mine, theirs) \
            and (base is None or _id_list(base)):
        bm = {x["id"]: x for x in (base or [])}
        tm = {x["id"]: x for x in theirs}
        mm = {x["id"]: x for x in mine}
        out = []
        for x in mine:
            i = x["id"]
            if i in tm:
                out.append(merge3(bm.get(i), tm[i], x))
            elif not (i in bm and x == bm[i]):    # sinon : supprimé côté serveur sans modification locale
                out.append(x)
        out += [x for x in theirs if x["id"] not in mm and x["id"] not in bm]  # créés côté serveur
        return out
    return mine
