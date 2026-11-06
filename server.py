#!/usr/bin/env python3
"""MyTeam - serveur local. Bibliothèque standard uniquement, aucune dépendance."""
import base64, hashlib, hmac, json, mimetypes, os, re, secrets, threading
import urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = Path(os.environ.get("APM_DATA", ROOT / "data"))
HOST = os.environ.get("APM_HOST", "127.0.0.1")
PORT = int(os.environ.get("APM_PORT", "8000"))
OPEN_REG = os.environ.get("APM_ALLOW_REGISTER", "1") == "1"
LOCK = threading.Lock()
(DATA / "users").mkdir(parents=True, exist_ok=True)
USERS, SESSIONS = DATA / "users.json", DATA / "sessions.json"


class Err(Exception):
    def __init__(self, code, msg, retry=None):
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


def pw_hash(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return salt + "$" + h


def pw_ok(pw, stored):
    return hmac.compare_digest(pw_hash(pw, stored.split("$")[0]), stored)


def call_ai(s, system, messages):
    """Appelle l'API du service IA : format Anthropic ou compatible OpenAI."""
    key, url, model = s.get("apiKey", ""), s.get("url", "").rstrip("/"), s.get("model", "")
    if not key:
        raise Err(400, "Clé API manquante pour le service « %s »" % s.get("name", "?"))
    anth = s.get("kind") == "anthropic"
    if anth:
        u = url + "/messages"
        hd = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        body = {"model": model, "max_tokens": 8192, "system": system, "messages": messages}
    else:
        u = url + "/chat/completions"
        hd = {"Authorization": "Bearer " + key}
        body = {"model": model, "messages": [{"role": "system", "content": system}] + messages}
    hd.update({"Content-Type": "application/json", "User-Agent": "myteam/1.0"})
    req = urllib.request.Request(u, json.dumps(body).encode(), hd, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:400]
        if e.code in (429, 529):  # quota / surcharge : l'agent se suspend puis reprend
            try:
                wait = int(e.headers.get("retry-after", "60"))
            except ValueError:
                wait = 60
            raise Err(429, "Quota atteint : " + txt, wait)
        raise Err(502, "Erreur du service IA (%s) : %s" % (e.code, txt))
    except urllib.error.URLError as e:
        raise Err(502, "Service IA injoignable : %s" % e.reason)
    if anth:
        return "".join(b.get("text", "") for b in res.get("content", []))
    return res["choices"][0]["message"]["content"]


def gh(token, method, path, body=None, ok404=False):
    """Appel de l'API REST GitHub (aucun git local requis)."""
    req = urllib.request.Request(
        "https://api.github.com" + path,
        json.dumps(body).encode() if body is not None else None,
        {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "myteam/1.0",
         "Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404 and ok404:
            return None
        txt = e.read().decode(errors="replace")[:300]
        if e.code == 403 and "not accessible" in txt:
            txt = "permission manquante sur le token GitHub (%s %s) : vérifiez Contents et Pull requests en « Read and write »" % (method, path.split("?")[0])
        raise Err(e.code if e.code in (404, 405, 409, 422) else 502, "GitHub %s : %s" % (e.code, txt))
    except urllib.error.URLError as e:
        raise Err(502, "GitHub injoignable : %s" % e.reason)


def git_op(u, d):
    """Opérations git d'un projet : ensure_branch, put_file, pr_merge, release."""
    st = rj(state_path(u), {})
    p = next((x for x in st.get("projects", []) if x.get("id") == d.get("projectId")), None)
    if not p:
        raise Err(404, "Projet introuvable")
    repo = p.get("repo") or {}
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", repo.get("url", ""))
    tok = repo.get("token", "")
    if not m or not tok:
        raise Err(400, "Dépôt GitHub ou token non configuré pour ce projet")
    base = d.get("base") or repo.get("branch") or "main"
    R = "/repos/%s/%s" % m.groups()
    q = urllib.parse.quote
    op = d.get("op")
    if op == "ensure_branch":
        b = d["branch"]
        if gh(tok, "GET", R + "/git/ref/heads/" + q(b), ok404=True):
            return {"created": False}
        ref = gh(tok, "GET", R + "/git/ref/heads/" + q(base), ok404=True)
        if not ref:
            raise Err(404, "Branche de base « %s » introuvable (dépôt vide ?)" % base)
        gh(tok, "POST", R + "/git/refs", {"ref": "refs/heads/" + b, "sha": ref["object"]["sha"]})
        return {"created": True}
    if op == "put_file":
        path, b = d["path"], d["branch"]
        cur = gh(tok, "GET", R + "/contents/" + q(path) + "?ref=" + q(b), ok404=True)
        body = {"message": d.get("message", "update " + path), "branch": b,
                "content": base64.b64encode(str(d.get("content", "")).encode()).decode()}
        if isinstance(cur, dict) and "sha" in cur:
            body["sha"] = cur["sha"]
        r = gh(tok, "PUT", R + "/contents/" + q(path), body)
        return {"sha": r["commit"]["sha"]}
    if op == "pr_merge":
        head, pr = d["head"], None
        try:
            pr = gh(tok, "POST", R + "/pulls", {"title": d.get("title", head), "head": head,
                                                 "base": base, "body": d.get("body", "")})
        except Err as e:
            if e.code != 422:
                raise
            if "No commits between" in e.msg:
                return {"skipped": True}
            if "already exists" not in e.msg:
                raise
            lst = gh(tok, "GET", R + "/pulls?state=open&base=%s&head=%s" % (q(base), q(m.group(1) + ":" + head)))
            if not lst:
                raise
            pr = lst[0]
        gh(tok, "PUT", R + "/pulls/%d/merge" % pr["number"], {"merge_method": "merge"})
        return {"number": pr["number"]}
    if op == "release":
        r = gh(tok, "POST", R + "/releases", {"tag_name": d["tag"], "target_commitish": base,
                                              "name": d.get("name", d["tag"]), "body": d.get("body", "")})
        return {"url": r.get("html_url")}
    raise Err(400, "Opération git inconnue")


def state_path(user):
    return DATA / "users" / (user + ".json")


class H(BaseHTTPRequestHandler):
    server_version = "MyTeam/1.0"

    def out(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20_000_000:
            raise Err(413, "Requête trop volumineuse")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise Err(400, "JSON invalide")

    def auth(self):
        h = self.headers.get("Authorization", "")
        t = h[7:] if h.startswith("Bearer ") else ""
        u = rj(SESSIONS, {}).get(t)
        if not u:
            raise Err(401, "Non connecté")
        return u, t

    def run(self, fn):
        try:
            fn()
        except Err as e:
            self.out(e.code, {"error": e.msg, "retry_after": e.retry})
        except Exception as e:
            self.out(500, {"error": "Erreur serveur : %s" % e})

    def do_GET(self):
        self.run(self.get)

    def do_POST(self):
        self.run(self.post)

    def do_PUT(self):
        self.run(self.put)

    def get(self):
        p = self.path.split("?")[0]
        if p == "/api/state":
            u, _ = self.auth()
            return self.out(200, rj(state_path(u), {}))
        if p.startswith("/api/"):
            raise Err(404, "Route inconnue")
        f = (STATIC / ("index.html" if p == "/" else p.lstrip("/"))).resolve()
        if STATIC not in f.parents or not f.is_file():
            raise Err(404, "Introuvable")
        b = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(f.name)[0] or "text/plain") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def put(self):
        if self.path.split("?")[0] != "/api/state":
            raise Err(404, "Route inconnue")
        u, _ = self.auth()
        d = self.body()
        if not isinstance(d, dict):
            raise Err(400, "État invalide")
        with LOCK:
            wj(state_path(u), d)
        self.out(200, {"ok": True})

    def post(self):
        p = self.path.split("?")[0]
        d = self.body()
        if p in ("/api/register", "/api/login"):
            u, pw = str(d.get("user", "")), str(d.get("password", ""))
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", u) or not pw:
                raise Err(400, "Identifiant (lettres, chiffres, . _ -) et mot de passe requis")
            with LOCK:
                users = rj(USERS, {})
                if p == "/api/register":
                    if not OPEN_REG:
                        raise Err(403, "Inscriptions fermées")
                    if u in users:
                        raise Err(409, "Ce compte existe déjà")
                    users[u] = pw_hash(pw)
                    wj(USERS, users)
                elif u not in users or not pw_ok(pw, users[u]):
                    raise Err(401, "Identifiants incorrects")
                tok = secrets.token_urlsafe(32)
                s = rj(SESSIONS, {})
                s[tok] = u
                wj(SESSIONS, s)
            return self.out(200, {"token": tok})
        u, t = self.auth()
        if p == "/api/logout":
            with LOCK:
                s = rj(SESSIONS, {})
                s.pop(t, None)
                wj(SESSIONS, s)
            return self.out(200, {"ok": True})
        if p == "/api/agent/run":
            st = rj(state_path(u), {})
            s = next((x for x in st.get("services", []) if x.get("id") == d.get("serviceId")), None)
            if not s:
                raise Err(404, "Service IA introuvable")
            msgs = [{"role": m["role"], "content": str(m["content"])}
                    for m in d.get("messages", []) if m.get("role") in ("user", "assistant")]
            return self.out(200, {"text": call_ai(s, str(d.get("system", "")), msgs)})
        if p == "/api/git":
            return self.out(200, git_op(u, d))
        raise Err(404, "Route inconnue")


if __name__ == "__main__":
    print("MyTeam demarre : http://%s:%d   (Ctrl+C pour arreter)" % (HOST, PORT), flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
