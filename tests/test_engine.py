"""Tests du moteur d'agents : faux GitHub local + IA scriptée (aucun réseau, aucune clé)."""
import base64
import hashlib
import json
from datetime import datetime
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
        mg = re.fullmatch(r"/pulls/(\d+)", rest)
        if mg and m == "GET":
            pr = PRS.get(int(mg.group(1)))
            if not pr:
                return self.send(404, {"message": "Not Found"})
            return self.send(200, {"number": int(mg.group(1)), "state": "open" if pr["open"] else "closed", "merged": bool(pr.get("merged"))})
        mm = re.match(r"/pulls/(\d+)/merge", rest)
        if mm:
            pr = PRS[int(mm.group(1))]
            r = git_merge(pr["base"], BRANCHES[pr["head"]])
            if r == "conflict":
                return self.send(405, {"message": "Pull Request is not mergeable"})
            pr["open"], pr["merged"] = False, True
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
import project_rules as prules  # noqa: E402
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
            if len(vus) == 1:
                return 'Fait.\n```action\n{"do":"write_file","path":"x.py","content":"1","message":"feat: x"}\n```'
            return 'Je conclus.\n```action\n{"do":"task_status","id":"t1","status":"à vérifier","note":"x.py écrit"}\n```'
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
        self.assertIn("aucun état local", self.logs())

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

    # -- autonomie : tâches persistantes, lancement automatique, reprise, coordination
    def set_task(self, tid, **kw):
        st = store.load("u")
        next(x for x in st["projects"][0]["tasks"] if x["id"] == tid).update(kw)
        store.wj(store.state_path("u"), st)

    def launched(self):
        return [s[1][2] for s in self.spawned if s[0] == "run_task"]

    def notes(self):
        return [s[1][4] for s in self.spawned if s[0] == "run_task"]

    def test_l_agent_n_abandonne_pas_apres_une_erreur(self):
        calls = []

        def fake(s, system, msgs):
            calls.append(msgs[-1]["content"])
            if len(calls) == 1:
                return 'Je lis.\n```action\n{"do":"read_file","path":"absent.py"}\n```'
            if len(calls) == 2:
                return "Je ne trouve pas le fichier, je m'arrête là."                                   # l'agent renonce après l'erreur
            return ('Je crée le fichier.\n```action\n{"do":"write_file","path":"x.py","content":"1","message":"feat: x"}\n```\n'
                    '```action\n{"do":"task_status","id":"t1","status":"à vérifier","note":"x.py créé"}\n```')
        with mock.patch.object(engine, "call_ai", fake):
            engine.run_task("u", "p1", "t1")
        self.assertEqual(len(calls), 3)
        self.assertIn("Fichier introuvable", calls[1])                                                 # l'erreur est renvoyée à l'agent
        self.assertTrue(calls[2].startswith("[Application] Tu n’as pas déclaré la tâche terminée"))   # puis il est relancé sans intervention
        t = proj()["tasks"][0]
        self.assertEqual((t["status"], t["result"], BR["agent/bob"]["x.py"]), ("à vérifier", "x.py créé", "1"))

    def test_etapes_epuisees_le_travail_continue(self):
        calls = []

        def fake(s, system, msgs):
            calls.append(msgs[-1]["content"])
            if len(calls) <= engine.MAX_ROUND_STEPS:
                return '```action\n{"do":"read_file","path":"README.md"}\n```'
            return '```action\n{"do":"task_status","id":"t1","status":"à vérifier","note":"relu"}\n```'
        with mock.patch.object(engine, "call_ai", fake):
            engine.run_task("u", "p1", "t1")
        self.assertIn("Poursuis ta tâche", calls[engine.MAX_ROUND_STEPS])                              # le retour non traité n'est pas perdu
        self.assertEqual(proj()["tasks"][0]["status"], "à vérifier")

    def test_deux_tours_sans_action_valent_rendu(self):
        calls = []
        with mock.patch.object(engine, "call_ai", lambda s, sy, m: calls.append(1) or "Voici le livrable."):
            engine.run_task("u", "p1", "t1")
        self.assertEqual((len(calls), proj()["tasks"][0]["status"]), (2, "à vérifier"))                # une relance, puis on considère le travail rendu

    def test_travail_sans_fin_reprend_automatiquement(self):
        with mock.patch.object(engine, "call_ai", lambda *a: '```action\n{"do":"write_file","path":"x.py","content":"1"}\n```'):
            engine.run_task("u", "p1", "t1")                                                           # l'agent agit sans jamais conclure
        t = proj()["tasks"][0]
        self.assertEqual((t["status"], t.get("resume"), "fail" in t, t["running"]), ("à faire", True, False, False))
        engine.tick("u", "p1")
        self.assertIn("reprends là où tu t’es arrêté", self.notes()[0])                             # le pilote automatique la relance

    def test_pilote_automatique(self):
        st = store.load("u")
        pr = st["projects"][0]
        pr["agents"][0]["mode"] = "manual"                                                             # Alice : chat manuel, jamais lancée seule
        pr["tasks"] = [
            {"id": "t1", "parent": "", "title": "Login", "who": "a2", "status": "à faire", "modules": [], "by": ""},
            {"id": "t3", "parent": "", "title": "Suite", "who": "a2", "status": "à faire", "modules": [], "by": "", "deps": ["t1"]},
            {"id": "t4", "parent": "", "title": "Tests", "who": "a3", "status": "à faire", "modules": [], "by": ""},
            {"id": "t5", "parent": "", "title": "Cadrage", "who": "a1", "status": "à faire", "modules": [], "by": ""},
            {"id": "t6", "parent": "", "title": "Doc", "who": "a2", "status": "à faire", "modules": [], "by": ""},
            {"id": "t7", "parent": "", "title": "Parent", "who": "a3", "status": "à faire", "modules": [], "by": ""},
            {"id": "t8", "parent": "t7", "title": "Enfant", "who": "a3", "status": "terminée", "modules": [], "by": ""}]
        store.wj(store.state_path("u"), st)
        engine.tick("u", "p1")
        self.assertEqual(self.launched(), ["t1", "t4"])           # une tâche par agent ; ni dépendance non satisfaite, ni agent en chat manuel, ni tâche parente
        self.assertEqual([x.get("auto_n") for x in proj()["tasks"][:3]], [1, None, 1])

    def test_pilote_automatique_garde_fous(self):
        def lances(**projet):
            self.spawned.clear()
            self.set(**projet)
            engine.tick("u", "p1")
            return [s[1][2] for s in self.spawned if s[0] == "run_task"]
        self.set_task("t1", status="à faire")
        self.assertEqual(lances(), ["t1"])
        self.assertEqual(lances(autopilot="off"), [])                                                  # désactivé par l'utilisateur
        self.assertEqual(lances(autopilot="on", budget="0.01", usage={"cost": 0.5}), [])               # plafond de budget atteint
        self.set(budget="", usage={})
        self.set_task("t1", fail={"kind": "other", "msg": "x", "ts": 1}, sup_ts=time.time())           # en échec : pas relancée aveuglément
        self.assertEqual(lances(), [])
        self.set_task("t1", fail=None, auto_n=engine.MAX_AUTO)
        self.assertEqual(lances(), [])
        d = proj()["decisions"]
        self.assertTrue(len(d) == 1 and d[0]["escalation"])                                            # trop de relances automatiques : l'utilisateur décide
        lances()
        self.assertEqual(len(proj()["decisions"]), 1)                                                  # une seule question

    def test_execution_fantome_reinitialisee(self):
        self.set_task("t1", running=True, touched=time.time() - 5000)
        engine.tick("u", "p1")
        t = proj()["tasks"][0]
        self.assertFalse(t["running"])
        self.assertEqual(t["fail"]["kind"], "internal")
        self.assertTrue(proj()["decisions"][0]["escalation"])                                          # et l'utilisateur est prévenu

    def test_erreur_inattendue_ne_bloque_pas_la_tache(self):
        with mock.patch.object(engine, "pull", side_effect=RuntimeError("boom")):
            engine.run_task("u", "p1", "t1")
        t = proj()["tasks"][0]
        self.assertEqual((t["running"], t["status"], t["fail"]["kind"]), (False, "à faire", "internal"))
        self.assertIn("Erreur interne", self.logs())

    def test_erreur_inconnue_reessayee_puis_remontee(self):
        def bizarre(*a):
            raise Err(502, "Erreur du service IA (400) : requête invalide")
        for n in range(engine.MAX_OTHER):
            engine.TIMERS.clear()
            with mock.patch.object(engine, "call_ai", bizarre), mock.patch.object(engine.threading, "Timer") as T:
                engine.run_task("u", "p1", "t1")
            t = proj()["tasks"][0]
            self.assertEqual((t["retries"], "fail" in t, T.call_args[0][0]), (n + 1, False, engine.backoff(n)))
            self.set_task("t1", retry_at=None)
        with mock.patch.object(engine, "call_ai", bizarre):
            engine.run_task("u", "p1", "t1")
        self.assertEqual(proj()["tasks"][0]["fail"]["kind"], "other")                                  # puis l'agent principal est sollicité

    def test_contexte_trop_long_compacte_l_historique(self):
        st = store.load("u")
        st["projects"][0]["agents"][1]["history"] = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 5000} for i in range(20)]
        store.wj(store.state_path("u"), st)
        appels = []

        def fake(s, system, msgs):
            appels.append(len(msgs))
            if len(appels) == 1:
                raise Err(502, "Erreur du service IA (400) : prompt is too long: 250000 tokens > 200000 maximum")
            return "ok"
        with mock.patch.object(engine, "call_ai", fake):
            engine.ask("u", "p1", "a2", "salut", "Vous")
        h = proj()["agents"][1]["history"]
        self.assertTrue(appels[0] > 20 and appels[1] <= 5)                                             # 2e appel avec un historique réduit
        self.assertTrue(len(h) <= 6 and all(len(m["content"]) < 1700 for m in h))
        self.assertIn("Contexte trop long", self.logs())

    def test_historique_allege(self):
        with mock.patch.object(engine, "call_ai", lambda *a: "y" * 20000):
            engine.ask("u", "p1", "a2", "salut", "Vous")
        self.assertLess(len(proj()["agents"][1]["history"][-1]["content"]), 6100)                       # pas de gros fichiers dans l'historique

    def test_conversation_reessayee_apres_quota(self):
        def quota(*a):
            raise Err(429, "quota", 5)
        with mock.patch.object(engine, "call_ai", quota), mock.patch.object(engine.threading, "Timer") as T:
            engine.say("u", "p1", "a1", "bonjour")
        self.assertEqual(T.call_args[0][0], 5)
        self.assertIn("nouvelle tentative automatique", self.logs())
        with mock.patch.object(engine, "call_ai", lambda *a: "Re-bonjour"):
            T.call_args[0][1]()                                                                        # le minuteur se déclenche
        msgs = [m for m in proj()["log"] if m["from"] == "Vous" and m["to"] == "Alice"]
        self.assertEqual([m["text"] for m in msgs], ["bonjour"])                                       # le message n'est pas journalisé deux fois
        self.assertTrue(any(m["from"] == "Alice" and m["text"] == "Re-bonjour" for m in proj()["log"]))

    def test_les_agents_peuvent_se_contacter_par_defaut(self):
        self.set_task("t3", who="a3")
        st = store.load("u"); st["projects"][0]["tasks"].append({"id": "t4", "parent": "", "title": "Tests e2e", "who": "a3", "status": "en cours", "modules": [], "by": ""}); store.wj(store.state_path("u"), st)
        with mock.patch.object(engine, "call_ai", lambda *a: "Reçu."):
            note, back = engine.do_act("u", "p1", "a3", {"do": "message", "to": "Bob", "text": "quelle interface ?"}, 0)   # Carol : aucune restriction
        self.assertEqual(back, "[Réponse de Bob] Reçu.")
        for talk, attendu in ((["none"], "ne peut pas écrire"), (["a1"], "ne peut pas écrire")):
            st = store.load("u"); st["projects"][0]["agents"][2]["talk"] = talk; store.wj(store.state_path("u"), st)
            self.assertIn(attendu, engine.do_act("u", "p1", "a3", {"do": "message", "to": "Bob", "text": "x"}, 0)[1])
        bob = proj()["agents"][1]
        prompt = engine.sys_prompt(proj(), bob)
        self.assertTrue("Travail des autres agents" in prompt and "Carol : « Tests e2e »" in prompt)    # il sait qui fait quoi
        self.assertNotIn("Carol : « Login »", prompt)
        self.assertIn("Agents joignables : tous", engine.ctx_text(proj(), proj()["agents"][0]))      # le principal
        self.assertIn("Agents joignables : Alice", engine.ctx_text(proj(), bob))                          # restriction explicite

    def test_message_de_tache_dependances_et_coordination(self):
        self.set_task("t1", status="terminée", result="API login prête : POST /login", who="a2")
        st = store.load("u"); st["projects"][0]["tasks"].append({"id": "t4", "parent": "", "title": "Tests e2e", "who": "a3", "status": "en cours", "modules": [], "by": ""}); store.wj(store.state_path("u"), st)
        vus = []
        with mock.patch.object(engine, "call_ai", lambda s, sy, m: vus.append((sy, m[-1]["content"])) or "ok"):
            engine.run_task("u", "p1", "t3")
        sy, msg = vus[0]
        self.assertTrue("API login prête : POST /login" in msg and "Tâches dont tu dépends" in msg and "Coordination" in msg)   # compte rendu de la dépendance transmis
        self.assertIn("Tests e2e", sy)

    def test_creation_de_tache_declenche_le_pilote(self):
        self.set(autonomy="auto")
        engine.do_act("u", "p1", "a1", {"do": "task", "title": "Nouvelle", "assignee": "Carol"}, 0)
        self.assertIn("tick", [s[0] for s in self.spawned])                                            # lancée sans attendre le prochain passage

    # -- déblocage : conflits obsolètes, actions de l'utilisateur, réinitialisation d'un agent, diagnostic
    def stuck_conflict(self):
        self.conflict_setup()
        engine.submit_mr("u", "p1", "a2", "Login", "", "t1")                                       # conflit réel + question à l'utilisateur
        return proj()["conflicts"][0]

    def fix_outside(self):
        github.Repo(proj()).merge_with_resolution("agent/bob", "release/v1.0.0", {"app.py": "résolu à la main"}, "fix")

    def test_conflit_obsolete_leve_automatiquement(self):
        c = self.stuck_conflict()
        self.assertTrue(engine.open_conflict_for(proj(), "a2"))
        self.assertEqual(engine.reconcile_conflicts("u", "p1", force=True), [])                    # toujours un vrai conflit : on n'y touche pas
        self.fix_outside()                                                                         # résolu à la main sur GitHub
        self.assertEqual(engine.reconcile_conflicts("u", "p1", force=True), [c["id"]])
        p = proj()
        self.assertEqual((p["conflicts"][0]["status"], p["decisions"][0]["status"]), ("résolu", "répondue"))   # conflit ET question closes
        self.assertFalse(engine.open_conflict_for(p, "a2"))
        self.assertEqual(p["mrs"][0]["status"], "ouverte")                                         # la merge request reprend toute seule

    def test_tache_debloquee_quand_le_conflit_est_leve(self):
        self.stuck_conflict()
        self.set_task_status("t1", "à faire")
        self.fix_outside()
        with mock.patch.object(engine, "call_ai", lambda *a: "Livrable."):
            engine.run_task("u", "p1", "t1")                                                       # plus besoin d'attendre ni de relancer à la main
        self.assertEqual((proj()["conflicts"][0]["status"], proj()["tasks"][0]["status"]), ("résolu", "à vérifier"))

    def test_message_de_blocage_explicite_sans_doublon(self):
        self.stuck_conflict()
        st, msg = engine.submit_mr("u", "p1", "a2", "Login")
        self.assertEqual(st, "skip")
        for attendu in ("Conflit de fusion n°", "app.py", "onglet Décisions", "aucun état local"):
            self.assertIn(attendu, msg)
        self.assertEqual(engine.pull("u", "p1", "a2")[0], "conflict")
        self.assertEqual(len(proj()["conflicts"]), 1)                                              # pas de second enregistrement

    def test_utilisateur_abandonne_un_conflit(self):
        c = self.stuck_conflict()
        self.set_task_status("t1", "en attente")
        engine.conflict_action("u", "p1", c["id"], "abandon")
        p = proj()
        self.assertEqual((p["conflicts"][0]["status"], p["decisions"][0]["status"], p["tasks"][0]["status"]), ("abandonné", "répondue", "à faire"))
        self.assertFalse(engine.open_conflict_for(p, "a2"))                                        # l'agent n'est plus bloqué

    def test_utilisateur_declare_un_conflit_resolu(self):
        c = self.stuck_conflict()
        engine.conflict_action("u", "p1", c["id"], "resolved")
        self.assertEqual(proj()["conflicts"][0]["status"], "résolu")
        self.assertEqual(proj()["conflicts"][1]["status"], "ouvert")                               # s'il persiste vraiment, il est signalé de nouveau

    def test_utilisateur_revérifie_un_conflit(self):
        c = self.stuck_conflict()
        engine.conflict_action("u", "p1", c["id"], "check")
        self.assertIn("toujours présent", self.logs())
        self.assertEqual(proj()["conflicts"][0]["status"], "ouvert")

    def test_utilisateur_relance_la_resolution(self):
        self.set(conflict_policy="author")
        self.conflict_setup()
        engine.submit_mr("u", "p1", "a2", "Login", "", "t1")
        c = proj()["conflicts"][0]
        with mock.patch.object(engine, "call_ai", self.resolver("Bob")):
            engine.conflict_action("u", "p1", c["id"], "retry")
        self.assertEqual((proj()["conflicts"][0]["status"], BR["agent/bob"]["app.py"]), ("résolu", "version Bob + release"))

    def test_reinitialiser_un_agent(self):
        self.stuck_conflict()
        st = store.load("u"); pr = st["projects"][0]
        pr["agents"][1].update(history=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}], notes="n", work=2, state="suspendu")
        pr["tasks"][0].update(running=True, status="en cours", retry_at=1, retries=3)
        pr["pending"] = [{"id": "px", "agent": "a2", "name": "Bob", "service": "S", "full": "f", "short": "s", "new": True, "d": ""}]
        pr["mrs"] = [{"id": "m9", "n": 9, "from": "agent/bob", "to": "release/v1.0.0", "by": "Bob", "by_id": "a2", "status": "ouverte", "task": "t1"}]
        store.wj(store.state_path("u"), st)
        ev = threading.Event()
        engine.WAIT["px"] = {"u": "u", "ev": ev, "text": None}
        engine.agent_reset("u", "p1", "a2", history=True, notes=True, branch=True, onboard_after=True)
        p = proj(); bob = p["agents"][1]; t = p["tasks"][0]
        self.assertEqual((bob["history"], bob["notes"], bob["work"], bob["state"]), ([], "", 0, "actif"))
        self.assertEqual((t["status"], t["running"], "retry_at" in t, p["pending"], ev.is_set()), ("à faire", False, False, [], True))   # attente manuelle annulée
        self.assertEqual(([c["status"] for c in p["conflicts"]], p["decisions"][0]["status"], p["mrs"][0]["status"]), (["abandonné"], "répondue", "annulée"))
        self.assertEqual(set(BR["agent/bob"]) - {".agents/Bob.md"}, set(BR["release/v1.0.0"]))     # branche remise sur la branche de travail + contexte rejoué
        self.assertEqual(BR["agent/bob"]["app.py"], "version release")                            # les commits de l'agent sont écartés
        self.assertIn("réinitialisé", self.logs())

    def test_reinitialiser_sans_toucher_a_la_branche(self):
        self.start()
        self.write(1, "api.py", "print(1)")
        st = store.load("u"); st["projects"][0]["agents"][1]["history"] = [{"role": "user", "content": "x"}]; store.wj(store.state_path("u"), st)
        engine.agent_reset("u", "p1", "a2", history=True, notes=False, branch=False, onboard_after=False)
        self.assertEqual(proj()["agents"][1]["history"], [])
        self.assertEqual(BR["agent/bob"]["api.py"], "print(1)")                                    # le travail versionné est conservé

    def test_diagnostic_factuel(self):
        self.stuck_conflict()
        texts = " ¦ ".join(i["text"] for i in engine.diagnose(proj()))
        for attendu in ("Conflit de fusion n°", "« Suite » attend : Login", "décision(s) vous attendent"):
            self.assertIn(attendu, texts)
        self.assertTrue(any(i.get("conflict") for i in engine.diagnose(proj())))
        _, back = engine.do_act("u", "p1", "a2", {"do": "get_blockers"}, 0)                      # les agents voient les mêmes faits
        self.assertTrue("Blocages actuels" in back and "Conflit de fusion" in back)

    def test_diagnostic_tolere_un_etat_incomplet(self):
        pr = {"agents": [{"id": "a", "name": "Bob"}], "tasks": [{"id": "t", "title": "X", "who": "a", "status": "à faire", "fail": {}}], "mrs": [{"id": "m", "status": "ouverte"}],
              "conflicts": [{"id": "c", "agent": "a", "status": "pause"}]}
        texts = " ¦ ".join(i["text"] for i in engine.diagnose(pr))
        self.assertTrue("Conflit de fusion n°?" in texts and "Merge request #?" in texts)               # aucun plantage, même sans numéro ni détails

    def test_consigne_contre_les_causes_inventees(self):
        regles = "\n".join(engine.RULES)
        self.assertTrue("MERGE_HEAD" in regles and "n’invente pas de cause" in regles and "get_blockers" in regles)
        self.assertIn("get_blockers", engine.sys_prompt(proj(), proj()["agents"][1]))

    def test_classer_une_question_sans_objet(self):
        self.set_task("t1", status="en cours")
        engine.do_act("u", "p1", "a2", {"do": "ask_user", "question": "Peux-tu nettoyer mon environnement ?", "options": ["Oui", "Non"], "task": "t1"}, 0)
        d = proj()["decisions"][0]
        self.assertEqual(proj()["tasks"][0]["status"], "en attente")
        engine.decide("u", "p1", d["id"], "dismiss", note="sans objet")
        p = proj()
        self.assertEqual((p["decisions"][0]["status"], p["tasks"][0]["status"], "blocked" in p["tasks"][0]), ("ignorée", "à faire", False))

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

    # -- « blockers fantômes » : MR closes sur GitHub mais encore ouvertes dans l'application
    def open_mr(self):
        self.start()
        self.write(1, "api.py", "print(1)")
        engine.submit_mr("u", "p1", "a2", "Login", "corps", "t1")
        return proj()["mrs"][0]

    def blockers(self):
        return engine.blockers_text(proj())

    def test_mr_fusionnee_sur_github_retiree_des_blocages(self):
        m = self.open_mr()
        self.set_task_status("t1", "terminée")
        self.assertIn("Merge request #%s" % m["n"], self.blockers())
        self.assertEqual(engine.deps_unmet(proj(), proj()["tasks"][1]), ["Login"])                 # t1 terminée, mais sa MR est encore « ouverte » dans l'application
        PRS[m["n"]].update(open=False, merged=True)                                                 # fusionnée à la main sur GitHub
        self.assertEqual(engine.sync_mrs("u", "p1", force=True), [(m["n"], "fusionnée")])
        p = proj()
        self.assertEqual(p["mrs"][0]["status"], "fusionnée")
        self.assertNotIn("Merge request #", self.blockers())
        self.assertEqual(engine.deps_unmet(p, p["tasks"][1]), [])                                  # la tâche dépendante n'attend plus
        self.assertIn("débloquées", self.logs())

    def test_mr_fermee_sans_fusion_est_annulee(self):
        m = self.open_mr()
        PRS[m["n"]]["open"] = False                                                                # abandonnée sur GitHub
        self.assertEqual(engine.sync_mrs("u", "p1", force=True), [(m["n"], "annulée")])
        self.assertEqual(proj()["mrs"][0]["status"], "annulée")
        self.assertNotIn("Merge request #", self.blockers())

    def test_mr_toujours_ouverte_n_est_pas_touchee_et_verification_espacee(self):
        m = self.open_mr()
        self.assertEqual(engine.sync_mrs("u", "p1", force=True), [])
        self.assertEqual(proj()["mrs"][0]["status"], "ouverte")
        PRS[m["n"]].update(open=False, merged=True)
        self.assertEqual(engine.sync_mrs("u", "p1"), [])                                           # vérifiée il y a moins de MR_SYNC_EVERY s : pas d'appel GitHub
        self.assertEqual(proj()["mrs"][0]["status"], "ouverte")
        self.assertEqual(len(engine.sync_mrs("u", "p1", force=True)), 1)

    def test_mr_introuvable_ou_github_en_panne_ne_conclut_rien(self):
        m = self.open_mr()
        PRS.pop(m["n"])                                                                            # 404 : accès ou numéro
        self.assertEqual(engine.sync_mrs("u", "p1", force=True), [])
        self.assertEqual(proj()["mrs"][0]["status"], "ouverte")
        self.set(repo={"url": "", "branch": "main", "token": ""})                                 # dépôt non configuré
        self.assertEqual(engine.sync_mrs("u", "p1", force=True), [])

    def test_le_suivi_retire_les_mr_closes_meme_sans_suivi_actif(self):
        m = self.open_mr()
        self.set(supervision="off")
        PRS[m["n"]].update(open=False, merged=True)
        engine.tick("u", "p1")
        self.assertEqual(proj()["mrs"][0]["status"], "fusionnée")

    def test_get_blockers_donne_un_etat_frais(self):
        m = self.open_mr()
        PRS[m["n"]].update(open=False, merged=True)
        r = engine.do_act("u", "p1", "a2", {"do": "get_blockers"}, 0)
        self.assertNotIn("Merge request #", r[1])                                                  # plus de blocage fantôme dans la réponse de l'outil
        self.assertEqual(proj()["mrs"][0]["status"], "fusionnée")

    def test_bouton_marquer_terminee_libere_tout(self):
        m = self.open_mr()
        self.set_task_status("t1", "terminée")
        st = store.load("u")                                                                       # MR en conflit + question de validation en attente
        pr = st["projects"][0]
        pr["mrs"][0]["status"] = "conflit"
        pr["conflicts"] = [{"id": "c1", "n": 1, "agent": "a2", "branch": "agent/bob", "target": "release/v1.0.0", "files": ["x"], "origin": "mr", "mr": m["id"], "task": "t1", "status": "ouvert", "d": "x"}]
        pr["decisions"] = [{"id": "d1", "n": 2, "kind": "question", "status": "attente", "mr_review": m["id"], "question": "?", "options": ["Oui", "Non"], "task": ""}]
        store.wj(store.state_path("u"), st)
        self.assertIn("Merge request #", self.blockers())
        engine.mr_done("u", "p1", m["id"])                                                         # GitHub n'est pas consulté : le bouton est le secours
        p = proj()
        self.assertEqual((p["mrs"][0]["status"], p["mrs"][0]["note"]), ("terminée", "Marquée terminée par l’utilisateur."))
        self.assertEqual((p["conflicts"][0]["status"], p["decisions"][0]["status"]), ("résolu", "répondue"))
        self.assertNotIn("Merge request #", self.blockers())
        self.assertEqual(engine.deps_unmet(p, p["tasks"][1]), [])
        engine.mr_done("u", "p1", m["id"])                                                         # deuxième clic : sans effet
        self.assertIn("déjà close", self.logs())
        self.assertEqual(engine.sync_mrs("u", "p1", force=True), [])                               # le mécanisme automatique ne la rouvre pas

    def test_la_release_n_est_plus_bloquee_par_une_mr_close(self):
        m = self.open_mr()
        PRS[m["n"]].update(open=False, merged=True)
        engine.release("u", "p1", "v1.0.0", "notes")
        self.assertNotIn("encore ouverte", self.logs())
        self.assertEqual(proj()["mrs"][0]["status"], "fusionnée")


class Regles(unittest.TestCase):
    """Règles du projet : communication aux agents, workflows (déclencheurs, étapes, échecs), minuteries, cohérence."""
    def setUp(self):
        reset()
        st = make_state()
        pr = st["projects"][0]
        pr["agents"].append({"id": "a3", "name": "Carol", "service": "s1", "role": "r3", "branch": "agent/carol", "canBug": False, "talk": [], "history": [], "notes": ""})
        pr["tasks"] = [{"id": "t1", "parent": "", "title": "Login", "desc": "", "who": "a2", "status": "en cours", "modules": [], "by": ""}]
        store.wj(store.state_path("u"), st)
        self.spawned = []
        self.sp = mock.patch.object(engine, "spawn", lambda fn, *a, **k: self.spawned.append((fn.__name__, a, k)))
        self.sp.start()
        engine.TIMERS.clear()
        self.n = 0

    def tearDown(self):
        self.sp.stop()

    # -- aides
    def set(self, **kw):
        st = store.load("u"); st["projects"][0].update(kw); store.wj(store.state_path("u"), st)

    def set_agent(self, idx, **kw):
        st = store.load("u"); st["projects"][0]["agents"][idx].update(kw); store.wj(store.state_path("u"), st)

    def step(self, who="principal", text="Fais-le", kind="ask", on_ko="stop", **kw):
        self.n += 1
        return dict({"id": "s%d" % self.n, "kind": kind, "who": who, "text": text, "on_ko": on_ko}, **kw)

    def add_wf(self, name="Validation", steps=None, **kw):
        st = store.load("u"); pr = st["projects"][0]
        w = {"id": "w%d" % (len(pr.get("workflows", [])) + 1), "name": name, "desc": "", "enabled": True, "allow_agent": True, "schedule": {"mode": "off"}, "steps": steps or []}
        w.update(kw)
        pr.setdefault("workflows", []).append(w)
        store.wj(store.state_path("u"), st)
        return w["id"]

    def logs(self):
        return " ¦ ".join(m["text"] for m in proj()["log"])

    def run_wf(self, wfid, ai, **kw):
        """Démarre un workflow et le déroule avec une IA scriptée ; retourne le déroulement."""
        with mock.patch.object(engine, "call_ai", ai):
            rid = engine.workflow_start("u", "p1", wfid, "utilisateur", **kw)
            engine.wf_continue("u", "p1", rid)
        return next(r for r in proj()["wf_runs"] if r["id"] == rid)

    @staticmethod
    def verdicts(*outs):
        """IA qui conclut chaque étape avec les issues données, dans l'ordre (la dernière se répète)."""
        calls = []

        def f(s, system, msgs):
            calls.append(msgs[-1]["content"])
            o = outs[min(len(calls) - 1, len(outs) - 1)]
            return 'Étape traitée.\n```action\n{"do":"wf_step","outcome":"%s","comment":"commentaire %d"}\n```' % (o, len(calls))
        f.calls = calls
        return f

    # -- communication des modifications
    def test_modification_communiquee_aux_agents_concernes(self):
        self.set(rules="Toujours écrire des tests.")
        self.set_agent(1, rules="Réponds toujours en JSON.")
        entries = engine.rules_changed("u", "p1")
        self.assertEqual([e["scope"] for e in entries], ["global", "agent:a2"])
        for nom in ("Alice", "Bob", "Carol"):                                                      # règle globale : tous
            self.assertRegex(self.logs(), "Règles mises à jour : Les règles globales ont été modifiées")
        bob_log = [m["text"] for m in proj()["log"] if m["to"] == "Bob" and "Règles mises à jour" in m["text"]][0]
        carol_log = [m["text"] for m in proj()["log"] if m["to"] == "Carol" and "Règles mises à jour" in m["text"]][0]
        self.assertIn("Tes règles spécifiques", bob_log)
        self.assertNotIn("Tes règles spécifiques", carol_log)                                       # règle d'un agent : lui seul
        bob, carol = proj()["agents"][1], proj()["agents"][2]
        pb, pc = engine.sys_prompt(proj(), bob), engine.sys_prompt(proj(), carol)
        self.assertTrue("Règles spécifiques à toi (Bob)" in pb and "Réponds toujours en JSON." in pb and "Toujours écrire des tests." in pb)
        self.assertNotIn("Réponds toujours en JSON.", pc)
        self.assertIn("Nouveautés depuis ta dernière activité", pb)
        self.assertTrue("Tes règles spécifiques ont été modifiées" in pb and "Tes règles spécifiques" not in pc)   # nouveautés ciblées
        self.assertIn("Les règles globales ont été modifiées", pc)
        self.assertIn("Réponds toujours en JSON.", BR["agent/bob"][".agents/Bob.md"])               # fichier de contexte de l'agent
        self.assertNotIn("Réponds toujours en JSON.", BR["agent/carol"][".agents/Carol.md"])
        self.assertIn("Toujours écrire des tests.", BR["agent/carol"][".agents/Carol.md"])
        self.assertIn("rules_analysis", [s[0] for s in self.spawned])                               # contrôle de cohérence relancé
        with mock.patch.object(engine, "call_ai", lambda *a: "ok"):
            engine.ask("u", "p1", "a2", "salut", "Vous")
        self.assertNotIn("Nouveautés depuis", engine.sys_prompt(proj(), proj()["agents"][1]))      # vues : plus signalées

    def test_pas_de_communication_sans_changement(self):
        self.set(rules="R1")
        engine.rules_changed("u", "p1")
        n = len(proj()["log"])
        self.spawned.clear()
        self.assertEqual(engine.rules_changed("u", "p1"), [])
        self.assertEqual((len(proj()["log"]), self.spawned), (n, []))

    def test_workflows_communiques(self):
        engine.rules_changed("u", "p1")
        self.add_wf("Validation", [self.step()])
        entries = engine.rules_changed("u", "p1")
        self.assertEqual([e["text"] for e in entries], ["Workflow « Validation » créé."])
        prompt = engine.sys_prompt(proj(), proj()["agents"][1])
        self.assertTrue("« Validation »" in prompt and "run_workflow" in prompt and "Workflow « Validation » créé." in prompt)
        st = store.load("u"); st["projects"][0]["workflows"][0]["steps"][0]["text"] = "Autre consigne"; store.wj(store.state_path("u"), st)
        self.assertEqual([e["text"] for e in engine.rules_changed("u", "p1")], ["Workflow « Validation » modifié."])
        st = store.load("u"); st["projects"][0]["workflows"] = []; store.wj(store.state_path("u"), st)
        self.assertEqual([e["text"] for e in engine.rules_changed("u", "p1")], ["Workflow « Validation » supprimé."])
        self.add_wf("Caché", [self.step()], enabled=False)
        self.assertNotIn("Caché", engine.sys_prompt(proj(), proj()["agents"][1]))                  # un workflow désactivé n'est pas proposé

    # -- exécution des workflows
    def test_workflow_lance_par_l_utilisateur(self):
        w = self.add_wf("Validation", [self.step("principal", "Vérifie le travail"), self.step("role:r2", "Corrige si besoin")])
        ai = self.verdicts("ok")
        run = self.run_wf(w, ai, task="t1", note="urgent")
        self.assertEqual((run["status"], [r["outcome"] for r in run["results"]], run["results"][0]["comment"]), ("terminé", ["ok", "ok"], "commentaire 1"))
        self.assertTrue("« Validation » — étape 1/2" in ai.calls[0] and "Vérifie le travail" in ai.calls[0] and "Login" in ai.calls[0] and "urgent" in ai.calls[0])
        self.assertIn("Résultats des étapes précédentes", ai.calls[1])                              # l'étape 2 connaît le résultat de l'étape 1
        self.assertIn("wf_step", ai.calls[1])
        self.assertNotIn("wf_ctx", proj()["agents"][0])

    def test_echec_arret(self):
        w = self.add_wf("W", [self.step(on_ko="stop"), self.step()])
        ai = self.verdicts("ko")
        run = self.run_wf(w, ai)
        self.assertEqual((run["status"], len(ai.calls)), ("arrêté", 1))
        self.assertIn("étape 1 en échec", run["note"])

    def test_echec_continue(self):
        run = self.run_wf(self.add_wf("W", [self.step(on_ko="continue"), self.step()]), self.verdicts("ko", "ok"))
        self.assertEqual((run["status"], [r["outcome"] for r in run["results"]]), ("terminé", ["ko", "ok"]))

    def test_echec_saut_vers_une_etape(self):
        ai = self.verdicts("ko", "ok")
        run = self.run_wf(self.add_wf("W", [self.step(on_ko="goto", goto=3), self.step(text="ignorée"), self.step(text="finale")]), ai)
        self.assertEqual((run["status"], [r["i"] for r in run["results"]]), ("terminé", [1, 3]))
        self.assertFalse(any("ignorée" in c for c in ai.calls))

    def test_echec_question_a_l_utilisateur(self):
        w = self.add_wf("W", [self.step(on_ko="ask_user"), self.step(text="suite")])
        ai = self.verdicts("ko", "ok")
        run = self.run_wf(w, ai)
        d = proj()["decisions"][0]
        self.assertEqual((run["status"], run["waiting"], d["wf_run"], d["options"]), ("en cours", d["id"], run["id"], ["Continuer malgré l’échec", "Relancer cette étape", "Arrêter le workflow"]))
        engine.wf_continue("u", "p1", run["id"])                                                    # en pause : rien ne bouge tant que l'utilisateur n'a pas répondu
        self.assertEqual(len(ai.calls), 1)
        with mock.patch.object(engine, "call_ai", ai):
            engine.decide("u", "p1", d["id"], "answer", answer="Relancer cette étape")             # l'étape est rejouée (cette fois : ok), puis la suite
        run = proj()["wf_runs"][0]
        self.assertEqual((run["status"], len(ai.calls), [r["outcome"] for r in run["results"]]), ("terminé", 3, ["ko", "ok", "ok"]))

    def test_echec_reponses_continuer_ou_arreter(self):
        for reponse, statut, nb in (("Continuer malgré l’échec", "terminé", 2), ("Arrêter le workflow", "arrêté", 1)):
            st = store.load("u"); st["projects"][0].update(workflows=[], wf_runs=[], decisions=[]); store.wj(store.state_path("u"), st)
            w = self.add_wf("W" + reponse[:3], [self.step(on_ko="ask_user"), self.step(text="suite")])
            ai = self.verdicts("ko", "ok")
            run = self.run_wf(w, ai)
            with mock.patch.object(engine, "call_ai", ai):
                engine.decide("u", "p1", proj()["decisions"][-1]["id"], "answer", answer=reponse)
            self.assertEqual((proj()["wf_runs"][-1]["status"], len(ai.calls)), (statut, nb))

    def test_etape_question_a_l_utilisateur(self):
        w = self.add_wf("Contrôle", [self.step(kind="ask_user", text="Valides-tu le périmètre ?"), self.step(text="suite")])
        ai = self.verdicts("ok")
        run = self.run_wf(w, ai)
        d = proj()["decisions"][0]
        self.assertTrue("Valides-tu le périmètre ?" in d["question"] and d["options"] == ["Continuer", "Arrêter le workflow"] and len(ai.calls) == 0)
        with mock.patch.object(engine, "call_ai", ai):
            engine.decide("u", "p1", d["id"], "answer", answer="Oui, avec la réserve X")           # une réponse libre vaut « continuer »
        run = proj()["wf_runs"][0]
        self.assertEqual((run["status"], run["results"][0]["comment"], len(ai.calls)), ("terminé", "Oui, avec la réserve X", 1))

    def test_etape_sans_verdict_reclamee_puis_acceptee(self):
        calls = []

        def muet(s, system, msgs):
            calls.append(msgs[-1]["content"])
            return "Fait, rien d'autre."
        run = self.run_wf(self.add_wf("W", [self.step()]), muet)
        self.assertEqual((len(calls), run["status"], run["results"][0]["comment"]), (2, "terminé", "sans verdict explicite de l’agent"))
        self.assertIn("Conclus l’étape avec l’action wf_step", calls[1])

    def test_etape_demarrer_les_taches(self):
        self.set(autopilot="off")
        self.set_task_status = None
        st = store.load("u"); st["projects"][0]["tasks"][0]["status"] = "à faire"; store.wj(store.state_path("u"), st)
        run = self.run_wf(self.add_wf("Matin", [self.step(kind="start_tasks")]), lambda *a: self.fail("aucun appel IA pour cette étape"))
        self.assertEqual((run["status"], run["results"][0]["comment"]), ("terminé", "1 tâche(s) démarrée(s)"))
        self.assertIn("run_task", [s[0] for s in self.spawned])                                    # démarrage forcé, même avec le lancement automatique désactivé

    def test_etape_sans_agent_ou_sans_tache(self):
        run = self.run_wf(self.add_wf("W1", [self.step(who="assignee")]), self.verdicts("ok"))     # « agent assigné » sans tâche cible
        self.assertEqual((run["status"], run["results"][0]["outcome"]), ("arrêté", "ko"))
        self.assertIn("aucune tâche cible", run["results"][0]["comment"])
        run = self.run_wf(self.add_wf("W2", [self.step(who="agent:zzz")]), self.verdicts("ok"))
        self.assertIn("agent introuvable", run["results"][0]["comment"])

    def test_workflow_declenche_par_un_agent(self):
        w = self.add_wf("Validation", [self.step()])
        do = lambda aid, **x: engine.do_act("u", "p1", aid, x, 0)
        note, back = do("a2", do="run_workflow", workflow="validation", task="t1", note="fini")    # insensible à la casse
        self.assertIn("déclenché", note)
        self.assertEqual(self.spawned[-1][0], "wf_continue")
        run = proj()["wf_runs"][0]
        self.assertEqual((run["trigger"], run["requester"], run["task"], run["note"]), ("agent Bob", "a2", "t1", "fini"))
        self.assertIn("déjà en cours", do("a2", do="run_workflow", workflow="Validation", task="t1")[1])   # pas de doublon sur la même tâche
        self.assertIn("introuvable", do("a2", do="run_workflow", workflow="Inconnu")[1])
        st = store.load("u"); st["projects"][0]["workflows"][0]["allow_agent"] = False; store.wj(store.state_path("u"), st)
        self.assertIn("ne peut pas être déclenché par un agent", do("a3", do="run_workflow", workflow="Validation")[1])

    def test_limite_de_declenchements_par_heure(self):
        w = self.add_wf("W", [self.step()])
        for _ in range(prules.WF_MAX_PER_HOUR):
            engine._wf_end("u", "p1", engine.workflow_start("u", "p1", w, "utilisateur"), "terminé", "")
        with self.assertRaises(Err) as c:
            engine.workflow_start("u", "p1", w, "utilisateur")
        self.assertEqual(c.exception.code, 429)

    def test_workflow_supprime_pendant_l_execution_et_reprise(self):
        w = self.add_wf("W", [self.step(), self.step(text="suite")])

        def quota(*a):
            raise Err(429, "quota", 5)
        run = self.run_wf(w, quota)
        self.assertEqual((run["status"], run["step"]), ("échoué", 0))
        self.assertIn("étape 1", run["note"])
        with mock.patch.object(engine, "call_ai", self.verdicts("ok")):
            engine.wf_resume("u", "p1", run["id"])                                                  # « Reprendre » : repart de l'étape en échec
        self.assertEqual(proj()["wf_runs"][0]["status"], "terminé")
        st = store.load("u"); st["projects"][0]["workflows"] = []; store.wj(store.state_path("u"), st)
        rid = proj()["wf_runs"][0]["id"]
        self.assertIsNone(engine.wf_resume("u", "p1", rid))                                         # un déroulement terminé ne se reprend pas

    def test_arret_par_l_utilisateur(self):
        w = self.add_wf("W", [self.step(kind="ask_user", text="Continuer ?")])
        self.run_wf(w, self.verdicts("ok"))
        rid = proj()["wf_runs"][0]["id"]
        engine._wf_end("u", "p1", rid, "arrêté", "arrêté par l’utilisateur")
        self.assertEqual(proj()["wf_runs"][0]["status"], "arrêté")

    def test_boucle_limitee(self):
        w = self.add_wf("Boucle", [self.step(on_ko="goto", goto=1)])
        run = self.run_wf(w, self.verdicts("ko"))
        self.assertEqual(run["status"], "arrêté")
        self.assertIn("limite de %d étapes" % prules.WF_MAX_STEPS, run["note"])

    # -- minuteries
    def test_minuterie_quotidienne(self):
        w = {"enabled": True, "schedule": {"mode": "daily", "time": "08:00"}}
        lun = lambda h, m: datetime(2026, 11, 9, h, m)                                              # un lundi
        self.assertEqual(prules.due(w, {}, lun(7, 59)), (False, None))                             # pas encore
        self.assertEqual(prules.due(w, {}, lun(8, 1))[0], True)
        self.assertEqual(prules.due(w, {"last_day": "2026-11-09"}, lun(9, 0)), (False, None))     # déjà fait aujourd'hui
        self.assertEqual(prules.due(w, {}, lun(15, 0)), (False, {"last_day": "2026-11-09"}))       # serveur arrêté : trop tard, on saute
        self.assertEqual(prules.due(dict(w, schedule={"mode": "daily", "time": "08:00", "days": [1, 2]}), {}, lun(8, 1)), (False, None))   # lundi exclu
        self.assertEqual(prules.due(dict(w, enabled=False), {}, lun(8, 1)), (False, None))

    def test_minuterie_par_intervalle(self):
        w = {"enabled": True, "schedule": {"mode": "interval", "every": 30}}
        t0 = datetime(2026, 11, 9, 10, 0)
        due, st = prules.due(w, {}, t0)
        self.assertEqual(due, False)                                                                # 1re observation : le compte à rebours démarre
        self.assertEqual(prules.due(w, st, datetime(2026, 11, 9, 10, 29))[0], False)
        self.assertEqual(prules.due(w, st, datetime(2026, 11, 9, 10, 30))[0], True)

    def test_minuterie_declenche_le_workflow_une_fois_par_jour(self):
        self.add_wf("Matin", [self.step(kind="start_tasks")], schedule={"mode": "daily", "time": "08:00"})

        class Horloge:
            @staticmethod
            def now():
                return datetime(2026, 11, 9, 8, 5)
        with mock.patch.object(engine, "datetime", Horloge):
            engine.wf_timers("u", "p1")
            engine.wf_timers("u", "p1")                                                             # 2e passage du suivi : pas de doublon
        runs = proj()["wf_runs"]
        self.assertEqual((len(runs), runs[0]["trigger"]), (1, "minuterie"))
        self.assertEqual(proj()["wf_state"]["w1"]["last_day"], "2026-11-09")
        self.assertEqual([s[0] for s in self.spawned if s[0] == "wf_continue"], ["wf_continue"])

    def test_minuteries_independantes_du_suivi(self):
        self.set(supervision="off")
        self.add_wf("Matin", [self.step(kind="start_tasks")], schedule={"mode": "daily", "time": "00:00"})

        class Horloge:
            @staticmethod
            def now():
                return datetime(2026, 11, 9, 0, 30)
        with mock.patch.object(engine, "datetime", Horloge):
            engine.tick("u", "p1")
        self.assertEqual(len(proj()["wf_runs"]), 1)                                                 # le suivi est désactivé, la minuterie fonctionne

    # -- cohérence
    def test_controle_de_coherence(self):
        p = {"mainId": "", "autopilot": "on", "rules": "",
             "agents": [{"id": "a", "name": "Bob", "role": "r", "mode": "manual"}], "roles": [{"id": "r", "name": "Dev"}], "tasks": [],
             "conflict_policy": "agent:zzz",
             "workflows": [
                 {"id": "1", "name": "", "steps": []},
                 {"id": "2", "name": "Doublon", "steps": [{"id": "x", "kind": "ask", "who": "principal", "text": "a"}]},
                 {"id": "3", "name": "doublon", "schedule": {"mode": "daily", "time": "25:99"}, "steps": [
                     {"id": "y", "kind": "ask", "who": "assignee", "text": ""},
                     {"id": "z", "kind": "ask", "who": "agent:a", "text": "t", "on_ko": "goto", "goto": 9},
                     {"id": "w", "kind": "ask", "who": "role:inconnu", "text": "t", "on_ko": "goto", "goto": 1},
                     {"id": "v", "kind": "start_tasks"}]},
                 {"id": "4", "name": "Court", "enabled": False, "schedule": {"mode": "interval", "every": 2}, "steps": [{"id": "u", "kind": "ask_user", "text": "ok ?", "on_ko": "ask_user"}]}]}
        out = prules.lint(p)
        texts = " ¦ ".join("%s|%s|%s" % (i["level"], i["where"], i["text"]) for i in out)
        for attendu in ("n’a pas de nom", "Aucune étape définie", "même nom", "Heure de déclenchement invalide", "Instruction vide", "aucun agent principal n’est défini",
                        "minuterie, qui ne fournit aucune tâche", "Saut vers une étape inexistante", "Aucun agent n’a le rôle désigné", "Retour vers l’étape 1 : boucle possible",
                        "Intervalle très court", "désactivé : elle ne se déclenchera pas", "La règle désigne un agent qui n’existe plus", "Aucun agent principal", "Aucune règle globale",
                        "est en chat manuel", "déjà actif"):
            self.assertIn(attendu, texts)
        self.assertEqual({i["level"] for i in out}, {"erreur", "avertissement", "info"})

    def test_workflow_coherent_sans_erreur(self):
        self.add_wf("Valide", [self.step("principal", "Vérifie"), self.step("role:r3", "Teste")], schedule={"mode": "daily", "time": "08:00", "days": [0, 1, 2, 3, 4]})
        self.set(rules="Règle claire.")
        self.assertEqual([i for i in prules.lint(proj()) if i["level"] == "erreur"], [])

    def test_analyse_de_coherence_par_un_agent(self):
        self.set(rules="Les agents ne doivent jamais fusionner dans la branche principale.")
        self.set_agent(1, rules="Tu peux fusionner directement dans main quand tu as fini.")
        self.add_wf("Validation", [self.step("principal", "Vérifie")])
        vus = []

        def analyste(s, system, msgs):
            vus.append(msgs[-1]["content"])
            return ('Voici mon analyse.\n```action\n' + json.dumps({"do": "rules_report", "issues": [
                {"severity": "erreur", "where": "Règles de Bob", "text": "Contredit la règle globale sur la branche principale.", "suggestion": "Retirer la phrase."},
                {"severity": "n'importe quoi", "where": "Workflow", "text": "Étape imprécise.", "suggestion": ""}, {"severity": "info", "text": ""}]}) + '\n```')
        with mock.patch.object(engine, "call_ai", analyste):
            engine.rules_analysis("u", "p1")
        rr = proj()["rules_report"]
        self.assertEqual([(i["level"], i["where"]) for i in rr["ai"]], [("erreur", "Règles de Bob"), ("info", "Workflow")])   # niveau invalide ramené à info, point vide ignoré
        self.assertEqual((rr["analyzing"], rr["ai_note"]), ("", ""))
        for attendu in ("Règles intégrées", "Les agents ne doivent jamais fusionner", "Tu peux fusionner directement", "« Validation »", "Autonomie des agents"):
            self.assertIn(attendu, vus[0])                                                          # l'agent reçoit toutes les règles et les réglages
        self.assertIn("refusée" if False else "Aucune analyse", engine.do_act("u", "p1", "a2", {"do": "rules_report", "issues": []}, 0)[1])   # personne d'autre ne peut écrire le rapport

    def test_analyse_reponse_non_structuree_ou_impossible(self):
        self.set(rules="R")
        with mock.patch.object(engine, "call_ai", lambda *a: "Tout me semble cohérent."):
            engine.rules_analysis("u", "p1")
        rr = proj()["rules_report"]
        self.assertTrue(rr["ai"][0]["text"] == "Tout me semble cohérent." and "non structurée" in rr["ai_note"] and rr["analyzing"] == "")
        for ag in range(3):
            self.set_agent(ag, mode="manual")
        engine.rules_analysis("u", "p1")
        self.assertIn("aucun agent en mode API", proj()["rules_report"]["ai_note"])
        self.set(rules="")
        engine.rules_analysis("u", "p1")
        self.assertEqual(proj()["rules_report"]["ai_note"], "Aucune règle à analyser.")

    def test_verification_a_la_demande(self):
        self.add_wf("Vide", [])
        with mock.patch.object(engine, "call_ai", lambda *a: "RAS"):
            engine.rules_check("u", "p1")
        rr = proj()["rules_report"]
        self.assertTrue(any("Aucune étape définie" in i["text"] for i in rr["static"]) and rr["ai"])
        self.assertEqual(proj().get("rules_log", []), [])                                           # un simple contrôle ne notifie personne


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

    def test_diagnostic_http(self):
        self.call("POST", "/api/register", {"user": "h3", "password": "x"})
        tk = self.call("POST", "/api/login", {"user": "h3", "password": "x"})["token"]
        r0 = self.call("GET", "/api/state", tok=tk)
        self.call("PUT", "/api/state", {"rev": r0["rev"], "state": {"projects": [{"id": "p", "name": "P", "agents": [{"id": "a", "name": "Bob"}], "tasks": [], "mrs": [], "decisions": [],
                  "conflicts": [{"id": "c", "n": 1, "agent": "a", "files": ["x.py"], "status": "pause"}]}]}}, tk)
        r = self.call("GET", "/api/diagnostic?project=p", tok=tk)
        self.assertTrue(r["items"][0]["level"] == "bloquant" and r["items"][0]["conflict"] == "c" and "x.py" in r["items"][0]["text"])
        self.assertEqual(self.call("GET", "/api/diagnostic?project=zzz", tok=tk)["_code"], 404)
        self.assertEqual(self.call("GET", "/api/diagnostic?project=p")["_code"], 401)

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
