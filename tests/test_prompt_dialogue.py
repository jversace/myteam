"""(Nom choisi pour être chargé APRÈS test_engine.py, qui configure l'adresse du faux GitHub avant le premier import d'engine.)
Points 4 et 6 de l'économie de tokens : notifications sans appel IA, résumé automatique des échanges anciens, spécifications à la demande,
stabilité des blocs de prompt mis en cache (aucun réseau, IA scriptée)."""
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
from store import Err  # noqa: E402
from test_locks import make_state, proj  # noqa: E402

SPEC = "# Cahier des charges\nIntro du projet.\n\n## Auth\n" + "Connexion par mot de passe, session de 8 h. " * 40 + "\n\n## Facturation\n" + "Une facture par mois, TVA incluse. " * 40 + "\n\n## Divers\nfin\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.st = make_state(self.tmp)
        store.wj(store.state_path("u"), self.st)
        locks.STOP.clear()
        self.sent = []
        self.ai = mock.patch.object(engine, "call_ai", self.fake_ai)
        self.ai.start()
        self.sp = mock.patch.object(engine, "spawn", lambda *a, **k: None)
        self.sp.start()
        self.addCleanup(self.ai.stop)
        self.addCleanup(self.sp.stop)

    def fake_ai(self, s, system, msgs):
        self.sent.append((system, list(msgs)))
        return "Reçu."

    def act(self, who, do, **x):
        return engine.do_act("u", "p1", who, {"do": do, **x}, 0, {"n": 0, "t0": time.time()})

    def save(self):
        store.wj(store.state_path("u"), self.st)

    def agent(self, aid):
        return next(a for a in proj()["agents"] if a["id"] == aid)


class Notifications(Base):
    def test_notify_ne_declenche_aucun_appel_ia_et_arrive_au_prochain_echange(self):
        note, back = self.act("a2", "notify", to="Carl", kind="done", text="module Auth terminé", task="")
        self.assertEqual(note, "")
        self.assertIn("aucun appel IA", back)
        self.assertEqual(self.sent, [])                                          # rien n'a été envoyé à une IA
        self.assertEqual(len(self.agent("a3")["inbox"]), 1)
        engine.ask("u", "p1", "a3", "Bonjour", "Vous")
        first = self.sent[0][1][-1]["content"]
        self.assertTrue(first.startswith("[Application] Notifications reçues (sans réponse attendue)"))
        self.assertIn("Bob (terminé) : module Auth terminé", first)
        self.assertTrue(first.rstrip().endswith("[Vous] Bonjour"))
        self.assertNotIn("inbox", self.agent("a3"))                              # remise : boîte vidée
        self.assertIn("module Auth terminé", self.agent("a3")["history"][0]["content"])   # et conservée dans son historique
        engine.ask("u", "p1", "a3", "Encore", "Vous")
        self.assertNotIn("Notifications reçues", self.sent[1][1][-1]["content"])  # une seule fois

    def test_notification_non_perdue_si_l_appel_echoue(self):
        self.act("a2", "notify", to="Carl", text="info utile")
        with mock.patch.object(engine, "call_ai", side_effect=Err(429, "quota")):
            with self.assertRaises(Err):
                engine.ask("u", "p1", "a3", "x", "Vous")
        self.assertEqual(len(self.agent("a3")["inbox"]), 1)                      # toujours en attente
        engine.ask("u", "p1", "a3", "y", "Vous")
        self.assertIn("info utile", self.sent[-1][1][-1]["content"])

    def test_refus_et_plafond(self):
        self.assertIn("inconnu", self.act("a2", "notify", to="Zoé", text="x")[1])
        self.assertIn("inconnu", self.act("a2", "notify", to="Bob", text="x")[1])        # pas à soi-même
        self.assertIn("vide", self.act("a2", "notify", to="Carl", text="  ")[1])
        self.st["projects"][0]["agents"][1]["talk"] = ["a1"]                              # Bob ne peut écrire qu'à Alice
        self.save()
        self.assertIn("ne peut pas écrire", self.act("a2", "notify", to="Carl", text="x")[1])
        for i in range(engine.INBOX_MAX + 5):
            self.act("a1", "notify", to="Carl", text="n%d" % i)
        box = self.agent("a3")["inbox"]
        self.assertEqual((len(box), box[-1]["text"], box[0]["text"]), (engine.INBOX_MAX, "n%d" % (engine.INBOX_MAX + 4), "n5"))
        self.assertEqual(self.act("a1", "notify", to="Carl", kind="bizarre", text="k") and self.agent("a3")["inbox"][-1]["kind"], "info")

    def test_notification_journalisee_et_action_filtree(self):
        self.act("a2", "notify", to="Carl", text="point d'étape")
        self.assertTrue(any(m["text"] == "(notification) point d'étape" and m["from"] == "Bob" and m["to"] == "Carl" for m in proj()["log"]))
        p = proj()
        self.assertIn('"do":"notify"', engine.actions_extra(p, p["agents"][1]))
        solo = dict(p, agents=[p["agents"][1]])
        self.assertNotIn('"do":"notify"', engine.actions_extra(solo, solo["agents"][0]))   # personne à qui écrire : action non proposée


class Resume(Base):
    def test_historique_borne_et_echanges_anciens_resumes(self):
        ag = {"history": []}
        for i in range(engine.HIST_KEEP // 2 + 4):
            engine.push_history(ag, [{"role": "user", "content": "[Vous] demande numéro %d" % i},
                                     {"role": "assistant", "content": 'Fait %d.\n```action\n{"do":"read_file","path":"a"}\n```\n```action\n{"do":"read_file","path":"b"}\n```\n```action\n{"do":"edit_file"}\n```' % i}])
        self.assertEqual(len(ag["history"]), engine.HIST_KEEP)
        d = ag["digest"].split("\n")
        self.assertEqual(len(d), 4)                                              # 4 échanges sortis de l'historique
        self.assertIn("Vous : demande numéro 0", d[0])
        self.assertIn("→ Fait 0.", d[0])
        self.assertIn("[actions : read_file×2, edit_file]", d[0])
        self.assertTrue(all(len(l) <= engine.DIGEST_LINE for l in d))

    def test_resume_plafonne_et_garde_le_plus_recent(self):
        ag = {"history": []}
        for i in range(200):
            engine.push_history(ag, [{"role": "user", "content": "[Vous] sujet %03d " % i + "x" * 100}, {"role": "assistant", "content": "ok"}])
        self.assertLessEqual(len(ag["digest"]), engine.DIGEST_MAX)
        self.assertIn("sujet 184", ag["digest"])                                 # le plus récent des échanges sortis
        self.assertNotIn("sujet 010", ag["digest"])

    def test_retours_d_actions_resumes_sans_texte_brut(self):
        ag = {"history": []}
        msgs = [{"role": "user", "content": engine.OLD_RESULT + " :\n\n" + "contenu énorme " * 100},
                {"role": "assistant", "content": 'Je modifie.\n```action\n{"do":"edit_file"}\n```'}]
        engine.push_history(ag, msgs * (engine.HIST_KEEP // 2 + 1))
        self.assertEqual(ag["digest"], "- → Je modifie. [actions : edit_file]")
        self.assertNotIn("énorme", ag["digest"])

    def test_resume_dans_le_prompt_changeant_et_efface_a_la_reinitialisation(self):
        self.st["projects"][0]["agents"][1]["digest"] = "- Vous : ancienne demande"
        self.save()
        p = proj()
        sp = engine.sys_prompt(p, p["agents"][1])
        self.assertIn("ancienne demande", sp.blocks[2])
        self.assertNotIn("ancienne demande", sp.blocks[0] + sp.blocks[1])
        self.st["projects"][0]["agents"][1]["inbox"] = [{"id": "i1", "from": "Alice", "text": "t"}]
        self.save()
        with mock.patch.object(engine, "onboard", lambda *a, **k: None):
            engine.agent_reset("u", "p1", "a2", history=True, notes=False, branch=False, onboard_after=False)
        b = self.agent("a2")
        self.assertNotIn("digest", b)
        self.assertNotIn("inbox", b)

    def test_depassement_de_contexte_garde_un_resume(self):
        ag = self.st["projects"][0]["agents"][1]
        ag["history"] = [m for i in range(10) for m in ({"role": "user", "content": "[Vous] sujet %d" % i}, {"role": "assistant", "content": "ok %d" % i})]
        self.save()
        calls = []

        def ai(s, system, msgs):
            calls.append(1)
            if len(calls) == 1:
                raise Err(400, "prompt is too long: context length exceeded")
            return "ok"
        with mock.patch.object(engine, "call_ai", ai):
            engine.ask("u", "p1", "a2", "go", "Vous")
        d = self.agent("a2").get("digest", "")
        self.assertIn("sujet 0", d)
        self.assertIn("sujet 7", d)                                              # les 8 premiers échanges compactés sont résumés


class Specifications(Base):
    def setUp(self):
        super().setUp()
        self.st["projects"][0]["spec"] = SPEC
        self.st["projects"][0]["modules"] = [{"id": "m1", "name": "Auth", "desc": ""}, {"id": "m2", "name": "Facturation", "desc": ""}]
        self.st["projects"][0]["tasks"] = [{"id": "t1", "title": "Login", "status": "en cours", "who": "a2", "modules": ["m1"]}]
        self.save()

    def test_spec_courte_en_entier_spec_longue_en_sommaire(self):
        p = proj()
        self.assertEqual(engine.spec_prompt(dict(p, spec="# Spec\nCourte.")), "# Spécifications actuelles\n# Spec\nCourte.")
        txt = engine.spec_prompt(p)
        self.assertIn("sommaire", txt)
        self.assertIn("- Cahier des charges", txt)
        self.assertIn("  - Auth (~", txt)
        self.assertNotIn("mot de passe", txt)                                    # pas le contenu
        self.assertLess(len(txt), 500)
        self.assertEqual(engine.spec_prompt(dict(p, spec="")), "")
        sans_titres = engine.spec_prompt(dict(p, spec="x" * 6000))
        self.assertIn("début", sans_titres)
        self.assertLess(len(sans_titres), 2800)

    def test_sections_de_la_tache_en_cours_dans_le_bloc_changeant(self):
        p = proj()
        sp = engine.sys_prompt(p, p["agents"][1])                                # Bob : tâche en cours sur Auth
        self.assertIn("sections liées à ta tâche en cours", sp.blocks[2])
        self.assertIn("session de 8 h", sp.blocks[2])
        self.assertNotIn("TVA incluse", sp.blocks[2])                            # Facturation n'est pas son module
        self.assertNotIn("session de 8 h", sp.blocks[0] + sp.blocks[1])
        self.assertIn("sommaire", sp.blocks[0])
        carl = engine.sys_prompt(p, p["agents"][2])                              # Carl : pas de tâche
        self.assertNotIn("sections liées", carl.blocks[2])
        self.assertEqual(sp.blocks[0], carl.blocks[0])                           # bloc commun toujours identique

    def test_read_spec_sommaire_section_et_erreurs(self):
        note, back = self.act("a2", "read_spec")
        self.assertIn("Auth", back)
        self.assertIn("Facturation", back)
        self.assertNotIn("mot de passe", back)
        note, back = self.act("a2", "read_spec", section="Facturation")
        self.assertIn("TVA incluse", back)
        self.assertNotIn("mot de passe", back)
        self.assertIn("lignes", back)
        self.assertIn("introuvable", self.act("a2", "read_spec", section="Zzz")[1])
        near = self.act("a2", "read_spec", section="Factur")[1]
        self.assertIn("TVA incluse", near)                                       # début de titre : une seule section correspond
        note, back = self.act("a2", "read_spec", from_line=1, to_line=2)
        self.assertIn("Intro du projet", back)
        self.st["projects"][0]["spec"] = ""
        self.save()
        self.assertIn("pas encore", self.act("a2", "read_spec")[1])

    def test_spec_edit_apres_read_spec(self):
        self.act("a1", "spec_edit", edits=[{"old": "fin", "new": "FIN"}])
        self.assertTrue(proj()["spec"].endswith("FIN\n"))
        self.assertIn("read_spec", self.act("a1", "spec_edit", edits=[{"old": "absent", "new": "x"}])[1])


class CacheStable(Base):
    """Les deux premiers blocs du prompt (mis en cache) ne doivent pas changer quand seul ce qui bouge à chaque tour change."""
    def blocks(self, aid="a2"):
        p = proj()
        return engine.sys_prompt(p, next(a for a in p["agents"] if a["id"] == aid)).blocks

    def test_verrous_taches_resume_boite_ne_touchent_pas_les_blocs_caches(self):
        self.st["projects"][0]["spec"] = SPEC
        self.save()
        b0 = self.blocks()
        ok, _ = locks.acquire("u", "p1", "a3", ["x.txt"])                        # un verrou d'un autre agent
        self.assertTrue(ok)
        self.st = store.load("u")
        self.st["projects"][0]["tasks"] = [{"id": "t1", "title": "T", "status": "en cours", "who": "a2", "modules": []}]
        self.st["projects"][0]["agents"][1]["digest"] = "- Vous : x"
        self.st["projects"][0]["agents"][1]["notes"] = "- une note"
        self.save()
        b1 = self.blocks()
        self.assertEqual(b0[0], b1[0])
        self.assertEqual(b0[1], b1[1])
        self.assertNotEqual(b0[2], b1[2])
        self.assertIn("x.txt : Carl", b1[2])                                     # la liste des verrous est dans le bloc changeant
        self.assertNotIn("x.txt", b1[0] + b1[1])
        locks.release("u", "p1", "a3")

    def test_modifier_les_specifications_ne_change_que_le_bloc_commun(self):
        self.st["projects"][0]["spec"] = SPEC
        self.save()
        b0 = self.blocks()
        self.st = store.load("u")
        self.st["projects"][0]["spec"] = SPEC.replace("Intro du projet.", "Nouvelle intro.")
        self.save()
        b1 = self.blocks()
        self.assertEqual(b0[0], b1[0])                                           # le sommaire ne contient pas le texte : toujours en cache
        self.st["projects"][0]["spec"] = SPEC + "\n## Nouvelle section\nx\n"
        self.save()
        self.assertNotEqual(b0[0], self.blocks()[0])                             # un nouveau titre change le sommaire : normal

    def test_actions_nouvelles_dans_le_bloc_commun(self):
        for n in ("read_spec",):
            self.assertIn('"do":"%s"' % n, engine.actions_common())
        self.assertNotIn('"do":"notify"', engine.actions_common())               # dépend des agents joignables : bloc propre à l'agent


if __name__ == "__main__":
    unittest.main()
