"""Versionnement : format du numéro, concordance avec CHANGELOG.md, route publique, titre et menu « À propos »."""
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

os.environ.setdefault("APM_DATA", tempfile.mkdtemp())

import version  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class Numero(unittest.TestCase):
    def test_format(self):
        self.assertRegex(version.VERSION, r"^\d+\.\d+\.\d+$")

    def test_changelog_concorde_avec_version(self):
        """Rappel de la règle : toute modification met à jour version.py ET l'entrée de tête du CHANGELOG."""
        txt = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        m = re.search(r"^## \[(\d+\.\d+\.\d+)\]", txt, re.M)
        self.assertIsNotNone(m, "CHANGELOG.md sans entrée de version")
        self.assertEqual(m.group(1), version.VERSION, "version.py et CHANGELOG.md ne concordent pas")

    def test_depot(self):
        self.assertEqual(version.REPO_URL, "https://github.com/jversace/myteam")


class Serveur(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.sv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.base = "http://127.0.0.1:%d" % cls.sv.server_address[1]
        threading.Thread(target=cls.sv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.sv.shutdown()
        cls.sv.server_close()

    def test_route_version_publique(self):
        with urllib.request.urlopen(self.base + "/api/version") as r:      # sans jeton : visible à la connexion
            d = json.loads(r.read())
        self.assertEqual(d, {"version": version.VERSION, "repo": version.REPO_URL})

    def test_titre_de_page_versionne(self):
        with urllib.request.urlopen(self.base + "/") as r:
            html = r.read().decode("utf-8")
        self.assertIn("<title>myTeam v%s</title>" % version.VERSION, html)


class Front(unittest.TestCase):
    def test_a_propos_et_titre(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-v="about"', html)
        self.assertIn("function aboutView()", html)
        self.assertIn("verTag()", html)


if __name__ == "__main__":
    unittest.main()
