"""Harmonisation des règles : socle + module par mode, sujets, priorité (agent > rôle/module > global > défaut), plus récent qui remplace,
question à la saisie quand une règle plus large touche le même sujet qu'une règle plus spécifique."""
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("APM_DATA", tempfile.mkdtemp())

import engine  # noqa: E402
import rights  # noqa: E402
import rulebook  # noqa: E402
import store  # noqa: E402
from test_locks import make_state, proj  # noqa: E402

OLD_RULES_CHARS = 6710          # taille de l'ancienne liste RULES (20 règles envoyées à tous les agents)


def pr(**kw):
    p = make_state(tempfile.mkdtemp())["projects"][0]
    p.update(kw)
    return p


def block_text(sp):
    return "\n".join(sp.blocks)


class Prompt(unittest.TestCase):
    def test_module_par_mode(self):
        p = pr()
        a = p["agents"][1]
        a["source"] = ""
        gh = engine.sys_prompt(p, a)
        self.assertIn("merge_request vers release/<version>", gh)
        self.assertNotIn("lock_file (liste", gh)
        a["source"] = "local"
        loc = engine.sys_prompt(p, a)
        self.assertIn("lock_file (liste", loc)
        self.assertNotIn("merge_request vers release/<version>", loc)
        with mock.patch.object(engine, "git_mode", lambda _p: True):
            git = engine.sys_prompt(p, a)
        self.assertIn("Worktree à toi seul", git)
        self.assertNotIn("lock_file (liste", git)
        self.assertNotIn("merge_request vers release/<version>", git)

    def test_bloc_partage_identique_pour_tous_les_agents(self):
        p = pr()
        p["agents"][1]["source"] = ""
        p["instructions"] = [{"id": "i1", "n": 1, "scope": "agent:Bob", "subject": "langue", "text": "Réponds en anglais", "active": True}]
        s1, s2 = engine.sys_prompt(p, p["agents"][1]), engine.sys_prompt(p, p["agents"][2])
        self.assertEqual(s1.blocks[0], s2.blocks[0])          # le bloc mis en cache est commun : rien de propre à un agent
        self.assertNotIn("Réponds en français", s1.blocks[0])  # les règles modifiables sont dans le bloc propre à l'agent

    def test_moins_de_tokens_qu_avant(self):
        for src, git in (("", False), ("local", False), ("local", True)):
            p = pr()
            a = p["agents"][1]
            a["source"] = src
            with mock.patch.object(engine, "git_mode", lambda _p, g=git: g):
                n = len("\n".join(rulebook.locked_core() + engine.agent_rules_lines(p, a)))
            self.assertLess(n, OLD_RULES_CHARS * 0.45)

    def test_plus_de_contradiction_entre_modes(self):
        p = pr()
        a = p["agents"][1]
        a["source"] = ""
        t = "\n".join(rulebook.effective(engine.agent_mode(p, a)))
        self.assertNotIn("lock_file", t)
        self.assertNotIn("dépôt git local", t)               # l'ancienne règle R11 contredisait le worktree git local
        self.assertNotIn("principal", t.split("release")[-1].split("publie")[0])   # la publication n'est plus attribuée à l'agent principal
        self.assertIn("Tu ne publies jamais une release", t)

    def test_messages_de_conflit_alignes_sur_edit_file(self):
        src = Path(engine.__file__).read_text(encoding="utf-8")
        self.assertNotIn("fichier COMPLET", src)

    def test_sujets_de_l_interface_identiques(self):
        html = (Path(engine.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        m = re.search(r"const SUBJ=(\[.*?\]);", html)
        self.assertTrue(m, "constante SUBJ absente de index.html")
        self.assertEqual(re.findall(r'\["(\w+)"', m.group(1)), list(rulebook.SUBJECTS))

    def test_sujets_modifiables_seulement(self):
        self.assertEqual(rights.clean_subject("langue"), "langue")
        for locked in ("droits", "fin_de_tache", "release", "verrous", "inconnu", ""):
            self.assertEqual(rights.clean_subject(locked), "")
        ids = {s for (_i, s, lk, _t) in rulebook.CORE if not lk}
        self.assertEqual(ids, set(rulebook.SUBJECTS))


class Priorite(unittest.TestCase):
    def setUp(self):
        self.p = pr()
        self.bob, self.carl = self.p["agents"][1], self.p["agents"][2]

    def ins(self, n, scope, subject="langue", text=None, **kw):
        return dict({"id": "i%d" % n, "n": n, "scope": scope, "subject": subject, "text": text or "règle %d" % n, "active": True}, **kw)

    def test_regle_utilisateur_remplace_le_defaut(self):
        self.p["instructions"] = [self.ins(1, "global", text="Réponds en anglais")]
        self.assertEqual(rights.overridden_subjects(self.p, self.bob), {"langue"})
        sp = engine.sys_prompt(self.p, self.bob)
        self.assertNotIn("Réponds en français", sp)
        self.assertIn("Réponds en anglais", sp)

    def test_defaut_conserve_sans_regle(self):
        self.assertIn("Réponds en français", engine.sys_prompt(self.p, self.bob))

    def test_agent_l_emporte_sur_global(self):
        self.p["instructions"] = [self.ins(1, "global", text="Global : anglais"), self.ins(2, "agent:Bob", text="Bob : allemand")]
        lb, lc = rights.instruction_lines(self.p, self.bob), rights.instruction_lines(self.p, self.carl)
        self.assertEqual(len(lb), 1)
        self.assertIn("allemand", lb[0])
        self.assertEqual(len(lc), 1)
        self.assertIn("anglais", lc[0])                      # les autres agents gardent la règle globale

    def test_agent_prime_meme_si_global_plus_recent(self):
        self.p["instructions"] = [self.ins(1, "agent:Bob", text="Bob : allemand"), self.ins(5, "global", text="Global : anglais")]
        self.assertIn("allemand", rights.instruction_lines(self.p, self.bob)[0])

    def test_a_portee_egale_la_plus_recente(self):
        self.p["instructions"] = [self.ins(1, "role:Développeur back-end", text="ancienne"), self.ins(3, "module:X", text="récente")]
        self.assertEqual(rights.subject_winners(self.p, self.bob)["langue"]["n"], 3)

    def test_instruction_sans_sujet_inchangee(self):
        self.p["instructions"] = [self.ins(1, "global", subject="", text="Toujours des tests")]
        self.assertEqual(rights.overridden_subjects(self.p, self.bob), set())
        self.assertIn("Toujours des tests", rights.instruction_lines(self.p, self.bob)[0])

    def test_meme_sujet_meme_portee_remplace(self):
        pp = {"instructions": []}
        rights.add_instruction(pp, {"id": "a", "scope": "global", "subject": "langue", "text": "v1"})
        off = rights.add_instruction(pp, {"id": "b", "scope": "global", "subject": "langue", "text": "v2"})
        self.assertEqual(off, [1])
        self.assertEqual([i["text"] for i in pp["instructions"] if i["active"]], ["v2"])

    def test_meme_sujet_portees_differentes_cohabitent(self):
        pp = {"instructions": []}
        rights.add_instruction(pp, {"id": "a", "scope": "agent:Bob", "subject": "langue", "text": "v1"})
        off = rights.add_instruction(pp, {"id": "b", "scope": "global", "subject": "langue", "text": "v2"})
        self.assertEqual(off, [])

    def test_conflit_seulement_si_plus_large_et_concerne(self):
        self.p["instructions"] = [self.ins(1, "agent:Bob")]
        self.assertEqual(len(rights.rule_conflicts(self.p, self.ins(0, "global"))), 1)
        self.assertEqual(len(rights.rule_conflicts(self.p, self.ins(0, "role:Développeur back-end"))), 1)     # Bob a ce rôle
        self.assertEqual(rights.rule_conflicts(self.p, self.ins(0, "role:Chef de projet")), [])                # il ne le concerne pas
        self.assertEqual(rights.rule_conflicts(self.p, self.ins(0, "agent:Carl")), [])
        self.p["instructions"] = [self.ins(1, "global")]
        self.assertEqual(rights.rule_conflicts(self.p, self.ins(0, "agent:Bob")), [])                          # plus spécifique : elle gagne d'office


class Saisie(unittest.TestCase):
    """Question posée à l'utilisateur quand une règle plus large touche le même sujet qu'une règle plus spécifique."""
    def setUp(self):
        store.wj(store.state_path("u"), make_state(tempfile.mkdtemp()))
        engine.instr_add("u", "p1", "agent:Bob", "Bob répond en allemand", (), "langue")

    def actives(self):
        return {i["scope"]: i for i in proj().get("instructions", []) if i.get("active", True)}

    def test_regle_plus_large_declenche_une_question(self):
        engine.instr_add("u", "p1", "global", "Tous répondent en anglais", (), "langue")
        self.assertEqual(list(self.actives()), ["agent:Bob"])          # la nouvelle règle attend la réponse
        d = [x for x in proj()["decisions"] if x.get("ruleconf")]
        self.assertEqual(len(d), 1)
        self.assertEqual(len(d[0]["options"]), 3)
        self.assertIn("n°1", d[0]["options"][0])

    def test_remplacer(self):
        engine.instr_add("u", "p1", "global", "Tous répondent en anglais", (), "langue")
        d = proj()["decisions"][0]
        engine.decide("u", "p1", d["id"], "answer", answer=d["options"][0])
        self.assertEqual(list(self.actives()), ["global"])

    def test_garder_la_specifique(self):
        engine.instr_add("u", "p1", "global", "Tous répondent en anglais", (), "langue")
        d = proj()["decisions"][0]
        engine.decide("u", "p1", d["id"], "answer", answer=d["options"][1])
        self.assertEqual(list(self.actives()), ["agent:Bob"])
        self.assertEqual(proj()["decisions"][0]["status"], "répondue")

    def test_sujets_differents(self):
        engine.instr_add("u", "p1", "global", "Tous commentent en anglais", (), "langue")
        d = proj()["decisions"][0]
        engine.decide("u", "p1", d["id"], "answer", answer=d["options"][2])
        a = self.actives()
        self.assertEqual(set(a), {"agent:Bob", "global"})
        self.assertNotIn("subject", a["global"])                       # la règle globale n'est plus rattachée au sujet : aucun conflit

    def test_regle_plus_specifique_sans_question(self):
        engine.instr_add("u", "p1", "global", "Tous répondent en anglais", (), "langue")
        engine.decide("u", "p1", proj()["decisions"][0]["id"], "answer", answer=proj()["decisions"][0]["options"][0])
        engine.instr_add("u", "p1", "agent:Carl", "Carl répond en italien", (), "langue")
        self.assertEqual(len([x for x in proj()["decisions"] if x["status"] == "attente"]), 0)
        self.assertEqual(set(self.actives()), {"global", "agent:Carl"})

    def test_instruction_sans_sujet_pas_de_question(self):
        engine.instr_add("u", "p1", "global", "Commits en français")
        self.assertEqual(proj().get("decisions", []), [])

    def test_action_instruction_de_l_agent_principal(self):
        out, _ = engine.do_act("u", "p1", "a1", {"do": "instruction", "text": "Tous répondent en anglais", "scope": "global", "subject": "langue"}, 0, {"n": 0, "t0": 0})
        self.assertIn("NON appliquée", out)
        self.assertEqual(len([x for x in proj()["decisions"] if x["status"] == "attente"]), 1)
        out, _ = engine.do_act("u", "p1", "a1", {"do": "instruction", "text": "Dossier docs/ pour la doc", "scope": "global"}, 0, {"n": 0, "t0": 0})
        self.assertIn("Instruction enregistrée", out)

    def test_deux_questions_en_attente_ne_se_remplacent_pas(self):
        engine.instr_add("u", "p1", "agent:Bob", "Bob coordonne par message", (), "coordination")
        engine.instr_add("u", "p1", "global", "Tous répondent en anglais", (), "langue")
        engine.instr_add("u", "p1", "global", "Pas de message inutile", (), "coordination")
        self.assertEqual(len([x for x in proj()["decisions"] if x["status"] == "attente"]), 2)


if __name__ == "__main__":
    unittest.main()
