"""Tests de gitlocal.py : vrais dépôts git dans des dossiers temporaires, remote = dépôt nu local (aucun réseau)."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gitlocal as gl                                   # noqa: E402
from store import Err                                   # noqa: E402

ID = ("Test", "test@example.com")


def sh(cwd, *args):
    return gl.git(list(args), cwd, identity=ID)[1]


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="myteam-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)

    def bare(self, content=None):
        """Remote nu ; avec `content` {fichier: texte}, un premier commit sur main y est poussé."""
        r = os.path.join(self.tmp, "remote.git")
        sh(self.tmp, "init", "--bare", "-b", "main", r)
        if content is not None:
            seed = os.path.join(self.tmp, "seed")
            sh(self.tmp, "init", "-b", "main", seed)
            for k, v in content.items():
                write(os.path.join(seed, k), v)
            sh(seed, "add", "-A")
            sh(seed, "commit", "-m", "seed")
            sh(seed, "push", r, "main")
        return r

    def repo(self, remote="", **kw):
        g = gl.LocalGit(self.root, remote=remote, **kw)
        g.init()
        return g

    def seeded(self, files=None):
        write(os.path.join(self.root, "a.txt"), "un\ndeux\ntrois\n")
        for k, v in (files or {}).items():
            write(os.path.join(self.root, k), v)
        return self.repo()


class Noms(unittest.TestCase):
    def test_slug_et_branche_de_tache(self):
        self.assertEqual(gl.slug("Éloïse Dupont"), "eloise-dupont")
        self.assertEqual(gl.slug("///", "x"), "x")
        b = gl.task_branch("Éloïse", "T12", "Ajouter le filtre « galerie »")
        self.assertEqual(b, "task/eloise/t12-ajouter-le-filtre-galerie")
        self.assertFalse(b.startswith("agent/"))            # pas de conflit de références avec une branche héritée `agent/<nom>`

    def test_norm_url(self):
        n = gl.norm_url
        self.assertEqual(n("https://github.com/Foo/Bar.git"), "github.com/foo/bar")
        self.assertEqual(n("https://user:tok@github.com/foo/bar/"), "github.com/foo/bar")
        self.assertEqual(n("git@github.com:foo/bar.git"), "github.com/foo/bar")
        self.assertNotEqual(n("https://github.com/foo/bar"), n("https://github.com/foo/baz"))

    def test_safe_join(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.assertTrue(gl.safe_join(d, "a/b.txt").startswith(os.path.realpath(d)))
        for bad in ("../x", "a/../../x", ".git/config", "x/.GIT/hooks", ".myteam/worktrees/a"):
            with self.assertRaises(Err, msg=bad):
                gl.safe_join(d, bad)
        if hasattr(os, "symlink"):
            out = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, out, True)
            os.symlink(out, os.path.join(d, "lien"))
            with self.assertRaises(Err):
                gl.safe_join(d, "lien/secret.txt")

    def test_commande_non_autorisee(self):
        with self.assertRaises(Err):
            gl.git(["config", "--global", "user.name", "x"], tempfile.gettempdir())
        with self.assertRaises(Err):
            gl.git(["daemon"], tempfile.gettempdir())

    def test_jeton_masque_et_transmis_par_l_environnement(self):
        self.assertEqual(gl._clean("fatal: https://x:SECRET123@h", "SECRET123"), "fatal: https://x:***@h")
        e = gl._env("SECRET123", "https://github.com/o/r.git", network=True)
        self.assertEqual(e["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertIn("Authorization: Basic", e["GIT_CONFIG_VALUE_0"])
        self.assertNotIn("SECRET123", "".join(e.values()))   # seulement encodé dans l'en-tête, jamais en clair
        self.assertNotIn("GIT_CONFIG_COUNT", gl._env("SECRET123", "https://github.com/o/r.git", network=False))
        self.assertNotIn("GIT_CONFIG_COUNT", gl._env("SECRET123", "/tmp/remote.git", network=True))   # remote local : pas d'en-tête


class Preflight(Base):
    def test_dossier_absent_vide_et_non_vide(self):
        self.assertFalse(gl.preflight(os.path.join(self.tmp, "nope"))["ok"])
        r = gl.preflight(self.root)
        self.assertEqual((r["ok"], r["state"]), (True, "empty"))
        write(os.path.join(self.root, "x.txt"), "1")
        r = gl.preflight(self.root)
        self.assertEqual((r["ok"], r["state"]), (True, "plain"))             # sans remote : initialisable à partir de l'existant

    def test_non_vide_sans_git_avec_remote_inutilisable(self):
        write(os.path.join(self.root, "x.txt"), "1")
        r = gl.preflight(self.root, "https://github.com/o/r")
        self.assertFalse(r["ok"])
        self.assertIn("inutilisable", r["problems"][0])
        with self.assertRaises(Err):
            gl.LocalGit(self.root, remote="https://github.com/o/r").init()
        self.assertFalse(os.path.exists(os.path.join(self.root, ".git")))    # rien n'a été touché

    def test_depot_propre_origin_different_modifications(self):
        g = self.seeded()
        self.assertTrue(gl.preflight(self.root)["ok"])
        sh(self.root, "remote", "add", "origin", "https://github.com/o/r.git")
        self.assertTrue(gl.preflight(self.root, "https://github.com/O/r")["ok"])
        r = gl.preflight(self.root, "https://github.com/o/autre")
        self.assertFalse(r["ok"])
        self.assertIn("ne correspond pas", r["problems"][0])
        write(os.path.join(self.root, "inconnu.txt"), "x")                    # fichier non suivi : simple avertissement
        r = gl.preflight(self.root, "https://github.com/o/r")
        self.assertEqual((r["ok"], len(r["warnings"])), (True, 1))
        write(os.path.join(self.root, "a.txt"), "modifié\n")                  # fichier suivi modifié : problème
        r = gl.preflight(self.root, "https://github.com/o/r")
        self.assertFalse(r["ok"])
        self.assertIn("non commitées", " ".join(r["problems"]))
        g.commit(self.root, "wip")

    def test_depot_sans_origin_alors_que_le_projet_en_definit_un(self):
        self.seeded()
        r = gl.preflight(self.root, "https://github.com/o/r")
        self.assertFalse(r["ok"])
        self.assertIn("pas de remote", r["problems"][0])

    def test_sous_dossier_d_un_autre_depot(self):
        self.seeded()
        sub = os.path.join(self.root, "sous")
        os.makedirs(sub)
        r = gl.preflight(sub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "nested")

    def test_merge_inacheve(self):
        g = self.seeded()
        w1 = g.worktree("alice", "task/alice/t1")
        write(os.path.join(w1, "a.txt"), "alice\n")
        g.commit(w1, "alice", "Alice")
        w2 = g.worktree("bob", "task/bob/t1", "develop")
        write(os.path.join(w2, "a.txt"), "bob\n")
        g.commit(w2, "bob", "Bob")
        # un merge inachevé dans le dépôt racine rend le dossier inutilisable
        sh(self.root, "branch", "-f", "tmp-a", "task/alice/t1")
        sh(self.root, "branch", "-f", "tmp-b", "task/bob/t1")
        sh(self.root, "checkout", "-q", "tmp-a")
        gl.git(["merge", "tmp-b"], self.root, check=False)
        r = gl.preflight(self.root)
        self.assertFalse(r["ok"])
        self.assertIn("inachevée", " ".join(r["problems"]))


class Initialisation(Base):
    def test_sans_remote(self):
        g = gl.LocalGit(self.root)
        self.assertEqual(g.init(), "initialisé")
        self.assertTrue(g.branch_exists("main") and g.branch_exists("develop"))
        self.assertTrue(os.path.exists(os.path.join(self.root, ".gitignore")))
        self.assertEqual(g.init(), "existant")                                  # idempotent

    def test_dossier_non_vide_sans_remote_conserve_les_fichiers(self):
        write(os.path.join(self.root, "src", "m.py"), "print(1)\n")
        g = gl.LocalGit(self.root)
        g.init()
        self.assertIn("src/m.py", sh(self.root, "ls-files"))

    def test_clone_d_un_remote_existant(self):
        r = self.bare({"README.md": "# demo\n"})
        g = gl.LocalGit(self.root, remote=r)
        self.assertEqual(g.init(), "cloné")
        self.assertEqual(read(os.path.join(self.root, "README.md")), "# demo\n")
        self.assertTrue(g.branch_exists("develop"))

    def test_remote_vide(self):
        r = self.bare()
        g = gl.LocalGit(self.root, remote=r)
        g.init()
        self.assertTrue(g.branch_exists("main") and g.branch_exists("develop"))
        self.assertTrue(g.has("HEAD"))

    def test_myteam_exclu_du_suivi(self):
        g = self.seeded()
        w = g.worktree("alice", "task/alice/t1")
        self.assertTrue(os.path.isdir(w))
        self.assertEqual(sh(self.root, "status", "--porcelain").strip(), "")    # .myteam/ n'apparaît pas comme fichier non suivi


class Worktrees(Base):
    def test_un_worktree_par_agent_isole(self):
        g = self.seeded()
        wa, wb = g.worktree("alice", "task/alice/t1"), g.worktree("bob", "task/bob/t1")
        self.assertNotEqual(wa, wb)
        write(os.path.join(wa, "a.txt"), "modif alice\n")
        self.assertEqual(read(os.path.join(wb, "a.txt")), "un\ndeux\ntrois\n")      # l'autre agent ne voit rien
        self.assertEqual(read(os.path.join(self.root, "a.txt")), "un\ndeux\ntrois\n")
        self.assertEqual(g.worktree("alice", "task/alice/t1"), wa)                  # idempotent

    def test_une_branche_un_seul_agent(self):
        g = self.seeded()
        g.worktree("alice", "task/alice/t1")
        with self.assertRaises(Err) as c:
            g.worktree("bob", "task/alice/t1")
        self.assertEqual(c.exception.code, 409)
        with self.assertRaises(Err):
            g.worktree("alice", "task/alice/t2")                                     # le worktree d'alice est déjà sur t1

    def test_branche_depuis_la_cible_et_source_introuvable(self):
        g = self.seeded()
        g.worktree("alice", "task/alice/t1", "develop")
        self.assertEqual(g.sha("task/alice/t1"), g.sha("develop"))
        with self.assertRaises(Err):
            g.worktree("bob", "task/bob/t1", "release/9.9")
        with self.assertRaises(Err):
            g.worktree("bob", "bad..name")

    def test_drop(self):
        g = self.seeded()
        w = g.worktree("alice", "task/alice/t1")
        g.drop_worktree("alice")
        self.assertFalse(os.path.exists(w))
        self.assertTrue(g.branch_exists("task/alice/t1"))                           # la branche est conservée
        g.worktree("bob", "task/alice/t1")                                           # …et peut être reprise (relais)


class Outils(Base):
    def test_status_commit_diff_log_show_search(self):
        g = self.seeded()
        w = g.worktree("alice", "task/alice/t1")
        self.assertEqual(g.status(w), [])
        self.assertIsNone(g.commit(w, "rien"))
        write(os.path.join(w, "a.txt"), "un\nDEUX\ntrois\n")
        write(os.path.join(w, "neuf.txt"), "bonjour\n")
        self.assertEqual({x["path"] for x in g.status(w)}, {"a.txt", "neuf.txt"})
        sha = g.commit(w, "Ajoute neuf et modifie a", agent="Alice", service="Claude", task="T1")
        self.assertTrue(sha)
        self.assertEqual(g.status(w), [])
        msg = sh(w, "log", "-1", "--format=%an|%ae|%B")
        self.assertIn("Alice|alice@myteam.local|Ajoute neuf et modifie a", msg)
        self.assertIn("Agent-Service: Claude", msg)
        self.assertIn("Task: T1", msg)
        d = g.diff(w, "develop")
        self.assertIn("+DEUX", d)
        self.assertIn("neuf.txt", d)
        self.assertIn("[…diff tronqué", g.diff(w, "develop", budget=20))
        self.assertIn("Ajoute neuf", g.log(w))
        self.assertEqual(g.show(w, "HEAD", "neuf.txt"), "bonjour\n")
        self.assertEqual(g.search(w, "deux"), ["a.txt:2:DEUX"])
        write(os.path.join(w, "libre.txt"), "deux fois deux\n")                      # fichier non suivi : trouvé aussi
        self.assertIn("libre.txt:1:deux fois deux", g.search(w, "DEUX"))
        with self.assertRaises(Err):
            g.search(w, "  ")
        with self.assertRaises(Err):
            g.show(w, "HEAD", "../x")
        with self.assertRaises(Err):
            g.show(w, "HEAD", "absent.txt")

    def test_les_hooks_du_depot_ne_s_executent_pas(self):
        g = self.seeded()
        w = g.worktree("alice", "task/alice/t1")
        hook = os.path.join(self.root, ".git", "hooks", "pre-commit")
        write(hook, "#!/bin/sh\necho piège > %s\nexit 1\n" % os.path.join(self.tmp, "hook-ran"))
        os.chmod(hook, 0o755)
        write(os.path.join(w, "b.txt"), "b\n")
        self.assertTrue(g.commit(w, "doit passer"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "hook-ran")))

    def test_secrets_ignores_par_defaut(self):
        g = self.seeded()
        w = g.worktree("alice", "task/alice/t1")
        write(os.path.join(w, ".env"), "TOKEN=abc\n")
        write(os.path.join(w, "ok.txt"), "ok\n")
        g.commit(w, "x")
        self.assertNotIn(".env", sh(w, "ls-files"))


class Synchronisation(Base):
    def prep(self):
        g = self.seeded()
        wa = g.worktree("alice", "task/alice/t1")
        wb = g.worktree("bob", "task/bob/t1")
        return g, wa, wb

    def test_integrate_puis_sync_sans_conflit(self):
        g, wa, wb = self.prep()
        write(os.path.join(wa, "alice.txt"), "alice\n")
        g.commit(wa, "alice", "Alice")
        r = g.integrate("task/alice/t1", "develop")
        self.assertEqual(r["state"], "merged")
        self.assertEqual(len(sh(g.root, "rev-list", "--parents", "-1", "develop").split()), 3)   # --no-ff : commit de fusion à deux parents
        self.assertTrue(g.merged("task/alice/t1", "develop"))
        self.assertFalse(g.merged("task/bob/t1", "develop") and g.ahead_behind("task/bob/t1", "develop")["behind"] == 0)
        write(os.path.join(wb, "bob.txt"), "bob\n")
        g.commit(wb, "bob", "Bob")
        s = g.sync(wb, "develop", "Bob")
        self.assertEqual(s["state"], "merged")
        self.assertTrue(os.path.exists(os.path.join(wb, "alice.txt")))                    # bob a récupéré le travail d'alice
        self.assertEqual(g.sync(wb, "develop", "Bob")["state"], "uptodate")
        self.assertEqual(g.integrate("task/alice/t1", "develop")["state"], "uptodate")
        self.assertEqual(g.integrate("task/bob/t1", "develop")["state"], "merged")

    def test_conflit_a_l_integration_etat_propre_et_info(self):
        g, wa, wb = self.prep()
        write(os.path.join(wa, "a.txt"), "un\nALICE\ntrois\n")
        g.commit(wa, "alice", "Alice")
        write(os.path.join(wb, "a.txt"), "un\nBOB\ntrois\n")
        g.commit(wb, "bob", "Bob")
        self.assertEqual(g.integrate("task/alice/t1", "develop")["state"], "merged")
        before = g.sha("develop")
        r = g.integrate("task/bob/t1", "develop")
        self.assertEqual(r["state"], "conflict")
        self.assertEqual(r["files"], ["a.txt"])
        i = r["info"][0]
        self.assertEqual((i["path"], i["base"], i["into"], i["source"]), ("a.txt", "un\ndeux\ntrois\n", "un\nALICE\ntrois\n", "un\nBOB\ntrois\n"))
        self.assertEqual(g.sha("develop"), before)                                        # cible intacte
        iw = os.path.join(g.wt_dir, "_integration")
        self.assertEqual(sh(iw, "status", "--porcelain").strip(), "")                      # worktree d'intégration propre
        self.assertEqual(g.integrate("task/alice/t1", "develop")["state"], "uptodate")     # et réutilisable

    def test_resolution_du_conflit_par_l_agent_puis_integration(self):
        g, wa, wb = self.prep()
        write(os.path.join(wa, "a.txt"), "un\nALICE\ntrois\n")
        g.commit(wa, "alice", "Alice")
        write(os.path.join(wb, "a.txt"), "un\nBOB\ntrois\n")
        g.commit(wb, "bob", "Bob")
        g.integrate("task/alice/t1", "develop")
        s = g.sync(wb, "develop", "Bob")
        self.assertEqual((s["state"], s["files"]), ("conflict", ["a.txt"]))
        self.assertIn("<<<<<<<", read(os.path.join(wb, "a.txt")))                         # marqueurs laissés à l'agent
        write(os.path.join(wb, "a.txt"), "un\nALICE+BOB\ntrois\n")
        self.assertTrue(g.commit(wb, "résolution", "Bob"))
        self.assertEqual(g.integrate("task/bob/t1", "develop")["state"], "merged")
        self.assertEqual(sh(g.root, "show", "develop:a.txt"), "un\nALICE+BOB\ntrois\n")

    def test_abort_de_sync(self):
        g, wa, wb = self.prep()
        write(os.path.join(wa, "a.txt"), "A\n")
        g.commit(wa, "a", "Alice")
        write(os.path.join(wb, "a.txt"), "B\n")
        g.commit(wb, "b", "Bob")
        g.integrate("task/alice/t1", "develop")
        self.assertEqual(g.sync(wb, "develop")["state"], "conflict")
        g.abort(wb)
        self.assertEqual(g.status(wb), [])
        self.assertEqual(read(os.path.join(wb, "a.txt")), "B\n")

    def test_squash(self):
        g, wa, wb = self.prep()
        for i in (1, 2):
            write(os.path.join(wa, "f%d.txt" % i), "x\n")
            g.commit(wa, "étape %d" % i, "Alice")
        r = g.integrate("task/alice/t1", "develop", message="Tâche T1", squash=True)
        self.assertEqual(r["state"], "merged")
        self.assertEqual(len(sh(g.root, "rev-list", "--parents", "-1", "develop").split()), 2)   # un seul parent : commit unique
        self.assertEqual(sh(g.root, "log", "-1", "--format=%s", "develop").strip(), "Tâche T1")

    def test_cible_occupee_par_un_agent_ou_par_le_depot_racine(self):
        g, wa, wb = self.prep()
        g.worktree("carl", "develop")                                                     # un agent occupe la cible
        with self.assertRaises(Err) as c:
            g.integrate("task/alice/t1", "develop")
        self.assertIn("déjà utilisée", c.exception.msg)
        g.drop_worktree("carl")
        sh(g.root, "checkout", "-q", "develop")                                           # le dépôt racine occupe la cible : détaché automatiquement
        write(os.path.join(wa, "z.txt"), "z\n")
        g.commit(wa, "z", "Alice")
        self.assertEqual(g.integrate("task/alice/t1", "develop")["state"], "merged")
        self.assertEqual(sh(g.root, "rev-parse", "--abbrev-ref", "HEAD").strip(), "HEAD")


class Remote(Base):
    def test_push_integration_et_suppression_de_branche_distante(self):
        r = self.bare({"README.md": "# demo\n"})
        g = self.repo(remote=r)
        w = g.worktree("alice", "task/alice/t1")
        write(os.path.join(w, "n.txt"), "n\n")
        g.commit(w, "n", "Alice")
        g.push("task/alice/t1")
        self.assertIn("refs/heads/task/alice/t1", sh(self.tmp, "ls-remote", r))
        g.integrate("task/alice/t1", "develop")
        g.push("develop")
        self.assertEqual(gl.git(["rev-parse", "develop"], r)[1].strip(), g.sha("develop"))
        g.push("task/alice/t1", delete=True)
        self.assertNotIn("task/alice/t1", sh(self.tmp, "ls-remote", r))
        g.fetch()

    def test_push_refuse_si_non_fast_forward(self):
        r = self.bare({"README.md": "# demo\n"})
        g = self.repo(remote=r)
        g.push("develop")
        other = os.path.join(self.tmp, "other")
        sh(self.tmp, "clone", r, other)
        sh(other, "checkout", "develop")
        write(os.path.join(other, "o.txt"), "o\n")
        sh(other, "add", "-A")
        sh(other, "commit", "-m", "autre")
        sh(other, "push", "origin", "develop")
        w = g.worktree("alice", "task/alice/t1")
        write(os.path.join(w, "n.txt"), "n\n")
        g.commit(w, "n", "Alice")
        g.integrate("task/alice/t1", "develop")
        with self.assertRaises(Err):                                                       # le remote a avancé : push refusé, pas de force
            g.push("develop")
        g.fetch()
        self.assertEqual(g.ahead_behind("develop", "origin/develop"), {"ahead": 2, "behind": 1})

    def test_push_sans_remote_et_nom_invalide(self):
        g = self.seeded()
        with self.assertRaises(Err):
            g.push("develop")
        r = self.bare({"README.md": "x\n"})
        shutil.rmtree(self.root)
        os.makedirs(self.root)
        g = self.repo(remote=r)
        with self.assertRaises(Err):
            g.push("bad..name")

    def test_jeton_absent_de_la_configuration_du_depot(self):
        r = self.bare({"README.md": "x\n"})
        g = gl.LocalGit(self.root, remote=r, token="SECRET-TOKEN-123")
        g.init()
        g.fetch()
        conf = read(os.path.join(self.root, ".git", "config"))
        self.assertNotIn("SECRET-TOKEN-123", conf)
        self.assertNotIn("extraheader", conf)


class Reprise(Base):
    def test_recover_lock_orphelin_et_merge_interrompu(self):
        g = self.seeded()
        wa, wb = g.worktree("alice", "task/alice/t1"), g.worktree("bob", "task/bob/t1")
        write(os.path.join(wa, "a.txt"), "A\n")
        g.commit(wa, "a", "Alice")
        write(os.path.join(wb, "a.txt"), "B\n")
        g.commit(wb, "b", "Bob")
        g.integrate("task/alice/t1", "develop")
        self.assertEqual(g.sync(wb, "develop")["state"], "conflict")                      # merge laissé en cours (crash simulé)
        gd = sh(wa, "rev-parse", "--git-dir").strip()
        lock = os.path.join(gd, "index.lock")
        write(lock, "")
        old = os.path.getmtime(lock) - 3600
        os.utime(lock, (old, old))
        fresh = os.path.join(g.root, ".git", "index.lock")                                  # verrou récent : ne doit PAS être supprimé
        write(fresh, "")
        done = g.recover()
        self.assertFalse(os.path.exists(lock))
        self.assertTrue(any("index.lock" in d for d in done))
        self.assertTrue(any("merge interrompu" in d for d in done))
        self.assertEqual(g.status(wb), [])
        self.assertTrue(os.path.exists(fresh))
        os.remove(fresh)

    def test_recover_worktree_disparu(self):
        g = self.seeded()
        w = g.worktree("alice", "task/alice/t1")
        shutil.rmtree(w)
        g.recover()
        self.assertNotIn(os.path.realpath(w), g.worktrees())
        g.worktree("alice", "task/alice/t1")                                                 # recréable


if __name__ == "__main__":
    unittest.main()
