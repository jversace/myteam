"""Tests du moteur d'agents : faux GitHub local + IA scriptée (aucun réseau, aucune clé)."""
import base64
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from urllib.parse import parse_qs, unquote, urlparse

BR, HEAD, SHA, PRS, REL, CNT = {}, {}, {}, {}, [], [0]


def reset():
    BR.clear(); HEAD.clear(); SHA.clear(); PRS.clear(); REL.clear()
    BR["main"] = {"README.md": "# demo"}
    HEAD["main"] = new_sha("main")


def new_sha(b):
    CNT[0] += 1
    s = "sha-%s-%d" % (b, CNT[0])
    SHA[s] = b
    return s


class GH(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def handle_any(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else {}
        rest, m = re.match(r"/repos/o/r(/.*)", unquote(u.path)).group(1), self.command
        if rest.startswith("/git/ref/heads/"):
            b = rest[len("/git/ref/heads/"):]
            return self.send(200, {"object": {"sha": HEAD[b]}}) if b in BR else self.send(404, {"message": "Not Found"})
        if rest == "/git/refs":
            b = body["ref"][len("refs/heads/"):]
            BR[b] = dict(BR[SHA[body["sha"]]]); HEAD[b] = new_sha(b)
            return self.send(201, {})
        if rest.startswith("/contents"):
            path = rest[len("/contents"):].lstrip("/")
            ref = (q.get("ref") or [None])[0] or body.get("branch")
            files = BR.get(ref)
            if m == "GET":
                if files is None:
                    return self.send(404, {})
                if path in files:
                    return self.send(200, {"type": "file", "sha": "x", "encoding": "base64", "content": base64.b64encode(files[path].encode()).decode()})
                pre = path + "/" if path else ""
                names = sorted({k[len(pre):].split("/")[0] for k in files if k.startswith(pre)})
                if not names:
                    return self.send(404, {})
                return self.send(200, [{"name": x, "type": "dir" if any(k.startswith(pre + x + "/") for k in files) else "file", "size": len(files.get(pre + x, ""))} for x in names])
            if m == "PUT":
                files[path] = base64.b64decode(body["content"]).decode(); HEAD[ref] = new_sha(ref)
                return self.send(200, {"commit": {"sha": HEAD[ref]}})
            if m == "DELETE":
                files.pop(path, None); HEAD[ref] = new_sha(ref)
                return self.send(200, {"commit": {"sha": HEAD[ref]}})
        if rest == "/pulls" and m == "POST":
            if BR[body["head"]] == BR[body["base"]]:
                return self.send(422, {"message": "Validation Failed: No commits between"})
            PRS[len(PRS) + 1] = (body["head"], body["base"])
            return self.send(201, {"number": len(PRS)})
        mm = re.match(r"/pulls/(\d+)/merge", rest)
        if mm:
            h, b = PRS[int(mm.group(1))]
            BR[b].update(BR[h]); HEAD[b] = new_sha(b)
            return self.send(200, {})
        if rest == "/releases":
            REL.append(body)
            return self.send(201, {"html_url": "http://x/release"})
        self.send(404, {"message": "?"})

    do_GET = do_POST = do_PUT = do_DELETE = handle_any


srv = HTTPServer(("127.0.0.1", 0), GH)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["APM_GITHUB_API"] = "http://127.0.0.1:%d" % srv.server_port
os.environ["APM_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai, engine, store  # noqa: E402
from ai import Reply  # noqa: E402
from store import Err  # noqa: E402


def make_state():
    ag = lambda i, n, **k: {**dict(id=i, name=n, service="s1", role="r1", branch="agent/" + n.lower(), canBug=False, talk=[], history=[], notes=""), **k}
    return {"services": [{"id": "s1", "name": "S", "kind": "openai", "url": "x", "model": "m", "apiKey": "k"}],
            "projects": [{"id": "p1", "name": "Demo", "rules": "Toujours des tests.", "spec": "", "mainId": "a1",
                          "repo": {"url": "https://github.com/o/r", "branch": "main", "token": "t"},
                          "roles": [{"id": "r1", "name": "Dev", "desc": "Développe"}],
                          "agents": [ag("a1", "Alice", canBug=True), ag("a2", "Bob") | {"talk": ["a1"]}],
                          "tasks": [], "bugs": [], "modules": [{"id": "m1", "name": "Auth", "desc": ""}],
                          "log": [], "commits": [], "releases": [], "mrs": []}]}


def proj():
    return store.load("u")["projects"][0]


def scripted(s, system, msgs):
    me = re.search(r"Prénom : (\w+)", system).group(1)
    last = msgs[-1]["content"]
    if me == "Alice":
        if last.startswith("[Vous]"):
            return 'Je regarde.\n```action\n{"do":"list_files","path":""}\n```\n```action\n{"do":"message","to":"Bob","text":"écris hello.txt"}\n```'
        if last.startswith("[Application] Résultats"):
            return 'Terminé.\n```action\n{"do":"task","title":"Suivi hello","assignee":"Bob","modules":["Auth"]}\n```\n```action\n{"do":"spec","text":"# Spec\\nhello"}\n```'
    if me == "Bob":
        if last.startswith("[Alice]"):
            return 'OK\n```action\n{"do":"write_file","path":"hello.txt","content":"salut","message":"feat: hello"}\n```\n```action\n{"do":"read_file","path":"hello.txt"}\n```\n```action\n{"do":"task","title":"interdit"}\n```'
        if last.startswith("[Application] Résultats"):
            assert "salut" in last and "refusée" in last      # lecture renvoyée + refus de droits renvoyé
            return "Fichier écrit et relu."
    return "ok"


class Usage(unittest.TestCase):
    """call_ai : lecture des tokens consommés (formats Anthropic et compatible OpenAI)."""
    def test_formats(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                anth = self.path.endswith("/messages")
                b = json.dumps({"content": [{"type": "text", "text": "salut"}], "usage": {"input_tokens": 21, "output_tokens": 9, "cache_read_input_tokens": 4}}
                               if anth else {"choices": [{"message": {"content": "hello"}}], "usage": {"prompt_tokens": 11, "completion_tokens": 7}}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        sv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=sv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d/v1" % sv.server_port
        r = ai.call_ai({"apiKey": "k", "url": base, "kind": "anthropic", "model": "m"}, "s", [{"role": "user", "content": "x"}])
        self.assertEqual((str(r), r.usage), ("salut", {"in": 25, "out": 9}))
        r = ai.call_ai({"apiKey": "k", "url": base, "kind": "openai", "model": "m"}, "s", [{"role": "user", "content": "x"}])
        self.assertEqual((str(r), r.usage), ("hello", {"in": 11, "out": 7}))
        sv.shutdown()


class Merge(unittest.TestCase):
    def test_three_way(self):
        base = {"tasks": [{"id": "t1", "title": "a", "status": "à faire"}, {"id": "t3", "title": "c"}], "log": []}
        theirs = {"tasks": [{"id": "t1", "title": "a", "status": "terminée"}, {"id": "t3", "title": "c"}, {"id": "t2", "title": "créée par agent"}], "log": [{"x": 1}]}
        mine = {"tasks": [{"id": "t1", "title": "renommée", "status": "à faire"}], "log": []}   # t3 supprimée localement
        m = store.merge3(base, theirs, mine)
        ids = [t["id"] for t in m["tasks"]]
        self.assertEqual(ids, ["t1", "t2"])
        self.assertEqual(m["tasks"][0], {"id": "t1", "title": "renommée", "status": "terminée"})
        self.assertEqual(m["log"], [{"x": 1}])


class Engine(unittest.TestCase):
    def setUp(self):
        reset()
        store.wj(store.state_path("u"), make_state())
        self.p = mock.patch.object(engine, "call_ai", scripted)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_onboarding_cree_branche_et_contexte(self):
        engine.onboard("u", "p1", "a2")
        self.assertIn("agent/bob", BR)
        ctx = BR["agent/bob"][".agents/Bob.md"]
        self.assertIn("Toujours des tests.", ctx)
        self.assertIn("write_file", ctx)
        self.assertTrue(any("Onboarding de Bob" in m["text"] for m in proj()["log"]))

    def test_dialogue_principal_fichiers_et_droits(self):
        engine.ask("u", "p1", "a1", "fais hello", "Vous")
        p = proj()
        self.assertEqual(BR["agent/bob"]["hello.txt"], "salut")             # écriture sur la branche de Bob
        self.assertNotIn("hello.txt", BR["main"])                           # jamais sur la base
        self.assertEqual([t["title"] for t in p["tasks"]], ["Suivi hello"])  # Bob n'a pas pu créer « interdit »
        self.assertEqual(p["tasks"][0]["who"], "a2")
        self.assertEqual(p["tasks"][0]["modules"], ["m1"])
        self.assertEqual(p["spec"], "# Spec\nhello")
        self.assertEqual(BR["agent/alice"]["SPEC.md"], "# Spec\nhello")
        texts = [(m["from"], m["to"], m["text"]) for m in p["log"]]
        self.assertIn(("Alice", "Bob", "écris hello.txt"), texts)
        self.assertIn(("Bob", "Alice", "Fichier écrit et relu."), texts)
        self.assertTrue(any("refusée" in t for _, _, t in texts))
        self.assertTrue(any(c["m"] == "feat: hello" for c in p["commits"]))
        self.assertEqual(p["agents"][0]["work"], 0)

    def test_ecriture_sur_branche_de_base_refusee(self):
        p = store.load("u"); p["projects"][0]["agents"][1]["branch"] = "main"; store.wj(store.state_path("u"), p)
        note, back = engine.repo_act("u", "p1", proj(), proj()["agents"][1], {"path": "x.txt", "content": "1"}, "write_file")
        self.assertIn("refusée", note)
        self.assertNotIn("x.txt", BR["main"])

    def test_chemins_dangereux(self):
        for bad in ("../x", ".github/workflows/ci.yml", ".agents/Alice.md", "a b$c"):
            with self.assertRaises(Err):
                engine.safe_path(bad, write=True)

    def test_release_fusionne_et_tag(self):
        engine.ask("u", "p1", "a1", "fais hello", "Vous")
        engine.onboard("u", "p1", "a1")
        engine.release("u", "p1", "v1.0.0", "notes")
        p = proj()
        self.assertEqual(BR["main"]["hello.txt"], "salut")                   # branche de Bob fusionnée
        self.assertIn("SPEC.md", BR["main"])                                 # branche d'Alice fusionnée
        self.assertEqual(REL[0]["tag_name"], "v1.0.0")
        self.assertEqual(p["releases"][0]["v"], "v1.0.0")
        self.assertEqual(len(p["mrs"]), 2)
        self.assertFalse(p["releasing"])

    def _attendre_prompt(self, sauf=None):
        for _ in range(200):
            for x in proj().get("pending", []):
                if x["id"] != sauf:
                    return x
            time.sleep(0.02)
        self.fail("aucun prompt manuel en attente")

    def test_mode_manuel_sans_api(self):
        st = store.load("u"); st["projects"][0]["agents"][1]["mode"] = "manual"; store.wj(store.state_path("u"), st)
        res = {}

        def boom(*a):
            raise AssertionError("l'API ne doit pas être appelée en mode manuel")
        with mock.patch.object(engine, "call_ai", boom):
            th = threading.Thread(target=lambda: res.update(r=engine.ask("u", "p1", "a2", "écris m.txt", "Vous")))
            th.start()
            x1 = self._attendre_prompt()
            self.assertTrue(x1["new"])
            self.assertIn("écris m.txt", x1["full"])
            self.assertIn("Prénom : Bob", x1["full"])                        # instructions complètes dans le 1er prompt
            engine.manual_answer("u", x1["id"], 'Je le fais.\n```action\n{"do":"write_file","path":"m.txt","content":"manuel","message":"feat: manuel"}\n```\n```action\n{"do":"read_file","path":"m.txt"}\n```')
            x2 = self._attendre_prompt(sauf=x1["id"])                        # le retour de lecture arrive dans un 2e prompt
            self.assertFalse(x2["new"])
            self.assertIn("manuel", x2["short"])
            engine.manual_answer("u", x2["id"], "Terminé.")
            th.join(5)
        self.assertEqual(res["r"], "Terminé.")
        self.assertEqual(BR["agent/bob"]["m.txt"], "manuel")
        self.assertEqual(proj()["pending"], [])
        self.assertTrue(proj()["agents"][1]["session"])

    def test_mode_manuel_annule(self):
        st = store.load("u"); st["projects"][0]["agents"][1]["mode"] = "manual"; store.wj(store.state_path("u"), st)
        res = {}

        def run():
            try:
                engine.ask("u", "p1", "a2", "x", "Vous")
            except Err as e:
                res["e"] = e
        th = threading.Thread(target=run); th.start()
        engine.manual_cancel("u", self._attendre_prompt()["id"])
        th.join(5)
        self.assertEqual(res["e"].code, 408)
        self.assertEqual(proj()["pending"], [])
        self.assertEqual(proj()["agents"][1]["work"], 0)
        with self.assertRaises(Err):
            engine.manual_answer("u", "inconnu", "x")

    def test_cout_par_agent_et_global(self):
        st = store.load("u"); st["services"][0].update(pin="3", pout="15"); store.wj(store.state_path("u"), st)

        def fake(s, system, msgs):
            r = Reply("ok")
            r.usage = {"in": 1000, "out": 500}
            return r
        with mock.patch.object(engine, "call_ai", fake):
            engine.ask("u", "p1", "a1", "x", "Vous")
            engine.ask("u", "p1", "a2", "y", "Vous")
        p = proj()
        a1 = p["agents"][0]["usage"]
        self.assertEqual((a1["calls"], a1["in"], a1["out"]), (1, 1000, 500))
        self.assertAlmostEqual(a1["cost"], 0.0105, 6)             # 1000×3/1e6 + 500×15/1e6
        self.assertEqual(p["usage"]["calls"], 2)
        self.assertAlmostEqual(p["usage"]["cost"], 0.021, 6)
        self.assertEqual([e["agent"] for e in p["ledger"]], ["Alice", "Bob"])
        engine.reset_usage("u", "p1")
        self.assertEqual((proj()["usage"], proj()["ledger"], proj()["agents"][0]["usage"]), ({}, [], {}))

    def test_quota_suspend_puis_reprise_planifiee(self):
        def quota(s, system, msgs):
            raise Err(429, "quota", 7)
        t = {"id": "t9", "title": "x", "desc": "", "who": "a2", "status": "à faire", "modules": [], "parent": ""}
        st = store.load("u"); st["projects"][0]["tasks"].append(t); store.wj(store.state_path("u"), st)
        with mock.patch.object(engine, "call_ai", quota), mock.patch.object(engine.threading, "Timer") as T:
            engine.run_task("u", "p1", "t9")
        p = proj()
        self.assertEqual(p["agents"][1]["state"], "suspendu")
        self.assertEqual(p["tasks"][0]["status"], "à faire")
        self.assertIn("retry_at", p["tasks"][0])
        self.assertEqual(T.call_args[0][0], 7)                               # reprise planifiée au délai demandé
        self.assertFalse(p["tasks"][0]["running"])


class Http(unittest.TestCase):
    """Routes HTTP réelles : comptes, état révisionné avec fusion, travaux."""
    @classmethod
    def setUpClass(cls):
        import server
        cls.sv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=cls.sv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.sv.server_port

    @classmethod
    def tearDownClass(cls):
        cls.sv.shutdown()

    def call(self, m, path, body=None, tok=None):
        import urllib.request, urllib.error
        r = urllib.request.Request(self.url + path, json.dumps(body).encode() if body is not None else None,
                                   {"Authorization": "Bearer " + tok} if tok else {}, method=m)
        try:
            return json.load(urllib.request.urlopen(r))
        except urllib.error.HTTPError as e:
            return dict(json.load(e), _code=e.code)

    def test_comptes_etat_et_fusion(self):
        self.assertIn("token", self.call("POST", "/api/register", {"user": "h1", "password": "x"}))
        self.assertEqual(self.call("POST", "/api/register", {"user": "h1", "password": "x"})["_code"], 409)
        self.assertEqual(self.call("POST", "/api/login", {"user": "h1", "password": "bad"})["_code"], 401)
        t = self.call("POST", "/api/login", {"user": "h1", "password": "x"})["token"]
        r0 = self.call("GET", "/api/state", tok=t)
        self.assertEqual(r0["state"], {})
        r1 = self.call("PUT", "/api/state", {"rev": r0["rev"], "state": {"projects": [{"id": "p", "tasks": [{"id": "t1", "title": "a"}]}]}}, t)
        # le serveur (agent) crée une tâche pendant que le navigateur renomme t1 sans avoir reçu cette création
        store.mutate("h1", lambda st: st["projects"][0]["tasks"].append({"id": "t2", "title": "agent"}))
        r2 = self.call("PUT", "/api/state", {"rev": r1["rev"], "state": {"projects": [{"id": "p", "tasks": [{"id": "t1", "title": "b"}]}]}}, t)
        self.assertEqual([(x["id"], x["title"]) for x in r2["state"]["projects"][0]["tasks"]], [("t1", "b"), ("t2", "agent")])
        self.assertEqual(self.call("GET", "/api/state?ser=%d" % r2["ser"], tok=t), {"same": True})
        self.assertEqual(self.call("GET", "/api/state")["_code"], 401)

    def test_test_de_cle(self):
        import server
        self.call("POST", "/api/register", {"user": "h2", "password": "x"})
        t = self.call("POST", "/api/login", {"user": "h2", "password": "x"})["token"]
        r0 = self.call("GET", "/api/state", tok=t)
        self.call("PUT", "/api/state", {"rev": r0["rev"], "state": {"services": [{"id": "s1", "name": "S", "kind": "openai", "url": "x", "model": "m", "apiKey": ""}], "projects": []}}, t)
        r = self.call("POST", "/api/service/test", {"serviceId": "s1"}, t)          # sans clé : message actionnable
        self.assertEqual(r["_code"], 400)
        self.assertIn("Configurer la clé", r["error"])
        with mock.patch.object(server, "call_ai", lambda s, sy, m: "OK"):
            self.assertEqual(self.call("POST", "/api/service/test", {"serviceId": "s1"}, t)["sample"], "OK")


if __name__ == "__main__":
    unittest.main()
