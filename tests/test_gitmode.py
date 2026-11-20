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


def make_state(localdir, nogit=None, url=""):
    ag = lambda i, n, **k: {**dict(id=i, name=n, service="s1", role="r1", branch="agent/" + n.lower(), canBug=False, talk=["a1", "a2", "a3"],
                                   history=[], notes="", source="local"), **k}
    p = {"id": "p1", "name": "Demo", "rules": "", "spec": "", "mainId": "a1", "localdir": str(localdir),
         "repo": {"url": url, "branch": "main", "token": ""},
         "roles": [{"id": "r1", "name": "Chef de projet", "desc": "Coordonne"}, {"id": "r2", "name": "Développeur back-end", "desc": "Développe"}],
         "agents": [ag("a1", "Alice"), ag("a2", "Bob", role="r2"), ag("a3", "Carl", role="r2")],
         "tasks": [], "bugs": [], "modules": [], "log": [], "commits": [], "releases": [], "mrs": []}
    if nogit is not None:
        p["nogit"] = nogit
    return {"services": [{"id": "s1", "name": "S", "kind": "openai", "url": "x", "model": "m", "apiKey": "k"}], "projects": [p]}


def proj():
    return store.load("u")["projects"][0]


def sh(cwd, *a):
    return gitlocal.git(list(a), cwd, identity=("T", "t@example.com"))[1]


class Base(unittest.TestCase):
    NOGIT = False
    URL = ""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="myteam-mode-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        store.wj(store.state_path("u"), make_state(self.root, self.NOGIT, self.URL))
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

    def test_pull_et_merge_request_redirigent_vers_sync(self):
        self.assertIn("sync", str(self.act("a2", "pull")))
        self.assertIn("sync", str(self.act("a2", "merge_request", title="t")))

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


if __name__ == "__main__":
    unittest.main()
