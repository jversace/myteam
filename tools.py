"""Outils tiers pilotés par les agents (Lot 1 : type CLI). Voir « Outils tiers » dans CLAUDE.md.

Principe : l'agent DEMANDE (`tool_run`), l'application VÉRIFIE et EXÉCUTE. L'agent ne touche jamais la machine directement.
- le registre (`st["tools"]`) est écrit par l'utilisateur seul ; un agent ne peut ni créer ni modifier un outil ;
- une commande est un programme + des arguments FIXES avec des paramètres nommés `{nom}` : jamais de shell, jamais de texte libre exécuté ;
- chaque valeur de paramètre est validée (type, motif, choix, chemin confiné à l'espace de travail, pas d'option déguisée) ;
- exécution : délai maximal (arbre de processus tué), sortie limitée, environnement nettoyé (ni jeton ni clé), programme jamais situé dans l'espace
  de travail de l'agent, un seul lancement à la fois par outil.
Fonctions pures (sauf `run` / `kill_agent`) : bibliothèque standard uniquement.
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import checks

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,39}$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
PLACE_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
TYPES = {"texte": "text", "text": "text", "entier": "int", "int": "int", "chemin": "path", "path": "path", "choix": "choice", "choice": "choice"}
TYPE_FR = {"text": "texte", "int": "entier", "path": "chemin", "choice": "choix"}
PLATFORMS = ("any", "windows", "linux", "macos")
LEVELS = ("free", "approve", "no")                          # libre / avec accord de l'utilisateur / interdit
DEFAULT_TIMEOUT, MAX_TIMEOUT = 120, 3600
MAX_VALUE, MAX_ARGS, MAX_PARAMS = 500, 40, 12
MAX_OUT_BYTES = 60000                                       # fin de sortie lue sur le disque (la sortie complète n'est jamais gardée en mémoire)
LOCK_WAIT = 60                                              # attente maximale d'un lancement déjà en cours du même outil (s)
FORBIDDEN_PATH_PARTS = (".git", ".agents")
SHELLS = {"sh", "bash", "zsh", "fish", "dash", "csh", "tcsh", "ksh", "ash", "cmd", "powershell", "pwsh", "wsl", "busybox", "env", "xargs", "nohup", "sudo", "su",
          "runas", "start", "eval", "exec"}
WIN_UNSAFE = re.compile(r"[&|<>^%!\"'`\r\n()]")             # métacaractères de cmd.exe : refusés dans les valeurs passées à un .bat / .cmd

RUNNING = {}                                                # (utilisateur, projet, agent, id) -> processus en cours
KILLED = set()
_LOCKS, _LOCKS_G = {}, threading.Lock()


# ---------------------------------------------------------------------------------------------------------------- définition


def parse_args(text):
    """Une ligne = un argument (les lignes vides sont ignorées). Les `{nom}` désignent des paramètres."""
    return [l.strip() for l in str(text or "").replace("\r", "").split("\n") if l.strip()]


def parse_params(text):
    """Une ligne = `nom ; type ; obligatoire|facultatif ; description ; contrainte`. Retourne (liste, erreurs).
    type : texte (défaut), entier, chemin, choix. contrainte : texte → motif (expression régulière, correspondance complète) ; choix → `a|b|c` ;
    entier → `min..max` ; chemin → `existant` (le fichier ou dossier doit exister)."""
    out, errs, seen = [], [], set()
    for n, raw in enumerate([l for l in str(text or "").replace("\r", "").split("\n") if l.strip()], 1):
        parts = [x.strip() for x in raw.split(";")]
        parts += [""] * (5 - len(parts))
        name, typ, req, desc = parts[0].lower(), (parts[1].lower() or "texte"), parts[2].lower(), parts[3]
        cons = ";".join(parts[4:]).strip() if len(parts) > 5 else parts[4]
        if not PARAM_RE.match(name):
            errs.append("paramètre %d : nom invalide « %s » (lettres minuscules, chiffres et _, commence par une lettre)" % (n, name[:30]))
            continue
        if name in seen:
            errs.append("paramètre « %s » déclaré deux fois" % name)
            continue
        if typ not in TYPES:
            errs.append("paramètre « %s » : type inconnu « %s » (texte, entier, chemin, choix)" % (name, typ[:20]))
            continue
        pt = TYPES[typ]
        p = {"name": name, "type": pt, "required": req in ("obligatoire", "requis", "o", "oui", "required", "true", "1"), "desc": desc[:200]}
        if pt == "text" and cons:
            try:
                re.compile(cons)
            except re.error:
                errs.append("paramètre « %s » : motif invalide « %s »" % (name, cons[:60]))
                continue
            p["pattern"] = cons[:200]
        elif pt == "choice":
            ch = [c.strip() for c in cons.split("|") if c.strip()]
            if not ch:
                errs.append("paramètre « %s » : un choix demande la liste des valeurs (a|b|c)" % name)
                continue
            p["choices"] = ch[:30]
        elif pt == "int" and cons:
            m = re.fullmatch(r"(-?\d+)?\s*\.\.\s*(-?\d+)?", cons)
            if not m:
                errs.append("paramètre « %s » : bornes invalides « %s » (attendu min..max)" % (name, cons[:30]))
                continue
            p["min"], p["max"] = (int(m.group(1)) if m.group(1) else None), (int(m.group(2)) if m.group(2) else None)
        elif pt == "path":
            p["exists"] = cons.lower() in ("existant", "exists", "existante")
        seen.add(name)
        out.append(p)
    if len(out) > MAX_PARAMS:
        errs.append("trop de paramètres (maximum %d)" % MAX_PARAMS)
    return out[:MAX_PARAMS], errs


def platform_ok(tool):
    pf = (tool or {}).get("platform") or "any"
    return pf == "any" or (pf == "windows" and os.name == "nt") or (pf == "macos" and sys.platform == "darwin") or (pf == "linux" and sys.platform.startswith("linux"))


def level_of(tool):
    lv = (tool or {}).get("level")
    return lv if lv in LEVELS else "approve"                 # défaut prudent : accord de l'utilisateur


def timeout_of(tool):
    try:
        return max(1, min(int((tool or {}).get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def def_hash(tool):
    """Empreinte de ce qui détermine la commande exécutée : une demande approuvée n'est exécutée que si elle est inchangée."""
    keys = ("command", "args_text", "params_text", "workdir", "timeout")
    return hashlib.sha256(json.dumps([str((tool or {}).get(k) or "") for k in keys], ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def _display(argv):
    return " ".join(shlex.quote(x) if re.search(r"[\s\"'$`\\;&|<>*?()\[\]{}!#~]", x) or not x else x for x in argv)


def _command_errors(cmd):
    errs = []
    c = str(cmd or "").strip()
    if not c:
        return ["le programme (commande) est obligatoire"]
    if len(c) > 260 or re.search(r"[\r\n\0;&|<>`$]", c):
        errs.append("le programme doit être un nom ou un chemin SEUL (pas d'argument, de pipe, de redirection ni d'enchaînement : les arguments vont dans « Arguments »)")
        return errs
    if os.path.isabs(c) or "/" in c or "\\" in c:
        if not os.path.isabs(c):
            errs.append("un chemin de programme doit être absolu (ou un simple nom trouvé dans le PATH)")
    base = os.path.basename(c.replace("\\", "/")).lower()
    for suf in (".exe", ".cmd", ".bat", ".com"):
        if base.endswith(suf):
            base = base[:-len(suf)]
    if base in SHELLS:
        errs.append("« %s » ouvrirait un accès libre à la machine (shell, élévation de privilèges) : refusé d'office ; déclare plutôt le programme précis à lancer" % base)
    return errs


def validate_def(tool):
    """{errors, warnings, risk, preview}. Contrôle statique d'un outil du registre, sans l'exécuter."""
    t = tool or {}
    errs, warns = [], []
    name = str(t.get("name") or "")
    if not NAME_RE.match(name):
        errs.append("nom invalide « %s » (2 à 40 caractères : minuscules, chiffres, - _ . ; c'est ce que les agents écrivent)" % name[:40])
    errs += _command_errors(t.get("command"))
    args = parse_args(t.get("args_text"))
    params, perrs = parse_params(t.get("params_text"))
    errs += perrs
    names = {p["name"] for p in params}
    if len(args) > MAX_ARGS:
        errs.append("trop d'arguments (maximum %d)" % MAX_ARGS)
    used = set()
    for tok in args:
        for m in PLACE_RE.findall(tok):
            used.add(m)
            if m not in names:
                errs.append("l'argument « %s » utilise {%s}, qui n'est pas un paramètre déclaré" % (tok[:60], m))
    for p in params:
        if p["name"] not in used:
            (errs if p["required"] else warns).append("le paramètre « %s » n'est utilisé dans aucun argument" % p["name"])
    if t.get("level") not in LEVELS:
        warns.append("niveau non défini : « avec accord de l'utilisateur » sera appliqué")
    if t.get("platform") not in PLATFORMS:
        warns.append("plateforme non définie : « toutes » sera appliqué")
    elif not platform_ok(t):
        warns.append("cet outil est déclaré pour une autre plateforme que celle du serveur : il ne sera pas proposé aux agents ici")
    wd = str(t.get("workdir") or "").strip()
    if wd and (os.path.isabs(wd) or ".." in re.split(r"[\\/]", wd)):
        errs.append("le dossier de travail doit être un sous-dossier RELATIF de l'espace de travail de l'agent")
    try:
        if t.get("timeout") not in (None, "") and not (1 <= int(t.get("timeout")) <= MAX_TIMEOUT):
            errs.append("délai invalide (1 à %d secondes)" % MAX_TIMEOUT)
    except (TypeError, ValueError):
        errs.append("délai invalide (nombre de secondes)")
    argv = [str(t.get("command") or "").strip()] + [PLACE_RE.sub(lambda m: "<%s>" % m.group(1), a) for a in args]
    preview = _display(argv)
    plain = _display([str(t.get("command") or "").strip()] + [PLACE_RE.sub("x", a) for a in args])   # sans « < > » : l'analyse y verrait des redirections
    risk = checks.analyze(plain) if not errs else {"level": "refusée", "flags": [], "refused": False}
    if not errs and risk["refused"]:
        errs.append("commande refusée d'office : " + " ; ".join(risk["flags"]))
    return {"errors": errs, "warnings": warns, "risk": {"level": risk["level"], "flags": risk["flags"]}, "preview": preview}


def usage(tool):
    params, _ = parse_params((tool or {}).get("params_text"))
    if not params:
        return "aucun paramètre : {\"do\":\"tool_run\",\"tool\":\"%s\"}" % (tool or {}).get("name")
    L = []
    for p in params:
        c = {"choice": " ∈ " + "|".join(p.get("choices", [])), "int": (" %s..%s" % (p.get("min", ""), p.get("max", ""))) if ("min" in p or "max" in p) else "",
             "path": " (relatif à ton espace de travail%s)" % (", doit exister" if p.get("exists") else ""), "text": (" motif %s" % p["pattern"]) if p.get("pattern") else ""}[p["type"]]
        L.append("%s (%s%s, %s)%s" % (p["name"], TYPE_FR[p["type"]], c, "obligatoire" if p["required"] else "facultatif", (" : " + p["desc"]) if p.get("desc") else ""))
    return "; ".join(L)


# ---------------------------------------------------------------------------------------------------------------- construction


def resolve_exe(command):
    """Chemin absolu du programme, ou None. Un simple nom est cherché dans le PATH du serveur (jamais dans le dossier courant)."""
    c = str(command or "").strip()
    if os.path.isabs(c):
        return c if os.path.isfile(c) and os.access(c, os.X_OK) else None
    return shutil.which(c, path=checks.clean_env().get("PATH") or os.defpath)


def _inside(path, root):
    try:
        path, root = os.path.realpath(path), os.path.realpath(root)
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _value(p, raw, cwd):
    """(texte validé, erreur) pour un paramètre."""
    if isinstance(raw, bool) or isinstance(raw, (list, dict)) or raw is None:
        return None, "valeur de « %s » invalide (texte ou nombre attendu)" % p["name"]
    v = str(raw)
    if len(v) > MAX_VALUE or "\0" in v or "\n" in v or "\r" in v:
        return None, "valeur de « %s » refusée (vide de sens, multiligne ou plus de %d caractères)" % (p["name"], MAX_VALUE)
    if not v.strip():
        return None, "valeur de « %s » vide" % p["name"]
    t = p["type"]
    if t == "int":
        try:
            n = int(v.strip())
        except ValueError:
            return None, "« %s » doit être un entier" % p["name"]
        if p.get("min") is not None and n < p["min"] or p.get("max") is not None and n > p["max"]:
            return None, "« %s » hors bornes (%s..%s)" % (p["name"], p.get("min", ""), p.get("max", ""))
        return str(n), None
    if t == "choice":
        return (v, None) if v in p.get("choices", []) else (None, "« %s » doit valoir : %s" % (p["name"], " | ".join(p.get("choices", []))))
    if t == "path":
        real = os.path.realpath(os.path.join(cwd, v))
        if not _inside(real, cwd):
            return None, "« %s » sort de ton espace de travail" % p["name"]
        rel = os.path.relpath(real, os.path.realpath(cwd))
        if any(part in FORBIDDEN_PATH_PARTS for part in re.split(r"[\\/]", rel)):
            return None, "« %s » vise un dossier réservé (%s)" % (p["name"], ", ".join(FORBIDDEN_PATH_PARTS))
        if p.get("exists") and not os.path.exists(real):
            return None, "« %s » : « %s » n'existe pas" % (p["name"], v[:80])
        return rel.replace("\\", "/"), None
    if v.lstrip().startswith("-"):
        return None, "« %s » ne peut pas commencer par « - » (ce serait une option déguisée)" % p["name"]
    if p.get("pattern") and not re.fullmatch(p["pattern"], v):
        return None, "« %s » ne respecte pas le format attendu (%s)" % (p["name"], p["pattern"])
    return v, None


def build_argv(tool, values, cwd, roots=()):
    """Construit la commande EXACTE d'un appel. Retourne {ok, error, argv, display, cwd, exe}. Jamais de shell : `argv` est une liste.
    `roots` : dossiers modifiables par les agents, dans lesquels le programme ne doit PAS se trouver."""
    bad = lambda m: {"ok": False, "error": m}
    v = validate_def(tool)
    if v["errors"]:
        return bad("outil mal configuré (%s) : signale-le à l'utilisateur" % "; ".join(v["errors"][:2]))
    if not platform_ok(tool):
        return bad("cet outil n'existe pas sur cette plateforme")
    if not isinstance(values, dict):
        return bad("« params » doit être un objet {\"nom\": \"valeur\"}")
    params, _ = parse_params(tool.get("params_text"))
    pm = {p["name"]: p for p in params}
    extra = [k for k in values if k not in pm]
    if extra:
        return bad("paramètre(s) inconnu(s) : %s" % ", ".join(str(k)[:30] for k in extra))
    base = os.path.realpath(cwd)
    wd = str(tool.get("workdir") or "").strip()
    run_cwd = os.path.realpath(os.path.join(base, wd)) if wd else base
    if wd and not (_inside(run_cwd, base) and os.path.isdir(run_cwd)):
        return bad("le dossier de travail de l'outil (« %s ») n'existe pas dans ton espace de travail" % wd[:60])
    vals = {}
    for p in params:
        if p["name"] not in values or values[p["name"]] in (None, ""):
            if p["required"]:
                return bad("paramètre obligatoire manquant : %s" % p["name"])
            continue
        s, e = _value(p, values[p["name"]], base)
        if e:
            return bad(e)
        vals[p["name"]] = s
    exe = resolve_exe(tool["command"])
    if not exe:
        return bad("le programme « %s » est introuvable sur la machine (ou non exécutable) : signale-le à l'utilisateur" % tool["command"][:80])
    for r in list(roots) + [base]:
        if r and _inside(exe, r):
            return bad("le programme est situé dans l'espace de travail des agents : refusé (un agent pourrait le modifier)")
    args = []
    for tok in parse_args(tool.get("args_text")):
        names = PLACE_RE.findall(tok)
        if any(n not in vals for n in names):
            continue                                         # argument lié à un paramètre facultatif absent : omis
        args.append(PLACE_RE.sub(lambda m: vals[m.group(1)], tok))
    if os.name == "nt" and exe.lower().endswith((".bat", ".cmd")) and any(WIN_UNSAFE.search(x) for x in vals.values()):
        return bad("valeur refusée : caractères spéciaux interdits pour un script .bat/.cmd")
    argv = [exe] + args
    return {"ok": True, "argv": argv, "display": _display([tool["command"]] + args), "cwd": run_cwd, "exe": exe}


# ---------------------------------------------------------------------------------------------------------------- exécution


def _lock_for(tid):
    with _LOCKS_G:
        return _LOCKS.setdefault(str(tid), threading.Lock())


def run(argv, cwd, timeout=DEFAULT_TIMEOUT, key=None, lock_id="tool"):
    """Exécute `argv` (liste, SANS shell) dans `cwd` : {ok, rc, secs, timed_out, killed, tail}. Un lancement à la fois par outil (`lock_id`) ; arbre de
    processus tué au délai ou par `kill_agent` ; sortie écrite sur disque et seule sa fin est lue ; environnement nettoyé ; entrée standard fermée."""
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    lk = _lock_for(lock_id)
    if not lk.acquire(timeout=LOCK_WAIT):
        return {"ok": False, "rc": -1, "secs": 0.0, "timed_out": False, "killed": False, "tail": "Outil occupé : un autre lancement du même outil dure depuis plus de %d s." % LOCK_WAIT}
    key = key or ("", "", "", str(time.time()))
    try:
        t0 = time.time()
        kw = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt" else {"start_new_session": True}
        with tempfile.TemporaryFile() as out:
            try:
                proc = subprocess.Popen(argv, shell=False, cwd=cwd, env=checks.clean_env(), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **kw)
            except OSError as e:
                return {"ok": False, "rc": -1, "secs": 0.0, "timed_out": False, "killed": False, "tail": "Lancement impossible : %s" % e}
            RUNNING[key] = proc
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                checks._kill(proc)
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
            finally:
                RUNNING.pop(key, None)
            killed = key in KILLED
            KILLED.discard(key)
            size = out.seek(0, 2)
            out.seek(max(0, size - MAX_OUT_BYTES))
            text = out.read().decode("utf-8", errors="replace")
        if timed_out:
            text += "\n[délai de %d s dépassé : outil interrompu]" % timeout
        if killed:
            text += "\n[outil interrompu : l'agent a été arrêté]"
        rc = proc.returncode if proc.returncode is not None else -1
        return {"ok": rc == 0 and not timed_out and not killed, "rc": rc, "secs": round(time.time() - t0, 1), "timed_out": timed_out, "killed": killed, "tail": checks.compact(text, 2500)}
    finally:
        lk.release()


def kill_agent(u, pid, aid):
    """Interrompt les outils en cours lancés par cet agent (arrêt ou annulation de l'agent). Retourne leur nombre."""
    n = 0
    for k in list(RUNNING):
        if tuple(k[:3]) == (u, pid, aid):
            proc = RUNNING.get(k)
            if proc is not None:
                KILLED.add(k)
                checks._kill(proc)
                n += 1
    return n
