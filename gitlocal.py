"""Client git local : l'APPLICATION exécute git (jamais les agents) sur le répertoire local d'un projet.

Voir docs/SPEC-git-local.md. Principes :
- un worktree par agent (`<localdir>/.myteam/worktrees/<nom>`) et un worktree d'intégration réservé aux merges ;
- aucune commande git libre : seules les sous-commandes de ALLOWED sont lancées, en liste d'arguments (jamais de shell),
  avec délai maximal, environnement nettoyé, sans invite interactive, sans hooks, sans configuration globale ;
- le jeton n'est jamais écrit dans l'URL du remote ni dans `.git/config` : il passe par l'environnement du processus
  (GIT_CONFIG_*), pour les seules commandes réseau, et est masqué de toute erreur remontée.
"""
import base64
import os
import re
import subprocess
import tempfile
import time
import unicodedata

from store import Err

ALLOWED = {"init", "clone", "fetch", "push", "ls-remote", "remote", "worktree", "branch", "checkout", "add", "rm", "commit",
           "status", "diff", "log", "show", "grep", "merge", "merge-base", "rev-parse", "rev-list", "for-each-ref", "ls-files",
           "symbolic-ref", "update-ref", "reset", "cat-file", "check-ref-format", "tag", "--version"}
NETWORK = {"clone", "fetch", "push", "ls-remote"}
KEEP_ENV = ("PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "SYSTEMROOT", "SystemDrive", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
            "APPDATA", "LOCALAPPDATA", "SSH_AUTH_SOCK", "SSH_ASKPASS")
TIMEOUT = 120                                              # secondes par commande (réseau compris)
MAX_OUT = 4_000_000                                        # octets de sortie conservés
NOHOOKS = []                                               # dossier vide pointé par core.hooksPath (créé à la demande)
GITIGNORE = "# créé par myteam\n.env\n.env.*\n*.pem\n*.key\n__pycache__/\n*.pyc\nnode_modules/\n.venv/\ndata/\n"


# ---------- exécution
def _nohooks():
    if not NOHOOKS or not os.path.isdir(NOHOOKS[0]):
        NOHOOKS[:] = [tempfile.mkdtemp(prefix="myteam-nohooks-")]
    return NOHOOKS[0]


def _clean(text, token):
    """Retire le jeton (brut ou encodé) de tout texte destiné à un message ou un journal."""
    if token:
        for s in (token, base64.b64encode(("x-access-token:%s" % token).encode()).decode()):
            text = text.replace(s, "***")
    return text


def _env(token="", remote="", identity=None, network=False):
    e = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
    e.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
              "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    name, mail = identity or ("myteam", "myteam@myteam.local")
    e.update({"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": mail, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": mail})
    if network and token and remote.startswith("https://"):
        host = re.match(r"https://([^/]+)/", remote + "/")
        hdr = "Authorization: Basic " + base64.b64encode(("x-access-token:%s" % token).encode()).decode()
        e.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://%s/.extraheader" % (host.group(1) if host else "github.com"),
                  "GIT_CONFIG_VALUE_0": hdr})
    return e


def git(args, cwd, token="", remote="", identity=None, check=True, timeout=TIMEOUT, ok=(0,)):
    """Lance `git <args>` ; retourne (code, sortie standard). Lève Err si le code n'est pas dans `ok` (et check)."""
    if not args or args[0] not in ALLOWED:
        raise Err(500, "Commande git non autorisée : %s" % (args[0] if args else ""))
    cmd = ["git", "-c", "core.hooksPath=" + _nohooks(), "-c", "core.quotepath=false", "-c", "core.autocrlf=false",
           "-c", "core.longpaths=true", "-c", "commit.gpgsign=false", "-c", "protocol.ext.allow=never",
           "-c", "advice.detachedHead=false"] + list(args)
    try:
        r = subprocess.run(cmd, cwd=cwd, env=_env(token, remote, identity, args[0] in NETWORK), capture_output=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise Err(500, "git n'est pas installé (ou introuvable dans le PATH)")
    except subprocess.TimeoutExpired:
        raise Err(504, "git %s : délai de %d s dépassé" % (args[0], timeout))
    out = r.stdout[:MAX_OUT].decode("utf-8", errors="replace")
    if check and r.returncode not in ok:
        err = _clean((r.stderr.decode("utf-8", errors="replace") or out).strip(), token)
        raise Err(409 if args[0] in ("merge", "worktree", "checkout", "push") else 500, "git %s : %s" % (args[0], err[-600:]))
    return r.returncode, out


def git_version():
    return git(["--version"], os.getcwd())[1].strip()


# ---------- noms
def slug(text, default="x", n=40):
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9._-]+", "-", t).strip("-._")
    return (t[:n].strip("-._")) or default


def task_branch(agent, task_id, title=""):
    """`task/<agent>/<id>-<titre>` : espace de noms distinct de `agent/<nom>` (branche héritée) pour éviter le conflit de références git."""
    return "task/%s/%s" % (slug(agent, "agent", 20), slug("%s-%s" % (task_id, title) if title else task_id, "t", 50))


def norm_url(u):
    """Forme comparable d'une URL de remote : sans identifiants, `.git` ni casse ; `git@h:o/r` → `h/o/r`."""
    u = (u or "").strip()
    m = re.match(r"^[\w.-]+@([^:/]+):(.+)$", u)
    if m:
        u = "%s/%s" % m.groups()
    u = re.sub(r"^[a-z+]+://", "", u, flags=re.I)
    u = re.sub(r"^[^@/]*@", "", u)
    return re.sub(r"(\.git)?/*$", "", u).lower()


def safe_join(root, rel):
    """Chemin absolu de `rel` dans `root`, ou Err : jamais hors du dossier, jamais `.git`, liens symboliques résolus."""
    rel = str(rel or "").replace("\\", "/").strip("/")
    parts = [x for x in rel.split("/") if x]
    if any(x in (".", "..") for x in parts) or any(x.lower() == ".git" for x in parts) or (parts and parts[0].lower() == ".myteam"):
        raise Err(403, "Chemin interdit : %s" % rel)
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, *parts))
    if full != base and not full.startswith(base + os.sep):
        raise Err(403, "Chemin hors du dossier de travail : %s" % rel)
    return full


# ---------- contrôles préalables
def preflight(root, remote=""):
    """Contrôle un répertoire local avant usage. {ok, state: git|empty|plain, problems[], warnings[]}.
    Un problème rend le dossier inutilisable (alerte, agents concernés arrêtés) ; un avertissement est seulement signalé."""
    problems, warnings, state = [], [], "plain"
    if not root or not os.path.isdir(root):
        return {"ok": False, "state": "missing", "problems": ["Le répertoire local n'existe pas : %s" % root], "warnings": []}
    try:
        git_version()
    except Err as e:
        return {"ok": False, "state": "nogit", "problems": [e.msg], "warnings": []}
    root = os.path.realpath(root)
    entries = [n for n in os.listdir(root) if n != ".myteam"]
    rc, top = git(["rev-parse", "--show-toplevel"], root, check=False)
    top = os.path.realpath(top.strip()) if rc == 0 and top.strip() else ""
    if top and top != root:
        problems.append("Le répertoire est à l'intérieur d'un autre dépôt git (racine : %s) : indiquez la racine du dépôt." % top)
        return {"ok": False, "state": "nested", "problems": problems, "warnings": warnings}
    if top:
        state = "git"
        rc, origin = git(["remote", "get-url", "origin"], root, check=False)
        origin = origin.strip() if rc == 0 else ""
        if remote and not origin:
            problems.append("Le dépôt local n'a pas de remote « origin » alors que le projet en définit un.")
        elif remote and norm_url(origin) != norm_url(remote):
            problems.append("Le remote « origin » du dépôt local (%s) ne correspond pas à celui du projet (%s)." % (_clean(origin, ""), remote))
        gd = git(["rev-parse", "--git-dir"], root)[1].strip()
        gd = gd if os.path.isabs(gd) else os.path.join(root, gd)
        if any(os.path.exists(os.path.join(gd, f)) for f in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD")):
            problems.append("Une opération git (merge, rebase…) est inachevée dans le dépôt local.")
        if git(["status", "--porcelain", "--untracked-files=no"], root)[1].strip():
            problems.append("Le dépôt local contient des modifications non commitées.")
        elif git(["status", "--porcelain"], root)[1].strip():
            warnings.append("Le dépôt local contient des fichiers non suivis.")
    else:
        if not entries:
            state = "empty"
        elif remote:
            problems.append("Dossier non vide, sans dépôt git, alors que le projet définit un remote : dossier inutilisable (un clone exige un dossier vide).")
    return {"ok": not problems, "state": state, "problems": problems, "warnings": warnings}


# ---------- dépôt
class LocalGit:
    def __init__(self, root, remote="", token="", base="main", integration="develop"):
        self.root = os.path.realpath(root)
        self.remote, self.token, self.base, self.integration = remote or "", token or "", base or "main", integration or "develop"
        self.wt_dir = os.path.join(self.root, ".myteam", "worktrees")

    # -- exécution
    def g(self, *args, cwd=None, identity=None, **kw):
        net = args[0] in NETWORK
        return git(list(args), cwd or self.root, token=self.token if net else "", remote=self.remote, identity=identity, **kw)

    def has(self, ref, cwd=None):
        return self.g("rev-parse", "--verify", "--quiet", ref + "^{commit}", cwd=cwd, check=False)[0] == 0

    def sha(self, ref, cwd=None):
        rc, o = self.g("rev-parse", "--verify", "--quiet", ref + "^{commit}", cwd=cwd, check=False)
        return o.strip() if rc == 0 else None

    def branch_exists(self, b):
        return self.has("refs/heads/" + b)

    def check_branch(self, b):
        if self.g("check-ref-format", "--branch", b, check=False)[0] != 0:
            raise Err(400, "Nom de branche invalide : %s" % b)

    # -- initialisation (mode C) : à lancer par l'application, une fois, sous verrou de projet
    def init(self):
        pf = preflight(self.root, self.remote)
        if not pf["ok"]:
            raise Err(409, " ".join(pf["problems"]))
        if pf["state"] == "git":
            self._setup()
            return "existant"
        os.makedirs(self.root, exist_ok=True)
        if self.remote and pf["state"] == "empty":
            self.g("clone", self.remote, ".")
            how = "cloné"
        else:
            self.g("init", "-b", self.base)
            how = "initialisé"
        self._setup()
        if not self.has("HEAD"):                           # dépôt neuf ou remote vide : commit initial
            gi = os.path.join(self.root, ".gitignore")
            if not os.path.exists(gi):
                with open(gi, "w", encoding="utf-8", newline="\n") as f:
                    f.write(GITIGNORE)
            self.g("symbolic-ref", "HEAD", "refs/heads/" + self.base)      # branche de base, même si le dépôt est vide (HEAD non né)
            self.g("add", "-A")
            self.g("commit", "-m", "Initialisation du dépôt (myteam)", "--allow-empty")
        if not self.branch_exists(self.integration):
            self.g("branch", self.integration, self.base if self.branch_exists(self.base) else "HEAD")
        return how

    def _setup(self):
        os.makedirs(self.wt_dir, exist_ok=True)
        gd = self.g("rev-parse", "--git-common-dir")[1].strip()
        gd = gd if os.path.isabs(gd) else os.path.join(self.root, gd)
        ex = os.path.join(gd, "info", "exclude")
        os.makedirs(os.path.dirname(ex), exist_ok=True)
        cur = ""
        if os.path.exists(ex):
            with open(ex, encoding="utf-8") as f:
                cur = f.read()
        if ".myteam/" not in cur.split("\n"):
            with open(ex, "a", encoding="utf-8", newline="\n") as f:
                f.write(("" if cur.endswith("\n") or not cur else "\n") + ".myteam/\n")

    def ensure_local(self, b, source=None):
        """S'assure que la branche locale `b` existe : depuis `origin/b` si le remote l'a, sinon depuis `source` (par défaut l'intégration).
        Retourne True si elle vient d'être créée."""
        if self.branch_exists(b):
            return False
        self.check_branch(b)
        if self.remote:
            try:
                self.fetch()
            except Err:
                pass                                       # remote injoignable : on se rabat sur la source locale
            if self.has("origin/" + b):
                self.g("branch", b, "origin/" + b)
                return True
        src = source or self.integration
        if not self.has(src):
            raise Err(404, "Branche source « %s » introuvable" % src)
        self.g("branch", b, src)
        return True

    # -- worktrees
    def _wt_path(self, name):
        return os.path.join(self.wt_dir, slug(name, "agent", 40))

    def worktrees(self):
        """{chemin réel: branche ou None} de tous les worktrees du dépôt."""
        out, cur = {}, None
        for line in self.g("worktree", "list", "--porcelain")[1].splitlines():
            if line.startswith("worktree "):
                cur = os.path.realpath(line[9:])
                out[cur] = None
            elif line.startswith("branch ") and cur:
                out[cur] = line[7:].replace("refs/heads/", "", 1)
        return out

    def free_branch(self, b):
        """Une branche ne peut être sortie que dans un seul worktree : détache le dépôt racine s'il l'occupe, sinon erreur claire."""
        for path, br in self.worktrees().items():
            if br == b:
                if path == self.root:
                    if self.g("status", "--porcelain", "--untracked-files=no")[1].strip():
                        raise Err(409, "La branche %s est sortie dans le dépôt racine, qui contient des modifications non commitées." % b)
                    self.g("checkout", "--detach")
                else:
                    raise Err(409, "La branche %s est déjà utilisée dans %s (un seul agent par branche)." % (b, path))

    def worktree(self, name, branch, source=None):
        """Worktree de `name` sur `branch` (créée depuis `source` si elle n'existe pas). Retourne son chemin."""
        self.check_branch(branch)
        path = self._wt_path(name)
        wts = self.worktrees()
        if os.path.realpath(path) in wts:
            if wts[os.path.realpath(path)] != branch:
                raise Err(409, "Le worktree de %s est sur la branche %s, pas %s." % (name, wts[os.path.realpath(path)], branch))
            return path
        os.makedirs(self.wt_dir, exist_ok=True)
        if self.branch_exists(branch):
            self.g("worktree", "add", path, branch)
        else:
            src = source or self.integration
            if not self.has(src):
                raise Err(404, "Branche source « %s » introuvable" % src)
            self.g("worktree", "add", "-b", branch, path, src)
        return path

    def switch(self, name, branch, source=None, agent="myteam"):
        """Met le worktree de `name` sur `branch` (créée depuis `source` si besoin). S'il est sur une autre branche, le travail non commité y est
        d'abord enregistré dans un commit « WIP » (rien n'est perdu). Retourne (chemin, branche précédente ou None si inchangée)."""
        self.check_branch(branch)
        path = self._wt_path(name)
        real, wts = os.path.realpath(path), self.worktrees()
        if real not in wts or wts[real] == branch:
            return self.worktree(name, branch, source), None
        prev = wts[real]
        if self.g("status", "--porcelain", "--untracked-files=all", cwd=path)[1].strip():
            if prev is None:
                raise Err(409, "Le worktree de %s est détaché et contient des modifications non commitées." % name)
            self.commit(path, "WIP : changement de branche (%s → %s)" % (prev, branch), agent=agent)
        if self.branch_exists(branch):
            self.g("checkout", branch, cwd=path)
        else:
            src = source or self.integration
            if not self.has(src):
                raise Err(404, "Branche source « %s » introuvable" % src)
            self.g("checkout", "-b", branch, src, cwd=path)
        return path, prev

    def unmerged(self, branch, target):
        """Nombre de commits de `branch` (locale, sinon `origin/<branch>`) absents de `target` ; 0 si la branche n'existe pas."""
        ref = branch if self.has("refs/heads/" + branch) else ("origin/" + branch if self.remote and self.has("refs/remotes/origin/" + branch) else None)
        if not ref or not self.has(target):
            return 0
        return int(self.g("rev-list", "--count", "%s..%s" % (target, ref))[1].strip() or 0)

    def delete_branch(self, branch):
        """Supprime une branche locale (l'appelant a vérifié qu'elle est fusionnée) ; False si un worktree l'occupe ou si elle n'existe pas."""
        if not self.branch_exists(branch) or branch in self.worktrees().values():
            return False
        self.g("branch", "-D", branch)
        return True

    def drop_worktree(self, name, force=False):
        path = self._wt_path(name)
        if os.path.realpath(path) in self.worktrees():
            self.g("worktree", "remove", *(["--force"] if force else []), path)
        self.g("worktree", "prune")

    # -- état, commits, lecture (outils des agents, sans IA)
    def status(self, wt):
        return [{"code": l[:2].strip() or "?", "path": l[3:]} for l in self.g("status", "--porcelain", "--untracked-files=all", cwd=wt)[1].splitlines()]

    def commit(self, wt, message, agent="myteam", service="", task=""):
        """Commit programmatique de tout le worktree ; retourne le sha, ou None s'il n'y a rien à committer."""
        self.g("add", "-A", cwd=wt)
        if self.g("diff", "--cached", "--quiet", cwd=wt, check=False)[0] == 0:
            return None
        msg = str(message or "Mise à jour").strip()
        trailers = ["Agent: %s" % agent] + (["Agent-Service: %s" % service] if service else []) + (["Task: %s" % task] if task else [])
        self.g("commit", "-m", msg + "\n\n" + "\n".join(trailers), cwd=wt, identity=(agent, "%s@myteam.local" % slug(agent, "agent", 20)))
        return self.sha("HEAD", cwd=wt)

    def diff(self, wt, base, budget=20000):
        """Diff de la branche du worktree contre `base` (depuis l'ancêtre commun), tronqué à `budget` caractères."""
        t = self.g("diff", "%s...HEAD" % base, cwd=wt)[1]
        return t if len(t) <= budget else t[:budget] + "\n[…diff tronqué à %d caractères]" % budget

    def log(self, wt, n=20):
        return self.g("log", "-%d" % max(1, min(int(n), 100)), "--format=%h %an %ad %s", "--date=short", cwd=wt)[1].strip()

    def current_branch(self, wt):
        rc, o = self.g("symbolic-ref", "--short", "-q", "HEAD", cwd=wt, check=False)
        return o.strip() if rc == 0 else None

    def compare(self, base, head, budget=15000, per_file=3000):
        """Ce que `head` apporte par rapport à `base` : {commits: [« sha titre »], files: [noms], diff: texte}. Le diff est tronqué par fichier
        et au total ; les fichiers non détaillés sont listés pour être lus à la demande (équivalent de la comparaison GitHub, sans réseau)."""
        commits = [l for l in self.g("log", "--format=%h %s", "%s..%s" % (base, head))[1].splitlines() if l.strip()]
        files = [l.split("\t")[-1] for l in self.g("diff", "--name-status", "%s...%s" % (base, head))[1].splitlines() if l.strip()]
        full = self.g("diff", "%s...%s" % (base, head))[1]
        out, used, omitted = [], 0, []
        for blk in [b for b in re.split(r"(?m)^(?=diff --git )", full) if b.strip()]:
            name = (re.match(r"diff --git a/(.*?) b/", blk) or [None, "?"])[1]
            if "\nBinary files" in blk or "\nGIT binary patch" in blk:
                omitted.append("%s (binaire)" % name)
                continue
            if len(blk) > per_file:
                blk = blk[:per_file] + "\n[…diff tronqué]"
            if used + len(blk) > budget:
                omitted.append("%s (budget de diff atteint)" % name)
                continue
            used += len(blk)
            out.append(blk.rstrip("\n"))
        if omitted:
            out.append("Fichiers non détaillés : " + " ; ".join(omitted))
        return {"commits": commits, "files": files, "diff": "\n".join(out)}

    def show(self, wt, rev, path=None, limit=12000):
        spec = "%s:%s" % (rev, str(path).replace("\\", "/").strip("/")) if path else rev
        if path:
            safe_join(wt, path)
        rc, o = self.g("show", spec, cwd=wt, check=False)
        if rc != 0:
            raise Err(404, "Introuvable : %s" % spec)
        return o if len(o) <= limit else o[:limit] + "\n[…tronqué à %d caractères]" % limit

    def search(self, wt, pattern, limit=60):
        """Recherche texte (insensible à la casse, motif littéral) dans les fichiers suivis et non ignorés du worktree."""
        if not str(pattern or "").strip():
            raise Err(400, "Motif de recherche vide")
        rc, o = self.g("grep", "-n", "-I", "-i", "-F", "--untracked", "-e", str(pattern), cwd=wt, check=False)
        lines = o.splitlines()
        return lines[:limit] + (["[…%d résultats de plus]" % (len(lines) - limit)] if len(lines) > limit else [])

    # -- synchronisation et intégration
    def fetch(self):
        if self.remote:
            self.g("fetch", "origin", "--prune")

    def merged(self, branch, target):
        """Vrai si `branch` est entièrement fusionnée dans `target` (sert à déduire l'état des merge requests)."""
        return self.has(branch) and self.has(target) and self.g("merge-base", "--is-ancestor", branch, target, check=False)[0] == 0

    def ahead_behind(self, branch, target):
        a, b = self.g("rev-list", "--left-right", "--count", "%s...%s" % (branch, target))[1].split()
        return {"ahead": int(a), "behind": int(b)}

    def sync(self, wt, target, agent="myteam"):
        """Intègre `target` dans la branche du worktree. {state: uptodate|merged|conflict, files}. En cas de conflit, les marqueurs
        restent dans le worktree pour que l'agent les résolve (puis `commit`) ; `abort(wt)` annule."""
        if self.g("merge-base", "--is-ancestor", target, "HEAD", cwd=wt, check=False)[0] == 0:
            return {"state": "uptodate", "files": []}
        rc, o = self.g("merge", "--no-edit", "-m", "Merge %s" % target, target, cwd=wt, check=False, identity=(agent, "%s@myteam.local" % slug(agent, "agent", 20)))
        if rc == 0:
            return {"state": "merged", "files": []}
        files = self.g("diff", "--name-only", "--diff-filter=U", cwd=wt)[1].split()
        if not files:
            self.abort(wt)
            raise Err(409, "git merge a échoué : %s" % o[-300:])
        return {"state": "conflict", "files": files}

    def abort(self, wt):
        self.g("merge", "--abort", cwd=wt, check=False)

    def _integration_wt(self, target):
        path = os.path.join(self.wt_dir, "_integration")
        real, wts = os.path.realpath(path), self.worktrees()
        if wts.get(real) == target:
            return path
        self.free_branch(target)                           # le dépôt racine est détaché s'il occupe la cible ; un agent qui l'occupe = erreur
        if real in wts:
            self.g("checkout", target, cwd=path)
        else:
            os.makedirs(self.wt_dir, exist_ok=True)
            self.g("worktree", "add", path, target)
        return path

    def integration_path(self, target):
        """Chemin du worktree d'intégration, positionné sur `target` (à utiliser sous verrou d'intégration)."""
        return self._integration_wt(target)

    def rollback(self, target, sha):
        """Remet `target` (locale, non poussée) sur `sha` depuis le worktree d'intégration : annule une intégration refusée a posteriori."""
        wt = self._integration_wt(target)
        self.g("reset", "--hard", sha, cwd=wt)

    def integrate(self, branch, target, message=None, squash=False):
        """Fusionne `branch` dans `target` depuis le worktree d'intégration (sérialiser les appels). {state, sha | files, info}.
        En cas de conflit : merge annulé, `info` = [{path, base, into, source}] (même forme que github.Repo.conflict_info)."""
        wt = self._integration_wt(target)
        if self.g("merge-base", "--is-ancestor", branch, target, cwd=wt, check=False)[0] == 0:
            return {"state": "uptodate", "sha": self.sha(target)}
        msg = message or "Merge %s into %s" % (branch, target)
        rc, o = self.g("merge", "--squash" if squash else "--no-ff", *([] if squash else ["-m", msg]), branch, cwd=wt, check=False)
        if rc == 0:
            if squash:
                self.g("commit", "-m", msg, cwd=wt)
            return {"state": "merged", "sha": self.sha("HEAD", cwd=wt)}
        files = self.g("diff", "--name-only", "--diff-filter=U", cwd=wt)[1].split()
        info = [{"path": f, "base": self._blob(wt, ":1:", f), "into": self._blob(wt, ":2:", f), "source": self._blob(wt, ":3:", f)} for f in files]
        self.g("reset", "--hard", "HEAD", cwd=wt) if squash else self.abort(wt)
        if not files:
            raise Err(409, "git merge a échoué : %s" % o[-300:])
        return {"state": "conflict", "files": files, "info": info}

    def _blob(self, wt, stage, path):
        rc, o = self.g("show", stage + path, cwd=wt, check=False)
        return o if rc == 0 else None

    def update_target(self, target):
        """Aligne la cible locale sur `origin/<cible>` avant une intégration : avance rapide, ou fusion si elles ont divergé (après un push
        refusé). 'none' (pas de remote ou cible absente du remote) | 'uptodate' | 'updated'. Err 409 en cas de conflit avec le remote."""
        if not self.remote:
            return "none"
        self.fetch()
        ref = "origin/" + target
        if not self.has(ref):
            return "none"
        if not self.has(target):
            self.g("branch", target, ref)
            return "updated"
        if self.g("merge-base", "--is-ancestor", ref, target, check=False)[0] == 0:
            return "uptodate"
        if self.g("merge-base", "--is-ancestor", target, ref, check=False)[0] == 0:
            self.g("merge", "--ff-only", ref, cwd=self._integration_wt(target))
            return "updated"
        r = self.integrate(ref, target, message="Merge %s dans %s" % (ref, target))
        if r["state"] == "conflict":
            raise Err(409, "La branche %s locale est en conflit avec celle du remote (fichiers : %s) : intervention nécessaire." % (target, ", ".join(r["files"])))
        return "updated"

    def push_target(self, target, tries=3):
        """Pousse la cible ; si le remote a avancé entre-temps, la réaligne (fetch + fusion) puis réessaie, sans jamais forcer."""
        if not self.remote:
            return "none"
        for i in range(tries):
            self.update_target(target)
            try:
                self.push(target)
                return "pushed"
            except Err:
                if i == tries - 1:
                    raise
        return "none"

    # -- remote
    def push(self, branch, delete=False):
        if not self.remote:
            raise Err(400, "Aucun remote défini pour ce projet")
        self.check_branch(branch)
        self.g("push", "origin", *(["--delete", branch] if delete else ["--", "%s:refs/heads/%s" % (branch, branch)]))

    def push_tags(self):
        if self.remote:
            self.g("push", "origin", "--tags")

    # -- reprise après arrêt brutal (au démarrage)
    def recover(self, max_lock_age=120):
        """Supprime les index.lock orphelins, annule les merges interrompus, purge les worktrees disparus. Retourne la liste des actions."""
        done = []
        self.g("worktree", "prune")
        for path in list(self.worktrees()):
            gd = self.g("rev-parse", "--git-dir", cwd=path)[1].strip()
            gd = gd if os.path.isabs(gd) else os.path.join(path, gd)
            lock = os.path.join(gd, "index.lock")
            if os.path.exists(lock) and time.time() - os.path.getmtime(lock) > max_lock_age:
                os.remove(lock)
                done.append("index.lock supprimé : %s" % path)
            if os.path.exists(os.path.join(gd, "MERGE_HEAD")):
                self.abort(path)
                done.append("merge interrompu annulé : %s" % path)
        return done
