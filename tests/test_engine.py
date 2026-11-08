"""Tests du moteur d'agents : faux GitHub local + IA scriptée (aucun réseau, aucune clé)."""
import base64
import hashlib
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

COMMITS, BRANCHES, TREES, BLOBS, PRS, REL, CNT = {}, {}, {}, {}, {}, [], [0]


def _hash(s):
    return hashlib.sha1(s.encode()).hexdigest()


def mk_commit(files, parents, msg="c", tree=None):
    files = dict(files)
    if tree is None:
        tree = _hash(json.dumps(files, sort_keys=True))
        TREES[tree] = files
    CNT[0] += 1
    sha = _hash("%s|%s|%d" % (tree, parents, CNT[0]))
    COMMITS[sha] = {"tree": tree, "parents": list(parents), "msg": msg}
    return sha


def files_of(sha):
    return TREES[COMMITS[sha]["tree"]]


def reset():
    for d in (COMMITS, BRANCHES, TREES, BLOBS, PRS):
        d.clear()
    REL.clear()
    BRANCHES["main"] = mk_commit({"README.md": "# demo"}, [], "init")


class _BR:
    """Vue en lecture seule : nom de branche -> fichiers courants."""
    def __getitem__(self, b):
        return files_of(BRANCHES[b])

    def __contains__(self, b):
        return b in BRANCHES


BR = _BR()


def ancestors(sha):
    seen, todo = set(), [sha]
    while todo:
        x = todo.pop()
        if x not in seen:
            seen.add(x)
            todo += COMMITS[x]["parents"]
    return seen


def merge_base(a, b):
    common = ancestors(a) & ancestors(b)
    return next(c for c in common if not any(c in ancestors(o) and o != c for o in common))


def three_way(b, o, t):
    out, conf = {}, []
    for k in set(b) | set(o) | set(t):
        bv, ov, tv = b.get(k), o.get(k), t.get(k)
        if ov == tv:
            v = ov
        elif bv == ov:
            v = tv
        elif bv == tv:
            v = ov
        else:
            conf.append(k)
            continue
        if v is not None:
            out[k] = v
    return out, conf


def git_merge(into, head_sha):
    """Fusion `head_sha` dans la branche `into` : 'noop', 'merged' ou 'conflict'."""
    cur = BRANCHES[into]
    if head_sha in ancestors(cur):
        return "noop"
    if cur in ancestors(head_sha):
        BRANCHES[into] = head_sha
        return "merged"
    mb = merge_base(cur, head_sha)
    files, conf = three_way(files_of(mb), files_of(cur), files_of(head_sha))
    if conf:
        return "conflict"
    BRANCHES[into] = mk_commit(files, [cur, head_sha], "merge")
    return "merged"


def resolve_ref(ref):
    return BRANCHES.get(ref) or (ref if ref in COMMITS else None)


class GH(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj=None):
        b = json.dumps(obj if obj is not None else {}).encode()
        self.send_response(code)
        if code == 204:
            self.end_headers()
            return
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def handle_any(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else {}
        rest, m = re.match(r"/repos/o/r(/.*)", unquote(u.path)).group(1), self.command
        if rest.startswith("/git/ref/heads/") and m == "GET":
            b = rest[len("/git/ref/heads/"):]
            return self.send(200, {"object": {"sha": BRANCHES[b]}}) if b in BRANCHES else self.send(404, {"message": "Not Found"})
        if rest.startswith("/git/refs/heads/") and m == "PATCH":
            BRANCHES[rest[len("/git/refs/heads/"):]] = body["sha"]
            return self.send(200, {})
        if rest == "/git/refs" and m == "POST":
            BRANCHES[body["ref"][len("refs/heads/"):]] = body["sha"]
            return self.send(201, {})
        if rest == "/git/blobs":
            h = _hash(body["content"])
            BLOBS[h] = body["content"]
            return self.send(201, {"sha": h})
        if rest == "/git/trees":
            files = dict(TREES[body["base_tree"]])
            for e in body["tree"]:
                if e["sha"] is None:
                    files.pop(e["path"], None)
                else:
                    files[e["path"]] = BLOBS[e["sha"]]
            tree = _hash(json.dumps(files, sort_keys=True))
            TREES[tree] = files
            return self.send(201, {"sha": tree})
        if rest == "/git/commits":
            return self.send(201, {"sha": mk_commit({}, body["parents"], body["message"], tree=body["tree"])})
        mm = re.match(r"/git/commits/(\w+)", rest)
        if mm:
            return self.send(200, {"tree": {"sha": COMMITS[mm.group(1)]["tree"]}})
        mm = re.match(r"/compare/(.+)\.\.\.(.+)", rest)
        if mm:
            a, b = resolve_ref(mm.group(1)), resolve_ref(mm.group(2))
            if not a or not b:
                return self.send(404, {"message": "Not Found"})
            mb = merge_base(a, b)
            fb, fh = files_of(mb), files_of(b)
            files = []
            for k in sorted(set(fb) | set(fh)):
                if fb.get(k) != fh.get(k):
                    if k in fh:
                        BLOBS[_hash(fh[k])] = fh[k]
                    files.append({"filename": k, "status": "removed" if k not in fh else ("added" if k not in fb else "modified"), "sha": _hash(fh.get(k, ""))})
            mine = ancestors(b) - ancestors(a)
            return self.send(200, {"merge_base_commit": {"sha": mb}, "files": files, "ahead_by": len(mine), "behind_by": len(ancestors(a) - ancestors(b)),
                                   "commits": [{"sha": c, "commit": {"message": COMMITS[c]["msg"]}} for c in mine if len(COMMITS[c]["parents"]) < 2]})
        if rest == "/merges":
            if body["base"] not in BRANCHES or body["head"] not in BRANCHES:
                return self.send(404, {"message": "Not Found"})
            r = git_merge(body["base"], BRANCHES[body["head"]])
            return self.send({"noop": 204, "merged": 201, "conflict": 409}[r], {"sha": BRANCHES[body["base"]]} if r == "merged" else {"message": "Merge conflict"})
        if rest.startswith("/contents"):
            path = rest[len("/contents"):].lstrip("/")
            ref = (q.get("ref") or [None])[0] or body.get("branch")
            sha = resolve_ref(ref)
            if m == "GET":
                if sha is None:
                    return self.send(404, {})
                files = files_of(sha)
                if path in files:
                    return self.send(200, {"type": "file", "sha": "x", "encoding": "base64", "content": base64.b64encode(files[path].encode()).decode()})
                pre = path + "/" if path else ""
                names = sorted({k[len(pre):].split("/")[0] for k in files if k.startswith(pre)})
                if not names:
                    return self.send(404, {})
                return self.send(200, [{"name": x, "type": "dir" if any(k.startswith(pre + x + "/") for k in files) else "file", "size": len(files.get(pre + x, ""))} for x in names])
            files = dict(files_of(BRANCHES[ref]))
            if m == "PUT":
                files[path] = base64.b64decode(body["content"]).decode()
            else:
                files.pop(path, None)
            BRANCHES[ref] = mk_commit(files, [BRANCHES[ref]], body.get("message", "c"))
            return self.send(200, {"commit": {"sha": BRANCHES[ref]}})
        if rest == "/pulls" and m == "POST":
            if BRANCHES[body["head"]] in ancestors(BRANCHES[body["base"]]):
                return self.send(422, {"message": "Validation Failed: No commits between"})
            if any(x["head"] == body["head"] and x["base"] == body["base"] and x["open"] for x in PRS.values()):
                return self.send(422, {"message": "A pull request already exists"})
            PRS[len(PRS) + 1] = {"head": body["head"], "base": body["base"], "open": True}
            return self.send(201, {"number": len(PRS)})
        if rest == "/pulls" and m == "GET":
            return self.send(200, [{"number": k} for k, x in PRS.items() if x["open"] and x["head"] == (q.get("head") or [":"])[0].split(":", 1)[1] and x["base"] == q["base"][0]])
        mm = re.match(r"/pulls/(\d+)/merge", rest)
        if mm:
            pr = PRS[int(mm.group(1))]
            r = git_merge(pr["base"], BRANCHES[pr["head"]])
            if r == "conflict":
                return self.send(405, {"message": "Pull Request is not mergeable"})
            pr["open"] = False
            return self.send(200, {"merged": True})
        if rest == "/releases":
            REL.append(body)
            return self.send(201, {"html_url": "http://x/release"})
        self.send(404, {"message": "?"})

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = handle_any


srv = HTTPServer(("127.0.0.1", 0), GH)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["APM_GITHUB_API"] = "http://127.0.0.1:%d" % srv.server_port
os.environ["APM_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai, engine, github, store  # noqa: E402
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
        self.sp = mock.patch.object(engine, "spawn", lambda *a, **k: None)   # pas de thread d'arrière-plan dans les tests
        self.sp.start()
        engine.TIMERS.clear()

    def tearDown(self):
        self.p.stop()
        self.sp.stop()

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

        self.spawned = []
        self.sp = mock.patch.object(engine, "spawn", lambda fn, *a, **k: self.spawned.append((fn.__name__, a, k)))
        self.sp.start()
        engine.TIMERS.clear()

    def tearDown(self):
        self.sp.stop()

    def act(self, aid, **x):
        return engine.do_act("u", "p1", aid, x, 0)

    def set_task(self, tid, **kw):
        st = store.load("u")
        next(x for x in st["projects"][0]["tasks"] if x["id"] == tid).update(kw)
        store.wj(store.state_path("u"), st)

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
        self.assertIn("à vérifier", self.act("a2", do="task_status", id=fix["id"], status="terminée")[0])  # le dév. ne clôture pas lui-même
        self.assertEqual(proj()["bugs"][0]["status"], "en correction")
        self.assertIn("corrigé", self.act("a1", do="task_review", id=fix["id"], verdict="accepted", comment="ok")[0])  # vérifiée -> bug corrigé
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
        self.assertEqual((p["decisions"][0]["status"], p["decisions"][0]["answer"], p["tasks"][0]["status"]), ("répondue", "REST", "à vérifier"))   # l'agent a repris sa tâche seul puis l'a soumise
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


    # ---------- vérification des tâches, relances et suivi ----------
    def test_fin_de_tache_passe_a_verifier(self):
        with mock.patch.object(engine, "call_ai", lambda *a: "Livrable prêt."):
            engine.run_task("u", "p1", "t1")
        t = proj()["tasks"][0]
        self.assertEqual((t["status"], t["result"], t["running"]), ("à vérifier", "Livrable prêt.", False))   # jamais « terminée » d'office
        self.assertEqual([s[0] for s in self.spawned], ["review_task"])

    def test_un_agent_ne_cloture_pas_lui_meme(self):
        self.assertIn("à vérifier", self.act("a2", do="task_status", id="t1", status="terminée")[0])
        self.assertEqual(proj()["tasks"][0]["status"], "à vérifier")
        self.assertIn("refusée", self.act("a2", do="task_review", id="t1", verdict="accepted")[1])      # un dév. ne vérifie pas
        self.set_task("t2", status="à vérifier")
        self.assertIn("propre tâche", self.act("a1", do="task_review", id="t2", verdict="accepted")[1])  # ni sa propre tâche
        self.assertIn("Verdict invalide", self.act("a3", do="task_review", id="t1", verdict="rejected")[1])  # refus sans motif

    def test_verification_par_l_agent_principal(self):
        self.act("a2", do="task_status", id="t1", status="à vérifier", note="Fait : API login")
        vus = []

        def fake(s, system, msgs):
            vus.append(msgs[-1]["content"])
            return 'Je vérifie.\n```action\n{"do":"task_review","id":"t1","verdict":"accepted","comment":"conforme"}\n```'
        with mock.patch.object(engine, "call_ai", fake):
            engine.review_task("u", "p1", "t1")
        t = proj()["tasks"][0]
        self.assertEqual((t["status"], t["reviewed"]["by"], t.get("reviewing")), ("terminée", "Alice", None))
        self.assertTrue("Aucune écriture" in vus[0] and '"agent":"Bob"' in vus[0] and "Fait : API login" in vus[0])   # preuves + consigne de relire les fichiers

    def test_rejet_relance_puis_escalade(self):
        for i in range(2):
            self.set_task("t1", status="à vérifier")
            engine.apply_review("u", "p1", "t1", "Alice", "rejected", "Il manque les tests")
            t = proj()["tasks"][0]
            self.assertEqual((t["status"], t["review_note"], t["reviews"]), ("à faire", "Il manque les tests", i + 1))
        runs = [s for s in self.spawned if s[0] == "run_task"]
        self.assertEqual(len(runs), 2)
        self.assertIn("Il manque les tests", runs[0][1][4])                                            # l'agent reçoit le motif
        self.set_task("t1", status="à vérifier")
        engine.apply_review("u", "p1", "t1", "Alice", "rejected", "Toujours pas")
        d = proj()["decisions"][-1]
        self.assertEqual((proj()["tasks"][0]["status"], d["escalation"], d["task"]), ("en attente", True, "t1"))   # 3e refus : l'utilisateur décide

    def test_preuves_de_travail(self):
        st = store.load("u"); pr = st["projects"][0]
        pr["tasks"][0]["started"] = 1000
        pr["commits"] = [{"m": "feat: login", "by": "Bob", "ts": 2000, "sha": "abc1234", "d": ""},
                         {"m": "chore(Bob) : contexte agent", "by": "Bob", "ts": 2000, "sha": "x", "d": ""},
                         {"m": "ancien", "by": "Bob", "ts": 500, "sha": "y", "d": ""}]
        store.wj(store.state_path("u"), st)
        txt = engine.evidence(proj(), proj()["tasks"][0], proj()["agents"][1])
        self.assertTrue("1 commit(s)" in txt and "feat: login" in txt and "ancien" not in txt)

    def test_classification_et_delais(self):
        c = engine.classify
        self.assertEqual([c(Err(429, "q")), c(Err(402, "b")), c(Err(400, "Clé API manquante")), c(Err(502, "Erreur du service IA (401) : x")),
                          c(Err(502, "Modèle ou URL introuvable (404) : x")), c(Err(502, "Service IA injoignable : x")),
                          c(Err(502, "Erreur du service IA (503) : x")), c(Err(502, "Erreur du service IA (400) : x"))],
                         ["quota", "budget", "auth", "auth", "model", "temp", "temp", "other"])
        self.assertEqual([engine.backoff(0), engine.backoff(0, 7200), engine.backoff(1), engine.backoff(2), engine.backoff(3), engine.backoff(9)],
                         [60, 3600, 300, 900, 1800, 3600])

    def test_erreur_temporaire_relancee_puis_remontee(self):
        def down(*a):
            raise Err(502, "Service IA injoignable : timeout")
        with mock.patch.object(engine, "call_ai", down), mock.patch.object(engine.threading, "Timer") as T:
            engine.run_task("u", "p1", "t1")
        t = proj()["tasks"][0]
        self.assertEqual((t["retries"], "fail" in t, t["status"], T.call_args[0][0]), (1, False, "à faire", 60))
        engine.TIMERS.clear()
        self.set_task("t1", retries=engine.MAX_TEMP)
        with mock.patch.object(engine, "call_ai", down):
            engine.run_task("u", "p1", "t1")
        t = proj()["tasks"][0]
        self.assertEqual((t["fail"]["kind"], "retry_at" in t), ("temp", False))                        # trop d'échecs : on remonte

    def test_quota_reessaye_toutes_les_heures(self):
        def quota(*a):
            raise Err(429, "quota", 60)
        self.set_task("t1", retries=4)
        with mock.patch.object(engine, "call_ai", quota), mock.patch.object(engine.threading, "Timer") as T:
            engine.run_task("u", "p1", "t1")
        self.assertEqual((proj()["tasks"][0]["retries"], T.call_args[0][0]), (5, 3600))                # plus aucune escalade pour un simple quota
        self.assertNotIn("fail", proj()["tasks"][0])

    def test_suivi_budget_remonte_a_l_utilisateur(self):
        self.set_task("t1", status="à faire", fail={"kind": "budget", "msg": "Plafond de budget atteint", "ts": 1})
        with mock.patch.object(engine, "call_ai", lambda *a: self.fail("pas d'appel IA pour un plafond atteint")):
            engine.tick("u", "p1")
        p = proj()
        d = p["decisions"][0]
        self.assertEqual((d["escalation"], d["task"], p["tasks"][0]["status"], p["tasks"][0]["blocked"]), (True, "t1", "en attente", d["id"]))
        self.assertEqual(d["options"][0], "Relancer la tâche")

    def test_suivi_interroge_le_principal_puis_escalade(self):
        self.set_task("t1", status="à faire", fail={"kind": "other", "msg": "réponse incohérente", "ts": 1})
        vus = []
        with mock.patch.object(engine, "call_ai", lambda s, sy, m: vus.append(m[-1]["content"]) or "Je regarde."):
            engine.tick("u", "p1")
        self.assertTrue(len(vus) == 1 and "semblent bloquées" in vus[0] and "Login" in vus[0] and "relaunch" in vus[0])
        self.assertEqual(proj()["tasks"][0]["sup_n"], 1)
        with mock.patch.object(engine, "call_ai", lambda *a: self.fail("délai de carence : pas de nouvel appel")):
            engine.tick("u", "p1")
        self.assertEqual(proj().get("decisions", []), [])
        self.set_task("t1", sup_n=2)                                                                    # déjà relancé deux fois sans succès
        with mock.patch.object(engine, "call_ai", lambda *a: self.fail("escalade directe")):
            engine.tick("u", "p1")
        self.assertTrue(proj()["decisions"][0]["escalation"])

    def test_suivi_tache_inactive(self):
        self.set_task("t1", status="en cours", started=1, touched=time.time() - 4000)
        vus = []
        with mock.patch.object(engine, "call_ai", lambda s, sy, m: vus.append(m[-1]["content"]) or "ok"):
            engine.tick("u", "p1")
        self.assertIn("aucune activité", vus[0])

    def test_suivi_relances_et_verifications_planifiees(self):
        self.set_task("t1", status="à faire", retry_at=time.time() + 100)
        self.set_task("t2", status="à vérifier")
        recorded = []
        with mock.patch.object(engine, "schedule", lambda u, pid, tid, w: recorded.append((tid, round(w)))):
            engine.tick("u", "p1")
        self.assertEqual(recorded[0][0], "t1")
        self.assertTrue(90 <= recorded[0][1] <= 100)
        self.assertIn(("review_task", ("u", "p1", "t2"), {}), self.spawned)

    def test_suivi_desactive(self):
        self.set(supervision="off")
        self.set_task("t1", status="à faire", fail={"kind": "budget", "msg": "x", "ts": 1})
        engine.tick("u", "p1")
        self.assertEqual(proj().get("decisions", []), [])

    def test_principal_manuel_escalade_sans_ia(self):
        st = store.load("u"); st["projects"][0]["agents"][0]["mode"] = "manual"; store.wj(store.state_path("u"), st)
        self.set_task("t1", status="à faire", fail={"kind": "other", "msg": "bizarre", "ts": 1})
        with mock.patch.object(engine, "call_ai", lambda *a: self.fail("le principal est en chat manuel")):
            engine.tick("u", "p1")
        self.assertTrue(proj()["decisions"][0]["escalation"])

    def test_validation_par_l_utilisateur_sans_verificateur(self):
        st = store.load("u")
        for ag in st["projects"][0]["agents"]:
            if ag["id"] in ("a1", "a3"):
                ag["mode"] = "manual"
        store.wj(store.state_path("u"), st)
        self.set_task("t1", status="à vérifier", result="Terminé selon l'agent")
        engine.review_task("u", "p1", "t1")
        engine.review_task("u", "p1", "t1")                                                             # pas de doublon de question
        d = proj()["decisions"]
        self.assertEqual((len(d), d[0]["review"], proj()["tasks"][0]["status"]), (1, "t1", "à vérifier"))
        engine.decide("u", "p1", d[0]["id"], "answer", answer="Oui, la valider")
        t = proj()["tasks"][0]
        self.assertEqual((t["status"], t["reviewed"]["by"]), ("terminée", "Vous"))

    def test_reponses_a_une_escalade(self):
        def escalade():
            self.set_task("t1", status="à faire", fail={"kind": "budget", "msg": "x", "ts": 1}, blocked=None)
            engine.tick("u", "p1")
            return proj()["decisions"][-1]
        d = escalade()
        engine.decide("u", "p1", d["id"], "answer", answer=d["options"][1])                            # pause
        self.assertEqual(proj()["tasks"][0]["status"], "en attente")
        d = escalade()
        vus = []
        with mock.patch.object(engine, "call_ai", lambda s, sy, m: vus.append(m[-1]["content"]) or "Fait."):
            engine.decide("u", "p1", d["id"], "answer", answer="Utilise plutôt SQLite")                 # relance avec consigne libre
        self.assertIn("Utilise plutôt SQLite", vus[0])
        self.assertEqual(proj()["tasks"][0]["status"], "à vérifier")
        d = escalade()
        engine.decide("u", "p1", d["id"], "answer", answer=d["options"][2])                            # abandon
        t = proj()["tasks"][0]
        self.assertEqual((t["status"], t["abandoned"]), ("terminée", True))

    def test_relance_par_le_principal(self):
        self.assertIn("Relance", self.act("a1", do="relaunch", task="t1", note="Reprends avec SQLite")[0])
        self.assertEqual([s[0] for s in self.spawned], ["run_task"])
        self.assertIn("réservée", self.act("a2", do="relaunch", task="t1")[1])
        for _ in range(engine.MAX_RELAUNCH):
            self.act("a1", do="relaunch", task="t1")
        self.assertIn("Trop de relances", self.act("a1", do="relaunch", task="t1")[1])

    def test_lecture_de_la_branche_d_un_autre_agent(self):
        engine.repo_act("u", "p1", proj(), proj()["agents"][1], {"path": "api.py", "content": "print(1)", "message": "feat"}, "write_file")
        _, back = engine.repo_act("u", "p1", proj(), proj()["agents"][0], {"path": "api.py", "agent": "Bob"}, "read_file")
        self.assertIn("print(1)", back)
        self.assertIn("Agent inconnu", engine.repo_act("u", "p1", proj(), proj()["agents"][0], {"path": "api.py", "agent": "Zed"}, "read_file")[1])
        self.assertIn("introuvable", engine.repo_act("u", "p1", proj(), proj()["agents"][0], {"path": "api.py"}, "read_file")[1])   # sa propre branche


class Integration(unittest.TestCase):
    """Release avec branche de travail : pull, merge requests validées par l'agent principal, conflits, dépendances."""
    def setUp(self):
        reset()
        st = make_state()
        pr = st["projects"][0]
        pr["agents"].append({"id": "a3", "name": "Carol", "service": "s1", "role": "r3", "branch": "agent/carol", "canBug": False, "talk": [], "history": [], "notes": ""})
        pr["tasks"] = [{"id": "t1", "parent": "", "title": "Login", "desc": "", "who": "a2", "status": "en cours", "modules": [], "by": ""},
                       {"id": "t3", "parent": "", "title": "Suite", "desc": "", "who": "a2", "status": "à faire", "modules": [], "by": "", "deps": ["t1"]}]
        store.wj(store.state_path("u"), st)
        self.spawned = []
        self.sp = mock.patch.object(engine, "spawn", lambda fn, *a, **k: self.spawned.append((fn.__name__, a, k)))
        self.sp.start()
        engine.TIMERS.clear()

    def tearDown(self):
        self.sp.stop()

    # -- aides
    def start(self):
        engine.cycle_start("u", "p1", "v1.0.0")

    def write(self, idx, path, content):
        return engine.repo_act("u", "p1", proj(), proj()["agents"][idx], {"path": path, "content": content, "message": "feat: " + path}, "write_file")

    def on_release(self, path, content):
        github.Repo(proj()).put_file(proj()["cycle"]["branch"], path, content, "chore: " + path)

    def conflict_setup(self):
        self.start()
        self.on_release("app.py", "v0")
        self.write(1, "app.py", "version Bob")
        self.on_release("app.py", "version release")

    def set(self, **kw):
        st = store.load("u"); st["projects"][0].update(kw); store.wj(store.state_path("u"), st)

    def logs(self):
        return " ¦ ".join(m["text"] for m in proj()["log"])

    def resolver(self, who):
        """IA scriptée : renvoie resolve_merge avec le contenu fusionné ; vérifie que les 3 versions sont fournies."""
        def fake(s, system, msgs):
            last = msgs[-1]["content"]
            self.assertTrue("version Bob" in last and "version release" in last and "v0" in last)
            cid = re.search(r"\(id (\w+)\)", last).group(1)
            return "Je fusionne.\n```action\n%s\n```" % json.dumps({"do": "resolve_merge", "conflict": cid, "files": [{"path": "app.py", "content": "version Bob + release"}], "message": "merge"})
        return fake

    # -- release et branches
    def test_demarrer_une_release(self):
        self.start()
        p = proj()
        self.assertEqual((p["cycle"]["branch"], p["cycle"]["version"]), ("release/v1.0.0", "v1.0.0"))
        self.assertEqual(BR["release/v1.0.0"], BR["main"])
        engine.cycle_start("u", "p1", "v2")                                                        # une seule release à la fois
        self.assertEqual(proj()["cycle"]["version"], "v1.0.0")
        self.assertIn("déjà en cours", self.logs())
        engine.provision("u", "p1", "a2")                                                          # branche d'agent créée depuis la branche de travail
        self.assertEqual(set(BR["agent/bob"]) - {".agents/Bob.md"}, set(BR["release/v1.0.0"]))

    def test_branches_protegees(self):
        self.start()
        for protegee in ("release/v1.0.0", "main"):
            st = store.load("u"); st["projects"][0]["agents"][1]["branch"] = protegee; store.wj(store.state_path("u"), st)
            self.assertIn("refusée", self.write(1, "x.py", "1")[0])
            self.assertIn("refusé", engine.provision("u", "p1", "a2"))
        self.assertNotIn("x.py", BR["main"])

    def test_pull(self):
        self.assertEqual(engine.pull("u", "p1", "a2")[0], "skip")                                  # pas de release en cours
        self.start()
        engine.provision("u", "p1", "a2")
        self.on_release("shared.py", "x = 1")
        self.assertEqual(engine.pull("u", "p1", "a2")[0], "merged")
        self.assertEqual(BR["agent/bob"]["shared.py"], "x = 1")
        self.assertEqual(engine.pull("u", "p1", "a2")[0], "uptodate")

    # -- merge requests
    def test_pas_de_release_pas_de_merge_request(self):
        st, msg = engine.submit_mr("u", "p1", "a2", "x")
        self.assertEqual(st, "skip")
        self.assertIn("Aucune release en cours", msg)

    def test_merge_request_validee_par_l_agent_principal(self):
        self.start()
        self.write(1, "api.py", "print(1)")
        st, _ = engine.submit_mr("u", "p1", "a2", "Login", "corps", "t1")
        m = proj()["mrs"][0]
        self.assertEqual((st, m["status"], m["from"], m["to"], m["task"]), ("submitted", "ouverte", "agent/bob", "release/v1.0.0", "t1"))
        self.assertIn(("review_mr", ("u", "p1", m["id"]), {}), self.spawned)
        self.assertEqual(engine.submit_mr("u", "p1", "a2", "Login")[0], "exists")                   # pas de doublon
        self.assertNotIn("api.py", BR["release/v1.0.0"])                                           # rien n'est fusionné avant validation
        self.set_task_status("t1", "terminée")
        vus = []

        def fake(s, system, msgs):
            vus.append(msgs[-1]["content"])
            return 'Je valide.\n```action\n{"do":"mr_review","id":"%s","verdict":"approved","comment":"ok"}\n```' % m["id"]
        with mock.patch.object(engine, "call_ai", fake):
            engine.review_mr("u", "p1", m["id"])
        m = proj()["mrs"][0]
        self.assertEqual(m["status"], "fusionnée")
        self.assertEqual(BR["release/v1.0.0"]["api.py"], "print(1)")
        self.assertNotIn("api.py", BR["main"])                                                     # la branche principale reste intacte
        self.assertTrue("api.py" in vus[0] and "feat: api.py" in vus[0] and "Login" in vus[0])    # le validateur voit fichiers, commits, tâche
        self.assertIn("débloquées", self.logs())                                                   # la tâche dépendante « Suite » est débloquée

    def set_task_status(self, tid, status):
        st = store.load("u")
        next(x for x in st["projects"][0]["tasks"] if x["id"] == tid)["status"] = status
        store.wj(store.state_path("u"), st)

    def test_merge_request_refusee(self):
        self.start()
        self.write(1, "api.py", "print(1)")
        engine.submit_mr("u", "p1", "a2", "Login")
        mid = proj()["mrs"][0]["id"]
        self.assertIn("refusée", engine.apply_mr_review("u", "p1", mid, "Alice", "rejected", "il manque les tests"))
        self.assertEqual((proj()["mrs"][0]["status"], proj()["mrs"][0]["note"]), ("refusée", "il manque les tests"))
        ask = [s for s in self.spawned if s[0] == "ask"][0]
        self.assertTrue(ask[1][2] == "a2" and "il manque les tests" in ask[1][3])                  # l'auteur reçoit le motif
        self.assertNotIn("api.py", BR["release/v1.0.0"])

    def test_droits_de_validation(self):
        self.start()
        self.write(1, "api.py", "print(1)")
        engine.submit_mr("u", "p1", "a2", "Login")
        mid = proj()["mrs"][0]["id"]
        do = lambda aid, **x: engine.do_act("u", "p1", aid, x, 0)
        self.assertIn("refusée pour ton rôle", do("a2", do="mr_review", id=mid, verdict="approved")[1])   # un dév. ne valide pas
        self.assertIn("Verdict invalide", do("a1", do="mr_review", id=mid, verdict="rejected")[1])        # refus sans motif
        self.set(mrs=[dict(proj()["mrs"][0], by_id="a1")])
        self.assertIn("propre merge request", do("a1", do="mr_review", id=mid, verdict="approved")[1])

    def test_validation_par_l_utilisateur_sans_validateur(self):
        st = store.load("u")
        for ag in st["projects"][0]["agents"]:
            if ag["id"] in ("a1", "a3"):
                ag["mode"] = "manual"
        store.wj(store.state_path("u"), st)
        self.start()
        self.write(1, "api.py", "print(1)")
        engine.submit_mr("u", "p1", "a2", "Login")
        mid = proj()["mrs"][0]["id"]
        engine.review_mr("u", "p1", mid)
        engine.review_mr("u", "p1", mid)                                                           # pas de doublon de question
        d = proj()["decisions"]
        self.assertEqual((len(d), d[0]["mr_review"]), (1, mid))
        engine.decide("u", "p1", d[0]["id"], "answer", answer="Oui, fusionner")
        self.assertEqual(proj()["mrs"][0]["status"], "fusionnée")
        self.assertEqual(BR["release/v1.0.0"]["api.py"], "print(1)")

    # -- dépendances
    def test_dependances_entre_taches(self):
        with mock.patch.object(engine, "call_ai", lambda *a: self.fail("tâche non lancée : pas d'appel IA")):
            engine.run_task("u", "p1", "t3")
        t3 = proj()["tasks"][1]
        self.assertEqual(t3["waiting"], ["Login"])
        self.assertIn("attend", self.logs())
        self.set_task_status("t1", "terminée")
        self.set(mrs=[{"id": "m1", "n": 1, "task": "t1", "status": "ouverte", "from": "x", "to": "y", "by": "Bob", "by_id": "a2"}])
        self.assertEqual(engine.deps_unmet(proj(), proj()["tasks"][1]), ["Login"])                  # sa merge request n'est pas encore fusionnée
        self.set(mrs=[{"id": "m1", "n": 1, "task": "t1", "status": "fusionnée", "from": "x", "to": "y", "by": "Bob", "by_id": "a2"}])
        self.assertEqual(engine.deps_unmet(proj(), proj()["tasks"][1]), [])

    def test_creation_de_tache_avec_dependances(self):
        note, _ = engine.do_act("u", "p1", "a1", {"do": "task", "title": "Suite 2", "parent": "t1", "depends_on": ["t1", "inconnue"]}, 0)
        self.assertIn("Tâche créée", note)
        self.assertEqual(proj()["tasks"][-1]["deps"], ["t1"])

    def test_tache_pull_automatique_puis_merge_request(self):
        self.start()
        self.on_release("shared.py", "x = 1")
        vus = []

        def fake(s, system, msgs):
            vus.append(msgs[-1]["content"])
            return 'Fait.\n```action\n{"do":"write_file","path":"x.py","content":"1","message":"feat: x"}\n```'
        with mock.patch.object(engine, "call_ai", fake):
            engine.run_task("u", "p1", "t1")
        self.assertTrue("Pull automatique" in vus[0] and "merge_request" in vus[0])
        self.assertEqual(BR["agent/bob"]["shared.py"], "x = 1")                                    # le pull a eu lieu avant le travail
        t, m = proj()["tasks"][0], proj()["mrs"][0]
        self.assertEqual((t["status"], m["status"], m["task"]), ("à vérifier", "ouverte", "t1"))   # la MR est soumise à la fin de la tâche
        self.assertEqual([s[0] for s in self.spawned], ["review_mr", "review_task"])

    # -- conflits
    def test_conflit_decision_de_l_utilisateur(self):
        self.conflict_setup()
        st, msg = engine.submit_mr("u", "p1", "a2", "Login", "", "t1")
        p = proj()
        c, d = p["conflicts"][0], p["decisions"][0]
        self.assertTrue(st == "conflict" and "app.py" in msg)
        self.assertEqual((c["files"], c["status"], c["origin"], d["conflict"], d["options"]), (["app.py"], "ouvert", "mr", c["id"], engine.CONFLICT_OPTS))
        self.assertEqual(p["mrs"], [])                                                             # pas de MR tant que le conflit n'est pas résolu
        with mock.patch.object(engine, "call_ai", self.resolver("Bob")):
            engine.decide("u", "p1", d["id"], "answer", answer=d["options"][0])                    # l'auteur de la branche résout
        c = proj()["conflicts"][0]
        self.assertEqual((c["status"], c["resolved_by"], BR["agent/bob"]["app.py"]), ("résolu", "Bob", "version Bob + release"))
        self.assertEqual(len(COMMITS[BRANCHES["agent/bob"]]["parents"]), 2)                         # vrai commit de fusion
        self.assertIn("after_conflict", [s[0] for s in self.spawned])
        engine.after_conflict("u", "p1", c["id"])                                                  # la soumission reprend
        self.assertEqual(proj()["mrs"][0]["status"], "ouverte")

    def test_conflit_agent_designe_par_la_regle_du_projet(self):
        self.set(conflict_policy="agent:a3")
        self.conflict_setup()
        engine.submit_mr("u", "p1", "a2", "Login", "", "t1")
        self.assertEqual(proj().get("decisions", []), [])                                          # pas de question : la règle désigne Carol
        run = [s for s in self.spawned if s[0] == "resolve_conflict"][0]
        self.assertEqual(run[1][3], "a3")
        appels = []

        def carol(s, system, msgs):
            appels.append(msgs[-1]["content"])
            if len(appels) == 1:                                                                    # 1re réponse : incomplète
                cid = re.search(r"\(id (\w+)\)", msgs[-1]["content"]).group(1)
                return '```action\n{"do":"resolve_merge","conflict":"%s","files":[]}\n```' % cid
            return self.resolver("Carol")(s, system, msgs[:-1] + [{"role": "user", "content": appels[0]}])
        with mock.patch.object(engine, "call_ai", carol):
            engine.resolve_conflict("u", "p1", run[1][2], "a3")
        c = proj()["conflicts"][0]
        self.assertEqual((c["status"], c["resolved_by"], c["tries"], len(appels)), ("résolu", "Carol", 1, 2))   # refus renvoyé à l'agent, qui se corrige dans le même échange

    def test_conflit_non_resolu_remonte_a_l_utilisateur(self):
        self.set(conflict_policy="agent:a3")
        self.conflict_setup()
        engine.submit_mr("u", "p1", "a2", "Login", "", "t1")
        cid = proj()["conflicts"][0]["id"]
        with mock.patch.object(engine, "call_ai", lambda *a: "Je ne sais pas."):
            engine.resolve_conflict("u", "p1", cid, "a3")
        p = proj()
        self.assertEqual((p["conflicts"][0]["status"], p["decisions"][0]["conflict"]), ("escalade", cid))   # deux échecs : l'utilisateur tranche

    def test_validations_de_resolve_merge(self):
        self.conflict_setup()
        engine.submit_mr("u", "p1", "a2", "Login", "", "t1")
        cid = proj()["conflicts"][0]["id"]
        st = store.load("u"); st["projects"][0]["conflicts"][0]["resolver"] = "a2"; store.wj(store.state_path("u"), st)
        do = lambda aid, files: engine.do_act("u", "p1", aid, {"do": "resolve_merge", "conflict": cid, "files": files}, 0)[1]
        self.assertIn("ne t’est confié", do("a1", [{"path": "app.py", "content": "x"}]))             # seul le résolveur désigné
        self.assertIn("incomplète", do("a2", [{"path": "autre.py", "content": "x"}]))
        self.assertIn("marqueurs", do("a2", [{"path": "app.py", "content": "<<<<<<< a\nx\n>>>>>>> b"}]))
        self.assertEqual(proj()["conflicts"][0]["status"], "ouvert")

    def test_conflit_au_moment_de_la_fusion_puis_principal_resout(self):
        self.start()
        self.on_release("app.py", "v0")
        self.write(1, "app.py", "version Bob")
        engine.submit_mr("u", "p1", "a2", "Login", "", "t1")                                        # MR ouverte sans conflit
        mid = proj()["mrs"][0]["id"]
        self.on_release("app.py", "version release")                                               # la branche de travail bouge ensuite
        engine.apply_mr_review("u", "p1", mid, "Alice", "approved", "ok")
        p = proj()
        c, d = p["conflicts"][0], p["decisions"][0]
        self.assertEqual((p["mrs"][0]["status"], c["origin"], c["mr"]), ("conflit", "mr", mid))
        with mock.patch.object(engine, "call_ai", self.resolver("Alice")):
            engine.decide("u", "p1", d["id"], "answer", answer=d["options"][1])                    # l'agent principal résout
        self.assertEqual(proj()["conflicts"][0]["resolved_by"], "Alice")
        engine.after_conflict("u", "p1", c["id"])                                                  # la MR est alors fusionnée
        self.assertEqual(proj()["mrs"][0]["status"], "fusionnée")
        self.assertEqual(BR["release/v1.0.0"]["app.py"], "version Bob + release")

    def test_conflit_en_pause_puis_reessayer(self):
        self.start()
        self.on_release("app.py", "v0")
        self.write(1, "app.py", "version Bob")
        engine.submit_mr("u", "p1", "a2", "Login")
        mid = proj()["mrs"][0]["id"]
        self.on_release("app.py", "version release")
        engine.apply_mr_review("u", "p1", mid, "Alice", "approved", "ok")
        d = proj()["decisions"][0]
        engine.decide("u", "p1", d["id"], "answer", answer=d["options"][2])                        # « je m'en occupe »
        self.assertEqual(proj()["conflicts"][0]["status"], "pause")
        github.Repo(proj()).merge_with_resolution("agent/bob", "release/v1.0.0", {"app.py": "résolu à la main"}, "fix")   # résolu sur GitHub
        engine.mr_retry("u", "p1", mid)
        self.assertEqual((proj()["mrs"][0]["status"], proj()["conflicts"][0]["status"]), ("fusionnée", "résolu"))

    def test_conflit_au_demarrage_d_une_tache(self):
        self.conflict_setup()
        with mock.patch.object(engine, "call_ai", lambda *a: self.fail("la tâche ne démarre pas tant que le conflit n'est pas résolu")):
            engine.run_task("u", "p1", "t1")
        p = proj()
        self.assertEqual((p["tasks"][0]["status"], p["tasks"][0]["running"], p["conflicts"][0]["origin"], p["conflicts"][0]["task"]), ("en attente", False, "pull", "t1"))
        self.assertEqual(len(p["decisions"]), 1)
        with mock.patch.object(engine, "call_ai", lambda *a: self.fail("pas d'appel IA")):
            engine.run_task("u", "p1", "t1")                                                       # relancée à la main : toujours refusée
        self.assertIn("conflit de fusion est en cours", self.logs())

    def test_fusion_avec_resolution_conserve_les_autres_changements(self):
        self.start()
        for path, content in (("a.txt", "a0"), ("d.txt", "d0")):
            self.on_release(path, content)
        self.write(1, "a.txt", "a Bob")
        self.write(1, "b.txt", "b Bob")
        self.on_release("a.txt", "a release")
        self.on_release("c.txt", "c release")
        rp = github.Repo(proj())
        self.assertEqual([i["path"] for i in rp.conflict_info("agent/bob", "release/v1.0.0")], ["a.txt"])   # seul a.txt est un vrai conflit
        rp.merge_with_resolution("agent/bob", "release/v1.0.0", {"a.txt": "a résolu"}, "merge")
        f = BR["agent/bob"]
        self.assertEqual((f["a.txt"], f["b.txt"], f["c.txt"], f["d.txt"]), ("a résolu", "b Bob", "c release", "d0"))

    # -- release finale
    def test_publier_la_release(self):
        engine.release("u", "p1", "v1.0.0", "notes")
        self.assertIn("Aucune release en cours", self.logs())                                      # rien à publier sans cycle
        self.start()
        self.write(1, "api.py", "print(1)")
        engine.submit_mr("u", "p1", "a2", "Login")
        mid = proj()["mrs"][0]["id"]
        engine.release("u", "p1", "v1.0.0", "notes")
        self.assertIn("encore ouverte", self.logs())                                               # une MR ouverte bloque la publication
        self.assertNotIn("api.py", BR["main"])
        engine.apply_mr_review("u", "p1", mid, "Alice", "approved", "ok")
        engine.release("u", "p1", "v1.0.0", "notes")
        p = proj()
        self.assertEqual(BR["main"]["api.py"], "print(1)")                                         # la release finale arrive sur la branche principale
        self.assertEqual([r["tag_name"] for r in REL], ["v1.0.0"])
        self.assertTrue("cycle" not in p and p["releases"][0]["v"] == "v1.0.0" and not p["releasing"])
        self.assertEqual(p["mrs"][-1]["status"], "fusionnée")


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
