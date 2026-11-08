"""Accès au dépôt GitHub d'un projet via l'API REST (aucun git local requis).

Deux branches « structurantes » : la branche de base (ex. main, réservée à la release finale) et,
pendant une release, la branche de travail `release/<version>` (project["cycle"]) où les agents
soumettent leurs merge requests et d'où ils font leurs pull."""
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


def call_status(tok, method, path, body=None):
    """Retourne (code HTTP, JSON) sans lever d'exception pour un code HTTP (seul un échec réseau lève)."""
    req = urllib.request.Request(
        API + path, json.dumps(body).encode() if body is not None else None,
        {"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "myteam/1.0",
         "Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"message": raw[:300]}
    except urllib.error.URLError as e:
        raise Err(502, "GitHub injoignable : %s" % e.reason)


def call(tok, method, path, body=None, ok404=False):
    st, data = call_status(tok, method, path, body)
    if 200 <= st < 300:
        return data
    if st == 404 and ok404:
        return None
    txt = json.dumps(data, ensure_ascii=False)[:300]
    if st == 403 and "not accessible" in txt:
        txt = "permission manquante sur le token GitHub (%s %s) : Contents et Pull requests doivent être en « Read and write »" % (method, path.split("?")[0])
    raise Err(st if st in (404, 405, 409, 422) else 502, "GitHub %s : %s" % (st, txt))


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
        # branche d'intégration de la release en cours ; sinon la branche de base
        self.work = (project.get("cycle") or {}).get("branch") or self.base

    def _c(self, method, path, body=None, ok404=False):
        return call(self.tok, method, self.R + path, body, ok404)

    def _s(self, method, path, body=None):
        return call_status(self.tok, method, self.R + path, body)

    # ---- branches
    def sha(self, b):
        r = self._c("GET", "/git/ref/heads/" + q(b), ok404=True)
        return r["object"]["sha"] if r else None

    def ensure_branch(self, b, source=None):
        """Crée la branche depuis `source` (par défaut la branche d'intégration) si elle n'existe pas."""
        if self.sha(b):
            return False
        src = self.sha(source or self.work)
        if not src:
            raise Err(404, "Branche source « %s » introuvable (dépôt vide ?)" % (source or self.work))
        self._c("POST", "/git/refs", {"ref": "refs/heads/" + b, "sha": src})
        return True

    # ---- fichiers
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

    # ---- comparaisons, fusions, conflits
    def compare(self, base, head):
        return self._c("GET", "/compare/%s...%s" % (q(base), q(head)))

    def merge_branch(self, into, source, msg=None):
        """Fusionne `source` dans `into` (un « pull » si into est la branche d'un agent).
        état : uptodate (rien à faire), merged, ou conflict."""
        st, d = self._s("POST", "/merges", {"base": into, "head": source, "commit_message": msg or "Merge %s into %s" % (source, into)})
        if st == 201:
            return {"state": "merged", "sha": d.get("sha", "")}
        if st == 204:
            return {"state": "uptodate"}
        if st == 409:
            return {"state": "conflict"}
        raise Err(404 if st == 404 else 502, "GitHub %s : %s" % (st, json.dumps(d, ensure_ascii=False)[:300]))

    def file_at(self, path, ref):
        try:
            return self.read_file(ref, path)
        except Err:
            return None

    def conflict_info(self, into, source):
        """Fichiers modifiés des deux côtés depuis l'ancêtre commun, avec des versions différentes :
        [{path, base, into, source}]. Un fichier modifié d'un seul côté ne crée pas de conflit."""
        base_sha = self.compare(into, source)["merge_base_commit"]["sha"]
        mine = {f["filename"] for f in self.compare(base_sha, into)["files"]}
        theirs = {f["filename"] for f in self.compare(base_sha, source)["files"]}
        out = []
        for path in sorted(mine & theirs):
            a, b = self.file_at(path, into), self.file_at(path, source)
            if a != b:
                out.append({"path": path, "base": self.file_at(path, base_sha), "into": a, "source": b})
        return out

    def _blob(self, content):
        return self._c("POST", "/git/blobs", {"content": content, "encoding": "utf-8"})["sha"]

    def merge_with_resolution(self, into, source, resolutions, message):
        """Commit de fusion à deux parents sur `into` : l'arbre de `into`, plus les changements de `source`,
        avec les fichiers en conflit remplacés par `resolutions` {chemin: contenu complet, ou None = supprimer}."""
        into_sha, src_sha = self.sha(into), self.sha(source)
        base_sha = self.compare(into, source)["merge_base_commit"]["sha"]
        changed = self.compare(base_sha, source)["files"]
        entry = lambda path, sha: {"path": path, "mode": "100644", "type": "blob", "sha": sha}
        entries, seen = [], set()
        for f in changed:
            path = f["filename"]
            seen.add(path)
            if path in resolutions:
                c = resolutions[path]
                entries.append(entry(path, None if c is None else self._blob(c)))
            elif f["status"] == "removed":
                entries.append(entry(path, None))
            else:
                if f["status"] == "renamed" and f.get("previous_filename"):
                    entries.append(entry(f["previous_filename"], None))
                entries.append(entry(path, f["sha"]))
        for path, c in resolutions.items():
            if path not in seen:
                entries.append(entry(path, None if c is None else self._blob(c)))
        tree_of = self._c("GET", "/git/commits/" + into_sha)["tree"]["sha"]
        tree = self._c("POST", "/git/trees", {"base_tree": tree_of, "tree": entries})
        commit = self._c("POST", "/git/commits", {"message": message, "tree": tree["sha"], "parents": [into_sha, src_sha]})
        self._c("PATCH", "/git/refs/heads/" + q(into), {"sha": commit["sha"], "force": False})
        return commit["sha"]

    # ---- merge requests
    def pr_open(self, head, title, body, base=None):
        """Ouvre une MR head -> base (par défaut la branche d'intégration). {'skipped':True} si rien à fusionner."""
        base = base or self.work
        try:
            pr = self._c("POST", "/pulls", {"title": title, "head": head, "base": base, "body": body})
        except Err as e:
            if e.code != 422:
                raise
            if "No commits between" in e.msg:
                return {"skipped": True}
            if "already exists" not in e.msg:
                raise
            lst = self._c("GET", "/pulls?state=open&base=%s&head=%s" % (q(base), q(self.owner + ":" + head)))
            if not lst:
                raise
            pr = lst[0]
        return {"number": pr["number"]}

    def pr_merge_number(self, number):
        """Fusionne une MR ouverte : 'merged' ou 'conflict' (non fusionnable)."""
        st, d = self._s("PUT", "/pulls/%d/merge" % number, {"merge_method": "merge"})
        if 200 <= st < 300:
            return "merged"
        if st in (405, 409):
            return "conflict"
        raise Err(404 if st == 404 else 502, "GitHub %s : %s" % (st, json.dumps(d, ensure_ascii=False)[:300]))

    def pr_merge(self, head, title, body):
        """Release finale : MR head -> branche de base, fusionnée aussitôt. {'skipped':True} si rien à fusionner."""
        r = self.pr_open(head, title, body, base=self.base)
        if r.get("skipped"):
            return r
        if self.pr_merge_number(r["number"]) != "merged":
            raise Err(409, "La branche %s ne peut pas être fusionnée dans %s (conflit)" % (head, self.base))
        return r

    def release(self, tag, name, body):
        r = self._c("POST", "/releases", {"tag_name": tag, "target_commitish": self.base, "name": name, "body": body})
        return r.get("html_url")
