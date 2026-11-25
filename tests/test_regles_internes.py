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


class RegleEnTexte(unittest.TestCase):
    """Règles du projet et d'un agent en texte libre : une règle par ligne ou puce, sujet facultatif entre crochets."""
    def setUp(self):
        self.p = pr()
        self.bob, self.carl = self.p["agents"][1], self.p["agents"][2]

    def test_decoupage(self):
        t = "Règle libre\nsuite\n- [langue] Réponds en anglais\n- Pas de TODO\n[fichiers] Toujours edit_file\n* [urgent] autre mot"
        r = rulebook.parse_rules(t)
        self.assertEqual([x["subject"] for x in r], ["", "langue", "", "fichiers", ""])
        self.assertEqual(r[0]["text"], "Règle libre\nsuite")
        self.assertEqual(r[1]["text"], "Réponds en anglais")
        self.assertEqual(r[4]["subject"], "")                  # [urgent] n'est pas un sujet : la ligne reste telle quelle

    def test_texte_sans_sujet_inchange(self):
        t = "Règle un.\n\nRègle deux, longue\n   avec retrait.\n"
        self.p["rules"] = t
        self.assertEqual(prules_shared(self.p), t.strip())

    def test_regle_du_projet_a_sujet_remplace_le_defaut(self):
        self.p["rules"] = "Commits courts\n- [langue] Réponds en anglais"
        sp = engine.sys_prompt(self.p, self.bob)
        self.assertNotIn("Réponds en français", sp)
        self.assertIn("Réponds en anglais", sp)
        self.assertNotIn("Réponds en anglais", sp.blocks[0])   # propre à l'agent : jamais dans le bloc partagé
        self.assertIn("Commits courts", sp.blocks[0])

    def test_regle_d_agent_l_emporte_sur_celle_du_projet(self):
        self.p["rules"] = "- [langue] Anglais"
        self.bob["rules"] = "- [langue] Allemand"
        sb, sc = engine.sys_prompt(self.p, self.bob), engine.sys_prompt(self.p, self.carl)
        self.assertIn("Allemand", sb)
        self.assertNotIn("Anglais", sb)
        self.assertIn("Anglais", sc)
        self.assertNotIn("Allemand", sc)

    def test_instruction_plus_recente_de_meme_portee_bat_la_regle_en_texte(self):
        self.p["rules"] = "- [langue] Anglais"
        self.p["instructions"] = [{"id": "i", "n": 4, "scope": "global", "subject": "langue", "text": "Italien", "active": True}]
        sp = engine.sys_prompt(self.p, self.bob)
        self.assertIn("Italien", sp)
        self.assertNotIn("Anglais", sp)

    def test_regle_d_agent_en_texte_bat_instruction_globale(self):
        self.bob["rules"] = "- [langue] Allemand"
        self.p["instructions"] = [{"id": "i", "n": 4, "scope": "global", "subject": "langue", "text": "Italien", "active": True}]
        sp = engine.sys_prompt(self.p, self.bob)
        self.assertIn("Allemand", sp)
        self.assertNotIn("Italien", sp)

    def test_cohérence_signale_la_regle_qui_l_emporte(self):
        self.p["rules"] = "- [langue] Anglais"
        self.bob["rules"] = "- [langue] Allemand"
        msgs = " ¦ ".join(x["text"] for x in engine.prules.lint(self.p))
        self.assertIn("l’emporte sur", msgs)
        self.assertIn("Bob", msgs)

    def test_question_si_instruction_large_touche_une_regle_d_agent_en_texte(self):
        store.wj(store.state_path("u"), make_state(tempfile.mkdtemp()))
        st = store.load("u")
        st["projects"][0]["agents"][1]["rules"] = "Soigne les noms\n- [langue] Allemand"
        store.wj(store.state_path("u"), st)
        engine.instr_add("u", "p1", "global", "Tous en anglais", (), "langue")
        d = [x for x in proj()["decisions"] if x.get("ruleconf")]
        self.assertEqual(len(d), 1)
        engine.decide("u", "p1", d[0]["id"], "answer", answer=d[0]["options"][0])
        self.assertNotIn("Allemand", proj()["agents"][1]["rules"])      # la règle en texte de Bob est retirée
        self.assertIn("Soigne les noms", proj()["agents"][1]["rules"])
        self.assertEqual([i["scope"] for i in proj()["instructions"] if i["active"]], ["global"])


def prules_shared(p):
    return engine.prules.shared_rules_text(p)


def names(txt):
    return set(re.findall(r'\{"do":"(\w+)"', txt))


class ActionsFiltrees(unittest.TestCase):
    def setUp(self):
        self.p = pr()
        self.alice, self.bob, self.carl = self.p["agents"]       # Alice : agent principal (Chef de projet) ; Bob, Carl : développeurs

    def acts(self, a, source, git=False):
        a["source"] = source
        with mock.patch.object(engine, "git_mode", lambda _p, g=git: g):
            return names(engine.actions_text(self.p, a))

    def test_mode_github(self):
        n = self.acts(self.bob, "")
        self.assertTrue({"pull", "merge_request", "resolve_merge", "read_file", "edit_file", "ask_user", "task_status"} <= n)
        self.assertFalse(n & {"lock_file", "unlock_file", "get_locks", "status", "commit", "sync", "propose_check", "mr_review"})

    def test_mode_local(self):
        n = self.acts(self.bob, "local")
        self.assertTrue({"lock_file", "unlock_file", "get_locks"} <= n)
        self.assertFalse(n & {"pull", "merge_request", "resolve_merge", "mr_review", "status", "commit"})

    def test_mode_git(self):
        n = self.acts(self.bob, "local", git=True)
        self.assertTrue({"status", "commit", "diff", "search", "sync", "git_log", "git_show", "propose_check", "resolve_merge"} <= n)
        self.assertFalse(n & {"lock_file", "pull", "merge_request"})

    def test_agent_principal(self):
        n = self.acts(self.alice, "")
        self.assertTrue({"instruction", "spec", "stop_agent", "relaunch", "task_review", "mr_review", "module"} <= n)
        self.assertFalse(names(engine.actions_text(self.p, self.bob)) & {"instruction", "spec", "stop_agent", "relaunch", "task_review"})

    def test_actions_selon_les_droits(self):
        self.p["roles"].append({"id": "r9", "name": "Rédacteur technique"})
        self.carl["role"] = "r9"
        n = self.acts(self.carl, "")
        self.assertFalse(n & {"bug", "bug_status", "module", "propose_check"})
        self.assertIn("bug", self.acts(self.bob, ""))

    def test_workflows_et_messages(self):
        self.assertFalse(self.acts(self.bob, "") & {"run_workflow", "wf_step"})
        self.p["workflows"] = [{"id": "w", "name": "Revue", "enabled": True, "steps": []}]
        self.assertTrue({"run_workflow", "wf_step"} <= self.acts(self.bob, ""))
        self.bob["talk"] = ["none"]
        self.assertNotIn("message", self.acts(self.bob, ""))
        self.assertIn("message", self.acts(self.alice, ""))

    def test_instr_rewrite_jamais_dans_le_prompt(self):
        self.assertNotIn("instr_rewrite", self.acts(self.alice, ""))
        self.assertIn("instr_rewrite", engine.ACTIONS)

    def test_toute_action_est_classee(self):
        for line in engine.ACTION_LINES:
            engine.action_applies(self.p, self.bob, engine._aname(line))      # ne lève pas
        extra = {engine._aname(l) for l in engine.ACTION_LINES} - set(engine.ACTIONS_ALWAYS) - set(engine.ACTIONS_HIDDEN)
        self.assertEqual(len(extra) + len(engine.ACTIONS_ALWAYS) + len(engine.ACTIONS_HIDDEN), len(engine.ACTION_LINES))

    def test_moins_de_tokens(self):
        full = len(engine.ACTIONS)
        for a, src, git in ((self.bob, "", False), (self.bob, "local", False), (self.bob, "local", True)):
            a["source"] = src
            with mock.patch.object(engine, "git_mode", lambda _p, g=git: g):
                n = len(engine.actions_text(self.p, a))
            self.assertLess(n, full * 0.8)

    def test_bloc_partage_inchange(self):
        s1 = engine.sys_prompt(self.p, self.bob)
        self.bob["source"] = ""
        s2 = engine.sys_prompt(self.p, self.bob)
        self.assertEqual(s1.blocks[0], s2.blocks[0])
        self.assertIn("edit_file", s1.blocks[0])
        self.assertNotIn("merge_request", s1.blocks[0])
        self.assertIn("merge_request", s2)


class AnalyseCiblee(unittest.TestCase):
    def setUp(self):
        self.p = pr()

    def test_rien_pour_l_ia_si_tout_est_arbitre_par_le_code(self):
        self.p["rules"] = "- [langue] Anglais"
        self.p["agents"][1]["rules"] = "- [coordination] Écris peu"
        self.assertFalse(engine.prules.has_material(self.p))
        self.p["rules"] += "\n- Commits courts"
        self.assertTrue(engine.prules.has_material(self.p))

    def test_dump_sans_les_regles_a_sujet_ni_les_modes_inutiles(self):
        self.p["rules"] = "Commits courts\n- [langue] Anglais"
        self.p["agents"][1]["rules"] = "- [langue] Allemand"
        for a in self.p["agents"]:
            a["source"] = "local"
        txt = engine.prules.dump(self.p, engine.analysis_builtin(self.p))
        self.assertIn("Commits courts", txt)
        self.assertIn("Sujets tranchés AUTOMATIQUEMENT", txt)
        self.assertIn("[agent:Bob] pour Bob : Allemand", txt)
        self.assertIn("[global] pour Alice, Carl : Anglais", txt)
        builtin = "\n".join(engine.analysis_builtin(self.p))
        self.assertIn("lock_file", builtin)                        # mode local utilisé
        self.assertNotIn("merge_request vers", builtin)            # mode github inutilisé
        self.assertNotIn("Worktree", builtin)

    def test_regle_remplacee_pour_tous_absente_des_regles_integrees(self):
        self.p["rules"] = "- [langue] Anglais"
        self.assertNotIn("Réponds en français", "\n".join(engine.analysis_builtin(self.p)))
        self.p["agents"][1]["rules"] = ""
        self.p["instructions"] = []
        self.p["rules"] = ""
        self.assertIn("Réponds en français", "\n".join(engine.analysis_builtin(self.p)))

    def test_pas_de_nouvel_appel_si_le_texte_n_a_pas_change(self):
        store.wj(store.state_path("u"), make_state(tempfile.mkdtemp()))
        st = store.load("u")
        st["projects"][0]["rules"] = "Commits courts"
        store.wj(store.state_path("u"), st)
        calls = []

        def fake_ask(u, pid, aid, *a, **k):                     # l'agent répond par l'action rules_report (rapport structuré)
            calls.append(1)
            engine.mp(u, pid, lambda pp: pp["rules_report"].update(analyzing="", ai=[], ai_ts="x"))
            return "ok"
        with mock.patch.object(engine, "ask", fake_ask):
            engine.rules_analysis("u", "p1")
            self.assertEqual(len(calls), 1)
            engine.rules_analysis("u", "p1")                       # même texte : pas d'appel
            self.assertEqual(len(calls), 1)
            engine.rules_analysis("u", "p1", force=True)           # demande explicite
            self.assertEqual(len(calls), 2)
            st = store.load("u")
            st["projects"][0]["rules"] = "Commits courts et clairs"
            store.wj(store.state_path("u"), st)
            engine.rules_analysis("u", "p1")                       # texte modifié : nouvelle analyse
            self.assertEqual(len(calls), 3)

    def test_la_consolidation_conserve_le_sujet(self):
        store.wj(store.state_path("u"), make_state(tempfile.mkdtemp()))
        engine.instr_add("u", "p1", "global", "Tous en anglais", (), "langue")
        n = proj()["instructions"][0]["n"]
        st = store.load("u")
        st["projects"][0]["instr_consol"] = {"analyzing": "a1"}
        store.wj(store.state_path("u"), st)
        engine.do_act("u", "p1", "a1", {"do": "instr_rewrite", "items": [{"text": "Anglais pour tous", "scope": "global", "subject": "langue", "replaces": [n]}]}, 0, {"n": 0, "t0": 0})
        act = [i for i in proj()["instructions"] if i.get("active", True)]
        self.assertEqual([(i["text"], i.get("subject")) for i in act], [("Anglais pour tous", "langue")])


class DroitsParDefaut(unittest.TestCase):
    def test_front_sans_copie_des_droits(self):
        html = (Path(engine.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("DEF_CAPS", html)
        self.assertNotIn("DEF_BASE", html)
        self.assertIn("r.defcaps", html)

    def test_serveur_fournit_les_droits(self):
        src = (Path(engine.__file__).parent / "server.py").read_text(encoding="utf-8")
        self.assertIn('"defcaps": rights.DEFAULT_CAPS', src)
        self.assertIn('"defbase": rights.BASE_CAPS', src)
        self.assertEqual(rights.BASE_CAPS["check"], "approve")
        self.assertEqual(rights.DEFAULT_CAPS["Chef de projet"]["mr_review"], "free")
