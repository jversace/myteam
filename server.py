#!/usr/bin/env python3
"""MyTeam - serveur local. Bibliothèque standard uniquement, aucune dépendance.
HTTP, comptes et sessions ; la persistance est dans store.py, le moteur d'agents dans engine.py."""
import hashlib, hmac, json, mimetypes, os, re, secrets, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import engine
import store
from ai import call_ai
from store import Err, LOCK, SESSIONS, USERS, rj, wj

STATIC = store.ROOT / "static"
HOST = os.environ.get("APM_HOST", "127.0.0.1")
PORT = int(os.environ.get("APM_PORT", "8000"))
OPEN_REG = os.environ.get("APM_ALLOW_REGISTER", "1") == "1"


def pw_hash(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return salt + "$" + h


def pw_ok(pw, stored):
    return hmac.compare_digest(pw_hash(pw, stored.split("$")[0]), stored)


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
            ser = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("ser", [None])[0]
            if ser is not None and not store.changed(u, int(ser)):
                return self.out(200, {"same": True})
            rev, ser, st = store.get(u)
            return self.out(200, {"rev": rev, "ser": ser, "state": st})
        if p == "/api/diagnostic":
            u, _ = self.auth()
            pid = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("project", [""])[0]
            pr = next((x for x in store.load(u).get("projects", []) if x.get("id") == pid), None)
            if not pr:
                raise Err(404, "Projet introuvable")
            engine.sync_mrs(u, pid, force=True)               # état frais : pas de blocage fantôme dans le diagnostic
            pr = next((x for x in store.load(u).get("projects", []) if x.get("id") == pid), pr)
            return self.out(200, {"items": engine.diagnose(pr)})
        if p.startswith("/api/"):
            raise Err(404, "Route inconnue")
        f = (STATIC / ("index.html" if p == "/" else p.lstrip("/"))).resolve()
        if STATIC not in f.parents or not f.is_file():
            raise Err(404, "Introuvable")
        b = f.read_bytes()
        self.send_response(200)
        ct = mimetypes.guess_type(f.name)[0] or "text/plain"
        self.send_header("Content-Type", ct + ("; charset=utf-8" if ct.startswith("text/") or ct.endswith(("json", "javascript")) else ""))
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def put(self):
        if self.path.split("?")[0] != "/api/state":
            raise Err(404, "Route inconnue")
        u, _ = self.auth()
        d = self.body()
        if not isinstance(d.get("state"), dict):
            raise Err(400, "État invalide")
        rev, ser, st = store.put(u, d.get("rev"), d["state"])
        self.out(200, {"rev": rev, "ser": ser, "state": st})

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
        if p == "/api/service/test":
            sv = next((x for x in store.load(u).get("services", []) if x.get("id") == d.get("serviceId")), None)
            if not sv:
                raise Err(404, "Service IA introuvable")
            return self.out(200, {"ok": True, "sample": call_ai(sv, "Réponds uniquement par OK.", [{"role": "user", "content": "ping"}])[:80]})
        if p == "/api/manual/answer":
            if not str(d.get("text", "")).strip():
                raise Err(400, "Réponse vide")
            engine.manual_answer(u, str(d.get("id")), str(d["text"]))
            return self.out(200, {"ok": True})
        if p == "/api/manual/cancel":
            engine.manual_cancel(u, str(d.get("id")))
            return self.out(200, {"ok": True})
        if p == "/api/sync":
            pid = str(d.get("projectId", ""))
            engine.snap(u, pid)                            # 404 si le projet n'existe pas
            return self.out(200, {"items": engine.sync_all(u, pid)})
        if p == "/api/job":
            engine.start(u, d)
            return self.out(200, {"ok": True})
        raise Err(404, "Route inconnue")


if __name__ == "__main__":
    engine.recover()
    engine.start_supervisor()
    print("MyTeam demarre : http://%s:%d   (Ctrl+C pour arreter)" % (HOST, PORT), flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
