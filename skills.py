"""Skills d'agents : installation, classement, activation et consultation ciblée (bibliothèque standard uniquement).

Une skill est un dossier `data/skills/<utilisateur>/<id>/` dont le point d'entrée est `SKILL.md` (markdown avec un en-tête
`---` : name, description, + facultatif category/domain/subdomain/language/tags). L'index (`index.json`, même dossier) garde les
métadonnées, l'état actif/inactif, la source (fichier ou dépôt git) et les corrections de catégories faites par l'utilisateur.
Formats reconnus : dossier Agent Skills (SKILL.md + references/scripts/assets), paquet `.skill`/`.zip`, fichier `.md` seul,
commandes Claude Code (`.claude/commands/*.md`) et règles Cursor (`.cursor/rules/*.mdc`), ces deux derniers convertis en SKILL.md.
Les scripts d'une skill ne sont JAMAIS exécutés par l'application : seul le texte est consulté par les agents."""
import base64
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

import github
from store import DATA, Err

RAW = os.environ.get("APM_GITHUB_RAW", "https://raw.githubusercontent.com")
LOCK = threading.RLock()
MAX_FILE = 400_000            # octets : taille maximale d'un fichier de skill
MAX_SKILL = 3_000_000         # octets : taille maximale d'une skill (tous fichiers)
MAX_SKILL_FILES = 80
MAX_ZIP = 20_000_000          # octets décompressés
MAX_ENTRIES = 3000
MAX_FOUND = 300               # skills listées pour un dépôt
SUMMARY_FETCH = 120           # résumés lus pour la fenêtre de choix (les autres : nom seul)
READ_MAX = 12000              # caractères d'un retour de skills_read
FIND_MAX = 15
SCRIPT_EXT = (".sh", ".bash", ".ps1", ".bat", ".cmd", ".py", ".js", ".mjs", ".ts", ".rb", ".pl", ".exe", ".dll", ".so", ".jar")
INJECTION = re.compile(r"ignore (all |the )?(previous|prior|above) (instructions|rules)|ignore (toutes )?(les )?(instructions|règles) (précédentes|ci-dessus)|disregard (your|the) (system|previous)", re.I)

# --------------------------------------------------------------------------- stockage


def root(u):
    if u in ("", ".", ".."):
        raise Err(400, "Utilisateur invalide")
    return DATA / "skills" / u


def slug(s, default="skill", n=60):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:n].strip("-")
    return s or default


def _idx(u):
    try:
        d = json.loads((root(u) / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    if not isinstance(d.get("skills"), dict):
        d = {"skills": {}}
    return d


def _save(u, idx):
    r = root(u)
    r.mkdir(parents=True, exist_ok=True)
    tmp = r / "index.json.tmp"
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, r / "index.json")


def _dir(u, sid):
    d = (root(u) / sid).resolve()
    if root(u).resolve() not in d.parents:
        raise Err(400, "Identifiant de skill invalide")
    return d


# --------------------------------------------------------------------------- lecture du markdown et classement


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    return v


def _yaml(src):
    """Sous-ensemble de YAML suffisant pour les en-têtes de skills : `clé: valeur`, listes `[a, b]` ou `- a`, blocs `>`/`|`, sous-table d'un niveau."""
    out, L, i = {}, src.splitlines(), 0
    while i < len(L):
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$", L[i])
        i += 1
        if not m:
            continue
        k, v = m.group(1).lower(), m.group(2).strip()
        if v in (">", "|", ">-", "|-", ">+", "|+"):
            buf = []
            while i < len(L) and (L[i].startswith((" ", "\t")) or not L[i].strip()):
                buf.append(L[i].strip())
                i += 1
            out[k] = (" " if v[0] == ">" else "\n").join(buf).strip()
        elif v == "":
            sub = []
            while i < len(L) and L[i].startswith((" ", "\t", "-")):
                sub.append(L[i])
                i += 1
            items = [s.strip()[1:].strip() for s in sub if s.strip().startswith("-")]
            if items:
                out[k] = [_scalar(x) for x in items]
            else:
                d = {}
                for s in sub:
                    mm = re.match(r"^\s+([A-Za-z0-9_.-]+)\s*:\s*(.*)$", s)
                    if mm:
                        d[mm.group(1).lower()] = _scalar(mm.group(2))
                out[k] = d
        else:
            out[k] = _scalar(v)
    return out


def parse_front(text):
    """(en-tête, corps) d'un markdown ; en-tête vide s'il n'y en a pas."""
    t = text.lstrip("\ufeff")
    m = re.match(r"---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", t, re.S)
    if not m:
        return {}, t
    return _yaml(m.group(1)), t[m.end():]


def _words(*parts):
    t = unicodedata.normalize("NFKD", " ".join(str(x) for x in parts)).encode("ascii", "ignore").decode().lower()
    out = []
    for w in re.findall(r"[a-z0-9+#.\-]+", t):
        w = w.strip(".-")
        if w:
            out.append(w)
            if "-" in w:
                out.extend(x for x in w.split("-") if x)
    return out


LANGS = {
    "python": ("python", "pytest", "django", "flask", "fastapi", "pandas", "pip"),
    "javascript": ("javascript", "nodejs", "node", "npm", "react", "vue", "svelte", "jquery", "express"),
    "typescript": ("typescript", "tsx", "angular", "nextjs", "deno"),
    "java": ("java", "spring", "maven", "gradle", "jvm"),
    "kotlin": ("kotlin", "jetpack", "compose"),
    "csharp": ("csharp", "c#", "dotnet", "asp.net", "blazor"),
    "go": ("golang",),
    "rust": ("rust", "cargo", "tokio"),
    "php": ("php", "laravel", "symfony", "wordpress"),
    "ruby": ("ruby", "rails"),
    "swift": ("swift", "swiftui"),
    "cpp": ("c++", "cpp", "cmake"),
    "sql": ("sql", "postgres", "postgresql", "mysql", "sqlite"),
    "html-css": ("html", "css", "tailwind", "sass"),
    "shell": ("bash", "shell", "powershell", "zsh"),
}
DOMAINS = {
    "devops": ("deploiement", "deployer", "docker", "kubernetes", "k8s", "ci", "cd", "deploy", "deployment", "pipeline", "terraform", "ansible", "helm", "infrastructure", "devops"),
    "security": ("securite", "security", "vulnerability", "vulnerabilities", "owasp", "authentication", "secrets", "pentest", "cve", "threat"),
    "data": ("donnees", "data", "dataset", "csv", "xlsx", "excel", "analytics", "etl", "spreadsheet", "visualization", "pandas"),
    "docs": ("documentation", "docs", "readme", "docx", "pdf", "markdown", "pptx", "changelog", "wiki"),
    "design": ("conception", "maquette", "design", "ui", "ux", "figma", "layout", "accessibility", "a11y", "typography"),
    "product": ("exigences", "specifications", "requirements", "spec", "specification", "roadmap", "backlog", "user-story", "planning", "prd"),
    "writing": ("redaction", "traduction", "writing", "copywriting", "blog", "email", "translation", "editing", "prose"),
    "ai": ("llm", "prompt", "prompts", "agent", "agents", "mcp", "rag", "embedding", "embeddings", "claude"),
    "coding": ("developpement", "test", "tests", "testing", "pytest", "unittest", "jest", "tdd", "git", "commit", "commits", "rebase", "code", "coding", "programming", "refactor", "refactoring", "implement", "implementation", "debug", "debugging", "function", "compiler", "lint", "linting", "algorithm", "library", "framework", "sdk", "cli", "script", "scripts"),
}
SUBDOMAINS = {
    "backend": ("backend", "server", "api", "rest", "graphql", "endpoint", "django", "flask", "fastapi", "express", "spring", "laravel", "rails", "microservice", "microservices"),
    "frontend": ("frontend", "ui", "react", "vue", "svelte", "css", "html", "tailwind", "browser", "component", "components", "dom", "spa", "interface"),
    "integration": ("integration", "integrations", "webhook", "webhooks", "oauth", "connector", "mcp", "sync", "third-party", "sdk"),
    "database": ("base-de-donnees", "bdd", "database", "db", "sql", "postgres", "postgresql", "mysql", "sqlite", "mongodb", "orm", "schema", "query", "queries"),
    "mobile": ("mobile", "android", "ios", "swiftui", "flutter", "react-native"),
    "infra": ("docker", "kubernetes", "terraform", "infrastructure", "cloud", "aws", "azure", "gcp", "linux", "systemd"),
    "ci-cd": ("ci", "cd", "pipeline", "github-actions", "gitlab-ci", "jenkins", "deploy", "deployment"),
    "performance": ("performance", "profiling", "optimization", "optimisation", "cache", "latency", "benchmark"),
    "architecture": ("architecture", "ddd", "adr", "design-patterns", "patterns"),
    "review": ("relecture", "revue", "review", "code-review", "pull-request"),
    "testing": ("test", "tests", "testing", "pytest", "unittest", "jest", "tdd", "qa", "coverage", "e2e", "playwright"),
    "git": ("git", "commit", "commits", "branch", "merge", "rebase", "pull-request"),
    "scripting": ("script", "scripts", "cli", "automation"),
}
FUNCTIONS = {
    "review": ("review", "audit", "inspect", "lint", "critique"),
    "generate": ("generate", "scaffold", "create", "template", "boilerplate", "creating"),
    "test": ("test", "tests", "testing", "tdd", "coverage"),
    "document": ("document", "documentation", "readme", "docs", "changelog"),
    "debug": ("depannage", "bogue", "debug", "debugging", "troubleshoot", "troubleshooting", "diagnose", "fix", "bug", "bugs"),
    "refactor": ("refactor", "refactoring", "cleanup", "simplify"),
    "deploy": ("deploy", "deployment", "release", "publish", "ship"),
    "analyze": ("analyze", "analysis", "report", "profile", "profiling"),
    "migrate": ("migrate", "migration", "upgrade", "convert", "conversion"),
    "plan": ("plan", "planning", "spec", "architecture", "brainstorm", "roadmap"),
    "automate": ("automate", "automation", "workflow", "cron", "script"),
}


def _top(table, words, n, minimum=1):
    """Catégories dont les mots-clés reviennent le plus (nom compté double) ; au moins `minimum` occurrences pour être retenue."""
    sc = {k: sum(words.count(w) for w in ws) for k, ws in table.items()}
    return [k for k, v in sorted(sc.items(), key=lambda kv: -kv[1]) if v >= minimum][:n]


def _facet_values(v):
    if isinstance(v, str):
        v = re.split(r"[,;/]", v)
    return [slug(x, "", 30) for x in (v or []) if slug(x, "", 30)] if isinstance(v, list) else []


def classify(fm, name, desc, body):
    """Catégories d'une skill : l'en-tête fait foi (language, domain, subdomain, functional, category, tags) ; sinon déduction par mots-clés
    sur le nom, la description et le début du texte. Résultat modifiable à la main (les corrections sont conservées aux mises à jour)."""
    md = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    get = lambda *ks: next((fm.get(k) or md.get(k) for k in ks if fm.get(k) or md.get(k)), None)
    tags = _facet_values(get("tags", "keywords"))
    words = _words(name, name, desc, " ".join(tags), " ".join(tags))      # nom, résumé et mots-clés seulement : le corps du texte donne trop de faux positifs
    lang = _facet_values(get("language", "languages", "lang")) or _top(LANGS, words, 3)
    dom = _facet_values(get("domain", "category", "categories"))
    top = _top(DOMAINS, words, 1)
    domain = dom[0] if dom else (top[0] if top else ("coding" if lang else "general"))
    sub = _facet_values(get("subdomain", "sub-domain", "subcategory")) or _top(SUBDOMAINS, words, 2, 2)
    fun = _facet_values(get("functional", "function", "purpose")) or _top(FUNCTIONS, words, 2, 2)
    return {"language": lang, "functional": fun, "domain": domain, "subdomain": sub, "tags": tags}


def _first_para(body):
    for ln in body.splitlines():
        s = re.sub(r"^[#>*\-\s]+", "", ln).strip()
        if s and not s.startswith(("```", "---")):
            return s[:300]
    return ""


def describe(fm, body, fallback):
    """(nom, description) : en-tête, sinon premier titre / premier paragraphe, sinon nom de repli."""
    name = str(fm.get("name") or "").strip()
    if not name:
        h = re.search(r"^#\s+(.+)$", body, re.M)
        name = h.group(1).strip() if h else fallback
    desc = " ".join(str(fm.get("description") or "").split()) or _first_para(body)
    return name[:80], desc[:1200]


# --------------------------------------------------------------------------- découverte (liste de chemins -> skills candidates)

SKILL_RE = re.compile(r"(^|/)SKILL\.md$", re.I)
CMD_RE = re.compile(r"(^|/)\.claude/commands/(?:.+/)?[^/]+\.md$|(^|/)plugins/.+/commands/[^/]+\.md$", re.I)
RULE_RE = re.compile(r"(^|/)\.cursor/rules/.+\.mdc?$", re.I)


def _safe_rel(p):
    p = p.replace("\\", "/")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or ".." in parts or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or any(x.lower() == ".git" for x in parts):
        return None
    return "/".join(parts)


def discover(paths):
    """Skills trouvées dans une liste de chemins : `[{key, kind: skill|command|rule, root, entry, files}]` (trié, au plus MAX_FOUND)."""
    paths = sorted({q for q in (_safe_rel(x) for x in paths) if q})
    roots = sorted({q.rsplit("/", 1)[0] if "/" in q else "" for q in paths if SKILL_RE.search(q)}, key=len)
    out = []
    for r in roots:
        pre = r + "/" if r else ""
        nested = [o + "/" for o in roots if o != r and len(o) > len(r) and (not r or o.startswith(pre))]
        files = [q for q in paths if q.startswith(pre) and not any(q.startswith(n) for n in nested)]
        if not r:                                      # SKILL.md à la racine d'un dépôt : fichiers de premier niveau et dossiers standard seulement
            files = [q for q in files if "/" not in q or q.split("/")[0].lower() in ("references", "scripts", "assets", "examples", "templates", "resources")]
        entry = next((q for q in files if q.lower() == pre.lower() + "skill.md"), None)
        if entry:
            out.append({"key": r or ".", "kind": "skill", "root": r, "entry": entry, "files": files[:MAX_SKILL_FILES]})
    for q in paths:
        if CMD_RE.search(q):
            out.append({"key": q, "kind": "command", "root": q, "entry": q, "files": [q]})
        elif RULE_RE.search(q):
            out.append({"key": q, "kind": "rule", "root": q, "entry": q, "files": [q]})
    return out[:MAX_FOUND]


def _fallback_name(c):
    if c["kind"] == "skill":
        return (c["root"].rsplit("/", 1)[-1] if c["root"] else "skill")
    return re.sub(r"\.mdc?$", "", c["entry"].rsplit("/", 1)[-1], flags=re.I)


def _summary(c, text):
    """Résumé d'une candidate à partir du texte de son point d'entrée."""
    fm, body = parse_front(text)
    name, desc = describe(fm, body, _fallback_name(c))
    k = classify(fm, name, desc, body)
    return {"key": c["key"], "kind": c["kind"], "name": name, "description": desc[:300], "domain": k["domain"], "subdomain": k["subdomain"],
            "language": k["language"], "functional": k["functional"], "files": len(c["files"])}


# --------------------------------------------------------------------------- écriture d'une skill


def _decode(b):
    try:
        return b.decode("utf-8-sig")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def _is_text(b):
    return b"\x00" not in b[:2000]


def _warnings(files, body, desc):
    w = []
    sc = [f for f in files if f.lower().endswith(SCRIPT_EXT) or "/scripts/" in "/" + f.lower()]
    if sc:
        w.append("contient %d script(s) / exécutable(s) : l'application ne les exécute jamais, mais lisez-les avant d'autoriser un agent à les recopier" % len(sc))
    if INJECTION.search(body):
        w.append("contient une formulation qui ressemble à une injection de prompt (« ignore les instructions précédentes ») : relisez la skill")
    if not desc:
        w.append("aucune description : les agents la trouveront difficilement")
    if len(body) > 40000:
        w.append("très longue (%d caractères) : coûteuse à lire pour un agent" % len(body))
    return w


def install(u, cand, reader, source):
    """Écrit la skill `cand` (lue par `reader(chemin) -> octets`) : crée ou met à jour (même `ident`). Retourne {id, name, status}."""
    c = cand
    raw, size, blobs = {}, 0, {}
    for f in c["files"]:
        b = reader(f)
        if len(b) > MAX_FILE:
            if f == c["entry"]:
                raise Err(400, "Fichier d'entrée trop volumineux (%d Ko, maximum %d Ko)" % (len(b) // 1024, MAX_FILE // 1024))
            continue
        size += len(b)
        if size > MAX_SKILL:
            raise Err(400, "Skill trop volumineuse (maximum %d Mo)" % (MAX_SKILL // 1_000_000))
        raw[f] = b
    text = _decode(raw[c["entry"]])
    fm, body = parse_front(text)
    name, desc = describe(fm, body, _fallback_name(c))
    if c["kind"] != "skill":                                              # commande / règle : convertie en SKILL.md
        text = "---\nname: %s\ndescription: %s\n---\n\n%s" % (name.replace("\n", " "), (desc or name).replace("\n", " "), body.strip() + "\n")
    rel = {}
    if c["kind"] == "skill":
        pre = c["root"] + "/" if c["root"] else ""
        for f, b in raw.items():
            rel["SKILL.md" if f == c["entry"] else f[len(pre):]] = b
    else:
        rel["SKILL.md"] = text.encode("utf-8")
    ident = "%s:%s" % (source.get("type"), source.get("ident") or slug(name))
    k = classify(fm, name, desc, body)
    th = hashlib.sha1("\n".join("%s:%s" % (f, hashlib.sha1(b).hexdigest()) for f, b in sorted(rel.items())).encode()).hexdigest()
    with LOCK:
        idx = _idx(u)
        cur = next((s for s in idx["skills"].values() if s.get("ident") == ident), None)
        if cur:
            sid, status = cur["id"], ("unchanged" if cur.get("hash") == th else "updated")
        else:
            base, sid, n = slug(name, slug(_fallback_name(c))), None, 1
            sid = base
            while sid in idx["skills"]:
                n += 1
                sid = "%s-%d" % (base, n)
            status = "installed"
        d = _dir(u, sid)
        if status != "unchanged":
            tmp = d.with_name(sid + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            for f, b in rel.items():
                t = (tmp / f).resolve()
                if tmp.resolve() not in t.parents:
                    continue
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_bytes(b)
            shutil.rmtree(d, ignore_errors=True)
            os.replace(tmp, d)
        ov = (cur or {}).get("overrides") or {}
        meta = {"id": sid, "name": name, "description": desc, "format": c["kind"], "enabled": (cur or {}).get("enabled", True), "ident": ident,
                "source": {k2: v for k2, v in source.items() if k2 != "ident"}, "hash": th, "files": len(rel), "size": sum(len(b) for b in rel.values()),
                "scripts": any(f.lower().endswith(SCRIPT_EXT) or f.lower().startswith("scripts/") for f in rel), "warnings": _warnings(list(rel), body, desc),
                "installed": (cur or {}).get("installed") or int(time.time()), "updated": int(time.time()) if status != "unchanged" else (cur or {}).get("updated", int(time.time())),
                "overrides": ov}
        meta.update({kk: ov.get(kk, v) for kk, v in k.items()})
        idx["skills"][sid] = meta
        _save(u, idx)
    return {"id": sid, "name": name, "status": status}


# --------------------------------------------------------------------------- import de fichiers


def import_file(u, filename, data):
    """Importe un fichier : markdown seul (skill, commande ou règle), ou paquet `.zip` / `.skill` contenant une ou plusieurs skills."""
    fn = os.path.basename(str(filename or "")) or "skill.md"
    low = fn.lower()
    if low.endswith((".zip", ".skill")):
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise Err(400, "Archive illisible (zip attendu)")
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP:
            raise Err(400, "Archive trop volumineuse (%d fichiers, %d Mo décompressés au maximum %d / %d)" % (len(infos), sum(i.file_size for i in infos) // 1_000_000, MAX_ENTRIES, MAX_ZIP // 1_000_000))
        ok = {}
        for i in infos:
            q = _safe_rel(i.filename)
            if q is None or (i.external_attr >> 16) & 0o170000 == 0o120000:      # chemin dangereux (../, absolu) ou lien symbolique : ignoré
                continue
            ok[q] = i
        cands = discover(list(ok))
        if not cands:
            raise Err(400, "Aucune skill trouvée dans l'archive (un dossier avec SKILL.md est attendu)")
        return [install(u, c, lambda f: z.read(ok[f]), {"type": "file", "name": fn}) for c in cands]
    if not low.endswith((".md", ".mdc", ".markdown", ".txt")):
        raise Err(400, "Format non pris en charge : .md, .mdc, .zip ou .skill attendu")
    if len(data) > MAX_FILE:
        raise Err(400, "Fichier trop volumineux (maximum %d Ko)" % (MAX_FILE // 1024))
    if not _is_text(data):
        raise Err(400, "Fichier non textuel")
    kind = "rule" if low.endswith(".mdc") else "skill"
    c = {"key": fn, "kind": kind, "root": "", "entry": fn, "files": [fn]}
    return [install(u, c, lambda f: data, {"type": "file", "name": fn})]


# --------------------------------------------------------------------------- dépôts git (GitHub)


def _http(url, token="", accept="application/vnd.github+json", timeout=30):
    hd = {"User-Agent": "myteam/1.0", "Accept": accept}
    if token:
        hd["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hd), timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if e.code == 404:
            raise Err(404, "Introuvable sur GitHub (dépôt, branche ou chemin inexistant, ou dépôt privé : renseignez un jeton)")
        if e.code in (401, 403) and ("rate limit" in body.lower() or e.headers.get("X-RateLimit-Remaining") == "0"):
            raise Err(429, "Limite de requêtes GitHub atteinte (60 par heure sans jeton) : renseignez un jeton ou réessayez plus tard")
        if e.code in (401, 403):
            raise Err(e.code, "Accès refusé par GitHub : jeton invalide ou sans droit de lecture sur ce dépôt")
        raise Err(502, "GitHub a répondu %d" % e.code)
    except (urllib.error.URLError, OSError) as e:
        raise Err(502, "GitHub injoignable : %s" % getattr(e, "reason", e))


def _gh(path, token=""):
    return json.loads(_http(github.API + path, token))


def parse_git_url(url):
    """{kind: repo|topic, owner, repo, ref, rest} pour une adresse GitHub (dépôt, dossier /tree/…, fichier /blob/…, page /topics/…)."""
    s = str(url or "").strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/(.+?)/?$", s)
    if not m:
        m2 = re.match(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$", s)
        if not m2:
            raise Err(400, "Adresse non reconnue : seuls les dépôts GitHub sont pris en charge (https://github.com/<compte>/<dépôt>)")
        return {"kind": "repo", "owner": m2.group(1), "repo": m2.group(2).removesuffix(".git"), "rest": []}
    seg = [x for x in m.group(1).split("?")[0].split("#")[0].split("/") if x]
    if len(seg) == 2 and seg[0] == "topics":
        return {"kind": "topic", "topic": urllib.parse.unquote(seg[1])}
    if len(seg) < 2:
        raise Err(400, "Adresse incomplète : https://github.com/<compte>/<dépôt> attendu")
    out = {"kind": "repo", "owner": seg[0], "repo": seg[1].removesuffix(".git"), "rest": []}
    if len(seg) > 3 and seg[2] in ("tree", "blob"):
        out["rest"] = seg[3:]
        out["blob"] = seg[2] == "blob"
    return out


def topic_repos(topic, token=""):
    d = _gh("/search/repositories?q=%s&sort=stars&per_page=30" % urllib.parse.quote("topic:" + topic), token)
    return [{"repo": r["full_name"], "url": r.get("html_url") or "https://github.com/" + r["full_name"], "description": (r.get("description") or "")[:200], "stars": r.get("stargazers_count", 0)} for r in d.get("items", [])]


def _tree(pu, token):
    """(ref, sous-chemin, {chemin: sha}, tronqué) d'un dépôt ; la branche par défaut si l'adresse n'en donne pas (les noms de branche avec « / » sont essayés un à un)."""
    o, r, rest = pu["owner"], pu["repo"], list(pu.get("rest") or [])
    tries = []
    if rest:
        for k in range(1, min(len(rest), 4) + 1):
            tries.append(("/".join(rest[:k]), "/".join(rest[k:])))
    else:
        tries.append((_gh("/repos/%s/%s" % (o, r), token).get("default_branch") or "main", ""))
    last = None
    for ref, sub in tries:
        try:
            d = _gh("/repos/%s/%s/git/trees/%s?recursive=1" % (o, r, urllib.parse.quote(ref, safe="")), token)
        except Err as e:
            last = e
            if e.code != 404:
                raise
            continue
        if pu.get("blob") and sub:
            sub = sub.rsplit("/", 1)[0] if "/" in sub else ""
        files = {t["path"]: t.get("sha", "") for t in d.get("tree", []) if t.get("type") == "blob"}
        if sub:
            files = {p: s for p, s in files.items() if p == sub or p.startswith(sub + "/")}
        return ref, sub, files, bool(d.get("truncated"))
    raise last or Err(404, "Dépôt introuvable")


def _raw(pu, ref, token):
    def rd(path):
        return _http("%s/%s/%s/%s/%s" % (RAW, pu["owner"], pu["repo"], urllib.parse.quote(ref, safe="/"), urllib.parse.quote(path)), token, accept="*/*")
    return rd


def _hash_of(c, files):
    return hashlib.sha1("\n".join("%s:%s" % (f, files.get(f, "")) for f in c["files"]).encode()).hexdigest()


def list_repo(u, url, token=""):
    """Contenu d'une adresse : `{kind: "repos", items}` pour une page /topics/…, sinon `{kind: "skills", repo, ref, items, truncated}` avec le résumé de chaque skill trouvée."""
    pu = parse_git_url(url)
    if pu["kind"] == "topic":
        return {"kind": "repos", "topic": pu["topic"], "items": topic_repos(pu["topic"], token)}
    ref, sub, files, trunc = _tree(pu, token)
    cands = discover(list(files))
    if not cands:
        raise Err(404, "Aucune skill trouvée dans %s/%s (un dossier avec SKILL.md, .claude/commands ou .cursor/rules est attendu)" % (pu["owner"], pu["repo"]))
    rd = _raw(pu, ref, token)

    def one(c):
        try:
            s = _summary(c, _decode(rd(c["entry"])[:MAX_FILE]))
        except Err:
            s = {"key": c["key"], "kind": c["kind"], "name": _fallback_name(c), "description": "", "domain": "", "subdomain": [], "language": [], "functional": [], "files": len(c["files"])}
        return s
    with ThreadPoolExecutor(8) as ex:
        items = list(ex.map(one, cands[:SUMMARY_FETCH]))
    for c in cands[SUMMARY_FETCH:]:
        items.append({"key": c["key"], "kind": c["kind"], "name": _fallback_name(c), "description": "(résumé lu à l'installation)", "domain": "", "subdomain": [], "language": [], "functional": [], "files": len(c["files"])})
    have = {s["ident"]: s for s in _idx(u)["skills"].values() if s.get("ident")}
    for it, c in zip(items, cands):
        cur = have.get("git:%s/%s@%s" % (pu["owner"], pu["repo"], c["key"]))
        it["installed"] = bool(cur)
    return {"kind": "skills", "repo": "%s/%s" % (pu["owner"], pu["repo"]), "ref": ref, "path": sub, "items": items, "truncated": trunc}


def install_repo(u, url, keys=None, token=""):
    """Installe les skills `keys` (toutes si `keys` est None) d'un dépôt GitHub. Retourne la liste des résultats."""
    pu = parse_git_url(url)
    if pu["kind"] != "repo":
        raise Err(400, "Choisissez un dépôt (la page /topics/… ne fait que les lister)")
    ref, sub, files, _ = _tree(pu, token)
    cands = discover(list(files))
    if keys is not None:
        want = set(keys)
        cands = [c for c in cands if c["key"] in want]
    if not cands:
        raise Err(400, "Aucune skill à installer")
    rd = _raw(pu, ref, token)
    out = []
    for c in cands:
        cache = {}

        def reader(f, c=c, cache=cache):
            if f not in cache:
                cache[f] = rd(f)
            return cache[f]
        try:
            r = install(u, c, reader, {"type": "git", "repo": "%s/%s" % (pu["owner"], pu["repo"]), "ref": ref, "path": c["key"], "url": "https://github.com/%s/%s" % (pu["owner"], pu["repo"]),
                                      "ident": "%s/%s@%s" % (pu["owner"], pu["repo"], c["key"])})
            with LOCK:
                idx = _idx(u)
                if r["id"] in idx["skills"]:
                    idx["skills"][r["id"]]["remote_hash"] = _hash_of(c, files)
                    _save(u, idx)
        except Err as e:
            r = {"id": "", "name": _fallback_name(c), "status": "error", "error": e.msg}
        out.append(r)
    return out


def check_updates(u, token=""):
    """Compare, pour les skills venant d'un dépôt, l'état distant à la version installée : {id: update|ok|missing|error}."""
    idx, res, trees = _idx(u), {}, {}
    for s in idx["skills"].values():
        src = s.get("source") or {}
        if src.get("type") != "git":
            continue
        k = (src.get("repo"), src.get("ref"))
        try:
            if k not in trees:
                o, r = src["repo"].split("/", 1)
                _, _, files, _ = _tree({"owner": o, "repo": r, "rest": src["ref"].split("/")}, token)
                trees[k] = (files, {c["key"]: c for c in discover(list(files))})
            files, cands = trees[k]
            c = cands.get(src.get("path"))
            res[s["id"]] = "missing" if not c else ("ok" if _hash_of(c, files) == s.get("remote_hash") else "update")
        except Err:
            res[s["id"]] = "error"
    return res


def update_skill(u, sid, token=""):
    s = _get(u, sid)
    src = s.get("source") or {}
    if src.get("type") != "git":
        raise Err(400, "Cette skill vient d'un fichier : réimportez le fichier mis à jour (elle sera remplacée, état et catégories conservés)")
    return install_repo(u, "https://github.com/%s/tree/%s" % (src["repo"], src["ref"]), [src["path"]], token)


# --------------------------------------------------------------------------- gestion (liste, état, catégories, suppression)


def _get(u, sid):
    s = _idx(u)["skills"].get(sid)
    if not s:
        raise Err(404, "Skill introuvable")
    return s


def public(s):
    return {k: v for k, v in s.items() if k not in ("ident", "overrides", "remote_hash", "hash")}


def facets(items):
    out = {"domain": {}, "subdomain": {}, "language": {}, "functional": {}}
    for s in items:
        for f in out:
            for v in ([s.get(f)] if isinstance(s.get(f), str) else (s.get(f) or [])):
                if v:
                    out[f][v] = out[f].get(v, 0) + 1
    return out


def list_skills(u):
    items = sorted(_idx(u)["skills"].values(), key=lambda s: (s.get("domain") or "", s.get("name", "").lower()))
    return {"items": [public(s) for s in items], "facets": facets(items)}


def get_skill(u, sid):
    s = _get(u, sid)
    d = _dir(u, sid)
    files = sorted(str(p.relative_to(d)).replace("\\", "/") for p in d.rglob("*") if p.is_file())
    try:
        text = (d / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return {**public(s), "text": text, "file_list": files}


def set_enabled(u, sid, on):
    with LOCK:
        idx = _idx(u)
        if sid not in idx["skills"]:
            raise Err(404, "Skill introuvable")
        idx["skills"][sid]["enabled"] = bool(on)
        _save(u, idx)
    return {"id": sid, "enabled": bool(on)}


def uninstall(u, sid):
    with LOCK:
        idx = _idx(u)
        if sid not in idx["skills"]:
            raise Err(404, "Skill introuvable")
        shutil.rmtree(_dir(u, sid), ignore_errors=True)
        del idx["skills"][sid]
        _save(u, idx)
    return {"id": sid, "removed": True}


def set_meta(u, sid, fields):
    """Corrige les catégories d'une skill (domain, subdomain, language, functional, tags) ; conservées lors des mises à jour."""
    with LOCK:
        idx = _idx(u)
        s = idx["skills"].get(sid)
        if not s:
            raise Err(404, "Skill introuvable")
        ov = s.setdefault("overrides", {})
        for k in ("domain", "subdomain", "language", "functional", "tags"):
            if k in fields:
                v = _facet_values(fields[k])
                ov[k] = (v[0] if v else "") if k == "domain" else v
                s[k] = ov[k]
        _save(u, idx)
    return public(s)


# --------------------------------------------------------------------------- consultation par les agents


def _norm(x):
    return slug(x, "", 60)


def _match(s, f, want):
    vals = [s.get(f)] if isinstance(s.get(f), str) else (s.get(f) or [])
    w = _norm(want)
    return bool(w) and any(w == _norm(v) or w in _norm(v) for v in vals)


def _score(s, toks):
    if not toks:
        return 1
    name, tags, desc = set(_words(s.get("name"))), set(_words(" ".join(s.get("tags") or []), s.get("domain"), " ".join(s.get("subdomain") or []), " ".join(s.get("language") or []), " ".join(s.get("functional") or []))), set(_words(s.get("description")))
    return sum(3 * (t in name) + 2 * (t in tags) + (t in desc) for t in toks)


STOP = set("le la les un une des de du et ou pour avec dans sur the a an and or for with in on of to is est une faire utiliser use using".split())


def find(u, query="", domain="", subdomain="", language="", functional="", limit=8):
    """Skills ACTIVES correspondant au besoin : filtres de catégories (tous cumulés) + mots de `query` (nom > catégories > description).
    Sans aucun critère : le sommaire des catégories (comptes), jamais la liste complète."""
    act = [s for s in _idx(u)["skills"].values() if s.get("enabled", True)]
    crit = {"domain": domain, "subdomain": subdomain, "language": language, "functional": functional}
    if not (query or any(crit.values())):
        return {"summary": facets(act), "total": len(act), "items": []}
    for f, v in crit.items():
        if v:
            act = [s for s in act if _match(s, f, v)]
    toks = [w for w in _words(query) if w not in STOP and len(w) > 1]
    sc = sorted(((_score(s, toks), s) for s in act), key=lambda t: (-t[0], t[1].get("name", "")))
    n = max(1, min(int(limit or 8), FIND_MAX))
    items = [s for v, s in sc if v > 0][:n]
    return {"items": items, "total": len(act), "shown": len(items)}


def suggest(u, text, n=3):
    """Skills actives les plus proches d'un texte (tâche en cours) : seulement si la correspondance est nette (nom ou catégorie)."""
    toks = [w for w in dict.fromkeys(_words(text)) if w not in STOP and len(w) > 2]
    out = []
    for s in _idx(u)["skills"].values():
        if s.get("enabled", True) and toks:
            sc = _score(s, toks)
            if sc >= 5:
                out.append((sc, s))
    return [s for _, s in sorted(out, key=lambda t: (-t[0], t[1].get("name", "")))[:n]]


def read(u, ref, file="", from_line=None, to_line=None):
    """Texte d'une skill ACTIVE (SKILL.md par défaut, ou un fichier annexe : références, modèles…), par lignes, au plus READ_MAX caractères.
    Retourne (en-tête, texte, suite éventuelle)."""
    idx = _idx(u)["skills"]
    sid = ref if ref in idx else next((i for i, s in idx.items() if _norm(s.get("name")) == _norm(ref) or i == _norm(ref)), None)
    if not sid:
        raise Err(404, "Skill « %s » introuvable : fais d'abord skills_find." % ref)
    if not idx[sid].get("enabled", True):
        raise Err(403, "La skill « %s » est désactivée par l'utilisateur." % idx[sid]["name"])
    d = _dir(u, sid)
    rel = _safe_rel(file) if file else "SKILL.md"
    if not rel:
        raise Err(400, "Chemin de fichier invalide")
    f = (d / rel).resolve()
    if d not in f.parents or not f.is_file():
        others = sorted(str(p.relative_to(d)).replace("\\", "/") for p in d.rglob("*") if p.is_file())
        raise Err(404, "Fichier « %s » introuvable dans la skill. Fichiers : %s" % (rel, ", ".join(others[:30]) or "—"))
    raw = f.read_bytes()
    if not _is_text(raw):
        raise Err(400, "Fichier non textuel (les scripts et ressources binaires ne sont pas exécutés ni lus)")
    lines = _decode(raw).splitlines()
    a = max(int(from_line or 1), 1)
    b = min(int(to_line or len(lines)), len(lines))
    body, nxt = "", ""
    for i in range(a - 1, b):
        if len(body) + len(lines[i]) + 1 > READ_MAX:
            nxt = "[…tronqué à %d caractères : suite avec from_line=%d]" % (READ_MAX, i + 1)
            break
        body += lines[i] + "\n"
    extra = ""
    if rel == "SKILL.md":
        others = sorted(str(p.relative_to(d)).replace("\\", "/") for p in d.rglob("*") if p.is_file() and p.name != "SKILL.md")
        if others:
            extra = "Fichiers annexes (lecture avec file=\"…\") : " + ", ".join(others[:30])
    return idx[sid], rel, body.rstrip("\n"), nxt, extra


# --------------------------------------------------------------------------- routes HTTP (appelées par server.py)


def handle(u, method, path, query, d):
    """Routes /api/skills… : retourne l'objet JSON de la réponse."""
    sub = path[len("/api/skills"):].strip("/")
    tok = str(d.get("token") or "").strip()
    if method == "GET":
        if sub == "":
            return list_skills(u)
        if sub == "get":
            return get_skill(u, str((query.get("id") or [""])[0]))
    elif method == "POST":
        if sub == "import":
            try:
                data = base64.b64decode(str(d.get("data") or ""), validate=False)
            except ValueError:
                raise Err(400, "Contenu illisible")
            return {"results": import_file(u, d.get("filename"), data)}
        if sub == "discover":
            return list_repo(u, d.get("url"), tok)
        if sub == "install":
            return {"results": install_repo(u, d.get("url"), None if d.get("all") else [str(k) for k in (d.get("keys") or [])], tok)}
        if sub == "toggle":
            return set_enabled(u, str(d.get("id")), d.get("enabled"))
        if sub == "uninstall":
            return uninstall(u, str(d.get("id")))
        if sub == "update":
            return {"results": update_skill(u, str(d.get("id")), tok)}
        if sub == "check_updates":
            return {"updates": check_updates(u, tok)}
        if sub == "meta":
            return set_meta(u, str(d.get("id")), d.get("fields") or {})
    raise Err(404, "Route inconnue")
