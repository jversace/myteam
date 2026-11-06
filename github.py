"""Accès au dépôt GitHub d'un projet via l'API REST (aucun git local requis)."""
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from store import Err

API = os.environ.get("APM_GITHUB_API", "https://api.github.com")
q = urllib.parse.quote


def call(tok, method, path, body=None, ok404=False):
    req = urllib.request.Request(
        API + path, json.dumps(body).encode() if body is not None else None,
        {"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "myteam/1.0",
         "Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404 and ok404:
            return None
        txt = e.read().decode(errors="replace")[:300]
        if e.code == 403 and "not accessible" in txt:
            txt = "permission manquante sur le token GitHub (%s %s) : Contents et Pull requests doivent être en « Read and write »" % (method, path.split("?")[0])
        raise Err(e.code if e.code in (404, 405, 409, 422) else 502, "GitHub %s : %s" % (e.code, txt))
    except urllib.error.URLError as e:
        raise Err(502, "GitHub injoignable : %s" % e.reason)


class Repo:
    def __init__(self, project):
        r = project.get("repo") or {}
        m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", r.get("url", ""))
        self.tok = r.get("token", "")
        if not m or not self.tok:
            raise Err(400, "Dépôt GitHub ou token non configuré pour ce projet")
        self.owner, self.name = m.groups()
        self.R = "/repos/%s/%s" % (self.owner, self.name)
        self.base = r.get("branch") or "main"

    def _c(self, method, path, body=None, ok404=False):
        return call(self.tok, method, self.R + path, body, ok404)

    def ensure_branch(self, b):
        """Crée la branche depuis la branche de base si elle n'existe pas. Retourne True si créée."""
        if self._c("GET", "/git/ref/heads/" + q(b), ok404=True):
            return False
        ref = self._c("GET", "/git/ref/heads/" + q(self.base), ok404=True)
        if not ref:
            raise Err(404, "Branche de base « %s » introuvable (dépôt vide ?)" % self.base)
        self._c("POST", "/git/refs", {"ref": "refs/heads/" + b, "sha": ref["object"]["sha"]})
        return True

    def contents(self, b, path):
        return self._c("GET", "/contents/%s?ref=%s" % (q(path), q(b)), ok404=True)

    def read_file(self, b, path):
        """Retourne le texte du fichier, ou None s'il n'existe pas."""
        c = self.contents(b, path)
        if c is None:
            return None
        if isinstance(c, list):
            raise Err(400, "« %s » est un dossier" % path)
        if c.get("encoding") != "base64":
            raise Err(413, "Fichier trop volumineux pour la lecture : %s" % path)
        return base64.b64decode(c.get("content", "")).decode("utf-8", errors="replace")

    def list_dir(self, b, path):
        c = self.contents(b, path)
        if c is None:
            return None
        if isinstance(c, dict):
            raise Err(400, "« %s » est un fichier" % path)
        return [{"name": x["name"], "type": x["type"], "size": x.get("size", 0)} for x in c]

    def put_file(self, b, path, content, msg):
        cur = self.contents(b, path)
        body = {"message": msg, "branch": b, "content": base64.b64encode(str(content).encode()).decode()}
        if isinstance(cur, dict) and "sha" in cur:
            body["sha"] = cur["sha"]
        return self._c("PUT", "/contents/" + q(path), body)["commit"]["sha"]

    def delete_file(self, b, path, msg):
        cur = self.contents(b, path)
        if not isinstance(cur, dict) or "sha" not in cur:
            raise Err(404, "Fichier introuvable : %s" % path)
        r = self._c("DELETE", "/contents/" + q(path), {"message": msg, "branch": b, "sha": cur["sha"]})
        return r["commit"]["sha"]

    def pr_merge(self, head, title, body):
        """Crée la merge request head -> base puis la fusionne. {'skipped':True} si rien à fusionner."""
        try:
            pr = self._c("POST", "/pulls", {"title": title, "head": head, "base": self.base, "body": body})
        except Err as e:
            if e.code != 422:
                raise
            if "No commits between" in e.msg:
                return {"skipped": True}
            if "already exists" not in e.msg:
                raise
            lst = self._c("GET", "/pulls?state=open&base=%s&head=%s" % (q(self.base), q(self.owner + ":" + head)))
            if not lst:
                raise
            pr = lst[0]
        self._c("PUT", "/pulls/%d/merge" % pr["number"], {"merge_method": "merge"})
        return {"number": pr["number"]}

    def release(self, tag, name, body):
        r = self._c("POST", "/releases", {"tag_name": tag, "target_commitish": self.base, "name": name, "body": body})
        return r.get("html_url")
