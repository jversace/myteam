"""Économie de tokens : plan et lecture ciblée du code (symbols.py), relecture différentielle, actions tout-en-un (aucun réseau, aucune clé)."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("APM_DATA", tempfile.mkdtemp())

import engine  # noqa: E402
import locks  # noqa: E402
import store  # noqa: E402
import symbols  # noqa: E402
from test_locks import make_state, proj  # noqa: E402

PY = '''"""doc"""
import os

LIMITE = 5
petit = 1


def libre(a, b=2):
    return a + b


@decorateur
class Moteur(Base):
    def run(self):
        def interne():
            pass
        return 1

    async def stop(self):
        return 2


class Autre:
    def run(self):
        return 3
'''

JS = '''const API = "x";
function charger(id) {
  const o = {a: 1};
  return o;
}
const f = (x) => x * 2;
class Vue {
  rendre(n) {
    return "}" + n;
  }
}
'''

HTML = '''<!doctype html>
<html>
<style>
body { color: red; }
</style>
<body>
<script>
function boot() {
  return 1;
}
const SUBJ = [1];
</script>
</body>
</html>
'''

MD = '''# Titre
intro
## Partie A
texte
```
# pas un titre
```
## Partie B
fin
'''

GO = '''package main

type Serveur struct {
	a int
}

func (s *Serveur) Demarrer() error {
	return nil
}

func main() {
}
'''


class Analyse(unittest.TestCase):
    def test_python_classes_methodes_constantes(self):
        it = symbols.outline("m.py", PY)
        q = {i["qual"]: (i["kind"], i["start"], i["end"]) for i in it}
        self.assertEqual(q["libre"], ("def", 8, 9))
        self.assertEqual(q["Moteur"][0], "class")
        self.assertEqual(q["Moteur"][1], 12)                           # la ligne du décorateur est incluse
        self.assertEqual(q["Moteur.run"][0], "method")
        self.assertIn("Autre.run", q)
        self.assertIn("LIMITE", q)
        self.assertNotIn("petit", q)                                   # minuscules : pas une constante
        self.assertNotIn("Moteur.run.interne", q)                      # fonctions imbriquées non listées

    def test_recherche_par_nom(self):
        it = symbols.outline("m.py", PY)
        self.assertEqual(len(symbols.find(it, "run")), 2)              # ambigu : deux méthodes « run »
        self.assertEqual([i["qual"] for i in symbols.find(it, "Autre.run")], ["Autre.run"])
        self.assertEqual([i["qual"] for i in symbols.find(it, "MOTEUR")], ["Moteur"])
        self.assertEqual(symbols.find(it, "inexistant"), [])
        self.assertIn("libre", symbols.close_names(it, "libr"))

    def test_python_syntaxe_invalide_donne_none(self):
        self.assertIsNone(symbols.outline("x.py", "def (:\n"))
        self.assertIsNone(symbols.outline("x.bin", "abc"))

    def test_javascript_accolades_et_chaines(self):
        it = symbols.outline("a.js", JS)
        q = {i["qual"]: (i["kind"], i["start"], i["end"]) for i in it}
        self.assertEqual(q["charger"], ("function", 2, 5))
        self.assertEqual(q["f"], ("function", 6, 6))
        self.assertEqual(q["Vue"], ("class", 7, 11))                   # l'accolade fermante dans la chaîne "}" ne ferme rien
        self.assertEqual(q["Vue.rendre"][0], "method")
        self.assertEqual(q["API"][0], "const")

    def test_html_script_style_et_fonctions_avec_decalage(self):
        it = symbols.outline("i.html", HTML)
        q = {i["qual"]: (i["kind"], i["start"], i["end"]) for i in it}
        self.assertEqual(q["style"][0], "bloc")
        self.assertEqual(q["script.boot"][1:], (8, 10))                 # numéros de ligne du FICHIER, pas du script
        self.assertIn("script.SUBJ", q)

    def test_markdown_sections_hors_blocs_de_code(self):
        it = symbols.outline("d.md", MD)
        q = {i["qual"]: (i["start"], i["end"]) for i in it}
        self.assertEqual(q["Titre"], (1, 9))
        self.assertEqual(q["Titre.Partie A"], (3, 7))
        self.assertEqual(q["Titre.Partie B"], (8, 9))
        self.assertNotIn("pas un titre", " ".join(q))

    def test_go(self):
        q = {i["qual"]: i["kind"] for i in symbols.outline("m.go", GO)}
        self.assertEqual(q["Serveur"], "struct")
        self.assertEqual(q["Serveur.Demarrer"], "method")
        self.assertEqual(q["main"], "func")

    def test_format_et_plan(self):
        it = symbols.outline("m.py", PY)
        txt = symbols.format_outline(it, 27)
        self.assertIn("L8-9  def libre(a, b=2):", txt)
        self.assertIn("  L14-17  def run(self):", txt)                 # méthodes indentées sous leur classe
        m = symbols.repo_map([("src/m.py", it, 27)], others=["README", "x.png"])
        self.assertIn("src/m.py (27 l.) :", m)
        self.assertIn("Moteur{2}", m)
        self.assertIn("Autres fichiers : README, x.png", m)

    def test_plan_borne_par_budget(self):
        it = symbols.outline("m.py", PY)
        m = symbols.repo_map([("f%03d.py" % i, it, 27) for i in range(200)], budget=1500)
        self.assertLess(len(m), 2200)
        self.assertIn("fichier(s) de plus", m)

    def test_diff_compact(self):
        self.assertEqual(symbols.compact_diff("a\nb\n", "a\nb\n"), "")
        d = symbols.compact_diff("a\nb\nc\n", "a\nB\nc\n")
        self.assertIn("-b", d)
        self.assertIn("+B", d)

    def test_le_depot_lui_meme_est_analysable(self):
        root = Path(__file__).resolve().parent.parent
        for f in ("engine.py", "static/index.html", "CLAUDE.md"):
            self.assertTrue(symbols.outline(f, (root / f).read_text(encoding="utf-8")), f)


class Actions(unittest.TestCase):
    """Actions de l'agent en répertoire local (agent Bob = a2)."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "m.py").write_text(PY, encoding="utf-8")
        (self.tmp / "src" / "a.js").write_text(JS, encoding="utf-8")
        (self.tmp / "README.md").write_text(MD, encoding="utf-8")
        (self.tmp / "node_modules").mkdir()
        (self.tmp / "node_modules" / "x.js").write_text("function inutile() {}\n", encoding="utf-8")
        self.st = make_state(self.tmp)
        store.wj(store.state_path("u"), self.st)
        engine.SYM_CACHE.clear()
        engine.SYM_MAPS.clear()

    def act(self, who, kind, **x):
        return engine.do_act("u", "p1", who, {"do": kind, **x}, 0, {"n": 0, "t0": time.time()})

    def remember(self, agent, back):
        """Simule ce que fait `ask` : le retour d'actions entre dans l'historique de l'agent."""
        ag = next(a for a in self.st["projects"][0]["agents"] if a["id"] == agent)
        ag["history"] += [{"role": "user", "content": engine.OLD_RESULT + " :\n\n" + back}, {"role": "assistant", "content": "ok"}]
        store.wj(store.state_path("u"), self.st)

    # --- plan et lecture ciblée
    def test_plan_du_depot(self):
        note, back = self.act("a2", "outline", path="")
        self.assertIn("src/m.py", back)
        self.assertIn("Moteur{2}", back)
        self.assertIn("src/a.js", back)
        self.assertNotIn("inutile", back)                               # node_modules ignoré
        self.assertIn("README.md", back)

    def test_plan_d_un_fichier(self):
        note, back = self.act("a2", "outline", path="src/m.py")
        self.assertIn("L8-9  def libre(a, b=2):", back)
        self.assertIn("read_symbol", back)

    def test_read_symbol_renvoie_seulement_la_fonction_avec_ses_lignes(self):
        note, back = self.act("a2", "read_symbol", path="src/m.py", symbol="libre")
        self.assertIn("return a + b", back)
        self.assertNotIn("class Moteur", back)
        self.assertIn("lignes 8 à 9 sur", back)
        self.assertIn("libre", note)

    def test_read_symbol_ambigu_inconnu_et_contexte(self):
        note, back = self.act("a2", "read_symbol", path="src/m.py", symbol="run")
        self.assertIn("Moteur.run", back)
        self.assertIn("Autre.run", back)
        self.assertNotIn("return 1", back)                              # rien n'est lu tant que l'agent n'a pas précisé
        note, back = self.act("a2", "read_symbol", path="src/m.py", symbol="libr")
        self.assertIn("introuvable", back)
        self.assertIn("libre", back)                                    # nom proche suggéré
        note, back = self.act("a2", "read_symbol", path="src/m.py", symbol="Autre.run", context=2)
        self.assertIn("class Autre", back)

    def test_read_symbol_section_markdown_et_erreurs(self):
        note, back = self.act("a2", "read_symbol", path="README.md", symbol="Partie B")
        self.assertIn("fin", back)
        self.assertIn("introuvable", self.act("a2", "read_symbol", path="absent.py", symbol="x")[1])
        (self.tmp / "casse.py").write_text("def (:\n", encoding="utf-8")
        self.assertIn("read_file", self.act("a2", "read_symbol", path="casse.py", symbol="x")[1])

    def test_find_symbol(self):
        note, back = self.act("a2", "find_symbol", name="libre")
        self.assertEqual(back.strip(), "src/m.py:L8-9  def libre(a, b=2):")
        note, back = self.act("a2", "find_symbol", name="run")
        self.assertEqual(back.count("src/m.py:"), 2)
        note, back = self.act("a2", "find_symbol", name="Moteu")          # début de nom : repli sur « contient »
        self.assertIn("class Moteur", back)
        self.assertIn("Aucun symbole", self.act("a2", "find_symbol", name="zzz")[1])
        self.assertNotIn("inutile", self.act("a2", "find_symbol", name="inutile")[1].replace("Aucun symbole « inutile »", ""))

    def test_chemins_dangereux_refuses(self):
        for kw in ({"path": "../x"}, {"path": "a b/../../c"}):
            with self.assertRaises(engine.Err):
                engine.sym_act("u", "p1", proj(), proj()["agents"][1], kw, "outline")
        self.assertIn("Chemin invalide", self.act("a2", "outline", path="../x")[1])

    # --- relecture différentielle
    def test_relecture_identique_est_inchangee_puis_full(self):
        note, r1 = self.act("a2", "read_file", path="src/m.py", from_line=8, to_line=9)
        self.assertIn("return a + b", r1)
        self.remember("a2", r1)
        note, r2 = self.act("a2", "read_file", path="src/m.py", from_line=8, to_line=9)
        self.assertIn("INCHANGÉ", r2)
        self.assertNotIn("return a + b", r2)
        self.assertLess(len(r2), len(r1) + 150)
        note, r3 = self.act("a2", "read_file", path="src/m.py", from_line=8, to_line=9, full=True)
        self.assertIn("return a + b", r3)
        self.assertNotIn("INCHANGÉ", r3)

    def test_autre_zone_ou_autre_agent_donne_le_contenu_complet(self):
        note, r1 = self.act("a2", "read_file", path="src/m.py", from_line=8, to_line=9)
        self.remember("a2", r1)
        self.assertIn("return 1", self.act("a2", "read_file", path="src/m.py", from_line=14, to_line=17)[1])      # autre zone
        self.assertIn("return a + b", self.act("a3", "read_file", path="src/m.py", from_line=8, to_line=9)[1])  # Carl n'a rien lu

    def test_fichier_modifie_donne_seulement_le_diff(self):
        big = "".join("ligne %03d : %s\n" % (i, "x" * 30) for i in range(1, 61))
        (self.tmp / "g.txt").write_text(big, encoding="utf-8", newline="")
        note, r1 = self.act("a2", "read_file", path="g.txt")
        self.remember("a2", r1)
        (self.tmp / "g.txt").write_text(big.replace("ligne 030", "LIGNE TRENTE"), encoding="utf-8", newline="")
        note, r2 = self.act("a2", "read_file", path="g.txt")
        self.assertIn("MODIFIÉ", r2)
        self.assertIn("+LIGNE TRENTE", r2)
        self.assertNotIn("ligne 031", r2.split("@@")[0])               # pas le fichier entier
        self.assertLess(len(r2), len(r1) / 3)

    def test_ancien_retour_abrege_donne_le_contenu_complet(self):
        (self.tmp / "g.txt").write_text("".join("ligne %03d : %s\n" % (i, "x" * 30) for i in range(1, 61)), encoding="utf-8", newline="")
        note, r1 = self.act("a2", "read_file", path="g.txt")
        self.remember("a2", r1)
        self.assertIn("INCHANGÉ", self.act("a2", "read_file", path="g.txt")[1])        # tant qu'il est intact dans l'historique
        self.remember("a2", "x" * 34000)                                # un gros retour récent épuise le budget de l'historique : l'ancien y est abrégé
        self.assertIn("abrégé", engine.shrink_history(self.st["projects"][0]["agents"][1]["history"])[0]["content"])
        self.assertNotIn("INCHANGÉ", self.act("a2", "read_file", path="g.txt")[1])

    def test_lecture_trop_ancienne_donne_le_contenu_complet(self):
        note, r1 = self.act("a2", "read_file", path="src/m.py", from_line=8, to_line=9)
        self.remember("a2", r1)
        for _ in range(engine.REREAD_WINDOW // 2 + 1):                  # plus de 20 messages depuis : risque de sortir de l'historique envoyé
            self.remember("a2", "[Application] petit")
        self.assertNotIn("INCHANGÉ", self.act("a2", "read_file", path="src/m.py", from_line=8, to_line=9)[1])

    def test_read_symbol_aussi_differentiel(self):
        note, r1 = self.act("a2", "read_symbol", path="src/m.py", symbol="libre")
        self.remember("a2", r1)
        self.assertIn("INCHANGÉ", self.act("a2", "read_symbol", path="src/m.py", symbol="libre")[1])

    # --- notes, spécifications, contexte, soumission
    def test_note_ajoute_sans_reecrire_et_est_plafonnee(self):
        self.act("a2", "note", text="premier point")
        self.act("a2", "note", text="deuxième\npoint")
        self.assertEqual(proj()["agents"][1]["notes"], "- premier point\n- deuxième point")
        self.assertIn("Condense", self.act("a2", "note", text="y" * (engine.NOTES_MAX + 10))[1])
        self.assertIn("vide", self.act("a2", "note", text="  ")[0])
        self.assertEqual(len(proj()["agents"][1]["notes"]), len("- premier point\n- deuxième point"))

    def test_spec_edit_remplace_ajoute_et_reserve_au_principal(self):
        self.act("a1", "spec", text="# Spec\n\nAuth : mot de passe.\nFin.\n")
        note, back = self.act("a1", "spec_edit", edits=[{"old": "mot de passe", "new": "mot de passe + 2FA"}], append="## Ajout\nnouveau")
        self.assertIsNone(back)
        self.assertEqual(proj()["spec"], "# Spec\n\nAuth : mot de passe + 2FA.\nFin.\n\n## Ajout\nnouveau\n")
        self.assertIn("introuvable", self.act("a1", "spec_edit", edits=[{"old": "absent", "new": "x"}])[1])
        self.assertIn("apparaît", self.act("a1", "spec_edit", edits=[{"old": "e", "new": "x"}])[1])
        self.assertIn("refusé", self.act("a1", "spec_edit", edits=[])[1])
        self.assertIn("réservée", self.act("a2", "spec_edit", append="x")[1])
        self.assertIn("2FA", proj()["spec"])                            # les refus n'ont rien modifié

    def test_get_work_context_en_un_appel(self):
        st = self.st["projects"][0]
        st["tasks"] = [{"id": "t1", "title": "Écrire m.py", "status": "en cours", "who": "a2", "modules": []}]
        store.wj(store.state_path("u"), self.st)
        ok, _ = locks.acquire("u", "p1", "a2", ["src/m.py"])
        self.assertTrue(ok)
        note, back = self.act("a2", "get_work_context")
        self.assertIn("t1 « Écrire m.py » [en cours]", back)
        self.assertIn("Blocages :", back)
        self.assertIn("src/m.py", back)                                 # ses verrous
        locks.release("u", "p1", "a2")

    def test_submit_task_libere_les_verrous_et_passe_a_verifier(self):
        st = self.st["projects"][0]
        st["tasks"] = [{"id": "t1", "title": "Écrire m.py", "status": "en cours", "who": "a2", "modules": []}]
        store.wj(store.state_path("u"), self.st)
        locks.acquire("u", "p1", "a2", ["src/m.py"])
        with mock.patch.object(engine, "spawn", lambda *a, **k: None):
            note, back = self.act("a2", "submit_task", id="t1", summary="m.py écrit et testé")
        self.assertIn("Verrous libérés", back)
        t = proj()["tasks"][0]
        self.assertEqual((t["status"], t["result"]), ("à vérifier", "m.py écrit et testé"))
        self.assertEqual(proj()["locks"], [])

    def test_submit_task_refus(self):
        st = self.st["projects"][0]
        st["tasks"] = [{"id": "t1", "title": "T", "status": "en cours", "who": "a2", "modules": []},
                       {"id": "t2", "title": "U", "status": "terminée", "who": "a2", "modules": []}]
        store.wj(store.state_path("u"), self.st)
        self.assertIn("compte rendu", self.act("a2", "submit_task", id="t1")[1])
        self.assertIn("pas la tienne", self.act("a3", "submit_task", id="t1", summary="x")[1])
        self.assertIn("déjà", self.act("a2", "submit_task", id="t2", summary="x")[1])
        self.assertEqual(proj()["tasks"][0]["status"], "en cours")


class Prompt(unittest.TestCase):
    def test_nouvelles_actions_declarees_et_filtrees(self):
        names = [engine._aname(l) for l in engine.ACTION_LINES]
        self.assertEqual(len(names), len(set(names)))
        for n in ("outline", "read_symbol", "find_symbol", "get_work_context", "note", "submit_task", "spec_edit"):
            self.assertIn(n, names)
        st = make_state(tempfile.mkdtemp())
        p = st["projects"][0]
        main, dev = p["agents"][0], p["agents"][1]
        self.assertTrue(engine.action_applies(p, main, "spec_edit"))
        self.assertFalse(engine.action_applies(p, dev, "spec_edit"))      # réservé à l'agent principal : pas dans le prompt des autres
        for n in ("outline", "read_symbol", "find_symbol", "get_work_context", "note", "submit_task"):
            self.assertIn('"do":"%s"' % n, engine.actions_common())       # bloc commun mis en cache, identique pour tous les agents
        self.assertNotIn("spec_edit", engine.actions_common())


if __name__ == "__main__":
    unittest.main()
