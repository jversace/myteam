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


def set_policy(pol):
    st = store.load("u")
    st["projects"][0]["conflict_policy"] = pol
    store.wj(store.state_path("u"), st)



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
        set_policy("author")                                                                      # règle du projet : l'auteur résout (défaut : « ask »)
        self.base_file()
        self.work("a2", "a.txt", "un\nBOB\ntrois\n")
        self.work("a3", "a.txt", "un\nCARL\ntrois\n")
        self.assertEqual(engine.submit_mr("u", "p1", "a3", "Carl")[0], "submitted")              # les deux soumises avant toute intégration
        self.assertEqual(engine.submit_mr("u", "p1", "a2", "Bob")[0], "submitted")
        engine.apply_mr_review("u", "p1", self.mr("a2")["id"], "Alice", "approved", "")
        calls = []
        with mock.patch.object(engine, "spawn", lambda f, *a, **k: calls.append((f.__name__, a))):
            r = engine.apply_mr_review("u", "p1", self.mr("a3")["id"], "Alice", "approved", "")
        self.assertIn("confiée à Carl (auteur)", r)
        m = self.mr("a3")
        self.assertEqual((m["status"], m["merge_tries"], m["conflict_files"]), ("conflit", 1, ["a.txt"]))
        rc = [c for c in calls if c[0] == "resolve_conflict"]
        self.assertEqual((len(rc), rc[0][1][3]), (1, "a3"))                                       # la règle « auteur » confie la résolution à Carl
        calls2 = []
        with mock.patch.object(engine, "spawn", lambda f, *a, **k: calls2.append((f.__name__, a))):
            engine.resolve_conflict("u", "p1", rc[0][1][2], "a3")
        ask = [c for c in calls2 if c[0] == "ask"]
        self.assertEqual((len(ask), ask[0][1][2]), (1, "a3"))                                     # l'auteur est prévenu
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
        set_tasks(task("t1", "a2", "Nouvelle"), task("t2", "a2", "Autre", started=2))             # deux tâches en cours : pas de renommage automatique
        self.act("a2", "status")
        self.assertEqual(self.branch(), "agent/bob")                                               # travail non fusionné : branche héritée conservée
        self.assertIn("task/bob/", engine.repo_text(proj(), proj()["agents"][1]))
        engine.submit_mr("u", "p1", "a2", "Ancien travail")
        m = next(m for m in proj()["mrs"] if m["by_id"] == "a2")
        self.assertEqual(m["from"], "agent/bob")
        engine.apply_mr_review("u", "p1", m["id"], "Alice", "approved", "")
        self.act("a2", "status")
        self.assertEqual(self.branch(), "task/bob/t2-autre")                                       # héritée fusionnée : passage aux branches par tâche
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


class ConflitsRegle(Base):
    """Règle de conflit du projet appliquée aux conflits d'intégration git : auteur, agent désigné, décision de l'utilisateur."""
    RESOLU = "un\nBOB+CARL\ntrois\n"

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("un\ndeux\ntrois\n")

    def work(self, who, content):
        self.act(who, "write_file", path="a.txt", content=content)
        self.act(who, "commit", message="travail")

    def mr(self, who):
        return next(m for m in reversed(proj()["mrs"]) if m["by_id"] == who)

    def conflict_setup(self):
        """Bob et Carl modifient la même ligne ; les deux MR sont soumises avant toute intégration, celle de Bob est fusionnée : celle de Carl entre en conflit."""
        self.work("a2", "un\nBOB\ntrois\n")
        self.work("a3", "un\nCARL\ntrois\n")
        engine.submit_mr("u", "p1", "a3", "Carl")
        engine.submit_mr("u", "p1", "a2", "Bob")
        engine.apply_mr_review("u", "p1", self.mr("a2")["id"], "Alice", "approved", "")

    def approve_carl(self, sync_funcs=()):
        calls = []

        def sp(f, *a, **k):
            calls.append((f.__name__, a))
            if f.__name__ in sync_funcs:
                f(*a, **k)
        with mock.patch.object(engine, "spawn", sp):
            r = engine.apply_mr_review("u", "p1", self.mr("a3")["id"], "Alice", "approved", "")
        return r, calls

    def conflicts(self):
        return proj().get("conflicts", [])

    def resolver_answers(self, content=None, files=None):
        """Simule l'agent désigné : il répond au message de conflit par l'action resolve_merge."""
        got = []

        def fake_ask(u, pid, rid, msg, frm, **k):
            got.append((rid, msg))
            c = self.conflicts()[-1]
            res = engine.do_act("u", "p1", rid, {"do": "resolve_merge", "conflict": c["id"], "message": "résolution",
                                                 "files": files if files is not None else [{"path": "a.txt", "content": content or self.RESOLU}]}, 0, {"n": 0, "t0": time.time()})
            got.append(res)
        return fake_ask, got

    def test_auteur_resout_dans_son_worktree_et_le_conflit_est_clos_a_la_resoumission(self):
        set_policy("author")
        self.conflict_setup()
        r, calls = self.approve_carl()
        c = self.conflicts()[0]
        self.assertIn("confiée à Carl (auteur)", r)
        self.assertEqual((c["git"], c["mode"], c["resolver"], c["status"], c["files"], c["branch"], c["target"]), (True, "", "", "ouvert", ["a.txt"], "agent/carl", "develop"))
        engine.resolve_conflict("u", "p1", c["id"], "a3")                                         # lancé en arrière-plan par la règle (spawn)
        c = self.conflicts()[0]
        self.assertEqual((c["mode"], c["resolver"]), ("worktree", "a3"))
        self.assertEqual(self.mr("a3")["status"], "conflit")
        self.assertIn("conflit", str(self.act("a3", "sync")).lower())
        self.work("a3", self.RESOLU)
        with mock.patch.object(engine, "spawn", lambda *a, **k: None):
            self.assertEqual(engine.reconcile_conflicts("u", "p1", force=True), [])               # l'auteur a résolu mais pas encore resoumis : pas de fusion sans nouvelle validation
        self.assertEqual((self.conflicts()[0]["status"], self.mr("a3")["status"]), ("ouvert", "conflit"))
        self.assertEqual(engine.submit_mr("u", "p1", "a3", "Carl")[0], "submitted")
        self.assertEqual(self.conflicts()[0]["status"], "résolu")                                 # clos par la nouvelle soumission
        self.assertEqual(engine.reconcile_conflicts("u", "p1", force=True), [])                  # la revérification n'y touche pas
        self.assertIn("fusionnée", engine.apply_mr_review("u", "p1", self.mr("a3")["id"], "Alice", "approved", ""))
        self.assertEqual(sh(self.root, "show", "develop:a.txt"), self.RESOLU)

    def test_agent_designe_fournit_la_resolution_que_l_application_applique(self):
        set_policy("agent:a1")
        self.conflict_setup()
        fake_ask, got = self.resolver_answers()
        with mock.patch.object(engine, "ask", fake_ask):
            r, calls = self.approve_carl(sync_funcs=("resolve_conflict",))
        c = self.conflicts()[0]
        self.assertIn("confiée à Alice", r)
        self.assertEqual((c["mode"], c["resolver"], c["status"], c["resolved_by"]), ("direct", "a1", "résolu", "Alice"))
        self.assertIn("Version de la branche cible (develop)", got[0][1])                        # le résolveur reçoit les trois versions
        self.assertIn("BOB", got[0][1])
        self.assertIn("CARL", got[0][1])
        self.assertIn("l’application fusionne", got[1][0])
        m = self.mr("a3")
        self.assertEqual(m["resolution"], {"a.txt": self.RESOLU})                                 # résolution portée par la MR, appliquée à la fusion
        before = engine.git_repo("u", "p1", proj()).sha("develop")
        with mock.patch.object(engine, "spawn", lambda *a, **k: None):
            engine.after_conflict("u", "p1", c["id"])
        lg = engine.git_repo("u", "p1", proj())
        self.assertNotEqual(lg.sha("develop"), before)
        self.assertEqual(sh(lg.root, "show", "develop:a.txt"), self.RESOLU)
        self.assertEqual(len(sh(lg.root, "rev-list", "--parents", "-1", "develop").split()), 3)   # vrai commit de fusion
        self.assertEqual(self.mr("a3")["status"], "fusionnée")
        self.assertNotIn("resolution", self.mr("a3"))

    def test_resolution_incomplete_ou_avec_marqueurs_refusee(self):
        set_policy("agent:a1")
        self.conflict_setup()
        fake_ask, got = self.resolver_answers(files=[{"path": "autre.txt", "content": "x"}])
        with mock.patch.object(engine, "ask", fake_ask):
            self.approve_carl(sync_funcs=("resolve_conflict",))
        self.assertIn("Résolution incomplète : il manque a.txt", got[1][0])
        fake_ask2, got2 = self.resolver_answers(content="<<<<<<< x\nun\n=======\ndeux\n>>>>>>> y\n")
        with mock.patch.object(engine, "ask", fake_ask2):
            engine.resolve_conflict("u", "p1", self.conflicts()[0]["id"], "a1")
        self.assertIn("marqueurs de conflit", got2[1][0])
        self.assertEqual(self.conflicts()[0]["status"], "escalade")                               # deux échecs : l'utilisateur décide
        self.assertTrue([d for d in proj()["decisions"] if d["kind"] == "question"])

    def test_regle_ask_pose_une_question_puis_applique_le_choix(self):
        self.conflict_setup()                                                                     # politique par défaut : « ask »
        r, calls = self.approve_carl()
        self.assertIn("une décision est demandée", r)
        self.assertEqual([c[0] for c in calls if c[0] == "resolve_conflict"], [])
        q = next(d for d in proj()["decisions"] if d.get("conflict"))
        self.assertEqual((q["kind"], q["options"]), ("question", engine.CONFLICT_OPTS))
        self.assertIn("a.txt", q["question"])
        fake_ask, got = self.resolver_answers()                                                    # choix 2 : l'agent principal résout
        with mock.patch.object(engine, "ask", fake_ask), mock.patch.object(engine, "spawn", lambda *a, **k: None):
            engine.decide("u", "p1", q["id"], "accept", answer=engine.CONFLICT_OPTS[1])
        c = self.conflicts()[0]
        self.assertEqual((c["mode"], c["resolver"], c["status"]), ("direct", "a1", "résolu"))
        self.assertEqual(self.mr("a3")["resolution"], {"a.txt": self.RESOLU})

    def test_je_m_en_occupe_conflit_en_pause_puis_leve_par_la_verification(self):
        self.conflict_setup()
        self.approve_carl()
        q = next(d for d in proj()["decisions"] if d.get("conflict"))
        engine.decide("u", "p1", q["id"], "accept", answer=engine.CONFLICT_OPTS[2])
        c = self.conflicts()[0]
        self.assertEqual((c["status"], c["mode"]), ("pause", "manual"))
        self.assertIn("dépôt local", self.logs())
        engine.conflict_action("u", "p1", c["id"], "check")
        self.assertEqual(self.conflicts()[0]["status"], "pause")                                  # toujours en conflit
        self.assertIn("toujours présent", self.logs())
        self.assertIn("en pause", engine.merge_mr("u", "p1", self.mr("a3")["id"]))               # « Réessayer » : toujours en pause, pas de nouvelle question
        self.assertEqual(len(self.conflicts()), 1)
        self.act("a3", "sync")                                                                    # l'utilisateur (ici via l'agent) fusionne la cible dans la branche…
        self.work("a3", self.RESOLU)
        with mock.patch.object(engine, "spawn", lambda *a, **k: None):
            engine.conflict_action("u", "p1", c["id"], "check")                                   # …puis vérifie : le conflit est levé et la fusion reprend
        self.assertEqual(self.conflicts()[0]["status"], "résolu")
        self.assertEqual(self.mr("a3")["status"], "fusionnée")
        self.assertEqual(sh(self.root, "show", "develop:a.txt"), self.RESOLU)

    def test_trop_d_essais_escalade_vers_l_utilisateur(self):
        set_policy("author")
        self.conflict_setup()
        self.approve_carl()
        mid = self.mr("a3")["id"]
        with mock.patch.object(engine, "spawn", lambda *a, **k: None):
            for _ in range(2):
                engine.merge_mr("u", "p1", mid)
            r = engine.merge_mr("u", "p1", mid)
        self.assertIn("intervention manuelle", r)
        c = self.conflicts()[0]
        self.assertEqual((c["status"], len(self.conflicts())), ("escalade", 1))                   # un seul conflit suivi pour la MR
        self.assertTrue([d for d in proj()["decisions"] if d.get("conflict") == c["id"] and d["status"] == "attente"])

    def test_mr_close_ferme_le_conflit(self):
        set_policy("author")
        self.conflict_setup()
        self.approve_carl()
        mp = lambda pp: by_id(pp["mrs"], self.mr("a3")["id"]).update(status="annulée")
        pp = store.load("u")
        next(m for m in pp["projects"][0]["mrs"] if m["by_id"] == "a3")["status"] = "annulée"
        store.wj(store.state_path("u"), pp)
        engine.reconcile_conflicts("u", "p1", force=True)
        self.assertEqual(self.conflicts()[0]["status"], "abandonné")

    def test_gitlocal_conflicts_ne_modifie_rien(self):
        set_policy("author")
        self.conflict_setup()
        lg = engine.git_repo("u", "p1", proj())
        before = lg.sha("develop")
        info = lg.conflicts("agent/carl", "develop")
        self.assertEqual([i["path"] for i in info], ["a.txt"])
        self.assertEqual((info[0]["into"], info[0]["source"]), ("un\nBOB\ntrois\n", "un\nCARL\ntrois\n"))
        self.assertEqual((lg.sha("develop"), sh(os.path.join(lg.wt_dir, "_integration"), "status", "--porcelain").strip()), (before, ""))
        self.assertEqual(lg.conflicts("agent/bob", "develop"), [])                                # déjà fusionnée : aucun conflit


def by_id(items, i):
    return next((x for x in items if x["id"] == i), None)


class FinDeChantier(Base):
    """Branche héritée renommée, annulation d'un agent (modifications non commitées), poussée de la release à la demande."""
    BRANCHES = "task"

    def lg(self):
        return engine.git_repo("u", "p1", proj())

    def legacy(self):
        st = store.load("u")
        st["projects"][0]["branches"] = "agent"                                                    # ancien fonctionnement : une branche par agent
        store.wj(store.state_path("u"), st)
        self.act("a2", "write_file", path="ancien.txt", content="a\n")
        self.act("a2", "commit", message="avant la bascule")
        st = store.load("u")
        del st["projects"][0]["branches"]
        store.wj(store.state_path("u"), st)

    def test_branche_heritee_renommee_si_une_seule_tache_en_cours(self):
        self.legacy()
        set_tasks(task("t1", "a2", "Nouvelle"))
        self.act("a2", "status")
        lg = self.lg()
        self.assertEqual(lg.current_branch(self.wt("Bob")), "task/bob/t1-nouvelle")
        self.assertFalse(lg.branch_exists("agent/bob"))                                            # renommée, pas dupliquée
        self.assertTrue(os.path.exists(os.path.join(self.wt("Bob"), "ancien.txt")))                # son travail non fusionné est conservé
        self.assertIn("agent/bob renommée en task/bob/t1-nouvelle", self.logs())
        self.act("a2", "status")                                                                   # idempotent
        self.assertEqual(lg.current_branch(self.wt("Bob")), "task/bob/t1-nouvelle")

    def test_pas_de_renommage_avec_une_mr_ouverte_sur_l_ancienne_branche(self):
        self.legacy()
        st = store.load("u")
        st["projects"][0]["branches"] = "agent"
        store.wj(store.state_path("u"), st)
        engine.submit_mr("u", "p1", "a2", "Ancien travail")                                         # MR ouverte depuis agent/bob
        st = store.load("u")
        del st["projects"][0]["branches"]
        store.wj(store.state_path("u"), st)
        set_tasks(task("t1", "a2", "Nouvelle"))
        self.act("a2", "status")
        self.assertEqual(self.lg().current_branch(self.wt("Bob")), "agent/bob")                    # la MR garde son nom de branche

    def test_annulation_ecarte_les_modifications_non_commitees_seulement(self):
        self.act("a2", "write_file", path="garde.txt", content="v1\n")
        self.act("a2", "commit", message="premier")
        self.act("a2", "write_file", path="garde.txt", content="v2 non commitée\n")
        self.act("a2", "write_file", path="neuf.txt", content="x\n")
        txt = engine.stop_agent("u", "p1", "a2", cancel=True)
        self.assertIn("2 fichier(s) non commité(s) écarté(s)", txt)
        self.assertIn("garde.txt", txt)
        wt = self.wt("Bob")
        self.assertEqual(self.lg().status(wt), [])
        with open(os.path.join(wt, "garde.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "v1\n")                                                     # le commit est conservé
        self.assertFalse(os.path.exists(os.path.join(wt, "neuf.txt")))
        self.assertEqual(len(sh(wt, "log", "--format=%s").split("\n")) >= 2, True)

    def test_simple_arret_conserve_les_modifications(self):
        self.act("a2", "write_file", path="x.txt", content="x\n")
        txt = engine.stop_agent("u", "p1", "a2", cancel=False)
        self.assertIn("conservées", txt)
        self.assertTrue(os.path.exists(os.path.join(self.wt("Bob"), "x.txt")))
        locks.STOP.clear()
        set_tasks(task("t2", "a2", "Suite"))
        self.act("a2", "status")                                                                   # changement de tâche : enregistrées en WIP, rien de perdu
        self.assertIn("WIP", sh(self.root, "log", "-1", "--format=%s", "task/bob/libre"))

    def test_annulation_pendant_une_synchronisation_en_conflit(self):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("un\ndeux\n")
        for who, txt in (("a2", "un\nBOB\n"), ("a3", "un\nCARL\n")):
            self.act(who, "write_file", path="a.txt", content=txt)
            self.act(who, "commit", message=who)
        self.lg().integrate("task/bob/libre", "develop")
        self.assertIn("conflit", str(self.act("a3", "sync")).lower())
        with open(os.path.join(self.wt("Carl"), "a.txt"), encoding="utf-8") as f:
            self.assertIn("<<<<<<<", f.read())
        engine.stop_agent("u", "p1", "a3", cancel=True)
        locks.STOP.clear()
        self.assertEqual(self.lg().status(self.wt("Carl")), [])
        with open(os.path.join(self.wt("Carl"), "a.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "un\nCARL\n")                                               # retour à son dernier commit
        self.assertIn("Synchronisation", str(self.act("a3", "sync")))                              # le worktree est de nouveau utilisable


class PousseeRelease(RemoteBase):
    def test_reprise_de_la_poussee_apres_un_echec(self):
        engine.cycle_start("u", "p1", "v1.0")
        self.act("a2", "write_file", path="x.txt", content="x\n")
        self.act("a2", "commit", message="x")
        engine.submit_mr("u", "p1", "a2", "Bob")
        engine.apply_mr_review("u", "p1", self.mid("a2"), "Alice", "approved", "")
        shutil.rmtree(self.remote)                                                                 # remote injoignable à la publication
        engine.release("u", "p1", "v1.0", "n")
        self.assertIn("Attention", self.logs())
        sh(self.tmp, "init", "--bare", "-b", "main", self.remote)                                  # le remote est revenu (vide)
        engine.git_push_release("u", "p1")
        refs = sh(self.tmp, "ls-remote", self.remote)
        for r in ("refs/heads/main", "refs/heads/develop", "refs/tags/v1.0"):
            self.assertIn(r, refs)
        self.assertEqual(self.remote_show("main:x.txt"), "x\n")
        self.assertIn("tags poussés", self.logs())

    def test_sans_remote(self):
        st = store.load("u")
        st["projects"][0]["repo"]["url"] = ""
        store.wj(store.state_path("u"), st)
        engine.GIT_OK.clear()
        shutil.rmtree(self.root)
        os.makedirs(self.root)
        engine.git_push_release("u", "p1")
        self.assertIn("Aucun remote", self.logs())



class EconomieTokens(Base):
    """Plan, lecture ciblée et contexte de travail dans le worktree git de l'agent."""
    def test_outline_read_symbol_find_symbol_et_contexte_dans_le_worktree(self):
        self.act("a2", "write_file", path="src/x.py", content="def tache():\n    return 42\n\n\nclass Moteur:\n    def run(self):\n        return 1\n")
        n, back = self.act("a2", "outline", path="")
        self.assertIn("src/x.py", back)
        self.assertIn("Moteur{1}", back)
        self.assertIn("ton worktree", self.act("a2", "outline", path="src/x.py")[1])
        self.assertIn("return 42", self.act("a2", "read_symbol", path="src/x.py", symbol="tache")[1])
        self.assertNotIn("class Moteur", self.act("a2", "read_symbol", path="src/x.py", symbol="tache")[1])
        self.assertIn("src/x.py:L5-7", self.act("a2", "find_symbol", name="Moteur")[1])
        self.assertIn("Aucun symbole", self.act("a3", "find_symbol", name="Moteur")[1])        # Carl ne voit pas le worktree de Bob
        n, ctx = self.act("a2", "get_work_context")
        self.assertIn("src/x.py", ctx)                                                         # fichier modifié non commité
        self.assertIn("Ton worktree", ctx)

    def test_agent_autre_refuse_en_mode_git(self):
        self.act("a2", "write_file", path="x.py", content="def f():\n    pass\n")
        self.assertIn("git_show", self.act("a2", "outline", path="", agent="Carl")[1])



class SortiesCompactes(Base):
    """Point 3 de l'économie de tokens : run_checks à la demande, sorties nettoyées et plafonnées."""
    def test_run_checks_resume_compact_et_echecs_seuls(self):
        self.act("a2", "write_file", path="x.py", content="print(1)\n")
        self.assertNotIn("run_checks", engine.actions_extra(proj(), proj()["agents"][1]))      # aucun contrôle actif : action non proposée
        self.assertIn("Aucun contrôle", self.act("a2", "run_checks")[0])
        add_check("tests", OK_CMD)
        add_check("lint", FAIL_CMD, blocking=False)
        self.assertIn('"do":"run_checks"', engine.actions_extra(proj(), proj()["agents"][1]))
        note, back = self.act("a2", "run_checks")
        self.assertIn("1 réussi(s), 1 en échec", note)
        self.assertIn("✔ tests", back)
        self.assertIn("✖ lint", back)
        self.assertNotIn("Tout est vert", back)
        self.assertEqual(sum(1 for r in proj().get("check_runs", [])), 2)                        # historisé comme les autres exécutions

    def test_run_checks_tout_vert_et_refus_hors_git(self):
        add_check("tests", OK_CMD)
        self.act("a2", "write_file", path="x.py", content="print(1)\n")
        self.assertIn("Tout est vert", self.act("a2", "run_checks")[1])


class Plafonds(unittest.TestCase):
    def test_compact_retire_le_bruit_et_regroupe_les_repetitions(self):
        import checks
        raw = "\x1b[31mFAIL\x1b[0m test_a\n\n[=====>      ] 45%\n----------\n" + "warning: x\n" * 5 + "AssertionError: 1 != 2\n.....\n"
        out = checks.compact(raw)
        self.assertEqual(out, "FAIL test_a\nwarning: x  (×5)\nAssertionError: 1 != 2")
        self.assertEqual(checks.compact("a\n" * 5000), "a  (×5000)")                       # 5 000 lignes identiques : une seule
        self.assertTrue(checks.compact("".join("ligne %d\n" % i for i in range(5000)), 100).startswith("[…]"))

    def test_cap_back(self):
        self.assertEqual(engine.cap_back("abc"), "abc")
        r = engine.cap_back("x" * (engine.BACK_MAX + 500))
        self.assertTrue(r.startswith("x" * 100) and "tronqué" in r and len(r) < engine.BACK_MAX + 200)

    def test_list_files_plafonne(self):
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        for i in range(engine.LIST_MAX + 40):
            (tmp / ("f%04d.txt" % i)).write_text("x", encoding="utf-8")
        from test_locks import make_state as ms
        store.wj(store.state_path("u"), ms(tmp))
        note, back = engine.do_act("u", "p1", "a2", {"do": "list_files", "path": ""}, 0, {"n": 0, "t0": time.time()})
        self.assertEqual(back.count("f0"), engine.LIST_MAX)
        self.assertIn("40 entrées de plus", back)



class DiffCompact(Base):
    """Action diff : synthèse par fichier, diff compact, filtre par chemin, budget."""
    def commit(self, **files):
        for n, c in files.items():
            self.act("a2", "write_file", path=n.replace("__", "/"), content=c)
        self.act("a2", "commit", message="m")

    def test_synthese_puis_diff_et_mention_du_non_commite(self):
        self.commit(src__a="un\ndeux\n", src__b="x\n")
        self.act("a2", "write_file", path="src/c", content="pas commité\n")
        note, back = self.act("a2", "diff")
        self.assertIn("2 fichier(s) modifié(s), +3/-0", back)
        self.assertIn("A  src/a  +2/-0", back)
        self.assertIn("+deux", back)
        self.assertIn("1 fichier(s) modifié(s) NON commité(s)", back)
        self.assertNotIn("pas commité", back)

    def test_stat_seul_et_filtre_par_chemin(self):
        self.commit(src__a="un\ndeux\n", doc__b="texte\n")
        stat = self.act("a2", "diff", stat=True)[1]
        self.assertIn("A  doc/b", stat)
        self.assertNotIn("+deux", stat)                                       # pas de contenu en mode synthèse
        one = self.act("a2", "diff", path="doc")[1]
        self.assertIn("1 fichier(s)", one)
        self.assertIn("+texte", one)
        self.assertNotIn("deux", one)
        self.assertIn("Aucune différence", self.act("a2", "diff", path="inexistant")[1])
        self.assertIn("Chemin invalide", self.act("a2", "diff", path="../x")[1])

    def test_budget_fichiers_non_detailles_puis_demande_unitaire(self):
        self.commit(**{"f%d" % i: ("ligne %d\n" % i) * 300 for i in range(6)})            # ~ 2 700 car. chacun : 8 000 au total ne suffisent pas pour six
        back = self.act("a2", "diff")[1]
        self.assertIn("6 fichier(s) modifié(s)", back)
        self.assertIn("Non détaillés (budget atteint)", back)
        self.assertLess(len(back), 9500)
        un = self.act("a2", "diff", path="f5")[1]
        self.assertIn("+ligne 5", un)
        self.assertNotIn("Non détaillés", un)

    def test_fichier_unique_tres_long_recoit_tout_le_budget(self):
        self.commit(g="".join("ligne %04d\n" % i for i in range(400)))                    # ~ 4 400 car. > per_file (2 500)
        back = self.act("a2", "diff", path="g")[1]
        self.assertIn("+ligne 0399", back)                                                # un seul fichier désigné : budget complet


if __name__ == "__main__":
    unittest.main()
