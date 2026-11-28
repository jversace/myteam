"""Outils tiers (Lot 1, type CLI) : validation, construction sans shell, exécution encadrée, droits par rôle, accord de l'utilisateur, arrêt d'agent."""
import os
import stat
import sys
import tempfile
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

os.environ.setdefault("APM_DATA", tempfile.mkdtemp())

import engine  # noqa: E402
import locks  # noqa: E402
import rights  # noqa: E402
import system_actions as sysact  # noqa: E402
import store  # noqa: E402
import tools  # noqa: E402
from test_locks import make_state, proj  # noqa: E402

PY = sys.executable
LIRE = {"id": "t1", "name": "lire", "desc": "Affiche un fichier en majuscules", "command": PY, "kind": "cli", "level": "free", "enabled": True, "timeout": 20,
        "args_text": "-c\nimport sys; print(open(sys.argv[1], encoding='utf-8').read().strip().upper())\n{fichier}",
        "params_text": "fichier ; chemin ; obligatoire ; fichier à afficher ; existant"}


def tool(**k):
    return {**LIRE, **k}


class Definition(unittest.TestCase):
    def test_outil_valide(self):
        v = tools.validate_def(LIRE)
        self.assertEqual(v["errors"], [])
        self.assertIn("<fichier>", v["preview"])

    def test_nom_programme_et_placeholders(self):
        self.assertTrue(tools.validate_def(tool(name="Mauvais Nom"))["errors"])
        self.assertTrue(tools.validate_def(tool(command=""))["errors"])
        self.assertTrue(tools.validate_def(tool(command="pytest -q && ls"))["errors"])        # arguments ou enchaînement dans le programme
        self.assertTrue(tools.validate_def(tool(args_text="{inconnu}"))["errors"])             # paramètre non déclaré
        self.assertTrue(tools.validate_def(tool(args_text="-c\nprint(1)"))["errors"])         # paramètre obligatoire inutilisé
        self.assertTrue(tools.validate_def(tool(workdir="../dehors"))["errors"])
        self.assertTrue(tools.validate_def(tool(timeout="abc"))["errors"])

    def test_shells_et_elevation_refuses_d_office(self):
        for c in ("bash", "/bin/sh", "cmd.exe", "powershell", "pwsh", "sudo", "env"):
            e = tools.validate_def(tool(command=c, args_text="x", params_text=""))["errors"]
            self.assertTrue(e, c)

    def test_motif_refuse_par_l_analyse_de_vigilance(self):
        e = tools.validate_def({"name": "pousse", "command": "git", "args_text": "push\norigin", "params_text": ""})["errors"]
        self.assertTrue(any("refusée d'office" in x for x in e), e)

    def test_parametres(self):
        ps, errs = tools.parse_params("mode ; choix ; obligatoire ; mode ; a|b\nn ; entier ; facultatif ; nombre ; 1..5\nre ; texte ; o ; x ; [a-z]+\nf ; chemin ; facultatif ; f ; existant")
        self.assertEqual(errs, [])
        self.assertEqual([p["type"] for p in ps], ["choice", "int", "text", "path"])
        self.assertEqual(ps[1]["max"], 5)
        self.assertTrue(ps[3]["exists"])
        for bad in ("1a ; texte", "x ; inconnu", "x ; choix ; o ; d", "x ; entier ; o ; d ; a..b", "x ; texte ; o ; d ; ([", "x ; texte\nx ; texte"):
            self.assertTrue(tools.parse_params(bad)[1], bad)

    def test_empreinte_change_avec_la_commande(self):
        self.assertEqual(tools.def_hash(LIRE), tools.def_hash(dict(LIRE, desc="autre texte")))
        self.assertNotEqual(tools.def_hash(LIRE), tools.def_hash(dict(LIRE, args_text="-c\nprint(2)\n{fichier}")))


class Construction(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        Path(self.d, "a.txt").write_text("bonjour", encoding="utf-8")
        Path(self.d, "sous").mkdir()

    def build(self, vals, t=None, **k):
        return tools.build_argv(t or LIRE, vals, self.d, **k)

    def test_commande_sans_shell(self):
        b = self.build({"fichier": "a.txt"})
        self.assertTrue(b["ok"], b)
        self.assertIsInstance(b["argv"], list)
        self.assertEqual(b["argv"][-1], "a.txt")
        self.assertEqual(os.path.realpath(b["cwd"]), os.path.realpath(self.d))

    def test_chemin_confine(self):
        for v in ("../x", "/etc/passwd", "sous/../../x"):
            self.assertFalse(self.build({"fichier": v})["ok"], v)
        self.assertFalse(self.build({"fichier": "absent.txt"})["ok"])                          # « existant »
        self.assertFalse(self.build({"fichier": ".git/config"}, tool(params_text="fichier ; chemin ; obligatoire ; f"))["ok"])

    @unittest.skipIf(os.name == "nt", "liens symboliques")
    def test_lien_symbolique_vers_l_exterieur(self):
        out = tempfile.mkdtemp()
        Path(out, "secret.txt").write_text("s", encoding="utf-8")
        os.symlink(out, os.path.join(self.d, "lien"))
        self.assertFalse(self.build({"fichier": "lien/secret.txt"})["ok"])

    def test_option_deguisee_et_valeurs_invalides(self):
        t = tool(args_text="-c\nprint(1)\n{mot}", params_text="mot ; texte ; obligatoire ; mot")
        self.assertFalse(self.build({"mot": "--version"}, t)["ok"])
        self.assertFalse(self.build({"mot": "a\nb"}, t)["ok"])
        self.assertFalse(self.build({"mot": "x" * 600}, t)["ok"])
        self.assertFalse(self.build({"mot": ["a"]}, t)["ok"])
        self.assertFalse(self.build({"mot": "ok", "autre": "x"}, t)["ok"])                     # paramètre inconnu
        self.assertFalse(self.build({}, t)["ok"])                                              # obligatoire manquant
        self.assertTrue(self.build({"mot": "valeur; rm -rf /"}, t)["ok"])                       # du texte, jamais interprété par un shell

    def test_motif_choix_entier(self):
        t = tool(args_text="-c\nprint(1)\n{m}\n{n}\n{c}", params_text="m ; texte ; o ; d ; [a-z]+\nn ; entier ; o ; d ; 1..5\nc ; choix ; o ; d ; a|b")
        self.assertTrue(self.build({"m": "abc", "n": 3, "c": "a"}, t)["ok"])
        self.assertFalse(self.build({"m": "ABC", "n": 3, "c": "a"}, t)["ok"])
        self.assertFalse(self.build({"m": "abc", "n": 9, "c": "a"}, t)["ok"])
        self.assertFalse(self.build({"m": "abc", "n": "x", "c": "a"}, t)["ok"])
        self.assertFalse(self.build({"m": "abc", "n": 3, "c": "z"}, t)["ok"])

    def test_argument_lie_a_un_parametre_facultatif_absent_est_omis(self):
        t = tool(args_text="-c\nprint(1)\n--nom={nom}\n{n}", params_text="nom ; texte ; facultatif ; d\nn ; entier ; o ; d")
        b = self.build({"n": 2}, t)
        self.assertTrue(b["ok"])
        self.assertEqual(b["argv"][1:], ["-c", "print(1)", "2"])

    def test_programme_introuvable_ou_dans_l_espace_de_travail(self):
        self.assertFalse(self.build({"fichier": "a.txt"}, tool(command="programme-qui-n-existe-pas-xyz"))["ok"])
        if os.name != "nt":
            sc = Path(self.d, "mon_outil.sh")
            sc.write_text("#!/bin/sh\necho x\n", encoding="utf-8")
            sc.chmod(sc.stat().st_mode | stat.S_IXUSR)
            r = self.build({}, tool(command=str(sc), args_text="", params_text=""))
            self.assertFalse(r["ok"])
            self.assertIn("espace de travail", r["error"])
            r = tools.build_argv(tool(), {"fichier": "a.txt"}, self.d, roots=[os.path.dirname(PY)])        # dossier « modifiable par les agents »
            self.assertFalse(r["ok"])

    def test_sous_dossier_de_travail(self):
        b = self.build({"fichier": "a.txt"}, tool(workdir="sous", params_text="fichier ; texte ; obligatoire ; f"))
        self.assertTrue(b["ok"])
        self.assertTrue(b["cwd"].endswith("sous"))
        self.assertFalse(self.build({"fichier": "a.txt"}, tool(workdir="absent"))["ok"])

    def test_outil_mal_configure_ou_autre_plateforme(self):
        self.assertFalse(self.build({"fichier": "a.txt"}, tool(command="bash"))["ok"])
        autre = "windows" if os.name != "nt" else "linux"
        self.assertFalse(self.build({"fichier": "a.txt"}, tool(platform=autre))["ok"])


class Execution(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_succes_echec_et_sortie_nettoyee(self):
        r = tools.run([PY, "-c", "print('bonjour')"], self.d, 20)
        self.assertTrue(r["ok"])
        self.assertIn("bonjour", r["tail"])
        r = tools.run([PY, "-c", "import sys; print('erreur', file=sys.stderr); sys.exit(3)"], self.d, 20)
        self.assertFalse(r["ok"])
        self.assertEqual(r["rc"], 3)
        self.assertIn("erreur", r["tail"])

    def test_delai_maximal(self):
        t0 = time.time()
        r = tools.run([PY, "-c", "import time; time.sleep(30)"], self.d, 1)
        self.assertTrue(r["timed_out"])
        self.assertFalse(r["ok"])
        self.assertLess(time.time() - t0, 15)

    def test_environnement_nettoye(self):
        os.environ["GITHUB_TOKEN"] = "secret-xyz"
        try:
            r = tools.run([PY, "-c", "import os; print(os.environ.get('GITHUB_TOKEN', 'absent'))"], self.d, 20)
        finally:
            del os.environ["GITHUB_TOKEN"]
        self.assertIn("absent", r["tail"])

    def test_lancement_impossible(self):
        r = tools.run([os.path.join(self.d, "nexiste-pas")], self.d, 5)
        self.assertFalse(r["ok"])
        self.assertIn("Lancement impossible", r["tail"])

    def test_sortie_volumineuse_limitee(self):
        r = tools.run([PY, "-c", "print('x' * 10 + chr(10)) ; [print('ligne', i) for i in range(20000)]"], self.d, 30)
        self.assertLessEqual(len(r["tail"]), 2600)
        self.assertIn("ligne 19999", r["tail"])

    def test_arret_de_l_agent_interrompt_l_outil(self):
        res = {}
        key = ("u", "p1", "a2", "k1")
        th = threading.Thread(target=lambda: res.update(tools.run([PY, "-c", "import time; time.sleep(60)"], self.d, 120, key=key, lock_id="kill")))
        th.start()
        for _ in range(100):
            if key in tools.RUNNING:
                break
            time.sleep(0.05)
        self.assertEqual(tools.kill_agent("u", "p1", "a2"), 1)
        th.join(15)
        self.assertFalse(th.is_alive())
        self.assertTrue(res["killed"])
        self.assertFalse(res["ok"])
        self.assertEqual(tools.kill_agent("u", "p1", "a2"), 0)


class Moteur(unittest.TestCase):
    """Agent bob (a2, rôle r2, droit sur l'outil) ; alice (a1, rôle r1, aucun droit)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "a.txt").write_text("bonjour", encoding="utf-8")
        self.set_state()
        locks.STOP.clear()

    def set_state(self, t=None, grant=True, level="free"):
        st = make_state(self.tmp)
        p = st["projects"][0]
        st["roles"] = [{"id": "r1", "name": "Chef de projet", "desc": ""}, {"id": "r2", "name": "Développeur back-end", "desc": "", "tools": {"t1": True} if grant else {}}]
        p.pop("roles")
        st["tools"] = [dict(t or LIRE, level=level)]
        p["tasks"] = []
        store.wj(store.state_path("u"), st)

    def act(self, who, kind, **x):
        return engine.do_act("u", "p1", who, {"do": kind, **x}, 0, {"n": 0, "t0": time.time()})

    def agent(self, aid):
        p = store.load("u")["projects"][0]
        return p, next(a for a in p["agents"] if a["id"] == aid)

    def test_droits_par_role(self):
        p, bob = self.agent("a2")
        _, alice = self.agent("a1")
        self.assertEqual(rights.tool_level(p, bob, p["_tools"][0]), rights.FREE)
        self.assertEqual(rights.tool_level(p, alice, p["_tools"][0]), rights.NO)                # même l'agent principal : seul le rôle donne le droit
        self.assertTrue(engine.action_applies(p, bob, "tool_run"))
        self.assertTrue(engine.action_applies(p, bob, "tools_list"))
        self.assertFalse(engine.action_applies(p, alice, "tool_run"))
        n, back = self.act("a1", "tool_run", tool="lire", params={"fichier": "a.txt"})
        self.assertIn("Aucun outil", back)
        self.assertEqual(proj().get("tool_runs", []), [])

    def test_outil_desactive_ou_interdit(self):
        for kw in ({"level": "no"}, {"t": dict(LIRE, enabled=False)}):
            self.set_state(**kw)
            p, bob = self.agent("a2")
            self.assertFalse(engine.action_applies(p, bob, "tool_run"), kw)

    def test_outil_non_valide_non_propose(self):
        self.set_state(t=dict(LIRE, command="bash"))
        p, bob = self.agent("a2")
        self.assertFalse(engine.action_applies(p, bob, "tool_run"))

    def test_actions_dans_le_prompt_seulement_avec_droit(self):
        p, bob = self.agent("a2")
        _, alice = self.agent("a1")
        self.assertIn('"do":"tool_run"', engine.actions_text(p, bob))
        self.assertNotIn('"do":"tool_run"', engine.actions_text(p, alice))

    def test_liste_des_outils(self):
        n, back = self.act("a2", "tools_list")
        self.assertIn("lire", back)
        self.assertIn("fichier", back)
        self.assertIn("Lancement libre", back)

    def test_lancement_libre(self):
        n, back = self.act("a2", "tool_run", tool="lire", params={"fichier": "a.txt"}, why="vérifier")
        self.assertIn("BONJOUR", back)
        self.assertIn("réussi", back)
        runs = proj()["tool_runs"]
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0]["ok"])
        self.assertEqual(runs[0]["by"], "Bob")
        self.assertEqual(runs[0]["approved"], "libre")
        self.assertTrue(any("Outil « lire » réussi" in m["text"] for m in proj()["log"]))

    def test_parametre_invalide_refuse_avec_usage(self):
        n, back = self.act("a2", "tool_run", tool="lire", params={"fichier": "../secret"})
        self.assertIn("refusé", back)
        self.assertIn("Usage", back)
        self.assertEqual(proj().get("tool_runs", []), [])
        n, back = self.act("a2", "tool_run", tool="inconnu", params={})
        self.assertIn("lire", back)                                                              # liste des outils disponibles

    def test_pas_d_espace_de_travail_en_mode_github(self):
        st = store.load("u")
        for a in st["projects"][0]["agents"]:
            a["source"] = "github"
        store.wj(store.state_path("u"), st)
        p, bob = self.agent("a2")
        self.assertFalse(engine.action_applies(p, bob, "tool_run"))

    def test_plafond_par_echange(self):
        ctx = {"n": 0, "t0": time.time()}
        outs = [engine.do_act("u", "p1", "a2", {"do": "tool_run", "tool": "lire", "params": {"fichier": "a.txt"}}, 0, ctx) for _ in range(engine.TOOL_PER_EXCHANGE + 1)]
        self.assertIn("Trop d'outils", outs[-1][1])
        self.assertEqual(len(proj()["tool_runs"]), engine.TOOL_PER_EXCHANGE)

    # --- accord de l'utilisateur
    def demande(self):
        self.set_state(level="approve")
        n, back = self.act("a2", "tool_run", tool="lire", params={"fichier": "a.txt"}, why="relire")
        d = [x for x in proj()["decisions"] if x["kind"] == "tool"]
        return n, back, d

    def test_avec_accord_rien_ne_s_execute_avant(self):
        n, back, d = self.demande()
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["status"], "attente")
        self.assertIn("rien ne s’exécute avant", back)
        self.assertIn(os.path.basename(PY), d[0]["payload"]["command"])
        self.assertEqual(proj().get("tool_runs", []), [])

    def test_demande_identique_dedoublonnee_demandes_differentes_conservees(self):
        self.demande()
        n, back = self.act("a2", "tool_run", tool="lire", params={"fichier": "a.txt"}, why="encore")
        self.assertIn("attend déjà", back)
        (self.tmp / "b.txt").write_text("autre", encoding="utf-8")
        self.act("a2", "tool_run", tool="lire", params={"fichier": "b.txt"})
        self.assertEqual(len([x for x in proj()["decisions"] if x["kind"] == "tool" and x["status"] == "attente"]), 2)

    def test_approbation_execute_et_remet_le_resultat_a_l_agent(self):
        n, back, d = self.demande()
        with mock.patch.object(engine, "run_task") as rt:
            engine.decide("u", "p1", d[0]["id"], "accept")
        p = proj()
        self.assertEqual(next(x for x in p["decisions"] if x["id"] == d[0]["id"])["status"], "acceptée")
        self.assertEqual(p["tool_runs"][0]["approved"], "Vous")
        self.assertIn("BONJOUR", p["tool_runs"][0]["tail"])
        bob = next(a for a in p["agents"] if a["id"] == "a2")
        self.assertIn("BONJOUR", bob["inbox"][0]["text"])                                        # sans tâche : boîte de notifications, 0 appel IA
        rt.assert_not_called()

    def test_approbation_relance_la_tache_de_l_agent(self):
        self.set_state(level="approve")
        st = store.load("u")
        st["projects"][0]["tasks"] = [{"id": "t9", "title": "Lire", "desc": "", "who": "a2", "status": "en cours", "modules": []}]
        store.wj(store.state_path("u"), st)
        self.act("a2", "tool_run", tool="lire", params={"fichier": "a.txt"})
        d = [x for x in proj()["decisions"] if x["kind"] == "tool"][0]
        self.assertEqual(d["task"], "t9")
        with mock.patch.object(engine, "run_task") as rt:
            engine.decide("u", "p1", d["id"], "accept")
        rt.assert_called_once()
        self.assertEqual(rt.call_args.args[2], "t9")
        self.assertIn("BONJOUR", rt.call_args.kwargs["note"])

    def test_refus_ne_lance_rien_et_previent_l_agent(self):
        n, back, d = self.demande()
        with mock.patch.object(engine, "ask") as ask:
            engine.decide("u", "p1", d[0]["id"], "refuse", note="pas maintenant")
        self.assertEqual(proj().get("tool_runs", []), [])
        self.assertEqual(next(x for x in proj()["decisions"] if x["id"] == d[0]["id"])["status"], "refusée")
        self.assertIn("pas maintenant", ask.call_args.args[3])

    def test_definition_modifiee_apres_la_demande_annule_l_execution(self):
        n, back, d = self.demande()
        st = store.load("u")
        st["tools"][0]["args_text"] = "-c\nprint('PIRATÉ')\n{fichier}"
        store.wj(store.state_path("u"), st)
        engine.decide("u", "p1", d[0]["id"], "accept")
        p = proj()
        self.assertEqual(p.get("tool_runs", []), [])
        x = next(x for x in p["decisions"] if x["id"] == d[0]["id"])
        self.assertEqual(x["status"], "refusée")
        self.assertIn("modifiée", x["note"])
        bob = next(a for a in p["agents"] if a["id"] == "a2")
        self.assertIn("n’a pas été exécutée", bob["inbox"][0]["text"])

    def test_droit_retire_apres_la_demande_annule_l_execution(self):
        n, back, d = self.demande()
        st = store.load("u")
        st["roles"][1]["tools"] = {}
        store.wj(store.state_path("u"), st)
        engine.decide("u", "p1", d[0]["id"], "accept")
        self.assertEqual(proj().get("tool_runs", []), [])

    def test_un_agent_ne_peut_pas_modifier_le_registre(self):
        before = store.load("u")["tools"]
        for kind in ("tool", "tool_add", "tools"):
            self.act("a2", kind, name="x", command="ls")
        self.assertEqual(store.load("u")["tools"], before)

    # --- arrêt d'un agent
    def test_arret_de_l_agent_interrompt_son_outil(self):
        self.set_state(t=dict(LIRE, args_text="-c\nimport time; time.sleep(60)\n{fichier}"))
        out = {}
        th = threading.Thread(target=lambda: out.update(r=self.act("a2", "tool_run", tool="lire", params={"fichier": "a.txt"})))
        th.start()
        for _ in range(100):
            if any(k[2] == "a2" for k in tools.RUNNING):
                break
            time.sleep(0.05)
        txt = engine.stop_agent("u", "p1", "a2", by="Vous", reason="test")
        th.join(20)
        self.assertFalse(th.is_alive())
        self.assertIn("outil(s) en cours interrompu(s)", txt)
        self.assertIn("interrompu", out["r"][1])
        locks.STOP.clear()


class Serveur(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.sv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.base = "http://127.0.0.1:%d" % cls.sv.server_address[1]
        threading.Thread(target=cls.sv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.sv.shutdown()
        cls.sv.server_close()

    def post(self, path, body, token=None):
        rq = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
        with urllib.request.urlopen(rq) as r:
            return json.loads(r.read())

    def test_controle_des_outils_exige_une_session(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post("/api/tools/check", {"tools": [LIRE]})
        self.assertIn(cm.exception.code, (401, 403))

    def test_controle_des_outils(self):
        tok = self.post("/api/register", {"user": "outils_test", "password": "mdp-outils-1"})["token"]
        r = self.post("/api/tools/check", {"tools": [LIRE, dict(LIRE, id="t2", command="bash")]}, tok)["results"]
        self.assertEqual(r["t1"]["errors"], [])
        self.assertTrue(r["t2"]["errors"])
        self.assertIn("<fichier>", r["t1"]["preview"])


class Front(unittest.TestCase):
    def test_menu_outils_et_cartes(self):
        html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
        for k in ('data-v="tools"', "function toolsView()", "function toolCard(", "function toolRunsCard(", 'a=="newtl"', "/api/tools/check", "tools:[]"):
            self.assertIn(k, html, k)


# ====================================================================================================== Lot 2 : outils API, propositions, workflows
class Echo(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8") if n else ""
        if self.path.startswith("/redir"):
            self.send_response(302)
            self.send_header("Location", "/ailleurs")
            self.end_headers()
            return
        if self.path.startswith("/lent"):
            time.sleep(3)
        code = 404 if self.path.startswith("/absent") else 200
        out = json.dumps({"method": self.command, "path": self.path, "body": body, "auth": self.headers.get("Authorization", ""), "ctype": self.headers.get("Content-Type", "")}).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
    do_GET = do_POST = do_PUT = do_DELETE = _go


API = {"id": "ta", "name": "api-items", "desc": "Crée un élément", "kind": "api", "method": "POST", "url": "http://127.0.0.1:{port}/items/{id}?q={q}", "level": "free", "enabled": True,
       "headers_text": "Authorization: Bearer SECRET", "body_text": "{\"nom\": \"{nom}\", \"n\": \"{n}\", \"opt\": \"{opt}\"}",
       "params_text": "id ; entier ; o ; id\nq ; texte ; o ; q\nnom ; texte ; o ; nom\nn ; entier ; o ; n\nopt ; texte ; facultatif ; o", "timeout": 10}


class ServeurEcho(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sv = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
        cls.port = cls.sv.server_address[1]
        threading.Thread(target=cls.sv.serve_forever, daemon=True).start()
        cls.api = dict(API, url=API["url"].replace("{port}", str(cls.port)))

    @classmethod
    def tearDownClass(cls):
        cls.sv.shutdown()
        cls.sv.server_close()


class OutilApi(ServeurEcho):
    def test_definition(self):
        self.assertEqual(tools.validate_def(self.api)["errors"], [])
        self.assertIn("POST http://127.0.0.1", tools.validate_def(self.api)["preview"])
        bad = lambda **k: tools.validate_def(dict(self.api, **k))["errors"]
        self.assertTrue(bad(url="http://exemple.org/x/{id}?q={q}"))                        # adresse non locale sans autorisation
        self.assertEqual(tools.validate_def(dict(self.api, url="http://exemple.org/{id}?q={q}", remote=True))["risk"]["level"], "élevée")
        self.assertTrue(bad(url="ftp://127.0.0.1/x"))
        self.assertTrue(bad(url="http://{id}.local/x?q={q}"))                              # hôte à paramètre
        self.assertTrue(bad(url="http://user:pass@127.0.0.1/{id}?q={q}"))                  # identifiants dans l'adresse
        self.assertTrue(bad(headers_text="X-Cle: {nom}"))                                  # un en-tête ne reçoit jamais de paramètre
        self.assertTrue(bad(headers_text="Host: autre"))
        self.assertTrue(bad(body_text="{pas du json"))
        self.assertTrue(bad(method="GET"))                                                 # GET sans corps
        self.assertTrue(bad(method="TRACE"))
        self.assertTrue(bad(params_text=API["params_text"] + "\nf ; chemin ; facultatif ; f"))    # pas de chemin pour une API
        self.assertTrue(bad(body_text='{"x": "{inconnu}"}'))

    def test_construction_echappee(self):
        b = tools.prepare(self.api, {"id": 7, "q": "a b&c=d/e", "nom": 'x"y\\z', "n": "4"})
        self.assertTrue(b["ok"], b)
        self.assertIn("/items/7?q=a%20b%26c%3Dd%2Fe", b["url"])
        self.assertEqual(json.loads(b["body"]), {"nom": 'x"y\\z', "n": 4})                  # entier typé ; clé facultative absente supprimée
        self.assertNotIn("SECRET", b["display"])                                           # les en-têtes ne sont jamais affichés
        self.assertTrue(tools.prepare(self.api, {"id": 1, "q": "-x", "nom": "a", "n": 1})["ok"])      # « - » n'est une option que pour un programme
        self.assertFalse(tools.prepare(self.api, {"id": "x", "q": "a", "nom": "a", "n": 1})["ok"])
        self.assertFalse(tools.prepare(self.api, {"id": 1, "q": "a", "nom": "a", "n": 1, "z": 1})["ok"])

    def test_structure_du_corps_non_injectable(self):
        b = tools.prepare(self.api, {"id": 1, "q": "a", "nom": '", "admin": true, "x": "', "n": 1})
        self.assertEqual(sorted(json.loads(b["body"])), ["n", "nom"])

    def test_appel_reel(self):
        b = tools.prepare(self.api, {"id": 7, "q": "z", "nom": "Bob", "n": 2})
        r = tools.execute(self.api, b)
        self.assertTrue(r["ok"])
        self.assertEqual(r["rc"], 200)
        got = json.loads(r["tail"])
        self.assertEqual((got["method"], got["path"], got["auth"], got["ctype"]), ("POST", "/items/7?q=z", "Bearer SECRET", "application/json"))
        self.assertEqual(json.loads(got["body"]), {"nom": "Bob", "n": 2})

    def test_erreur_http_redirection_delai_injoignable(self):
        g = dict(self.api, method="GET", body_text="", url="http://127.0.0.1:%d/{id}?q={q}" % self.port, params_text="id ; texte ; o ; id\nq ; texte ; o ; q")
        r = tools.execute(g, tools.prepare(g, {"id": "absent", "q": "1"}))
        self.assertFalse(r["ok"])
        self.assertEqual(r["rc"], 404)
        r = tools.execute(g, tools.prepare(g, {"id": "redir", "q": "1"}))
        self.assertEqual(r["rc"], 302)                                                     # redirection NON suivie
        r = tools.execute(dict(g, timeout=1), tools.prepare(g, {"id": "lent", "q": "1"}))
        self.assertTrue(r["timed_out"])
        morte = dict(g, url="http://127.0.0.1:1/{id}?q={q}")
        r = tools.execute(morte, tools.prepare(morte, {"id": "a", "q": "1"}))
        self.assertFalse(r["ok"])
        self.assertIn("impossible", r["tail"])

    def test_pas_d_espace_de_travail(self):
        self.assertFalse(tools.needs_workspace(self.api))
        self.assertTrue(tools.needs_workspace(LIRE))
        self.assertNotEqual(tools.def_hash(self.api), tools.def_hash(dict(self.api, headers_text="Authorization: autre")))


class MoteurLot2(ServeurEcho):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "a.txt").write_text("bonjour", encoding="utf-8")
        locks.STOP.clear()
        self.state(tools_list=[dict(LIRE), dict(self.api)])

    def state(self, tools_list, roles_tools=None, caps=None, source=None):
        st = make_state(self.tmp)
        p = st["projects"][0]
        st["roles"] = [{"id": "r1", "name": "Chef de projet", "desc": ""},
                       {"id": "r2", "name": "Développeur back-end", "desc": "", "tools": roles_tools if roles_tools is not None else {t["id"]: True for t in tools_list}, **({"caps": caps} if caps else {})}]
        p.pop("roles")
        if source:
            for a in p["agents"]:
                a["source"] = source
        st["tools"] = tools_list
        store.wj(store.state_path("u"), st)

    def act(self, who, do_, **x):
        return engine.do_act("u", "p1", who, {"do": do_, **x}, 0, {"n": 0, "t0": time.time()})

    def agent(self, aid="a2"):
        p = store.load("u")["projects"][0]
        return p, next(a for a in p["agents"] if a["id"] == aid)

    # --- outils API
    def test_outil_api_utilisable_sans_espace_de_travail(self):
        self.state([dict(self.api)], source="github")                                      # agent GitHub : pas de répertoire local
        p, bob = self.agent()
        self.assertTrue(engine.action_applies(p, bob, "tool_run"))
        n, back = self.act("a2", "tool_run", tool="api-items", params={"id": 3, "q": "x", "nom": "Bob", "n": 1})
        self.assertIn("réussi", back)
        self.assertIn("/items/3?q=x", back)
        self.assertNotIn("SECRET", store.load("u")["projects"][0]["tool_runs"][0]["cmd"])

    def test_outil_cli_toujours_indisponible_en_mode_github(self):
        self.state([dict(LIRE)], source="github")
        p, bob = self.agent()
        self.assertFalse(engine.action_applies(p, bob, "tool_run"))

    # --- propositions d'outils
    PROP = dict(name="compte-lignes", kind="cli", desc="Compte les lignes d'un fichier", command=PY,
                args=["-c", "import sys; print(len(open(sys.argv[1]).read().splitlines()))", "{fichier}"], params=["fichier ; chemin ; obligatoire ; fichier ; existant"],
                explain="Cet outil lit un fichier et affiche son nombre de lignes. Il ne modifie rien.", reason="Pour mesurer la taille des fichiers")

    def propose(self, **k):
        return self.act("a2", "tool_propose", **{**self.PROP, **k})

    def decisions(self, kind="toolnew"):
        return [d for d in store.load("u")["projects"][0].get("decisions", []) if d["kind"] == kind]

    def test_proposition_enregistree_sans_rien_ajouter(self):
        avant = store.load("u")["tools"]
        n, back = self.propose()
        d = self.decisions()
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["status"], "attente")
        self.assertEqual(store.load("u")["tools"], avant)
        self.assertIn("Proposition enregistrée", back)
        self.assertIn("fichier", d[0]["payload"]["preview"] + d[0]["payload"]["tool"]["params_text"])

    def test_proposition_invalide_ou_incomplete_refusee_sans_carte(self):
        self.assertIn("invalide", self.propose(command="bash", args=["-c", "ls"], params=[])[1])
        self.assertIn("incomplète", self.propose(explain="court")[1])
        self.assertIn("incomplète", self.propose(name="")[1])
        self.assertIn("invalide", self.propose(args=["{inconnu}"])[1])
        self.assertEqual(self.decisions(), [])

    def test_droit_de_proposer(self):
        self.state([dict(LIRE)], caps={"tool": "no"})
        p, bob = self.agent()
        self.assertFalse(engine.action_applies(p, bob, "tool_propose"))
        n, back = self.propose()
        self.assertIn("t’autorise pas", back)
        self.assertEqual(self.decisions(), [])

    def test_agent_ne_fixe_ni_en_tetes_ni_adresse_distante(self):
        n, back = self.act("a2", "tool_propose", name="api-neuve", kind="api", desc="API locale", url="http://exemple.org/x", method="GET", headers_text="Authorization: pirate",
                           explain="Appelle une API sur Internet pour récupérer des données.", reason="r")
        self.assertIn("non locale", back)
        self.assertEqual(self.decisions(), [])
        n, back = self.act("a2", "tool_propose", name="api-neuve", kind="api", desc="API locale", url="http://127.0.0.1:%d/x" % self.port, method="GET", headers_text="Authorization: pirate",
                           remote=True, explain="Appelle une API locale pour récupérer des données.", reason="r")
        self.assertIn("enregistrée", back)
        self.assertNotIn("headers_text", self.decisions()[0]["payload"]["tool"])
        self.assertNotIn("remote", self.decisions()[0]["payload"]["tool"])
        with mock.patch.object(engine, "run_task"):
            engine.decide("u", "p1", self.decisions()[0]["id"], "accept")
        t = next(t for t in store.load("u")["tools"] if t["name"] == "api-neuve")
        self.assertFalse(t.get("headers_text"))
        self.assertFalse(t.get("remote"))

    def test_approbation_ajoute_l_outil_avec_accord_et_autorise_le_role(self):
        self.propose()
        with mock.patch.object(engine, "run_task"):
            engine.decide("u", "p1", self.decisions()[0]["id"], "accept", fields={"grant": "1"})
        st = store.load("u")
        t = next(t for t in st["tools"] if t["name"] == "compte-lignes")
        self.assertEqual((t["level"], t["enabled"], t["kind"]), ("approve", True, "cli"))              # jamais « libre » d'office
        self.assertTrue(next(r for r in st["roles"] if r["id"] == "r2")["tools"][t["id"]])
        self.assertEqual(self.decisions()[0]["status"], "acceptée")
        p, bob = self.agent()
        self.assertEqual(rights.tool_level(p, bob, p["_tools"][-1]), rights.APPROVE)
        self.assertIn("acceptée", bob["inbox"][-1]["text"])

    def test_approbation_sans_attribution_du_role(self):
        self.propose()
        with mock.patch.object(engine, "run_task"):
            engine.decide("u", "p1", self.decisions()[0]["id"], "accept", fields={"grant": ""})
        st = store.load("u")
        t = next(t for t in st["tools"] if t["name"] == "compte-lignes")
        self.assertNotIn(t["id"], next(r for r in st["roles"] if r["id"] == "r2")["tools"])
        p, bob = self.agent()
        self.assertEqual(rights.tool_level(p, bob, t), rights.NO)

    def test_modification_d_un_outil_existant(self):
        self.state([dict(LIRE, headers_text="X-A: 1", level="free")])
        n, back = self.act("a2", "tool_propose", name="lire", kind="cli", desc="Affiche un fichier", command=PY, args=["-c", "print('v2')", "{fichier}"],
                           params=["fichier ; chemin ; obligatoire ; f ; existant"], explain="Même outil, affichage simplifié du fichier demandé.", reason="r")
        d = self.decisions()[0]
        self.assertEqual(d["payload"]["id"], "t1")
        self.assertTrue(d["payload"]["prev"])
        with mock.patch.object(engine, "run_task"):
            engine.decide("u", "p1", d["id"], "accept")
        t = store.load("u")["tools"][0]
        self.assertIn("v2", t["args_text"])
        self.assertEqual((t["level"], t.get("headers_text")), ("free", "X-A: 1"))                  # niveau et en-têtes de l'utilisateur conservés
        self.assertEqual(len(store.load("u")["tools"]), 1)
        n, back = self.act("a2", "tool_propose", name="lire", kind="cli", desc="Affiche un fichier", command=PY, args=["-c", "print('v2')", "{fichier}"],
                           params=["fichier ; chemin ; obligatoire ; f ; existant"], explain="Même outil, affichage simplifié du fichier demandé.", reason="r")
        self.assertIn("existe déjà", back)

    def test_version_revisee_remplace_la_proposition_en_attente_et_plafond(self):
        self.propose()
        self.propose(desc="Compte les lignes (version 2)")
        d = self.decisions()
        self.assertEqual([x["status"] for x in d], ["remplacée", "attente"])
        for i in range(3):
            self.propose(name="outil-%d" % i)
        n, back = self.propose(name="outil-9")
        self.assertIn("en attente", back)

    def test_refus_previent_l_agent(self):
        self.propose()
        with mock.patch.object(engine, "ask") as ask:
            engine.decide("u", "p1", self.decisions()[0]["id"], "refuse", note="inutile")
        self.assertEqual(len(store.load("u")["tools"]), 2)
        self.assertIn("inutile", ask.call_args.args[3])


class ActionWorkflow(ServeurEcho):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "a.txt").write_text("bonjour", encoding="utf-8")
        st = make_state(self.tmp)
        p = st["projects"][0]
        st["roles"] = [{"id": "r1", "name": "Chef de projet", "desc": ""}, {"id": "r2", "name": "Développeur back-end", "desc": ""}]
        p.pop("roles")
        st["tools"] = [dict(LIRE, level="free"), dict(self.api), dict(LIRE, id="t3", name="avec-accord", level="approve"),
                       dict(LIRE, id="t4", name="echec", level="free", args_text="-c\nimport sys; sys.exit(3)\n{fichier}")]
        p["workflows"] = [{"id": "w1", "name": "WF", "enabled": True, "steps": []}]
        p["wf_runs"] = [{"id": "run1", "wf": "w1", "task": "", "requester": "", "results": []}]
        store.wj(store.state_path("u"), st)

    def ctx(self):
        p = store.load("u")["projects"][0]
        return p, p["workflows"][0], p["wf_runs"][0]

    def step(self, **k):
        return {"kind": "system", "action": "run_tool", "value": "lire", "who": "agent:a2", "text": "fichier=a.txt", "confirm": "0", **k}

    def lint(self, st):
        p, wf, run = self.ctx()
        return [m for lvl, m in sysact.lint_step(p, st, False, wf) if lvl == "erreur"]

    def test_controle_statique_de_l_etape(self):
        self.assertEqual(self.lint(self.step()), [])
        self.assertTrue(self.lint(self.step(value="inconnu")))
        self.assertTrue(any("accord" in m for m in self.lint(self.step(value="avec-accord", confirm="0"))))     # l'accord de l'utilisateur ne se contourne pas
        self.assertEqual(self.lint(self.step(value="avec-accord", confirm="1")), [])
        self.assertTrue(any("obligatoire" in m for m in self.lint(self.step(text=""))))
        self.assertTrue(any("inconnu" in m for m in self.lint(self.step(text="fichier=a.txt\nzzz=1"))))

    def test_confirmation_par_defaut(self):
        self.assertTrue(sysact.needs_confirm({"action": "run_tool"}))
        self.assertIn("Lancer un outil", sysact.describe(store.load("u")["projects"][0], self.step()))

    def run_step(self, st):
        p, wf, run = self.ctx()
        return sysact.run_step(engine, "u", "p1", p, wf, run, 0, st)

    def test_execution_ok_et_journal(self):
        issue, comment, wait = self.run_step(self.step())
        self.assertEqual(issue, "ok", comment)
        self.assertFalse(wait)
        self.assertIn("réussi", comment)
        r = store.load("u")["projects"][0]["tool_runs"][0]
        self.assertEqual((r["approved"], r["ok"]), ("Workflow", True))

    def test_echec_de_l_outil_est_un_ko(self):
        issue, comment, wait = self.run_step(self.step(value="echec"))
        self.assertEqual(issue, "ko")
        self.assertIn("code 3", comment)

    def test_variables_et_outil_api_sans_acteur_local(self):
        st = self.step(value="api-items", text="id=3\nq={task.id}x\nnom=Alice\nn=2", who="principal")
        issue, comment, wait = self.run_step(st)
        self.assertEqual(issue, "ok", comment)

    def test_outil_avec_accord_sans_confirmation_est_refuse_a_l_execution(self):
        issue, comment, wait = self.run_step(self.step(value="avec-accord", confirm="0"))
        self.assertEqual(issue, "ko")
        self.assertIn("accord", comment)
        self.assertEqual(store.load("u")["projects"][0].get("tool_runs", []), [])

    def test_confirmation_demandee_avant_l_execution(self):
        issue, comment, wait = self.run_step(self.step(confirm=""))
        self.assertTrue(wait)
        self.assertEqual(store.load("u")["projects"][0].get("tool_runs", []), [])
        d = [x for x in store.load("u")["projects"][0]["decisions"] if x.get("wf_run") == "run1"]
        self.assertEqual(len(d), 1)

    def test_outil_desactive_ou_inconnu(self):
        st = store.load("u")
        st["tools"][0]["enabled"] = False
        store.wj(store.state_path("u"), st)
        self.assertEqual(self.run_step(self.step())[0], "ko")
        self.assertEqual(self.run_step(self.step(value="nexistepas"))[0], "ko")


if __name__ == "__main__":
    unittest.main()
