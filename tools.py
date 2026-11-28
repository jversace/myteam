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
import urllib.error
import urllib.parse
import urllib.request

import checks

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,39}$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
PLACE_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
TYPES = {"texte": "text", "text": "text", "entier": "int", "int": "int", "chemin": "path", "path": "path", "choix": "choice", "choice": "choice"}
TYPE_FR = {"text": "texte", "int": "entier", "path": "chemin", "choice": "choix"}
KINDS = ("cli", "api")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")
HEADER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,39}$")
FORBIDDEN_HEADERS = ("host", "content-length", "transfer-encoding", "connection", "cookie")
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
    keys = ("kind", "command", "args_text", "params_text", "workdir", "timeout", "method", "url", "headers_text", "body_text", "remote")
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


def kind_of(tool):
    return (tool or {}).get("kind") or "cli"


def parse_headers(text):
    """Une ligne = `Nom: valeur` (valeurs FIXES, jamais fournies par un agent). Retourne (dict, erreurs)."""
    out, errs = {}, []
    for raw in [l for l in str(text or "").replace("\r", "").split("\n") if l.strip()]:
        n, _, v = raw.partition(":")
        n, v = n.strip(), v.strip()
        if not HEADER_RE.match(n) or n.lower() in FORBIDDEN_HEADERS:
            errs.append("en-tête refusé ou invalide : « %s »" % n[:40])
        elif PLACE_RE.search(v):
            errs.append("l'en-tête « %s » ne peut pas contenir de paramètre (ses valeurs sont fixes : jetons et clés restent hors de portée des agents)" % n)
        else:
            out[n] = v
    return out, errs


def _api_url(tool):
    """(modèle d'URL, erreurs) : http(s), hôte LITTÉRAL, local sauf autorisation explicite, sans identifiants."""
    url = str(tool.get("url") or "").strip()
    errs = []
    pu = urllib.parse.urlsplit(PLACE_RE.sub("x", url))
    if pu.scheme not in ("http", "https") or not pu.hostname:
        return url, ["l'adresse (URL) doit commencer par http:// ou https:// et contenir un hôte"]
    if PLACE_RE.search(urllib.parse.urlsplit(url).netloc):
        errs.append("l'hôte de l'adresse ne peut pas contenir de paramètre")
    if "@" in pu.netloc:
        errs.append("l'adresse ne doit pas contenir d'identifiants (utilisez un en-tête)")
    if not tool.get("remote") and pu.hostname.lower() not in LOCAL_HOSTS and not pu.hostname.startswith("127."):
        errs.append("adresse non locale (%s) : cochez « autoriser une adresse non locale » si c'est voulu (les données du projet pourraient alors quitter la machine)" % pu.hostname[:60])
    return url, errs


def _body_tokens(node):
    if isinstance(node, str):
        return PLACE_RE.findall(node)
    if isinstance(node, list):
        return [x for e in node for x in _body_tokens(e)]
    if isinstance(node, dict):
        return [x for k, v in node.items() for x in PLACE_RE.findall(str(k)) + _body_tokens(v)]
    return []


def validate_def(tool):
    """{errors, warnings, risk, preview}. Contrôle statique d'un outil du registre, sans l'exécuter."""
    t = tool or {}
    errs, warns = [], []
    name = str(t.get("name") or "")
    if not NAME_RE.match(name):
        errs.append("nom invalide « %s » (2 à 40 caractères : minuscules, chiffres, - _ . ; c'est ce que les agents écrivent)" % name[:40])
    kind = kind_of(t)
    params, perrs = parse_params(t.get("params_text"))
    errs += perrs
    names = {p["name"] for p in params}
    used, flags = set(), []
    level_risk = "faible"
    if kind not in KINDS:
        errs.append("type d'outil inconnu : %s" % kind)
        preview = ""
    elif kind == "api":
        meth = str(t.get("method") or "GET").upper()
        if meth not in METHODS:
            errs.append("méthode HTTP invalide : %s" % meth)
        _, uerrs = _api_url(t)
        errs += uerrs
        _, herrs = parse_headers(t.get("headers_text"))
        errs += herrs
        used |= set(PLACE_RE.findall(str(t.get("url") or "")))
        body = str(t.get("body_text") or "").strip()
        if body:
            if meth == "GET":
                errs.append("une requête GET n'a pas de corps")
            try:
                used |= set(_body_tokens(json.loads(body)))
            except ValueError:
                errs.append("le corps doit être du JSON valide (les {paramètres} se placent dans des textes : \"{nom}\")")
        for p in params:
            if p["type"] == "path":
                errs.append("le paramètre « %s » : le type chemin n'existe pas pour un outil API" % p["name"])
        if not errs:
            if t.get("remote"):
                level_risk, flags = "élevée", ["adresse non locale : des données peuvent quitter la machine"]
            elif meth != "GET":
                level_risk, flags = "moyenne", ["requête %s : peut modifier des données de l'outil" % meth]
        preview = ("%s %s" % (meth, PLACE_RE.sub(lambda m: "<%s>" % m.group(1), str(t.get("url") or "")))) + ((" corps " + re.sub(r"\s+", " ", body)[:120]) if body else "")
    else:
        errs += _command_errors(t.get("command"))
        args = parse_args(t.get("args_text"))
        if len(args) > MAX_ARGS:
            errs.append("trop d'arguments (maximum %d)" % MAX_ARGS)
        for tok in args:
            used |= set(PLACE_RE.findall(tok))
        wd = str(t.get("workdir") or "").strip()
        if wd and (os.path.isabs(wd) or ".." in re.split(r"[\\/]", wd)):
            errs.append("le dossier de travail doit être un sous-dossier RELATIF de l'espace de travail de l'agent")
        argv = [str(t.get("command") or "").strip()] + [PLACE_RE.sub(lambda m: "<%s>" % m.group(1), a) for a in args]
        preview = _display(argv)
        plain = _display([str(t.get("command") or "").strip()] + [PLACE_RE.sub("x", a) for a in args])   # sans « < > » : l'analyse y verrait des redirections
        if not errs:
            r = checks.analyze(plain)
            if r["refused"]:
                errs.append("commande refusée d'office : " + " ; ".join(r["flags"]))
            else:
                level_risk, flags = r["level"], r["flags"]
    for u_ in sorted(used - names):
        errs.append("{%s} est utilisé mais n'est pas un paramètre déclaré" % u_)
    for p in params:
        if p["name"] not in used:
            (errs if p["required"] else warns).append("le paramètre « %s » n'est utilisé nulle part" % p["name"])
    if t.get("level") not in LEVELS:
        warns.append("niveau non défini : « avec accord de l'utilisateur » sera appliqué")
    if t.get("platform") not in PLATFORMS:
        warns.append("plateforme non définie : « toutes » sera appliqué")
    elif not platform_ok(t):
        warns.append("cet outil est déclaré pour une autre plateforme que celle du serveur : il ne sera pas proposé aux agents ici")
    try:
        if t.get("timeout") not in (None, "") and not (1 <= int(t.get("timeout")) <= MAX_TIMEOUT):
            errs.append("délai invalide (1 à %d secondes)" % MAX_TIMEOUT)
    except (TypeError, ValueError):
        errs.append("délai invalide (nombre de secondes)")
    return {"errors": errs, "warnings": warns, "risk": {"level": "refusée" if errs else level_risk, "flags": [] if errs else flags}, "preview": preview}


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


def _value(p, raw, cwd, dash=True):
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
    if dash and v.lstrip().startswith("-"):
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


# ---------------------------------------------------------------------------------------------------------------- type API (HTTP)


def _subst_body(node, vals, types):
    """Substitue les paramètres DANS les textes du JSON (jamais dans sa structure) : un texte exactement égal à « {nom} » prend la valeur typée,
    une valeur facultative absente supprime la clé ; ailleurs le texte est remplacé tel quel (aucune injection de structure possible)."""
    if isinstance(node, str):
        m = PLACE_RE.fullmatch(node)
        if m:
            return (int(vals[m.group(1)]) if types.get(m.group(1)) == "int" else vals[m.group(1)]) if m.group(1) in vals else _MISSING
        return PLACE_RE.sub(lambda m: vals.get(m.group(1), ""), node)
    if isinstance(node, list):
        return [x for x in (_subst_body(e, vals, types) for e in node) if x is not _MISSING]
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            r = _subst_body(v, vals, types)
            if r is not _MISSING:
                out[PLACE_RE.sub(lambda m: vals.get(m.group(1), ""), k)] = r
        return out
    return node


_MISSING = object()


def build_call(tool, values):
    """Requête HTTP EXACTE d'un appel d'outil API : {ok, error, method, url, headers, body, display}. Les valeurs sont validées puis échappées
    (URL : encodage pourcent ; corps : JSON) ; les en-têtes sont ceux de l'outil, jamais ceux d'un agent."""
    bad = lambda m: {"ok": False, "error": m}
    v = validate_def(tool)
    if v["errors"]:
        return bad("outil mal configuré (%s) : signale-le à l'utilisateur" % "; ".join(v["errors"][:2]))
    if not isinstance(values, dict):
        return bad("« params » doit être un objet {\"nom\": \"valeur\"}")
    params, _ = parse_params(tool.get("params_text"))
    pm = {p["name"]: p for p in params}
    extra = [k for k in values if k not in pm]
    if extra:
        return bad("paramètre(s) inconnu(s) : %s" % ", ".join(str(k)[:30] for k in extra))
    vals = {}
    for p in params:
        if p["name"] not in values or values[p["name"]] in (None, ""):
            if p["required"]:
                return bad("paramètre obligatoire manquant : %s" % p["name"])
            continue
        s, e = _value(p, values[p["name"]], "", dash=False)
        if e:
            return bad(e)
        vals[p["name"]] = s
    url = PLACE_RE.sub(lambda m: urllib.parse.quote(vals.get(m.group(1), ""), safe=""), str(tool["url"]).strip())
    body = None
    if str(tool.get("body_text") or "").strip():
        body = json.dumps(_subst_body(json.loads(tool["body_text"]), vals, {p["name"]: p["type"] for p in params}), ensure_ascii=False)
    meth = str(tool.get("method") or "GET").upper()
    headers = parse_headers(tool.get("headers_text"))[0]
    return {"ok": True, "method": meth, "url": url, "headers": headers, "body": body, "display": "%s %s%s" % (meth, url, (" corps " + body[:300]) if body else "")}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None                                         # une redirection pourrait mener hors de l'adresse déclarée : le code 3xx est rendu tel quel


def run_api(call, timeout=DEFAULT_TIMEOUT, lock_id="api"):
    """Exécute la requête : {ok (2xx), rc (code HTTP), secs, timed_out, killed, tail}. Sans proxy, sans redirection, réponse limitée."""
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    lk = _lock_for(lock_id)
    if not lk.acquire(timeout=LOCK_WAIT):
        return {"ok": False, "rc": -1, "secs": 0.0, "timed_out": False, "killed": False, "tail": "Outil occupé : un autre appel du même outil dure depuis plus de %d s." % LOCK_WAIT}
    try:
        t0 = time.time()
        hd = {"User-Agent": "myTeam-tools", **call["headers"]}
        data = call["body"].encode("utf-8") if call.get("body") is not None else None
        if data is not None and not any(k.lower() == "content-type" for k in hd):
            hd["Content-Type"] = "application/json"
        rq = urllib.request.Request(call["url"], data=data, method=call["method"], headers=hd)
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        timed_out, code, text = False, -1, ""
        try:
            with op.open(rq, timeout=timeout) as r:
                code, text = r.status, r.read(MAX_OUT_BYTES).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            code = e.code
            try:
                text = e.read(MAX_OUT_BYTES).decode("utf-8", errors="replace")
            except Exception:
                text = ""
        except (TimeoutError, OSError) as e:
            timed_out = "timed out" in str(e).lower() or isinstance(e, TimeoutError)
            reason = getattr(e, "reason", e)
            text = ("[délai de %d s dépassé]" % timeout) if timed_out else "Appel impossible : %s" % reason
        return {"ok": 200 <= code < 300, "rc": code, "secs": round(time.time() - t0, 1), "timed_out": timed_out, "killed": False, "tail": checks.compact(text, 2500)}
    finally:
        lk.release()


# ---------------------------------------------------------------------------------------------------------------- point d'entrée commun


def needs_workspace(tool):
    return kind_of(tool) == "cli"


def prepare(tool, values, cwd="", roots=()):
    """Appel validé d'un outil (CLI ou API) : {ok, error, display, …}."""
    return build_call(tool, values) if kind_of(tool) == "api" else build_argv(tool, values, cwd, roots)


def execute(tool, b, key=None):
    """Exécute un appel préparé par `prepare`."""
    if kind_of(tool) == "api":
        return run_api(b, timeout_of(tool), lock_id=tool["id"])
    return run(b["argv"], b["cwd"], timeout_of(tool), key=key, lock_id=tool["id"])
