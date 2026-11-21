"""Mode git local raccordé au moteur : un worktree par agent, outils git exécutés par l'application (aucun réseau, IA non utilisée)."""
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("APM_DATA", tempfile.mkdtemp())

import engine  # noqa: E402
import gitlocal  # noqa: E402
import locks  # noqa: E402
import store  # noqa: E402


def make_state(localdir, nogit=None, url="", branches=None):
    ag = lambda i, n, **k: {**dict(id=i, name=n, service="s1", role="r1", branch="agent/" + n.lower(), canBug=False, talk=["a1", "a2", "a3"],
                                   history=[], notes="", source="local"), **k}
    p = {"id": "p1", "name": "Demo", "rules": "", "spec": "", "mainId": "a1", "localdir": str(localdir),
         "repo": {"url": url, "branch": "main", "token": ""},
         "roles": [{"id": "r1", "name": "Chef de projet", "desc": "Coordonne"}, {"id": "r2", "name": "Développeur back-end", "desc": "Développe"}],
         "agents": [ag("a1", "Alice"), ag("a2", "Bob", role="r2"), ag("a3", "Carl", role="r2")],
         "tasks": [], "bugs": [], "modules": [], "log": [], "commits": [], "releases": [], "mrs": []}
    if nogit is not None:
        p["nogit"] = nogit
    if branches is not None:
        p["branches"] = branches
    return {"services": [{"id": "s1", "name": "S", "kind": "openai", "url": "x", "model": "m", "apiKey": "k"}], "projects": [p]}


def proj():
    return store.load("u")["projects"][0]


def sh(cwd, *a):
    return gitlocal.git(list(a), cwd, identity=("T", "t@example.com"))[1]


class Base(unittest.TestCase):
    NOGIT = False
    URL = ""
    BRANCHES = "agent"                                     # les classes historiques testent la stratégie « une branche par agent »

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="myteam-mode-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        store.wj(store.state_path("u"), make_state(self.root, self.NOGIT, self.URL, self.BRANCHES))
        locks.STOP.clear()
        engine.GIT_OK.clear()
        self.sp = mock.patch.object(engine, "spawn", lambda *a, **k: None)
        self.sp.start()
        self.addCleanup(self.sp.stop)

    def act(self, who, kind, **x):
        return engine.do_act("u", "p1", who, {"do": kind, **x}, 0, {"n": 0, "t0": time.time()})

    def wt(self, name):
        return os.path.join(self.root, ".myteam", "worktrees", gitlocal.slug(name))

    def logs(self):
        return "\n".join(m["text"] for m in proj()["log"])


class NonRegression(Base):
    """Par défaut (réglage absent) ou « sans git » : le comportement historique (verrous, aucun dépôt) est inchangé."""
    NOGIT = None

    def test_reglage_absent_ou_vrai_reste_sans_git(self):
        for flag in (None, True):
            p = proj()
            self.assertFalse(engine.git_mode(dict(p, nogit=flag) if flag is not None else p))
        self.act("a2", "write_file", path="src/x.py", content="print(1)", message="m")
        with open(os.path.join(self.root, "src", "x.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "print(1)")
        self.assertFalse(os.path.exists(os.path.join(self.root, ".git")))                  # aucun dépôt créé
        self.assertFalse(os.path.exists(os.path.join(self.root, ".myteam")))
        self.assertIn("n’existe qu’en mode git local", str(self.act("a2", "status")))       # outils git réservés au mode git

    def test_verrous_toujours_disponibles(self):
        r = self.act("a2", "lock_file", paths=["a.txt"])
        self.assertNotIn("Pas de verrou", str(r))
        self.assertEqual(len(proj()["locks"]), 1)


class ModeGit(Base):
    def test_ecriture_dans_le_worktree_isole_et_sans_verrou(self):
        self.assertTrue(engine.git_mode(proj()))
        self.act("a2", "write_file", path="src/x.py", content="print(1)\n")
        self.assertTrue(os.path.exists(os.path.join(self.wt("Bob"), "src", "x.py")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "src", "x.py")))           # le dossier racine n'est pas touché
        r = self.act("a3", "list_files", path="")
        self.assertNotIn("src", str(r))                                                     # Carl ne voit pas le travail de Bob
        self.assertIn("src/", str(self.act("a2", "list_files", path="")))
        self.assertIn("print(1)", str(self.act("a2", "read_file", path="src/x.py")))
        r = self.act("a2", "lock_file", paths=["a.txt"])
        self.assertIn("Pas de verrou", str(r))
        self.assertEqual(proj().get("locks", []), [])

    def test_commit_status_diff_search_log(self):
        self.act("a2", "write_file", path="src/x.py", content="def tache():\n    return 42\n")
        self.assertIn("src/x.py", str(self.act("a2", "status")))
        n, back = self.act("a2", "commit", message="Ajoute la tâche")
        self.assertIn("Commit", n)
        self.assertTrue(any(c["m"] == "Ajoute la tâche" for c in proj()["commits"]))
        self.assertIn("Rien à committer", str(self.act("a2", "status")))
        self.assertIn("Rien à committer", str(self.act("a2", "commit", message="encore")))
        self.assertIn("+    return 42", str(self.act("a2", "diff")))
        self.assertIn("src/x.py:2:", str(self.act("a2", "search", pattern="RETURN 42")))
        self.assertIn("Ajoute la tâche", str(self.act("a2", "git_log")))
        self.assertIn("Agent: Bob", sh(self.wt("Bob"), "log", "-1", "--format=%B"))
        self.assertIn("def tache", str(self.act("a3", "git_show", rev="agent/bob", path="src/x.py")))   # lire le travail d'un autre agent

    def test_sync_integre_le_travail_des_autres_et_gere_les_conflits(self):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("un\ndeux\ntrois\n")
        self.act("a2", "write_file", path="a.txt", content="un\nBOB\ntrois\n")                # crée dépôt (commit initial avec a.txt) + worktree
        self.act("a2", "commit", message="bob")
        self.act("a3", "write_file", path="a.txt", content="un\nCARL\ntrois\n")
        self.act("a3", "commit", message="carl")
        lg = engine.git_repo("u", "p1", proj())
        self.assertEqual(lg.integrate("agent/bob", "develop")["state"], "merged")             # (l'intégration par l'application arrive à l'étape suivante)
        self.assertIn("conflit", str(self.act("a3", "sync")).lower())
        self.assertIn("<<<<<<<", str(self.act("a3", "read_file", path="a.txt")))
        self.assertIn("refusée", str(self.act("a3", "sync")))                                  # tant que le conflit n'est pas traité : pas de nouveau sync
        self.act("a3", "write_file", path="a.txt", content="un\nBOB+CARL\ntrois\n")
        self.act("a3", "commit", message="résolution")
        self.assertIn("déjà", str(self.act("a3", "sync")))                                     # à jour
        self.assertEqual(lg.integrate("agent/carl", "develop")["state"], "merged")

    def test_abandon_de_sync(self):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("un\n")
        self.act("a2", "write_file", path="a.txt", content="BOB\n")
        self.act("a2", "commit", message="bob")
        self.act("a3", "write_file", path="a.txt", content="CARL\n")
        self.act("a3", "commit", message="carl")
        engine.git_repo("u", "p1", proj()).integrate("agent/bob", "develop")
        self.assertIn("conflit", str(self.act("a3", "sync")).lower())
        self.assertIn("annulée", str(self.act("a3", "sync", abort=True)))
        self.assertEqual(self.act("a3", "read_file", path="a.txt")[1].split("\n")[1:3], ["```", "CARL"])

    def test_sync_refuse_si_modifications_non_commitees(self):
        self.act("a2", "write_file", path="x.txt", content="x\n")
        self.assertIn("non commitées", str(self.act("a2", "sync")))

    def test_chemins_interdits(self):
        self.act("a2", "list_files", path="")                                                # crée le worktree
        wt = self.wt("Bob")
        for bad in (".git/config", "../x.txt", ".github/workflows/ci.yml", ".agents/Bob.md", "a/../../y.txt"):
            n, back = self.act("a2", "write_file", path=bad, content="x")
            self.assertTrue(back and any(w in back for w in ("interdit", "refusé", "invalide", "hors")), (bad, back))
        self.assertEqual(sorted(os.listdir(wt)), [".git", ".gitignore"])                      # rien d'autre que le commit initial dans le worktree
        self.assertFalse(os.path.exists(os.path.join(self.root, "x.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x.txt")))

    def test_branche_de_base_interdite_a_l_agent(self):
        p = proj()
        p["agents"][1]["branch"] = "develop"
        store.wj(store.state_path("u"), {**store.load("u"), "projects": [p]})
        r = self.act("a2", "write_file", path="x.txt", content="x")
        self.assertIn("interdite", str(r))
        self.assertFalse(os.path.exists(os.path.join(self.root, "x.txt")))

    def test_pull_redirige_vers_sync_et_merge_request_sans_travail(self):
        self.assertIn("sync", str(self.act("a2", "pull")))
        self.assertIn("Rien à soumettre", str(self.act("a2", "merge_request", title="t")))        # rien en avance sur la cible

    def test_provision_contexte_hors_des_branches(self):
        msg = engine.provision("u", "p1", "a2")
        self.assertIn("worktree prêt", msg)
        ctx = os.path.join(self.root, ".myteam", "agents", "bob.md")
        self.assertTrue(os.path.exists(ctx))
        self.assertFalse(os.path.exists(os.path.join(self.wt("Bob"), ".agents")))
        self.assertNotIn("bob.md", sh(self.root, "ls-files"))
        self.assertEqual(sh(self.root, "status", "--porcelain").strip(), "")                  # rien de non suivi côté dépôt
        n, back = self.act("a2", "context", text="Convention : snake_case")
        self.assertIn("Contexte mis à jour", n)
        with open(ctx, encoding="utf-8") as f:
            self.assertIn("snake_case", f.read())

    def test_prompt_decrit_le_worktree(self):
        p = proj()
        a = p["agents"][1]
        self.assertIn("WORKTREE GIT LOCAL", engine.repo_text(p, a))
        self.assertIn("AUCUN verrou", engine.repo_text(p, a))
        self.assertNotIn("lock_file", engine.repo_text(p, a))
        self.assertIn("worktree git local", engine.ctx_text(p, a).lower())
        self.assertIn('"do":"commit"', engine.ACTIONS)


class DossierInutilisable(Base):
    URL = "https://github.com/o/r"

    def test_non_vide_sans_git_avec_remote(self):
        with open(os.path.join(self.root, "existant.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        r = self.act("a2", "write_file", path="x.txt", content="x")
        self.assertIn("inutilisable", str(r))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".git")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "x.txt")))
        self.assertIn("inutilisable", engine.provision("u", "p1", "a2"))
        engine.localdir_check("u", "p1")
        self.assertIn("INUTILISABLE", self.logs())

    def test_localdir_check_dossier_vide_et_depot_existant(self):
        engine.localdir_check("u", "p1")
        self.assertIn("dossier vide : sera initialisé ou cloné", self.logs())


class DossierVideSansRemote(Base):
    def test_localdir_check_indique_le_mode(self):
        engine.localdir_check("u", "p1")
        self.assertIn("Mode git local", self.logs())
        self.assertIn("utilisable", self.logs())


class Mr(Base):
    """Merge requests internes : soumission, validation, intégration programmatique, conflits, état déduit de git, push."""

    def work(self, who, path, content, msg="travail"):
        self.act(who, "write_file", path=path, content=content)
        self.act(who, "commit", message=msg)

    def mr(self, who="a2"):
        return next(m for m in reversed(proj()["mrs"]) if m["by_id"] == who)

    def base_file(self):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("un\ndeux\ntrois\n")

    def test_soumission_commit_le_reste_et_cree_une_mr_interne(self):
        self.act("a2", "write_file", path="src/x.py", content="print(1)\n")                    # pas de commit de l'agent
        st, msg = engine.submit_mr("u", "p1", "a2", "Tâche : X", "", "")
        self.assertEqual(st, "submitted", msg)
        m = self.mr()
        self.assertEqual((m["git"], m["from"], m["to"], m["status"], m["n"]), (True, "agent/bob", "develop", "ouverte", 1))
        self.assertEqual(self.act("a2", "status")[1].split(". ")[-1], "Rien à committer.")        # le reste a été commité par l'application
        self.assertEqual(engine.submit_mr("u", "p1", "a2", "Tâche : X")[0], "exists")
        self.assertEqual(engine.submit_mr("u", "p1", "a3", "Rien")[0], "skip")                   # Carl n'a rien en avance

    def test_validation_fusionne_dans_la_cible(self):
        self.act("a3", "status")                                                                  # le worktree de Carl existe avant la fusion
        self.work("a2", "src/x.py", "print(1)\n")
        engine.submit_mr("u", "p1", "a2", "Tâche : X")
        r = engine.apply_mr_review("u", "p1", self.mr()["id"], "Alice", "approved", "ok")
        self.assertIn("fusionnée dans develop", r)
        self.assertEqual(self.mr()["status"], "fusionnée")
        lg = engine.git_repo("u", "p1", proj())
        self.assertEqual(sh(lg.root, "show", "develop:src/x.py"), "print(1)\n")
        self.assertTrue(any("Merge MR #1" in c["m"] for c in proj()["commits"]))
        self.assertEqual(self.act("a3", "sync")[0].split(" : ")[-1], "merged")                    # Carl récupère le travail de Bob
        self.assertTrue(os.path.exists(os.path.join(self.wt("Carl"), "src", "x.py")))
        self.assertIn("introuvable ou déjà traitée", engine.merge_mr("u", "p1", self.mr()["id"]))

    def test_refus_de_validation(self):
        self.work("a2", "src/x.py", "print(1)\n")
        engine.submit_mr("u", "p1", "a2", "Tâche : X")
        r = engine.apply_mr_review("u", "p1", self.mr()["id"], "Alice", "rejected", "manque les tests")
        self.assertIn("refusée", r)
        self.assertEqual(self.mr()["status"], "refusée")
        lg = engine.git_repo("u", "p1", proj())
        self.assertNotEqual(gitlocal.git(["cat-file", "-e", "develop:src/x.py"], lg.root, check=False)[0], 0)    # absent de la cible

    def test_conflit_detecte_a_la_soumission(self):
        self.base_file()
        self.work("a2", "a.txt", "un\nBOB\ntrois\n")
        self.work("a3", "a.txt", "un\nCARL\ntrois\n")
        engine.submit_mr("u", "p1", "a2", "Bob")
        engine.apply_mr_review("u", "p1", self.mr("a2")["id"], "Alice", "approved", "")
        st, msg = engine.submit_mr("u", "p1", "a3", "Carl")
        self.assertEqual(st, "conflict")
        self.assertIn("a.txt", msg)
        self.assertIn("<<<<<<<", self.act("a3", "read_file", path="a.txt")[1])
        self.work("a3", "a.txt", "un\nBOB+CARL\ntrois\n", "résolution")
        self.assertEqual(engine.submit_mr("u", "p1", "a3", "Carl")[0], "submitted")
        self.assertIn("fusionnée", engine.apply_mr_review("u", "p1", self.mr("a3")["id"], "Alice", "approved", ""))
        self.assertEqual(sh(self.root, "show", "develop:a.txt"), "un\nBOB+CARL\ntrois\n")

    def test_conflit_a_l_integration_renvoye_a_l_auteur_puis_repris(self):
        self.base_file()
        self.work("a2", "a.txt", "un\nBOB\ntrois\n")
        self.work("a3", "a.txt", "un\nCARL\ntrois\n")
        self.assertEqual(engine.submit_mr("u", "p1", "a3", "Carl")[0], "submitted")              # les deux soumises avant toute intégration
        self.assertEqual(engine.submit_mr("u", "p1", "a2", "Bob")[0], "submitted")
        engine.apply_mr_review("u", "p1", self.mr("a2")["id"], "Alice", "approved", "")
        calls = []
        with mock.patch.object(engine, "spawn", lambda f, *a, **k: calls.append((f.__name__, a))):
            r = engine.apply_mr_review("u", "p1", self.mr("a3")["id"], "Alice", "approved", "")
        self.assertIn("renvoyée à Carl", r)
        m = self.mr("a3")
        self.assertEqual((m["status"], m["merge_tries"], m["conflict_files"]), ("conflit", 1, ["a.txt"]))
        ask = [c for c in calls if c[0] == "ask"]
        self.assertEqual(len(ask), 1)
        self.assertEqual(ask[0][1][2], "a3")                                                      # l'auteur est prévenu
        self.assertIn("sync", ask[0][1][3])
        self.assertEqual(sh(self.root, "show", "develop:a.txt"), "un\nBOB\ntrois\n")           # la cible n'a pas été touchée
        self.assertIn("conflit", str(self.act("a3", "sync")).lower())
        self.work("a3", "a.txt", "un\nBOB+CARL\ntrois\n", "résolution")
        st, msg = engine.submit_mr("u", "p1", "a3", "Carl")
        self.assertEqual((st, self.mr("a3")["status"]), ("submitted", "ouverte"))                 # la même MR repart en validation
        self.assertEqual(len([x for x in proj()["mrs"] if x["by_id"] == "a3"]), 1)
        self.assertIn("fusionnée", engine.apply_mr_review("u", "p1", self.mr("a3")["id"], "Alice", "approved", ""))
        self.assertEqual(sh(self.root, "show", "develop:a.txt"), "un\nBOB+CARL\ntrois\n")

    def test_trop_d_essais_demande_une_intervention(self):
        self.base_file()
        self.work("a2", "a.txt", "un\nBOB\ntrois\n")
        self.work("a3", "a.txt", "un\nCARL\ntrois\n")
        engine.submit_mr("u", "p1", "a3", "Carl")
        engine.submit_mr("u", "p1", "a2", "Bob")
        engine.apply_mr_review("u", "p1", self.mr("a2")["id"], "Alice", "approved", "")
        mid = self.mr("a3")["id"]
        for _ in range(3):
            engine.merge_mr("u", "p1", mid)
        self.assertIn("intervention manuelle", engine.merge_mr("u", "p1", mid))
        self.assertEqual(self.mr("a3")["status"], "conflit")

    def test_etat_deduit_de_git_sans_ia_ni_reseau(self):
        self.work("a2", "src/x.py", "print(1)\n")
        self.work("a3", "src/y.py", "print(2)\n")
        engine.submit_mr("u", "p1", "a2", "Bob")
        engine.submit_mr("u", "p1", "a3", "Carl")
        self.assertEqual(engine.sync_mrs("u", "p1", force=True), [])                              # rien n'est fusionné : elles restent ouvertes
        lg = engine.git_repo("u", "p1", proj())
        lg.integrate("agent/bob", "develop")                                                       # fusionnée « à la main » en dehors de l'application
        lg.drop_worktree("Carl")
        sh(lg.root, "branch", "-D", "agent/carl")                                                  # branche supprimée
        out = engine.sync_mrs("u", "p1", force=True)
        self.assertEqual(sorted(out), [(1, "fusionnée"), (2, "annulée")])
        self.assertEqual({m["by"]: m["status"] for m in proj()["mrs"]}, {"Bob": "fusionnée", "Carl": "annulée"})
        self.assertIn("dans git", self.logs())
        self.assertEqual(engine.sync_mrs("u", "p1", force=True), [])                              # idempotent

    def test_mr_github_et_git_coexistent_dans_sync_mrs(self):
        self.work("a2", "src/x.py", "print(1)\n")
        engine.submit_mr("u", "p1", "a2", "Bob")
        p = proj()
        p["mrs"].append({"id": "gh1", "n": 77, "from": "agent/z", "to": "release/v1", "title": "gh", "by": "Z", "by_id": "a3", "status": "ouverte", "d": ""})
        store.wj(store.state_path("u"), {**store.load("u"), "projects": [p]})
        engine.git_repo("u", "p1", p).integrate("agent/bob", "develop")
        out = engine.sync_mrs("u", "p1", force=True)                                               # la MR GitHub (sans accès) ne bloque pas celle de git
        self.assertEqual(out, [(1, "fusionnée")])
        self.assertEqual(by_id_status(proj(), "gh1"), "ouverte")

    def test_vue_du_validateur(self):
        self.work("a2", "src/x.py", "def f():\n    return 1\n", "Ajoute f")
        engine.submit_mr("u", "p1", "a2", "Bob")
        m = self.mr()
        commits, files, dif = engine.git_mr_view("u", "p1", proj(), m)
        self.assertIn("Ajoute f", commits)
        self.assertEqual(files, "src/x.py")
        self.assertIn("+    return 1", dif)
        asked = []
        with mock.patch.object(engine, "ask", lambda *a, **k: asked.append(a)), mock.patch.object(engine, "spawn", lambda *a, **k: None):
            engine.review_mr("u", "p1", m["id"])
        if asked:                                                                                  # message envoyé au validateur
            txt = asked[0][3]
            self.assertIn("git_show", txt)
            self.assertNotIn("read_file", txt.split("mr_review")[0].split("Diff :")[-1])
            self.assertIn("+    return 1", txt)


def by_id_status(p, mid):
    return next(m for m in p["mrs"] if m["id"] == mid)["status"]


class RemoteBase(Base):
    """Avec un remote (dépôt nu local) : branche de l'agent poussée à la validation, cible poussée, remote qui avance entre-temps."""

    def setUp(self):
        super().setUp()
        self.remote = os.path.join(self.tmp, "remote.git")
        sh(self.tmp, "init", "--bare", "-b", "main", self.remote)
        seed = os.path.join(self.tmp, "seed")
        sh(self.tmp, "init", "-b", "main", seed)
        with open(os.path.join(seed, "README.md"), "w", encoding="utf-8") as f:
            f.write("# demo\n")
        sh(seed, "add", "-A")
        sh(seed, "commit", "-m", "seed")
        sh(seed, "push", self.remote, "main")
        st = store.load("u")
        st["projects"][0]["repo"]["url"] = self.remote
        store.wj(store.state_path("u"), st)
        engine.GIT_OK.clear()

    def remote_show(self, spec):
        return gitlocal.git(["show", spec], self.remote)[1]

    def work(self, who, path, content):
        self.act(who, "write_file", path=path, content=content)
        self.act(who, "commit", message="travail")

    def mid(self, who):
        return next(m for m in reversed(proj()["mrs"]) if m["by_id"] == who)["id"]


class MrRemote(RemoteBase):
    """Avec un remote (dépôt nu local) : branche de l'agent poussée à la validation, cible poussée, remote qui avance entre-temps."""

    def test_push_de_la_branche_de_l_agent_et_de_la_cible(self):
        self.work("a2", "src/x.py", "print(1)\n")
        self.assertEqual(engine.submit_mr("u", "p1", "a2", "Bob")[0], "submitted")
        r = engine.apply_mr_review("u", "p1", self.mid("a2"), "Alice", "approved", "")
        self.assertNotIn("Attention", r)
        refs = sh(self.tmp, "ls-remote", self.remote)
        self.assertIn("refs/heads/develop", refs)
        self.assertIn("refs/heads/agent/bob", refs)
        self.assertEqual(self.remote_show("develop:src/x.py"), "print(1)\n")
        self.assertEqual(self.remote_show("develop:README.md"), "# demo\n")

    def test_le_remote_a_avance_entre_temps(self):
        self.work("a2", "src/x.py", "print(1)\n")
        engine.submit_mr("u", "p1", "a2", "Bob")
        lg = engine.git_repo("u", "p1", proj())
        lg.push("develop")                                                                         # develop existe maintenant sur le remote
        other = os.path.join(self.tmp, "other")
        sh(self.tmp, "clone", self.remote, other)
        sh(other, "checkout", "develop")
        with open(os.path.join(other, "autre.txt"), "w", encoding="utf-8") as f:
            f.write("autre\n")
        sh(other, "add", "-A")
        sh(other, "commit", "-m", "autre machine")
        sh(other, "push", "origin", "develop")
        r = engine.apply_mr_review("u", "p1", self.mid("a2"), "Alice", "approved", "")
        self.assertNotIn("Attention", r)
        self.assertEqual(self.remote_show("develop:autre.txt"), "autre\n")                        # le travail de l'autre machine est conservé
        self.assertEqual(self.remote_show("develop:src/x.py"), "print(1)\n")
        self.assertEqual(lg.sha("develop"), gitlocal.git(["rev-parse", "develop"], self.remote)[1].strip())

    def test_sync_prend_la_cible_du_remote(self):
        self.work("a2", "src/x.py", "print(1)\n")
        engine.submit_mr("u", "p1", "a2", "Bob")
        engine.apply_mr_review("u", "p1", self.mid("a2"), "Alice", "approved", "")
        other = os.path.join(self.tmp, "other")
        sh(self.tmp, "clone", self.remote, other)
        sh(other, "checkout", "develop")
        with open(os.path.join(other, "externe.txt"), "w", encoding="utf-8") as f:
            f.write("externe\n")
        sh(other, "add", "-A")
        sh(other, "commit", "-m", "externe")
        sh(other, "push", "origin", "develop")
        self.assertIn("merged", self.act("a3", "sync")[0])                                         # Carl reçoit le travail venu du remote
        self.assertTrue(os.path.exists(os.path.join(self.wt("Carl"), "externe.txt")))

    def test_push_impossible_signale_sans_perdre_la_fusion(self):
        self.work("a2", "src/x.py", "print(1)\n")
        engine.submit_mr("u", "p1", "a2", "Bob")
        shutil.rmtree(self.remote)                                                                 # remote devenu injoignable
        r = engine.apply_mr_review("u", "p1", self.mid("a2"), "Alice", "approved", "")
        self.assertIn("fusionnée dans develop", r)
        self.assertIn("Attention", r)
        self.assertEqual(by_id_status(proj(), self.mid("a2")), "fusionnée")


def set_tasks(*tasks):
    st = store.load("u")
    st["projects"][0]["tasks"] = [dict(t) for t in tasks]
    store.wj(store.state_path("u"), st)


def task(i, who, title, status="en cours", started=1):
    return {"id": i, "title": title, "who": who, "status": status, "started": started, "deps": []}


class ParTache(Base):
    """Une branche par tâche (stratégie par défaut), branche libre sans tâche, branche héritée, nettoyage."""
    BRANCHES = "task"

    def lg(self):
        return engine.git_repo("u", "p1", proj())

    def branch(self, name="Bob"):
        return self.lg().current_branch(self.wt(name))

    def test_une_branche_par_tache_et_changement_de_tache(self):
        set_tasks(task("t1", "a2", "Ajoute x"))
        self.act("a2", "write_file", path="x.txt", content="x\n")
        self.assertEqual(self.branch(), "task/bob/t1-ajoute-x")
        self.assertIn("task/bob/t1-ajoute-x", self.act("a2", "status")[1])
        set_tasks(task("t1", "a2", "Ajoute x", "à vérifier"), task("t2", "a2", "Ajoute y", started=2))
        self.act("a2", "write_file", path="y.txt", content="y\n")
        self.assertEqual(self.branch(), "task/bob/t2-ajoute-y")
        self.assertFalse(os.path.exists(os.path.join(self.wt("Bob"), "x.txt")))                  # t2 repart de la cible : sans le travail de t1
        self.assertEqual(sh(self.root, "show", "task/bob/t1-ajoute-x:x.txt"), "x\n")            # t1 conservé (commit WIP automatique)
        self.assertIn("WIP", sh(self.root, "log", "-1", "--format=%s", "task/bob/t1-ajoute-x"))
        set_tasks(task("t1", "a2", "Ajoute x", started=3))                                         # t1 redémarre : on retrouve sa branche
        self.act("a2", "status")
        self.assertEqual(self.branch(), "task/bob/t1-ajoute-x")
        self.assertTrue(os.path.exists(os.path.join(self.wt("Bob"), "x.txt")))

    def test_deux_agents_deux_tâches_sans_interference(self):
        set_tasks(task("t1", "a2", "Bob"), task("t2", "a3", "Carl"))
        self.act("a2", "write_file", path="b.txt", content="b\n")
        self.act("a3", "write_file", path="c.txt", content="c\n")
        self.assertEqual((self.branch("Bob"), self.branch("Carl")), ("task/bob/t1-bob", "task/carl/t2-carl"))
        self.assertFalse(os.path.exists(os.path.join(self.wt("Carl"), "b.txt")))

    def test_sans_tache_branche_libre_puis_branche_de_tache(self):
        self.act("a2", "write_file", path="libre.txt", content="l\n")
        self.assertEqual(self.branch(), "task/bob/libre")
        set_tasks(task("t1", "a2", "Ajoute x"))
        self.act("a2", "status")
        self.assertEqual(self.branch(), "task/bob/t1-ajoute-x")
        set_tasks(task("t1", "a2", "Ajoute x", "à vérifier"))
        self.act("a2", "status")
        self.assertEqual(self.branch(), "task/bob/t1-ajoute-x")                                   # la tâche finie ne fait pas changer de branche

    def test_soumission_apres_fin_de_tache_puis_nettoyage_de_la_branche(self):
        set_tasks(task("t1", "a2", "Ajoute x"))
        self.act("a2", "write_file", path="x.txt", content="x\n")
        self.act("a2", "commit", message="x")
        set_tasks(task("t1", "a2", "Ajoute x", "à vérifier"))
        self.assertEqual(engine.submit_mr("u", "p1", "a2", "Tâche : Ajoute x", "", "t1")[0], "submitted")
        m = next(m for m in proj()["mrs"] if m["by_id"] == "a2")
        self.assertEqual((m["from"], m["task"]), ("task/bob/t1-ajoute-x", "t1"))
        self.assertIn("fusionnée", engine.apply_mr_review("u", "p1", m["id"], "Alice", "approved", ""))
        self.assertTrue(self.lg().branch_exists("task/bob/t1-ajoute-x"))                          # le worktree l'occupe encore
        set_tasks(task("t1", "a2", "Ajoute x", "terminée"), task("t2", "a2", "Suite", started=5))
        self.act("a2", "status")
        self.assertEqual(self.branch(), "task/bob/t2-suite")
        self.assertFalse(self.lg().branch_exists("task/bob/t1-ajoute-x"))                          # fusionnée : supprimée au changement de tâche
        self.assertTrue(os.path.exists(os.path.join(self.wt("Bob"), "x.txt")))                     # t2 part de la cible qui contient t1

    def test_branche_heritee_gardee_jusqu_a_sa_fusion(self):
        st = store.load("u")
        st["projects"][0]["branches"] = "agent"                                                    # ancien fonctionnement : une branche par agent
        store.wj(store.state_path("u"), st)
        self.act("a2", "write_file", path="ancien.txt", content="a\n")
        self.act("a2", "commit", message="avant la bascule")
        self.assertEqual(self.branch(), "agent/bob")
        st = store.load("u")
        del st["projects"][0]["branches"]                                                          # bascule vers une branche par tâche
        store.wj(store.state_path("u"), st)
        set_tasks(task("t1", "a2", "Nouvelle"))
        self.act("a2", "status")
        self.assertEqual(self.branch(), "agent/bob")                                               # travail non fusionné : branche héritée conservée
        self.assertIn("task/bob/", engine.repo_text(proj(), proj()["agents"][1]))
        engine.submit_mr("u", "p1", "a2", "Ancien travail")
        m = next(m for m in proj()["mrs"] if m["by_id"] == "a2")
        self.assertEqual(m["from"], "agent/bob")
        engine.apply_mr_review("u", "p1", m["id"], "Alice", "approved", "")
        self.act("a2", "status")
        self.assertEqual(self.branch(), "task/bob/t1-nouvelle")                                    # héritée fusionnée : passage aux branches par tâche
        self.assertTrue(self.lg().branch_exists("agent/bob"))                                      # l'ancienne branche n'est pas supprimée
        self.assertTrue(os.path.exists(os.path.join(self.wt("Bob"), "ancien.txt")))

    def test_mr_suivie_par_son_dernier_commit_si_la_branche_disparait(self):
        set_tasks(task("t1", "a2", "Ajoute x"))
        self.act("a2", "write_file", path="x.txt", content="x\n")
        self.act("a2", "commit", message="x")
        engine.submit_mr("u", "p1", "a2", "Bob")
        lg = self.lg()
        set_tasks(task("t1", "a2", "Ajoute x", "à vérifier"), task("t2", "a2", "Suite", started=5))
        self.act("a2", "status")                                                                   # le worktree quitte la branche de t1
        lg.integrate("task/bob/t1-ajoute-x", "develop")                                            # fusionnée hors de l'application…
        sh(lg.root, "branch", "-D", "task/bob/t1-ajoute-x")                                        # …puis branche supprimée
        self.assertEqual(engine.sync_mrs("u", "p1", force=True), [(1, "fusionnée")])               # fusionnée, pas « annulée »


class ParTacheRemote(RemoteBase):
    BRANCHES = "task"

    def prep(self):
        set_tasks(task("t1", "a2", "Ajoute x"))
        self.act("a2", "write_file", path="x.txt", content="x\n")
        self.act("a2", "commit", message="x")
        engine.submit_mr("u", "p1", "a2", "Bob", "", "t1")

    def branches(self):
        return sh(self.tmp, "ls-remote", "--heads", self.remote)

    def test_branche_distante_supprimee_apres_fusion(self):
        self.prep()
        r = engine.apply_mr_review("u", "p1", self.mid("a2"), "Alice", "approved", "")
        self.assertNotIn("Attention", r)
        self.assertNotIn("task/bob/t1-ajoute-x", self.branches())
        self.assertIn("refs/heads/develop", self.branches())
        self.assertEqual(self.remote_show("develop:x.txt"), "x\n")

    def test_option_conserver_les_branches_distantes(self):
        st = store.load("u")
        st["projects"][0]["keepbranches"] = True
        store.wj(store.state_path("u"), st)
        self.prep()
        engine.apply_mr_review("u", "p1", self.mid("a2"), "Alice", "approved", "")
        self.assertIn("refs/heads/task/bob/t1-ajoute-x", self.branches())

    def test_poussee_a_la_demande(self):
        set_tasks(task("t1", "a2", "Ajoute x"), task("t2", "a3", "Autre"))
        self.act("a2", "write_file", path="x.txt", content="x\n")
        self.act("a2", "commit", message="x")
        self.act("a3", "write_file", path="y.txt", content="y\n")
        self.act("a3", "commit", message="y")
        engine.git_push("u", "p1", "a2")                                                           # seulement Bob
        self.assertIn("task/bob/t1-ajoute-x", self.branches())
        self.assertNotIn("task/carl", self.branches())
        engine.git_push("u", "p1")                                                                 # tous
        self.assertIn("task/carl/t2-autre", self.branches())
        self.assertIn("poussée", self.logs())
        self.assertEqual(gitlocal.git(["rev-parse", "task/bob/t1-ajoute-x"], self.remote)[1].strip(),
                         gitlocal.git(["rev-parse", "task/bob/t1-ajoute-x"], self.root)[1].strip())


class PousseeSansRemote(Base):
    BRANCHES = "task"

    def test_sans_remote_ou_sans_mode_git(self):
        self.act("a2", "write_file", path="x.txt", content="x\n")
        engine.git_push("u", "p1")
        self.assertIn("Aucun remote", self.logs())


import rights  # noqa: E402
import sys  # noqa: E402

import shutil  # noqa: E402

PYX = "python3" if shutil.which("python3") else "python"          # chemin relatif : un chemin absolu serait (à raison) signalé « hors du dossier de travail »
OK_CMD = PYX + ' -c "pass"'
FAIL_CMD = PYX + ' -c "import sys; print(\'échec simulé\'); sys.exit(1)"'
BAD_FILE_CMD = PYX + ' -c "import os,sys; sys.exit(1 if os.path.exists(\'bad.txt\') else 0)"'


def add_check(name="tests", command=OK_CMD, when="both", blocking=True, baseline="green", retry_once=False, cid=None):
    st = store.load("u")
    c = {"id": cid or name, "name": name, "command": command, "when": when, "blocking": blocking, "timeout": 60, "retry_once": retry_once,
         "baseline": baseline, "active": True, "ver": 1}
    st["projects"][0].setdefault("checks", []).append(c)
    store.wj(store.state_path("u"), st)
    return c


def decisions(kind="check"):
    return [d for d in proj().get("decisions", []) if d["kind"] == kind]


class ChecksProposition(Base):
    """Un agent PROPOSE ; seule l'approbation de l'utilisateur active la commande (texte exact)."""
    EXPL = "Lance les tests du projet pour vérifier que rien n’est cassé ; ne modifie aucun fichier."

    def propose(self, who="a2", **kw):
        args = {"name": "tests", "command": OK_CMD, "when": "both", "blocking": True, "explain": self.EXPL, "reason": "Premier contrôle du projet"}
        args.update(kw)
        return self.act(who, "propose_check", **args)

    def test_proposition_creee_sans_rien_activer(self):
        note, back = self.propose()
        self.assertIn("vigilance faible", note)
        self.assertIn("rien ne s’exécute avant", back)
        d = decisions()[0]
        self.assertEqual((d["status"], d["byName"], d["payload"]["name"], d["payload"]["risk"]["level"]), ("attente", "Bob", "tests", "faible"))
        self.assertEqual(d["payload"]["explain"], self.EXPL)
        self.assertEqual(proj().get("checks", []), [])                                     # rien d'actif

    def test_commande_refusee_d_office_et_explication_obligatoire(self):
        r = self.propose(command="sudo apt install x")
        self.assertIn("refusée d’office", r[1])
        self.assertEqual(decisions(), [])
        r = self.propose(explain="ok")
        self.assertIn("Proposition incomplète", r[1])
        self.assertEqual(decisions(), [])
        r = self.propose(command="rm -rf build && pytest")                                  # élevée : proposée, mais signalée
        self.assertIn("vigilance élevée", r[0])
        self.assertEqual(decisions()[0]["payload"]["risk"]["level"], "élevée")

    def test_droit_interdit_et_jamais_libre(self):
        st = store.load("u")
        st["roles"].append({"id": "r3", "name": "Rédacteur technique", "desc": ""})            # les rôles sont globaux (hors projets)
        st["projects"][0]["agents"][2]["role"] = "r3"
        st["projects"][0]["agents"][1]["caps"] = {"check": "free"}                          # même « libre » : l'agent ne peut qu'proposer
        store.wj(store.state_path("u"), st)
        self.assertIn("ne t’autorise pas", self.propose("a3")[1])
        self.propose("a2")
        self.assertEqual(proj().get("checks", []), [])
        self.assertEqual(len(decisions()), 1)
        p = proj()
        self.assertEqual(rights.level(p, p["agents"][1], "check", False), rights.APPROVE)
        self.assertEqual(rights.level(p, p["agents"][2], "check", False), rights.NO)

    def test_doublon_en_attente_et_deja_actif(self):
        self.propose()
        self.assertIn("déjà en attente", self.propose()[0])
        self.assertEqual(len(decisions()), 1)
        add_check("lint", OK_CMD)
        self.assertIn("déjà actif", self.propose(name="lint")[1])

    def test_approbation_active_la_commande_et_etablit_la_reference(self):
        self.propose()
        did = decisions()[0]["id"]
        engine.decide("u", "p1", did, "accept")
        c = proj()["checks"][0]
        self.assertEqual((c["name"], c["command"], c["when"], c["blocking"], c["ver"], c["baseline"]), ("tests", OK_CMD, "both", True, 1, None))
        self.assertEqual(proj()["check_history"][0]["action"], "ajout")
        self.assertEqual(decisions()[0]["status"], "acceptée")
        engine.check_baseline("u", "p1", c["id"])                                           # lancé en arrière-plan à l'approbation
        self.assertEqual(proj()["checks"][0]["baseline"], "green")
        self.assertIn("état de référence VERT", self.logs())

    def test_etat_de_reference_rouge(self):
        self.propose(command=FAIL_CMD)
        engine.decide("u", "p1", decisions()[0]["id"], "accept")
        engine.check_baseline("u", "p1", proj()["checks"][0]["id"])
        self.assertEqual(proj()["checks"][0]["baseline"], "red")
        self.assertIn("reste informatif", self.logs())

    def test_l_approbation_ne_permet_pas_de_modifier_la_commande(self):
        self.propose()
        engine.decide("u", "p1", decisions()[0]["id"], "accept", fields={"command": "rm -rf /", "name": "piège", "title": "piège"})
        c = proj()["checks"][0]
        self.assertEqual((c["command"], c["name"]), (OK_CMD, "tests"))

    def test_modification_exige_une_nouvelle_approbation_et_garde_l_historique(self):
        self.propose()
        engine.decide("u", "p1", decisions()[0]["id"], "accept")
        cid = proj()["checks"][0]["id"]
        engine.check_baseline("u", "p1", cid)
        new = OK_CMD + " && " + OK_CMD
        self.propose(command=new, reason="ajoute une deuxième passe")
        d = decisions()[-1]
        self.assertEqual((d["payload"]["id"], d["payload"]["prev"]), (cid, OK_CMD))
        self.assertEqual(proj()["checks"][0]["command"], OK_CMD)                             # l'ancienne reste active jusqu'à la décision
        newer = new + " && " + OK_CMD
        self.propose(command=newer)                                                           # version révisée : remplace la proposition en attente
        ds = decisions()
        self.assertEqual([x["status"] for x in ds], ["acceptée", "remplacée", "attente"])
        engine.decide("u", "p1", ds[-1]["id"], "accept")
        c = proj()["checks"][0]
        self.assertEqual((c["command"], c["ver"], c["baseline"]), (newer, 2, None))          # commande différente : référence à rétablir
        h = proj()["check_history"][-1]
        self.assertEqual((h["action"], h["old"]["command"], h["new"]), ("modification", OK_CMD, newer))

    def test_refus_prevenu_l_agent(self):
        self.propose()
        asked = []
        with mock.patch.object(engine, "ask", lambda *a, **k: asked.append(a)):
            engine.decide("u", "p1", decisions()[0]["id"], "refuse", note="trop lent")
        self.assertEqual(proj().get("checks", []), [])
        self.assertEqual(decisions()[0]["status"], "refusée")
        self.assertEqual(asked[0][2], "a2")
        self.assertIn("trop lent", asked[0][3])

    def test_carte_et_prompt_sans_casser_le_formatage(self):
        add_check("avec%", PYX + ' -c "print(\'100%\')"')
        p = proj()
        txt = engine.repo_text(p, p["agents"][1])                                            # un « % » dans la commande ne doit pas faire échouer le prompt
        self.assertIn("100%", txt)
        self.assertIn("avec%", txt)
        self.assertIn('"do":"propose_check"', engine.ACTIONS)


class ChecksExecution(Base):
    """Contrôles approuvés exécutés par l'application à la soumission (worktree de l'agent), avec garde-fous."""

    def work(self, who="a2", path="src/x.py", content="print(1)\n"):
        self.act(who, "write_file", path=path, content=content)
        self.act(who, "commit", message="travail")

    def set_cmd(self, name, command):
        st = store.load("u")
        next(c for c in st["projects"][0]["checks"] if c["name"] == name)["command"] = command
        store.wj(store.state_path("u"), st)

    def test_echec_bloquant_renvoie_la_sortie_puis_transmet_apres_deux_tentatives(self):
        add_check("tests", FAIL_CMD)
        self.work()
        st, msg = engine.submit_mr("u", "p1", "a2", "Bob")
        self.assertEqual(st, "checks")
        self.assertIn("tentative 1/2", msg)
        self.assertIn("échec simulé", msg)                                                   # fin de la sortie renvoyée à l'agent
        self.assertEqual(proj()["mrs"], [])
        self.assertEqual(engine.submit_mr("u", "p1", "a2", "Bob")[1].count("tentative 2/2"), 1)
        st, msg = engine.submit_mr("u", "p1", "a2", "Bob")                                   # 3e : transmis au validateur tel quel
        self.assertEqual(st, "submitted")
        m = proj()["mrs"][0]
        self.assertIn("transmis au validateur", m["checks_note"])
        self.assertEqual([(c["name"], c["ok"], c["blocking"]) for c in m["checks"]], [("tests", False, True)])
        commits, files, dif = engine.git_mr_view("u", "p1", proj(), m)
        self.assertIn("✖ tests (bloquant)", commits)
        self.assertIn("⚠", commits)

    def test_une_reussite_remet_le_compteur_a_zero(self):
        add_check("tests", FAIL_CMD)
        self.work()
        engine.submit_mr("u", "p1", "a2", "Bob")
        self.assertEqual(proj()["check_tries"], {"agent/bob": 1})
        self.set_cmd("tests", OK_CMD)
        self.assertEqual(engine.submit_mr("u", "p1", "a2", "Bob")[0], "submitted")
        self.assertNotIn("agent/bob", proj().get("check_tries", {}))
        self.assertEqual([(c["name"], c["ok"]) for c in proj()["mrs"][0]["checks"]], [("tests", True)])

    def test_informatif_ou_reference_non_verte_ne_bloque_pas(self):
        add_check("lint", FAIL_CMD, blocking=False)
        add_check("tests", FAIL_CMD, blocking=True, baseline=None)                         # déclaré bloquant mais jamais vert sur la cible
        self.work()
        st, msg = engine.submit_mr("u", "p1", "a2", "Bob")
        self.assertEqual(st, "submitted", msg)
        m = proj()["mrs"][0]
        self.assertEqual([(c["name"], c["ok"], c["blocking"], c["declared_blocking"]) for c in m["checks"]], [("lint", False, False, False), ("tests", False, False, True)])
        txt = engine.git_mr_view("u", "p1", proj(), m)[0]
        self.assertIn("informatif", txt)
        self.assertIn("pas encore bloquant", txt)

    def test_selon_le_moment(self):
        add_check("integ", FAIL_CMD, when="integration")
        self.work()
        self.assertEqual(engine.submit_mr("u", "p1", "a2", "Bob")[0], "submitted")           # non lancé à la validation
        self.assertEqual(proj()["mrs"][0]["checks"], [])
        self.assertEqual(proj().get("check_runs", []), [])

    def test_rejouer_une_fois(self):
        marker = os.path.join(self.tmp, "marqueur")
        cmd = PYX + ' -c "import os,sys; f=r\'%s\'; ok=os.path.exists(f); open(f,\'w\').close(); sys.exit(0 if ok else 1)"' % marker
        add_check("instable", cmd, retry_once=True)
        self.work()
        self.assertEqual(engine.submit_mr("u", "p1", "a2", "Bob")[0], "submitted")
        c = proj()["mrs"][0]["checks"][0]
        self.assertEqual((c["ok"], c["flaky"]), (True, True))
        self.assertIn("instable", engine.git_mr_view("u", "p1", proj(), proj()["mrs"][0])[0])

    def test_executions_enregistrees_et_aucun_controle(self):
        self.work()
        engine.submit_mr("u", "p1", "a2", "Bob")
        self.assertIn("aucun contrôle configuré", engine.git_mr_view("u", "p1", proj(), proj()["mrs"][0])[0])      # le validateur n'est pas induit en erreur
        add_check("tests", OK_CMD)
        self.work(path="src/y.py", content="y\n")
        self.act("a2", "commit", message="y")
        engine.run_checks("u", "p1", proj(), self.wt("Bob"), "validation", "Bob")
        r = proj()["check_runs"][-1]
        self.assertEqual((r["name"], r["ok"], r["when"], r["who"]), ("tests", True, "validation", "Bob"))

    def test_soumission_refusee_renvoie_la_tache_a_l_agent_puis_escalade(self):
        set_tasks(task("t1", "a2", "Ajoute x", "à vérifier"))
        calls = []
        with mock.patch.object(engine, "spawn", lambda f, *a, **k: calls.append((f.__name__, a))):
            for n in (1, 2, 3):
                set_tasks(dict(proj()["tasks"][0], status="à vérifier"))
                self.assertTrue(engine.task_resubmit("u", "p1", "t1", proj()["agents"][1], "checks", "tests en échec"))
                self.assertEqual(proj()["tasks"][0]["status"], "à faire")
            self.assertEqual([c[0] for c in calls], ["run_task"] * 3)
            self.assertIn("[Soumission refusée] tests en échec", calls[0][1][4])
            set_tasks(dict(proj()["tasks"][0], status="à vérifier"))
            self.assertTrue(engine.task_resubmit("u", "p1", "t1", proj()["agents"][1], "checks", "tests en échec"))      # 4e : l'utilisateur est sollicité
        self.assertEqual(len(calls), 3)
        self.assertEqual(proj()["tasks"][0]["status"], "en attente")
        self.assertIn("contrôles automatiques en échec", [d for d in proj()["decisions"] if d["kind"] == "question"][0]["question"])
        self.assertFalse(engine.task_resubmit("u", "p1", "t1", proj()["agents"][1], "checks", "x"))                    # plus « à vérifier » : sans objet


class ChecksIntegration(Base):
    """Contrôles sur le résultat de la fusion, avant tout push ; référence ; restauration."""

    def work(self, who="a2", path="src/x.py", content="print(1)\n"):
        self.act(who, "write_file", path=path, content=content)
        self.act(who, "commit", message="travail")

    def mid(self, who="a2"):
        return next(m for m in reversed(proj()["mrs"]) if m["by_id"] == who)["id"]

    def test_echec_a_l_integration_annule_la_fusion_et_renvoie_a_l_auteur(self):
        add_check("tests", BAD_FILE_CMD, when="integration")
        self.work(path="bad.txt", content="x\n")
        engine.submit_mr("u", "p1", "a2", "Bob")
        lg = engine.git_repo("u", "p1", proj())
        before = lg.sha("develop")
        calls = []
        with mock.patch.object(engine, "spawn", lambda f, *a, **k: calls.append((f.__name__, a))):
            r = engine.apply_mr_review("u", "p1", self.mid(), "Alice", "approved", "")
        self.assertIn("contrôles automatiques en échec après intégration", r)
        self.assertEqual(lg.sha("develop"), before)                                           # fusion annulée : cible comme avant
        iw = os.path.join(lg.wt_dir, "_integration")
        self.assertEqual(sh(iw, "status", "--porcelain").strip(), "")
        self.assertEqual(sh(iw, "rev-parse", "HEAD").strip(), before)
        m = proj()["mrs"][0]
        self.assertEqual((m["status"], m["merge_tries"], m["conflict_files"]), ("conflit", 1, []))
        ask = [c for c in calls if c[0] == "ask"]
        self.assertEqual((len(ask), ask[0][1][2]), (1, "a2"))
        self.assertIn("tests", ask[0][1][3])
        self.assertEqual(proj()["check_runs"][-1]["when"], "integration")
        os.remove(os.path.join(self.wt("Bob"), "bad.txt"))                                    # l'agent corrige…
        self.act("a2", "commit", message="retire bad.txt")
        self.assertEqual(engine.submit_mr("u", "p1", "a2", "Bob")[0], "submitted")           # …et la même MR repart en validation
        self.assertEqual(len(proj()["mrs"]), 1)
        self.assertIn("fusionnée dans develop", engine.apply_mr_review("u", "p1", self.mid(), "Alice", "approved", ""))
        self.assertEqual(proj()["checks"][0]["baseline"], "green")

    def test_premiere_integration_etablit_la_reference(self):
        add_check("tests", OK_CMD, when="integration", baseline=None)
        self.work()
        engine.submit_mr("u", "p1", "a2", "Bob")
        engine.apply_mr_review("u", "p1", self.mid(), "Alice", "approved", "")
        self.assertEqual(proj()["checks"][0]["baseline"], "green")

    def test_reference_non_verte_ne_bloque_pas_la_fusion(self):
        add_check("tests", FAIL_CMD, when="integration", baseline=None)
        self.work()
        engine.submit_mr("u", "p1", "a2", "Bob")
        r = engine.apply_mr_review("u", "p1", self.mid(), "Alice", "approved", "")
        self.assertIn("fusionnée dans develop", r)
        self.assertIn("contrôles informatifs en échec : tests", r)
        self.assertEqual(proj()["checks"][0]["baseline"], None)

    def test_trop_d_echecs_demande_une_intervention(self):
        add_check("tests", BAD_FILE_CMD, when="integration")
        self.work(path="bad.txt", content="x\n")
        engine.submit_mr("u", "p1", "a2", "Bob")
        for _ in range(3):
            engine.merge_mr("u", "p1", self.mid())
        self.assertIn("intervention manuelle", engine.merge_mr("u", "p1", self.mid()))

    def test_retrait_et_restauration(self):
        add_check("tests", OK_CMD, cid="c1")
        engine.check_remove("u", "p1", "c1")
        self.assertEqual(proj()["checks"], [])
        h = proj()["check_history"][-1]
        self.assertEqual((h["action"], h["old"]["command"]), ("retrait", OK_CMD))
        engine.check_restore("u", "p1", h["id"])
        c = proj()["checks"][0]
        self.assertEqual((c["name"], c["command"], c["baseline"]), ("tests", OK_CMD, None))
        self.assertEqual(proj()["check_history"][-1]["action"], "restauration")

    def test_restauration_d_une_modification(self):
        add_check("tests", OK_CMD, cid="c1")
        d = {"byName": "Bob"}
        pp = store.load("u")["projects"][0]
        engine.checks_apply(pp, d, {"id": "c1", "name": "tests", "command": OK_CMD + " && " + OK_CMD, "when": "both", "blocking": True, "reason": "r"})
        st = store.load("u")
        st["projects"][0] = pp
        store.wj(store.state_path("u"), st)
        self.assertEqual(proj()["checks"][0]["ver"], 2)
        engine.check_restore("u", "p1", proj()["check_history"][-1]["id"])
        self.assertEqual(proj()["checks"][0]["command"], OK_CMD)
        self.assertEqual(proj()["checks"][0]["ver"], 3)


class ChecksRemote(RemoteBase):
    def test_rien_n_est_pousse_si_les_controles_echouent(self):
        add_check("tests", BAD_FILE_CMD, when="integration")
        self.act("a2", "write_file", path="bad.txt", content="x\n")
        self.act("a2", "commit", message="travail")
        engine.submit_mr("u", "p1", "a2", "Bob")
        engine.apply_mr_review("u", "p1", self.mid("a2"), "Alice", "approved", "")
        refs = sh(self.tmp, "ls-remote", self.remote)
        self.assertNotIn("refs/heads/develop", refs)
        self.assertNotIn("refs/heads/agent/bob", refs)


class Releases(Base):
    """Releases en mode git local : release/<version> depuis develop, fusion dans la base, tag annoté, retour dans develop."""

    def work(self, who="a2", path="src/x.py", content="print(1)\n"):
        self.act(who, "write_file", path=path, content=content)
        self.act(who, "commit", message="travail")

    def lg(self):
        return engine.git_repo("u", "p1", proj())

    def mid(self, who="a2"):
        return next(m for m in reversed(proj()["mrs"]) if m["by_id"] == who and not m.get("note"))["id"]

    def merged_in_release(self, content="print(1)\n", path="src/x.py"):
        engine.cycle_start("u", "p1", "v1.0")
        self.work(path=path, content=content)
        engine.submit_mr("u", "p1", "a2", "Bob")
        return engine.apply_mr_review("u", "p1", self.mid(), "Alice", "approved", "")

    def test_demarrage_cree_la_branche_et_redirige_les_mr_ouvertes(self):
        self.work()
        engine.submit_mr("u", "p1", "a2", "Bob")                                              # ouverte, vers develop
        self.assertEqual(proj()["mrs"][0]["to"], "develop")
        engine.cycle_start("u", "p1", "v1.0")
        p = proj()
        self.assertEqual(p["cycle"]["branch"], "release/v1.0")
        self.assertEqual(self.lg().sha("release/v1.0"), self.lg().sha("develop"))
        self.assertEqual(p["mrs"][0]["to"], "release/v1.0")
        self.assertIn("redirigée", self.logs())
        engine.cycle_start("u", "p1", "v2")
        self.assertIn("déjà en cours", self.logs())

    def test_publication_complete(self):
        self.assertIn("fusionnée dans release/v1.0", self.merged_in_release())
        lg = self.lg()
        self.assertNotEqual(gitlocal.git(["cat-file", "-e", "develop:src/x.py"], lg.root, check=False)[0], 0)     # develop n'a rien reçu pendant la release
        engine.release("u", "p1", "v1.0", "Première version")
        p = proj()
        self.assertEqual(sh(lg.root, "show", "main:src/x.py"), "print(1)\n")
        self.assertEqual(sh(lg.root, "cat-file", "-t", "v1.0").strip(), "tag")
        self.assertIn("Première version", sh(lg.root, "tag", "-l", "-n99", "v1.0"))
        self.assertEqual(sh(lg.root, "rev-parse", "v1.0^{commit}").strip(), lg.sha("main"))
        self.assertEqual(lg.sha("develop"), lg.sha("main"))                                    # develop avancée jusqu'à main (avance rapide)
        self.assertNotIn("cycle", p)
        self.assertEqual((p["releases"][-1]["v"], p["releases"][-1]["git"]), ("v1.0", True))
        self.assertEqual(p["mrs"][-1]["status"], "fusionnée")
        self.assertEqual(lg.current_branch(lg.root), "main")                                   # dépôt racine remis sur la base, à jour
        self.assertTrue(os.path.exists(os.path.join(self.root, "src", "x.py")))
        self.assertIn("La release est terminée", self.logs())
        self.assertFalse(p.get("releasing"))
        self.assertEqual(sh(os.path.join(lg.wt_dir, "_integration"), "status", "--porcelain").strip(), "")

    def test_apres_la_release_les_agents_repartent_de_develop(self):
        self.merged_in_release()
        engine.release("u", "p1", "v1.0", "n")
        self.assertEqual(engine.git_target(proj()), "develop")
        self.act("a3", "write_file", path="c.txt", content="c\n")                              # un nouvel agent part de develop, qui contient la release
        self.assertTrue(os.path.exists(os.path.join(self.wt("Carl"), "src", "x.py")))

    def test_refus_mr_ouverte_tache_a_verifier_tag_existant_sans_release(self):
        engine.release("u", "p1", "v9", "n")
        self.assertIn("Aucune release en cours", self.logs())
        engine.cycle_start("u", "p1", "v1.0")
        self.work()
        engine.submit_mr("u", "p1", "a2", "Bob")
        engine.release("u", "p1", "v1.0", "n")
        self.assertIn("encore ouverte", self.logs())
        engine.apply_mr_review("u", "p1", self.mid(), "Alice", "approved", "")
        set_tasks(task("t1", "a2", "À relire", "à vérifier"))
        engine.release("u", "p1", "v1.0", "n")
        self.assertIn("tâche(s) encore à vérifier", self.logs())
        set_tasks()
        self.lg().tag("v1.0", "déjà là", self.lg().sha("main"))
        engine.release("u", "p1", "v1.0", "n")
        self.assertIn("le tag v1.0 existe déjà", self.logs())
        self.assertIn("cycle", proj())                                                          # la release reste en cours
        self.assertEqual(self.lg().sha("main"), self.lg().sha("develop"))                      # rien n'a été fusionné dans main

    def test_controles_a_la_cloture_echec_laisse_la_base_intacte(self):
        self.merged_in_release(path="bad.txt", content="x\n")
        add_check("tests", BAD_FILE_CMD, when="integration")                                    # activé après la fusion de la MR : n'échoue qu'à la clôture
        lg = self.lg()
        before = lg.sha("main")
        engine.release("u", "p1", "v1.0", "n")
        self.assertEqual(lg.sha("main"), before)
        self.assertFalse(lg.tag_exists("v1.0"))
        self.assertIn("cycle", proj())
        self.assertIn("contrôles automatiques en échec sur main", self.logs())
        self.assertIn("la release reste en cours", self.logs())
        self.assertFalse(proj().get("releasing"))

    def test_retour_dans_develop_en_conflit_n_empeche_pas_la_publication(self):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("un\ndeux\n")
        self.act("a3", "status")                                                                 # crée le dépôt (commit initial avec a.txt)
        engine.cycle_start("u", "p1", "v1.0")
        self.work(path="a.txt", content="un\nBOB\n")
        engine.submit_mr("u", "p1", "a2", "Bob")
        engine.apply_mr_review("u", "p1", self.mid(), "Alice", "approved", "")
        lg = self.lg()
        w = lg.worktree("zed", "task/zed/t1", "develop")                                        # develop a bougé pendant la release (changement conflictuel)
        with open(os.path.join(w, "a.txt"), "w", encoding="utf-8") as f:
            f.write("un\nZED\n")
        lg.commit(w, "zed", "Zed")
        lg.integrate("task/zed/t1", "develop")
        dev = lg.sha("develop")
        engine.release("u", "p1", "v1.0", "n")
        self.assertNotIn("cycle", proj())                                                       # publiée malgré tout
        self.assertTrue(lg.tag_exists("v1.0"))
        self.assertEqual(lg.sha("develop"), dev)                                                # develop intacte
        self.assertIn("en conflit : à traiter à la main", self.logs())


class ReleasesRemote(RemoteBase):
    def work(self, path="src/x.py", content="print(1)\n"):
        self.act("a2", "write_file", path=path, content=content)
        self.act("a2", "commit", message="travail")

    def release_ready(self):
        engine.cycle_start("u", "p1", "v1.0")
        self.work()
        engine.submit_mr("u", "p1", "a2", "Bob")
        engine.apply_mr_review("u", "p1", self.mid("a2"), "Alice", "approved", "")

    def test_publication_poussee_sur_le_remote(self):
        engine.cycle_start("u", "p1", "v1.0")
        self.assertIn("refs/heads/release/v1.0", sh(self.tmp, "ls-remote", self.remote))        # la release est poussée dès son démarrage
        self.work()
        engine.submit_mr("u", "p1", "a2", "Bob")
        engine.apply_mr_review("u", "p1", self.mid("a2"), "Alice", "approved", "")
        engine.release("u", "p1", "v1.0", "Notes")
        refs = sh(self.tmp, "ls-remote", self.remote)
        self.assertIn("refs/tags/v1.0", refs)
        self.assertIn("refs/heads/develop", refs)
        self.assertEqual(self.remote_show("main:src/x.py"), "print(1)\n")
        self.assertEqual(self.remote_show("develop:src/x.py"), "print(1)\n")
        self.assertEqual(gitlocal.git(["rev-parse", "v1.0^{commit}"], self.remote)[1].strip(), gitlocal.git(["rev-parse", "main"], self.remote)[1].strip())
        self.assertIn("Branches et tag poussés", self.logs())
        self.assertNotIn("cycle", proj())

    def test_remote_injoignable_publie_en_local_avec_avertissement(self):
        self.release_ready()
        shutil.rmtree(self.remote)
        engine.release("u", "p1", "v1.0", "n")
        self.assertNotIn("cycle", proj())
        self.assertTrue(engine.git_repo("u", "p1", proj()).tag_exists("v1.0"))
        self.assertIn("poussée", self.logs().split("Release v1.0")[-1])
        self.assertIn("Attention", self.logs())

    def test_remote_qui_a_avance_est_integre_avant_la_publication(self):
        self.release_ready()
        other = os.path.join(self.tmp, "other")
        sh(self.tmp, "clone", self.remote, other)
        sh(other, "checkout", "-B", "main", "origin/main")
        with open(os.path.join(other, "externe.txt"), "w", encoding="utf-8") as f:
            f.write("e\n")
        sh(other, "add", "-A")
        sh(other, "commit", "-m", "externe")
        sh(other, "push", "origin", "main")
        engine.release("u", "p1", "v1.0", "n")
        self.assertEqual(self.remote_show("main:externe.txt"), "e\n")                          # le travail du remote est conservé
        self.assertEqual(self.remote_show("main:src/x.py"), "print(1)\n")


if __name__ == "__main__":
    unittest.main()
