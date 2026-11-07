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
                          "roles": [{"id": "r1", "name": "Chef de projet", "desc": "Coordonne"}, {"id": "r2", "name": "Développeur back-end", "desc": "Développe"}, {"id": "r3", "name": "Testeur QA", "desc": "Teste"}],
                          "agents": [ag("a1", "Alice", canBug=True), ag("a2", "Bob", role="r2", talk=["a1"])],
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
        self.assertEqual(p["tasks"], [])                                     # 1er niveau : soumis à validation (même pour le principal) ; Bob n'a pas pu créer « interdit »
        self.assertEqual([(d["kind"], d["payload"]["title"], d["status"]) for d in p["decisions"]], [("task", "Suivi hello", "attente")])
        engine.decide("u", "p1", p["decisions"][0]["id"], "accept")
        p = proj()
        self.assertEqual([(t["title"], t["who"], t["modules"], t["by"]) for t in p["tasks"]], [("Suivi hello", "a2", ["m1"], "Alice")])
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


class Droits(unittest.TestCase):
    """Capacités par rôle, validations, bugs, questions à l'utilisateur, instructions partagées, budget."""
    def setUp(self):
        reset()
        st = make_state()
        pr = st["projects"][0]
        pr["agents"].append({"id": "a3", "name": "Carol", "service": "s1", "role": "r3", "branch": "agent/carol", "canBug": False, "talk": [], "history": [], "notes": ""})
        pr["tasks"] = [{"id": "t1", "parent": "", "title": "Login", "desc": "", "who": "a2", "status": "en cours", "modules": [], "by": ""},
                       {"id": "t2", "parent": "", "title": "Autre", "desc": "", "who": "a1", "status": "à faire", "modules": [], "by": ""}]
        store.wj(store.state_path("u"), st)

    def act(self, aid, **x):
        return engine.do_act("u", "p1", aid, x, 0)

    def set(self, **kw):
        st = store.load("u"); st["projects"][0].update(kw); store.wj(store.state_path("u"), st)

    def test_sous_taches_selon_responsabilite(self):
        note, back = self.act("a2", do="task", title="Écrire l'API", parent="t1")            # sous sa tâche : libre
        self.assertIn("Tâche créée", note)
        self.assertIsNone(back)
        note, back = self.act("a2", do="task", title="Intrus", parent="t2")                   # tâche d'un autre : refusé
        self.assertIn("pas responsable", back)
        note, back = self.act("a1", do="task", title="Sous Autre", parent="t2")              # le principal : partout
        self.assertIn("Tâche créée", note)
        self.assertEqual([t["title"] for t in proj()["tasks"] if t.get("parent")], ["Écrire l'API", "Sous Autre"])
        self.assertIn("doublon", self.act("a2", do="task", title="écrire l'api", parent="t1")[1])

    def test_autonomie_du_projet(self):
        self.set(autonomy="prudent")
        self.assertIn("Proposition", self.act("a2", do="task", title="Sous prudent", parent="t1")[0])   # tout à valider
        self.assertEqual(proj()["decisions"][0]["kind"], "task")
        self.set(autonomy="auto")
        self.assertIn("Tâche créée", self.act("a1", do="task", title="Direct")[0])                       # 1er niveau libre en mode autonome

    def test_module_propose_puis_accepte(self):
        self.assertIn("refusée", self.act("a2", do="module", name="Paiement")[1])                          # un dév. ne crée pas de module
        self.assertIn("Proposition", self.act("a1", do="module", name="Paiement", desc="CB", why="besoin")[0])
        did = proj()["decisions"][0]["id"]
        engine.decide("u", "p1", did, "accept", fields={"name": "Paiements"})                              # modifié puis accepté
        self.assertEqual([m["name"] for m in proj()["modules"]], ["Auth", "Paiements"])
        self.assertEqual(proj()["decisions"][0]["status"], "acceptée")

    def test_cycle_de_vie_d_un_bug(self):
        note, _ = self.act("a2", do="bug", title="Crash login", sev="majeur", modules=["Auth"])
        p = proj()
        bug = p["bugs"][0]
        self.assertEqual((bug["status"], bug["modules"]), ("ouvert", ["m1"]))
        cont = [t for t in p["tasks"] if t.get("system")]
        self.assertEqual([t["title"] for t in cont], ["Corrections : Auth"])                               # conteneur créé sans validation
        note, _ = self.act("a1", do="bug_fix", bug=bug["id"], title="Corriger le crash", assignee="Bob")
        fix = [t for t in proj()["tasks"] if t.get("bug")][0]
        self.assertEqual((fix["parent"], fix["who"], proj()["bugs"][0]["status"]), (cont[0]["id"], "a2", "en correction"))
        self.assertIn("corrigé", self.act("a2", do="task_status", id=fix["id"], status="terminée")[0])    # tâche finie -> bug corrigé
        self.assertEqual(proj()["bugs"][0]["status"], "corrigé")
        self.assertIn("refusé", self.act("a2", do="bug_status", id=bug["id"], status="vérifié")[1])       # un dév. ne vérifie pas
        self.assertIn("vérifié", self.act("a3", do="bug_status", id=bug["id"], status="vérifié")[0])      # le QA oui
        self.assertEqual(proj()["bugs"][0]["status"], "vérifié")

    def test_question_bloquante_et_reponse(self):
        self.act("a2", do="ask_user", question="REST ou GraphQL ?", options=["REST", "GraphQL"], recommendation="REST", impact="coût", structural=True, task="t1")
        p = proj()
        d = p["decisions"][0]
        self.assertEqual((p["tasks"][0]["status"], p["tasks"][0]["blocked"], d["kind"], d["status"]), ("en attente", d["id"], "question", "attente"))
        vus = []

        def fake(s, system, msgs):
            vus.append((system, msgs[-1]["content"]))
            return "Compris."
        with mock.patch.object(engine, "call_ai", fake):
            engine.decide("u", "p1", d["id"], "answer", answer="REST")
        p = proj()
        self.assertEqual((p["decisions"][0]["status"], p["decisions"][0]["answer"], p["tasks"][0]["status"]), ("répondue", "REST", "en cours"))
        self.assertIn("REST", vus[0][1])                                                                  # l'agent reçoit la réponse
        self.assertTrue(any("REST ou GraphQL" in i["text"] for i in p["instructions"]))                   # décision structurante -> instructions

    def test_refus_renvoye_a_l_agent(self):
        self.act("a1", do="module", name="X", why="y")
        vus = []
        with mock.patch.object(engine, "call_ai", lambda s, sy, m: vus.append(m[-1]["content"]) or "ok"):
            engine.decide("u", "p1", proj()["decisions"][0]["id"], "refuse", note="pas maintenant")
        self.assertEqual(proj()["decisions"][0]["status"], "refusée")
        self.assertTrue("refusé" in vus[0] and "pas maintenant" in vus[0])
        with self.assertRaises(Err):
            engine.decide("u", "p1", proj()["decisions"][0]["id"], "accept")                              # déjà traitée

    def test_avancement_sans_ia(self):
        with mock.patch.object(engine, "call_ai", lambda *a: self.fail("aucun appel IA attendu")):
            back = self.act("a1", do="get_progress", agent="Bob")[1]
        self.assertIn("Bob", back)
        self.assertIn("1 en cours", back)

    def test_instructions_et_specs_partagees(self):
        self.assertIn("refusé", self.act("a2", do="instruction", text="x")[1])                              # réservé au principal
        self.act("a1", do="instruction", text="Toujours en Python 3.12", scope="global")
        self.act("a1", do="instruction", text="Style strict", scope="role:Développeur back-end")
        self.act("a1", do="spec", text="# Cahier des charges\nLogin")
        p = proj()
        bob, carol = p["agents"][1], p["agents"][2]
        sb, sc = engine.sys_prompt(p, bob), engine.sys_prompt(p, carol)
        self.assertIn("Toujours en Python 3.12", sb)
        self.assertIn("Style strict", sb)
        self.assertNotIn("Style strict", sc)                                                              # portée « rôle »
        self.assertIn("Cahier des charges", sc)                                                           # spécifications partagées
        self.assertIn("Nouveautés depuis ta dernière activité", sb)
        with mock.patch.object(engine, "call_ai", lambda *a: "ok"):
            engine.ask("u", "p1", "a2", "salut", "Vous")
        self.assertNotIn("Nouveautés depuis", engine.sys_prompt(proj(), proj()["agents"][1]))              # vu : plus signalé

    def test_plafond_de_budget(self):
        self.set(budget="0.01", usage={"calls": 1, "in": 1, "out": 1, "cost": 0.02})
        with mock.patch.object(engine, "call_ai", lambda *a: self.fail("pas d'appel au-delà du plafond")):
            with self.assertRaises(Err) as c:
                engine.ask("u", "p1", "a2", "x", "Vous")
        self.assertEqual(c.exception.code, 402)

    def test_limite_de_creations_par_echange(self):
        ctx = {"n": 0}
        for i in range(engine.MAX_CREATE):
            engine.do_act("u", "p1", "a2", {"do": "task", "title": "T%d" % i, "parent": "t1"}, 0, ctx)
        self.assertIn("Trop de créations", engine.do_act("u", "p1", "a2", {"do": "task", "title": "T9", "parent": "t1"}, 0, ctx)[1])


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
