"""Tests du répertoire local et des verrous de fichiers (aucun réseau, IA scriptée)."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("APM_DATA", tempfile.mkdtemp())

import engine  # noqa: E402
import locks  # noqa: E402
import store  # noqa: E402


def make_state(localdir):
    ag = lambda i, n, **k: {**dict(id=i, name=n, service="s1", role="r1", branch="agent/" + n.lower(), canBug=False, talk=["a1", "a2", "a3"], history=[], notes="", source="local"), **k}
    return {"services": [{"id": "s1", "name": "S", "kind": "openai", "url": "x", "model": "m", "apiKey": "k"}],
            "projects": [{"id": "p1", "name": "Demo", "rules": "", "spec": "", "mainId": "a1", "localdir": str(localdir),
                          "repo": {"url": "", "branch": "main", "token": ""},
                          "roles": [{"id": "r1", "name": "Chef de projet", "desc": "Coordonne"}, {"id": "r2", "name": "Développeur back-end", "desc": "Développe"}],
                          "agents": [ag("a1", "Alice"), ag("a2", "Bob", role="r2"), ag("a3", "Carl", role="r2")],
                          "tasks": [], "bugs": [], "modules": [], "log": [], "commits": [], "releases": [], "mrs": []}]}


def proj():
    return store.load("u")["projects"][0]


class Verrous(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "a.txt").write_text("original", encoding="utf-8")
        store.wj(store.state_path("u"), make_state(self.tmp))
        locks.STOP.clear()
        self.w = mock.patch.object(locks, "WAIT", 0.3)
        self.poll = mock.patch.object(locks, "POLL", 0.05)
        self.w.start(); self.poll.start()

    def tearDown(self):
        self.w.stop(); self.poll.stop()

    def act(self, who, kind, **x):
        p = proj()
        return engine.do_act("u", "p1", who, {"do": kind, **x}, 0, {"n": 0, "t0": time.time()})

    # --- répertoire local
    def test_ecriture_et_lecture_locale_sans_github(self):
        self.act("a2", "write_file", path="src/x.py", content="print(1)", message="m")
        self.assertEqual((self.tmp / "src" / "x.py").read_text(encoding="utf-8"), "print(1)")
        self.assertEqual(proj()["locks"], [])                                    # verrou éphémère rendu après l'écriture
        r = self.act("a3", "read_file", path="src/x.py")
        self.assertIn("print(1)", str(r))
        self.assertTrue(any(c["m"] == "m" for c in proj()["commits"]))

    def test_chemins_hors_repertoire_et_zones_interdites(self):
        out = self.tmp.parent / "dehors"
        out.mkdir(exist_ok=True)
        (self.tmp / "lien").symlink_to(out)
        p = proj()
        with self.assertRaises(Exception):
            locks.resolve(p, "lien/x.txt", write=True)
        for bad in ("../x", ".git/config", ".github/x.yml", ".agents/Bob.md"):
            with self.assertRaises(Exception):
                engine.safe_path(bad, write=True)

    # --- exclusion mutuelle
    def test_un_agent_attend_la_liberation_puis_ecrit(self):
        ok, _ = locks.acquire("u", "p1", "a2", ["a.txt"])
        self.assertTrue(ok)
        note, back = engine.local_act("u", "p1", proj(), proj()["agents"][2], {"path": "a.txt", "content": "Carl"}, "write_file", time.time())
        self.assertIn("refusée", note)                                           # attente épuisée : refus explicite
        self.assertEqual((self.tmp / "a.txt").read_text(encoding="utf-8"), "original")
        self.assertEqual(len(proj().get("lock_wait", [])), 0)                    # plus personne en attente

        res = {}
        th = threading.Thread(target=lambda: res.update(r=locks.acquire("u", "p1", "a3", ["a.txt"], wait=3)))
        th.start()
        time.sleep(0.3)
        self.assertEqual([w["name"] for w in proj()["lock_wait"]], ["Carl"])
        locks.release("u", "p1", "a2")
        th.join(5)
        self.assertTrue(res["r"][0])
        self.assertEqual([l["name"] for l in proj()["locks"]], ["Carl"])

    def test_tout_ou_rien(self):
        locks.acquire("u", "p1", "a2", ["b.txt"])
        ok, msg = locks.acquire("u", "p1", "a3", ["a.txt", "b.txt"], wait=0.2)
        self.assertFalse(ok)
        self.assertEqual([l["path"] for l in proj()["locks"]], ["b.txt"])        # a.txt n'a pas été pris à moitié

    def test_file_d_attente_equitable(self):
        locks.acquire("u", "p1", "a1", ["a.txt"])
        order = []
        def take(aid):
            ok, _ = locks.acquire("u", "p1", aid, ["a.txt"], wait=5)
            order.append(aid)
            time.sleep(0.2)
            locks.release("u", "p1", aid)
        t2 = threading.Thread(target=take, args=("a2",)); t2.start(); time.sleep(0.3)
        t3 = threading.Thread(target=take, args=("a3",)); t3.start(); time.sleep(0.3)
        locks.release("u", "p1", "a1")
        t2.join(8); t3.join(8)
        self.assertEqual(order, ["a2", "a3"])

    # --- secours
    def test_verrou_circulaire_refuse_immediatement(self):
        locks.acquire("u", "p1", "a2", ["a.txt"])
        locks.acquire("u", "p1", "a3", ["b.txt"])
        th = threading.Thread(target=lambda: locks.acquire("u", "p1", "a2", ["b.txt"], wait=3)); th.start()
        time.sleep(0.3)
        t0 = time.time()
        ok, msg = locks.acquire("u", "p1", "a3", ["a.txt"], wait=30)
        self.assertFalse(ok)
        self.assertLess(time.time() - t0, 2)                                     # pas d'attente : détecté tout de suite
        self.assertIn("circulaire", msg)
        locks.release("u", "p1", "a3")                                           # l'agent refusé libère : l'autre passe
        th.join(5)
        self.assertEqual([l["name"] for l in proj()["locks"] if l["path"] == "b.txt"], ["Bob"])

    def test_expiration_par_inactivite(self):
        locks.acquire("u", "p1", "a2", ["a.txt"])
        st = store.load("u"); st["projects"][0]["locks"][0]["touched"] -= locks.TTL + 5; store.wj(store.state_path("u"), st)
        notes = locks.maintain("u", "p1", {"a2"})
        self.assertEqual(proj()["locks"], [])
        self.assertIn("expiré", notes[0])

    def test_verrou_orphelin_libere_mais_pas_si_agent_actif(self):
        locks.acquire("u", "p1", "a2", ["a.txt"])
        st = store.load("u"); st["projects"][0]["locks"][0]["touched"] -= locks.ORPHAN + 5; store.wj(store.state_path("u"), st)
        self.assertEqual(locks.maintain("u", "p1", {"a2"}), [])                  # Bob travaille encore : on garde
        self.assertEqual(len(locks.maintain("u", "p1", set())), 1)               # plus de travail : libéré
        self.assertEqual(proj()["locks"], [])

    def test_redemarrage_libere_tout(self):
        locks.acquire("u", "p1", "a2", ["a.txt"])
        engine.recover()
        self.assertEqual(proj()["locks"], [])
        self.assertTrue(any("Redémarrage" in m["text"] for m in proj()["log"]))

    def test_fin_de_tache_libere(self):
        st = store.load("u"); st["projects"][0]["tasks"].append({"id": "t1", "title": "x", "desc": "", "who": "a2", "status": "à faire", "modules": [], "parent": ""}); store.wj(store.state_path("u"), st)
        def ai(s, system, msgs):
            return 'ok\n```action\n{"do":"lock_file","paths":["a.txt"]}\n```' if "Résultats" not in msgs[-1]["content"] else "fini"
        with mock.patch.object(engine, "call_ai", ai), mock.patch.object(engine, "spawn", lambda *a, **k: None):
            engine.run_task("u", "p1", "t1")
        self.assertEqual(proj()["locks"], [])                                    # le verrou ne survit pas à la tâche

    # --- agent principal / utilisateur
    def test_stop_agent_libere_et_interrompt(self):
        locks.acquire("u", "p1", "a2", ["a.txt"])
        st = store.load("u"); st["projects"][0]["tasks"].append({"id": "t1", "title": "x", "desc": "", "who": "a2", "status": "en cours", "running": True, "modules": [], "parent": ""}); store.wj(store.state_path("u"), st)
        r = self.act("a1", "stop_agent", agent="Bob", reason="bloqué")
        self.assertIn("arrêté", str(r))
        self.assertEqual(proj()["locks"], [])
        t = proj()["tasks"][0]
        self.assertEqual((t["status"], t["running"], t["stopped"]["by"]), ("en attente", False, "Alice"))
        with self.assertRaises(locks.Stopped):                                   # un ask démarré avant l'arrêt s'interrompt
            locks.check_stop("u", "p1", "a2", time.time() - 10)
        locks.check_stop("u", "p1", "a2", time.time() + 1)                       # un travail démarré après n'est pas concerné

    def test_cancel_restaure_les_fichiers(self):
        ok, _ = locks.acquire("u", "p1", "a2", ["a.txt", "neuf.txt"])
        (self.tmp / "a.txt").write_text("abîmé", encoding="utf-8")
        (self.tmp / "neuf.txt").write_text("créé par Bob", encoding="utf-8")
        self.act("a1", "stop_agent", agent="Bob", cancel=True, reason="erreur")
        self.assertEqual((self.tmp / "a.txt").read_text(encoding="utf-8"), "original")
        self.assertFalse((self.tmp / "neuf.txt").exists())
        self.assertEqual(proj()["locks"], [])

    def test_droits_des_actions_de_secours(self):
        r = self.act("a2", "stop_agent", agent="Carl")
        self.assertIn("réservée", str(r))
        r = self.act("a2", "force_unlock", path="a.txt")
        self.assertIn("réservée", str(r))

    def test_force_unlock_refuse_si_detenteur_actif(self):
        locks.acquire("u", "p1", "a2", ["a.txt"])
        st = store.load("u"); st["projects"][0]["agents"][1]["work"] = 1; store.wj(store.state_path("u"), st)
        self.assertIn("stop_agent", str(self.act("a1", "force_unlock", path="a.txt")))
        st = store.load("u"); st["projects"][0]["agents"][1]["work"] = 0; store.wj(store.state_path("u"), st)
        self.act("a1", "force_unlock", path="a.txt", reason="plantage")
        self.assertEqual(proj()["locks"], [])

    def test_ecriture_apres_liberation_forcee_refusee(self):
        ok, _ = locks.acquire("u", "p1", "a2", ["a.txt"])
        l = proj()["locks"][0]
        locks.force_release("u", "p1", l["id"])
        locks.acquire("u", "p1", "a3", ["a.txt"])
        note, _ = engine.local_act("u", "p1", proj(), proj()["agents"][1], {"path": "a.txt", "content": "Bob"}, "write_file", time.time())
        self.assertIn("refusée", note)
        self.assertEqual((self.tmp / "a.txt").read_text(encoding="utf-8"), "original")

    def test_diagnostic_et_jobs(self):
        locks.acquire("u", "p1", "a2", ["a.txt"])
        self.assertTrue(any("a.txt" in i["text"] for i in engine.diagnose(proj())))
        lid = proj()["locks"][0]["id"]
        engine.lock_release("u", "p1", lid)
        self.assertEqual(proj()["locks"], [])
        self.assertIn("accessible", (engine.localdir_check("u", "p1"), proj()["log"][-1]["text"])[1])

    def test_agent_github_ignore_les_verrous(self):
        st = store.load("u"); st["projects"][0]["agents"][1]["source"] = "github"; store.wj(store.state_path("u"), st)
        r = self.act("a2", "lock_file", paths=["a.txt"])
        self.assertIn("GitHub", str(r))


if __name__ == "__main__":
    unittest.main()
