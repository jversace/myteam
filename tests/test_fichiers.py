"""Tests des gros fichiers : lecture par plage, edit_file, write_chunk, garde-fou anti-troncature, abrégé de l'historique, nouvelles tentatives réseau."""
import http.client
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("APM_DATA", tempfile.mkdtemp())

import ai  # noqa: E402
import engine  # noqa: E402
import store  # noqa: E402
from store import Err  # noqa: E402
from test_locks import make_state, proj  # noqa: E402

BIG = "".join("ligne %03d : %s\n" % (i, "x" * 40) for i in range(1, 501))      # ~ 25 000 caractères, 500 lignes


class Lecture(unittest.TestCase):
    def test_vue_par_defaut_et_suite(self):
        body, lab, nxt = engine.read_view(BIG, {})
        self.assertEqual(len(body), engine.READ_MAX)
        self.assertEqual(lab, "")
        self.assertIn("offset=%d" % engine.READ_MAX, nxt)

    def test_plage_de_lignes(self):
        body, lab, nxt = engine.read_view(BIG, {"from_line": 10, "to_line": 12})
        self.assertEqual(body.splitlines()[0][:9], "ligne 010")
        self.assertEqual(len(body.splitlines()), 3)
        self.assertIn("lignes 10 à 12 sur 500", lab)
        self.assertIn("from_line=13", nxt)

    def test_plage_trop_grande_est_coupee_avec_la_suite(self):
        body, lab, nxt = engine.read_view(BIG, {"from_line": 1})
        self.assertLessEqual(len(body), engine.READ_MAX)
        n = body.count("\n")                                   # lignes complètes ; la dernière, coupée, est relue en entier à la suite
        self.assertIn("from_line=%d" % (n + 1), nxt)

    def test_offset_limit(self):
        body, lab, nxt = engine.read_view(BIG, {"offset": 100, "limit": 50})
        self.assertEqual(body, BIG[100:150])
        self.assertIn("caractères 100 à 150", lab)
        self.assertIn("offset=150", nxt)

    def test_petit_fichier_entier_sans_remarque(self):
        self.assertEqual(engine.read_view("abc\n", {}), ("abc\n", "", ""))

    def test_historique_abrege_indique_comment_relire(self):
        h = [{"role": "user", "content": engine.OLD_RESULT + " : " + "z" * 3000}, {"role": "assistant", "content": "ok"},
             {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        self.assertIn("from_line", engine.shrink_history(h, budget=1000)[0]["content"])


class Edition(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "big.py").write_text(BIG, encoding="utf-8", newline="")
        (self.tmp / "win.txt").write_bytes(b"un\r\ndeux\r\ntrois\r\n")
        store.wj(store.state_path("u"), make_state(self.tmp))
        engine.CHUNK_BUF.clear()

    def act(self, who, kind, **x):
        return engine.do_act("u", "p1", who, {"do": kind, **x}, 0, {"n": 0, "t0": time.time()})

    def read(self, name):
        return (self.tmp / name).read_bytes().decode("utf-8")

    def test_lecture_par_plage_dans_le_repertoire_local(self):
        note, back = self.act("a2", "read_file", path="big.py", from_line=498, to_line=500)
        self.assertIn("ligne 498", back)
        self.assertNotIn("ligne 497", back)
        self.assertIn("lignes 498 à 500 sur 500", back)

    def test_edit_file_modifie_sans_toucher_le_reste(self):
        note, back = self.act("a2", "edit_file", path="big.py", edits=[{"old": "ligne 250 : ", "new": "ligne 250 (modifiée) : "}], message="m")
        self.assertIsNone(back)
        self.assertIn("1 modification", note)
        new = self.read("big.py")
        self.assertEqual(len(new), len(BIG) + len(" (modifiée)"))
        self.assertEqual(new.replace(" (modifiée)", ""), BIG)

    def test_edit_file_insertion_apres_une_ancre(self):
        self.act("a2", "edit_file", path="big.py", edits=[{"old": "ligne 003 : " + "x" * 40 + "\n", "new": "ligne 003 : " + "x" * 40 + "\nAJOUT\n"}])
        self.assertIn("ligne 003 : " + "x" * 40 + "\nAJOUT\nligne 004", self.read("big.py"))

    def test_edit_file_texte_introuvable_ou_ambigu_ne_modifie_rien(self):
        for old, mot in (("n'existe pas", "introuvable"), ("x" * 40, "apparaît")):
            note, back = self.act("a2", "edit_file", path="big.py", edits=[{"old": "ligne 001", "new": "Z"}, {"old": old, "new": "Y"}])
            self.assertIn(mot, back)
            self.assertEqual(self.read("big.py"), BIG)                           # tout ou rien

    def test_edit_file_all_et_fichier_inconnu(self):
        self.act("a2", "edit_file", path="big.py", edits=[{"old": "ligne", "new": "L", "all": True}])
        self.assertNotIn("ligne", self.read("big.py"))
        note, back = self.act("a2", "edit_file", path="absent.py", edits=[{"old": "a", "new": "b"}])
        self.assertIn("Édition refusée", back)

    def test_edit_file_conserve_les_fins_de_ligne_windows(self):
        self.act("a2", "edit_file", path="win.txt", edits=[{"old": "deux\ntrois", "new": "2\n3"}])
        self.assertEqual(self.read("win.txt"), "un\r\n2\r\n3\r\n")

    def test_edit_file_arguments_invalides(self):
        for edits in ([], "x", [{"new": "a"}], [{"old": ""}]):
            self.assertIn("Édition refusée", self.act("a2", "edit_file", path="big.py", edits=edits)[1])

    def test_edit_file_respecte_les_verrous(self):
        import locks
        ok, _ = locks.acquire("u", "p1", "a3", ["big.py"])
        self.assertTrue(ok)
        with mock.patch.object(locks, "WAIT", 0.2), mock.patch.object(locks, "POLL", 0.05):
            note, back = self.act("a2", "edit_file", path="big.py", edits=[{"old": "ligne 001", "new": "Z"}])
        self.assertIn("refusée", back)
        self.assertEqual(self.read("big.py"), BIG)

    def test_garde_fou_ecriture_tronquee(self):
        note, back = self.act("a2", "write_file", path="big.py", content=BIG[:3000], message="m")
        self.assertIn("semble tronqué", back)
        self.assertEqual(self.read("big.py"), BIG)
        self.act("a2", "write_file", path="big.py", content=BIG[:3000], message="m", confirm_shrink=True)
        self.assertEqual(self.read("big.py"), BIG[:3000])

    def test_garde_fou_ne_gene_ni_creation_ni_petit_fichier_ni_agrandissement(self):
        self.act("a2", "write_file", path="neuf.txt", content="a")
        self.act("a2", "write_file", path="win.txt", content="x")                    # petit fichier existant
        self.act("a2", "write_file", path="big.py", content=BIG + "fin\n")           # plus long
        self.assertEqual(self.read("neuf.txt"), "a")
        self.assertEqual(self.read("win.txt"), "x")
        self.assertTrue(self.read("big.py").endswith("fin\n"))

    def test_drapeau_interne_raw_inaccessible_a_l_agent(self):
        note, back = self.act("a2", "read_file", path="big.py", _raw=True)
        self.assertLess(len(back), len(BIG))

    def test_write_chunk_reconstitue_dans_le_desordre(self):
        parts = [BIG[i:i + 9000] for i in range(0, len(BIG), 9000)]
        n = len(parts)
        sha = __import__("hashlib").sha256(BIG.encode()).hexdigest()
        for i in (2, 1):
            note, back = self.act("a2", "write_chunk", path="copie.py", index=i, total=n, content=parts[i - 1], message="gros fichier")
            self.assertIn("Manquants", back)
            self.assertFalse((self.tmp / "copie.py").exists())
        note, back = self.act("a2", "write_chunk", path="copie.py", index=3, total=n, content=parts[2], sha256=sha)
        self.assertEqual(self.read("copie.py"), BIG)
        self.assertIn(sha, back)
        self.assertTrue(any(c["m"] == "gros fichier" for c in proj()["commits"]))
        self.assertEqual(engine.CHUNK_BUF, {})

    def test_write_chunk_empreinte_fausse_n_ecrit_rien(self):
        note, back = self.act("a2", "write_chunk", path="c.py", index=1, total=1, content="abc", sha256="0" * 64)
        self.assertIn("empreinte", back)
        self.assertFalse((self.tmp / "c.py").exists())

    def test_write_chunk_parametres_invalides_et_expiration(self):
        for kw in ({"index": "a", "total": 2}, {"index": 3, "total": 2}, {"index": 1, "total": 999}):
            self.assertIn("Morceau refusé", self.act("a2", "write_chunk", path="c.py", content="x", **kw)[1])
        self.act("a2", "write_chunk", path="c.py", index=1, total=2, content="x")
        self.assertEqual(len(engine.CHUNK_BUF), 1)
        for v in engine.CHUNK_BUF.values():
            v["ts"] -= engine.CHUNK_TTL + 1
        self.act("a2", "write_chunk", path="d.py", index=1, total=2, content="y")
        self.assertEqual(len(engine.CHUNK_BUF), 1)                                  # le lot orphelin est purgé

    def test_edit_file_tient_le_verrou_pendant_toute_l_operation(self):
        vus = []
        vrai = engine.raw_read

        def espion(u, pid, p, a, path, t0):
            vus.append([(l["by"], l["path"]) for l in proj().get("locks", [])])
            return vrai(u, pid, p, a, path, t0)
        with mock.patch.object(engine, "raw_read", espion):
            note, back = self.act("a2", "edit_file", path="big.py", edits=[{"old": "ligne 001", "new": "Z"}])
        self.assertIsNone(back)
        self.assertEqual(vus, [[("a2", "big.py")]])                              # verrou déjà pris au moment de la lecture
        self.assertEqual(proj()["locks"], [])                                    # rendu ensuite
        self.assertNotIn("verrou conservé", note)

    def test_edit_file_rend_le_verrou_meme_en_cas_de_refus(self):
        self.act("a2", "edit_file", path="big.py", edits=[{"old": "introuvable", "new": "Z"}])
        self.assertEqual(proj()["locks"], [])

    def test_edit_file_conserve_un_verrou_deja_tenu(self):
        import locks
        ok, _ = locks.acquire("u", "p1", "a2", ["big.py"])
        note, back = self.act("a2", "edit_file", path="big.py", edits=[{"old": "ligne 001", "new": "Z"}])
        self.assertIn("verrou conservé", note)
        self.assertEqual([l["by"] for l in proj()["locks"]], ["a2"])

    def test_garde_fou_aussi_sous_verrou(self):
        vus = []
        vrai = engine.raw_read

        def espion(u, pid, p, a, path, t0):
            vus.append(len(proj().get("locks", [])))
            return vrai(u, pid, p, a, path, t0)
        with mock.patch.object(engine, "raw_read", espion):
            self.act("a2", "write_file", path="big.py", content=BIG[:3000])
        self.assertEqual(vus, [1])
        self.assertEqual(proj()["locks"], [])

    def test_write_chunk_agents_separes(self):
        self.act("a2", "write_chunk", path="c.py", index=1, total=2, content="A")
        self.act("a3", "write_chunk", path="c.py", index=2, total=2, content="B")
        self.assertFalse((self.tmp / "c.py").exists())                               # les morceaux de deux agents ne se mélangent pas

    def test_prompt_annonce_les_nouvelles_actions(self):
        for k in ("edit_file", "write_chunk", "from_line"):
            self.assertIn(k, engine.ACTIONS)


def _res(*fichiers, taille=12000):
    return engine.OLD_RESULT + " :\n\n" + "\n\n".join("Fichier %s (répertoire local) :\n```\n%s\n```" % (f, "z" * taille) for f in fichiers)


class HistoriqueBudget(unittest.TestCase):
    """Cause de la boucle de relecture : seuls 2 messages restaient intacts, donc lire 3 fichiers en abrégeait un, que l'agent relisait, etc."""

    def msgs(self, *res):
        h = []
        for r in res:
            h += [{"role": "user", "content": r}, {"role": "assistant", "content": "ok"}]
        return h

    def test_trois_fichiers_lus_restent_tous_lisibles(self):
        h = self.msgs(_res("app/db.py"), _res("app/main.py", taille=10000), _res("app/home.html", taille=3000), "[Vous] vas-y")
        r = engine.shrink_history(h)
        for i in (0, 2, 4):
            self.assertEqual(r[i]["content"], h[i]["content"])                      # db.py, main.py et home.html intacts (ancien comportement : db.py abrégé)

    def test_budget_depasse_abrege_les_plus_anciens(self):
        h = self.msgs(*[_res("f%d.py" % i) for i in range(6)])                       # 6 × 12 000 = 72 000 > budget
        r = engine.shrink_history(h)
        self.assertIn("abrégé", r[0]["content"])
        self.assertEqual(r[10]["content"], h[10]["content"])                         # les plus récents intacts
        self.assertLessEqual(sum(len(m["content"]) for m in r if m["role"] == "user" and m["content"].startswith(engine.OLD_RESULT) and "abrégé" not in m["content"]), engine.HIST_BUDGET + 12100)

    def test_fichier_relu_les_anciennes_copies_sont_abregees(self):
        h = self.msgs(*[_res("app/db.py", taille=6000) for _ in range(5)], "[Vous] x")
        r = engine.shrink_history(h)
        self.assertEqual([("abrégé" in r[i]["content"]) for i in (0, 2, 4, 6, 8)], [True, True, True, True, False])   # seule la copie la plus récente reste intacte

    def test_original_non_modifie(self):
        h = self.msgs(*[_res("f%d.py" % i) for i in range(6)])
        avant = [m["content"] for m in h]
        engine.shrink_history(h)
        self.assertEqual(avant, [m["content"] for m in h])


class Relectures(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "db.py").write_text(BIG, encoding="utf-8", newline="")
        store.wj(store.state_path("u"), make_state(self.tmp))
        engine.READS.clear()

    def act(self, who, kind, **x):
        return engine.do_act("u", "p1", who, {"do": kind, **x}, 0, {"n": 0, "t0": time.time()})

    def test_avertissement_des_la_troisieme_relecture(self):
        backs = [self.act("a2", "read_file", path="db.py")[1] for _ in range(4)]
        self.assertNotIn("⚠", backs[0] + backs[1])
        self.assertIn("lecture n°3", backs[2])
        self.assertIn("edit_file", backs[2])
        self.assertIn("lecture n°4", backs[3])

    def test_extraits_differents_ne_comptent_pas_ensemble(self):
        for i in range(5):
            back = self.act("a2", "read_file", path="db.py", from_line=1 + 10 * i, to_line=10 + 10 * i)[1]
            self.assertNotIn("⚠", back)

    def test_compteur_par_agent_et_remis_a_zero_apres_ecriture(self):
        for _ in range(2):
            self.act("a2", "read_file", path="db.py")
        self.assertNotIn("⚠", self.act("a3", "read_file", path="db.py")[1])           # autre agent : compteur à lui
        self.act("a2", "edit_file", path="db.py", edits=[{"old": "ligne 001", "new": "Z"}])
        self.assertNotIn("⚠", self.act("a2", "read_file", path="db.py")[1])           # écriture réussie : on repart de zéro

    def test_fichier_introuvable_pas_d_avertissement_parasite(self):
        for _ in range(4):
            note, back = self.act("a2", "read_file", path="absent.py")
        self.assertNotIn("⚠", back)


class Reseau(unittest.TestCase):
    svc = {"apiKey": "k", "url": "http://x", "model": "m", "kind": "openai", "name": "S"}
    OK = {"choices": [{"message": {"content": "salut"}}], "usage": {}}

    def test_reprise_apres_connexion_fermee(self):
        calls = []

        def fake(req, timeout=0):
            calls.append(1)
            if len(calls) < 3:
                raise http.client.RemoteDisconnected("Remote end closed connection without response")
            import io, json
            return io.BytesIO(json.dumps(self.OK).encode())
        with mock.patch.object(ai.urllib.request, "urlopen", fake), mock.patch.object(ai.time, "sleep"):
            self.assertEqual(ai.call_ai(self.svc, "s", [{"role": "user", "content": "a"}]), "salut")
        self.assertEqual(len(calls), 3)

    def test_abandon_propre_apres_les_tentatives(self):
        def fake(req, timeout=0):
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        with mock.patch.object(ai.urllib.request, "urlopen", fake), mock.patch.object(ai.time, "sleep"):
            with self.assertRaises(Err) as c:
                ai.call_ai(self.svc, "s", [{"role": "user", "content": "a"}])
        self.assertEqual(c.exception.code, 502)
        self.assertIn("RemoteDisconnected", c.exception.msg)


if __name__ == "__main__":
    unittest.main()
