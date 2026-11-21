"""Contrôles automatiques (tests, lint, build) : analyse de la commande et exécution encadrée. Voir docs/SPEC-git-local.md § 6 bis et 6 ter.

La commande est du CODE ARBITRAIRE exécuté sur la machine de l'application. Garde-fous :
- seule l'approbation de l'utilisateur l'active (jamais un agent) ; `analyze` calcule, INDÉPENDAMMENT de l'agent qui la propose, un niveau de
  vigilance (faible / moyenne / élevée) et refuse d'office certains motifs ;
- exécution avec délai maximal (arbre de processus tué), sortie limitée, environnement nettoyé (ni jeton, ni clé de service), une seule à la fois.
"""
import os
import re
import signal
import subprocess
import threading
import time

CHECK_LOCK = threading.Lock()                              # une exécution à la fois (ports, bases de test, fichiers temporaires)
KEEP_ENV = ("PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "SYSTEMROOT", "SystemDrive", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
            "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "VIRTUAL_ENV", "JAVA_HOME", "GOPATH", "CARGO_HOME", "RUSTUP_HOME", "NVM_DIR", "PYTHONPATH")
MAX_TIMEOUT = 3600
DEFAULT_TIMEOUT = 300
TAIL = 2500                                                # caractères de sortie conservés (fin de la sortie)
WHEN = ("validation", "integration", "both")

KNOWN = ("pytest", "python", "python3", "py", "npm", "npx", "yarn", "pnpm", "node", "ruff", "flake8", "pylint", "mypy", "black", "isort", "eslint", "prettier",
         "tsc", "go", "cargo", "rustc", "mvn", "gradle", "./gradlew", "gradlew", "./mvnw", "dotnet", "make", "ctest", "phpunit", "composer", "rspec", "bundle", "rake",
         "jest", "vitest", "unittest", "tox", "nox", "swift", "flutter", "dart", "deno", "bun")

# Motifs REFUSÉS d'office (aucune carte d'approbation) : (regex, explication)
REFUSED = [
    (r"(^|[\s;&|(`])sudo(\s|$)", "élévation de privilèges (sudo)"),
    (r"(^|[\s;&|(`])su\s+-?\w*", "changement d’utilisateur (su)"),
    (r"(curl|wget|iwr|invoke-webrequest)\b[^|;&\n]*\|\s*(sudo\s+)?(sh|bash|zsh|python\d?|perl|ruby|node|iex|invoke-expression|powershell|pwsh)\b", "téléchargement exécuté directement"),
    (r"\b(iex|invoke-expression)\b", "exécution de texte comme commande (iex)"),
    (r"base64\s+(-d|--decode)[^|;&\n]*\|\s*(sh|bash|zsh)", "commande dissimulée (base64 exécuté)"),
    (r"(-enc|-encodedcommand)\b", "commande PowerShell encodée"),
    (r"\brm\s+(-[a-z]*r[a-z]*f[a-z]*|-[a-z]*f[a-z]*r[a-z]*|--recursive\s+--force|--force\s+--recursive)\s+(/|~|\$home|\.\.|\*|/\*)(\s|$)", "suppression récursive hors du dossier de travail"),
    (r"\b(rd|rmdir)\s+/s\b[^|;&\n]*\b[a-z]:\\", "suppression récursive d’un lecteur"),
    (r"\bdel\s+(/[a-z]\s+)*[a-z]:\\", "suppression de fichiers hors du dossier de travail"),
    (r"\b(mkfs|fdisk|parted|diskpart)\b|\bformat\s+[a-z]:|\bdd\s+if=", "écriture sur un disque"),
    (r">\s*/dev/(sd|nvme|hd)", "écriture sur un disque"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "arrêt de la machine"),
    (r":\(\)\s*\{", "bombe fork"),
    (r"chmod\s+(-r\s+)?[0-7]*777\s+/", "droits ouverts sur le système"),
    (r"\bgit\s+(push|remote|config\s+--global|config\s+--system|credential)\b", "git sort du dossier de travail (push, remote, configuration globale)"),
    (r"\b(ssh|scp|sftp|rsync|nc|ncat|netcat|telnet)\b", "connexion réseau directe (ssh, scp, nc…)"),
    (r"\b(crontab|systemctl|launchctl|schtasks|reg\s+add|setx)\b", "modification du système (tâches planifiées, services, registre)"),
]

# Niveau ÉLEVÉ : (regex, explication)
HIGH = [
    (r"\b(rm|del|erase|rmdir|rd|remove-item|unlink|shred)\b|\bfind\b[^|;&\n]*-delete|shutil\.rmtree|\bgit\s+(clean|reset|checkout|rebase|stash|restore)\b", "SUPPRIME ou écrase des fichiers"),
    (r"\b(curl|wget|iwr|invoke-webrequest|invoke-restmethod|irm|ftp)\b|requests\.(post|put)|urllib\.request|fetch\(|http\.client", "accède à Internet (peut envoyer ou télécharger des données)"),
    (r"\.env\b|id_rsa|\.ssh\b|\.aws\b|\.npmrc|\.pypirc|\.netrc|credentials|\bprintenv\b|\benv\s*(\||>|$)|\$\{?\w*(token|secret|password|key)\w*\}?|%\w*(token|secret|password|key)\w*%",
     "touche à des secrets ou à des variables d’environnement"),
    (r"(^|[\s=:\"'(])(\.\./|~/|/etc/|/usr/|/var/|/home/|/root/|/tmp/|[a-z]:\\|%userprofile%|%appdata%|\$home\b)", "sort du dossier de travail"),
    (r"\bchmod\b|\bchown\b|\bicacls\b|\battrib\b", "modifie des droits d’accès"),
]

# Niveau MOYEN : (regex, explication)
MEDIUM = [
    (r"\b(pip3?|python3?\s+-m\s+pip|uv\s+pip|poetry|pipenv|npm|yarn|pnpm|bun|apt(-get)?|brew|choco|winget|cargo|go|composer|gem|bundle|dotnet|mvn|gradle|\./gradlew|gradlew)\s+(install|add|get|restore|ci|update|upgrade|i)\b",
     "télécharge des paquets depuis Internet"),
    (r"(^|[^>])>>?\s*\S|\btee\b|\bmkdir\b|\bmd\s|\bcp\b|\bcopy\b|\bmv\b|\bmove\b|\btouch\b|\bsed\s+-i|\bnew-item\b|\bset-content\b|\bout-file\b", "écrit ou déplace des fichiers"),
    (r"--fix\b|--write\b|\s-w\s|\bblack\b(?![^|;&\n]*--check)|\bisort\b(?![^|;&\n]*--check)|\bgofmt\s+-w|\bcargo\s+fmt\b(?![^|;&\n]*--check)|\bprettier\b(?![^|;&\n]*(--check|-c\b))",
     "peut MODIFIER tes fichiers (correction ou mise en forme automatique)"),
    (r"&&|\|\||;|\||`|\$\(", "enchaîne plusieurs commandes"),
]

LEVELS = ("faible", "moyenne", "élevée")


def analyze(command):
    """{level: faible|moyenne|élevée|refusée, flags: [explications], refused: bool}. Analyse du TEXTE seul (déterministe, sans IA) : c'est un garde-fou,
    pas une garantie ; la commande exacte reste montrée à l'utilisateur."""
    cmd = str(command or "").strip()
    if not cmd:
        return {"level": "refusée", "flags": ["commande vide"], "refused": True}
    if len(cmd) > 2000 or "\n" in cmd.strip() and len(cmd.splitlines()) > 20:
        return {"level": "refusée", "flags": ["commande trop longue : utilise un script du dépôt, examiné lors des validations"], "refused": True}
    low = cmd.lower()
    refused = [why for rx, why in REFUSED if re.search(rx, low)]
    if refused:
        return {"level": "refusée", "flags": refused, "refused": True}
    level, flags = 0, []
    for lv, rules in ((2, HIGH), (1, MEDIUM)):
        for rx, why in rules:
            if re.search(rx, low):
                level = max(level, lv)
                flags.append(why)
    first = re.split(r"[\s;&|]", low.lstrip("(`$ "), 1)[0].strip("\"'")
    if not flags and os.path.basename(first.replace("\\", "/")).removesuffix(".exe").removesuffix(".cmd") not in KNOWN and first not in KNOWN:
        level = max(level, 1)
        flags.append("programme non reconnu (« %s ») : vérifie ce qu’il fait" % first[:40])
    return {"level": LEVELS[level], "flags": flags, "refused": False}


def clean_env():
    return {k: os.environ[k] for k in KEEP_ENV if k in os.environ} | {"CI": "1", "PYTHONDONTWRITEBYTECODE": "1", "GIT_TERMINAL_PROMPT": "0"}


def tail(text, n=TAIL):
    text = str(text or "").strip()
    return text if len(text) <= n else "[…]\n" + text[-n:]


def _kill(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=20)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run(command, cwd, timeout=DEFAULT_TIMEOUT):
    """Exécute `command` dans `cwd` : {ok, rc, secs, timed_out, tail}. Une seule exécution à la fois ; arbre de processus tué au délai ; sortie
    (stdout+stderr) limitée aux derniers caractères ; environnement nettoyé."""
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    with CHECK_LOCK:
        t0 = time.time()
        kw = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt" else {"start_new_session": True}
        try:
            proc = subprocess.Popen(command, shell=True, cwd=cwd, env=clean_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, **kw)
        except OSError as e:
            return {"ok": False, "rc": -1, "secs": 0.0, "timed_out": False, "tail": "Lancement impossible : %s" % e}
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(proc)
            try:
                out, _ = proc.communicate(timeout=10)
            except Exception:
                out = b""
        text = (out or b"").decode("utf-8", errors="replace")
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
        if timed_out:
            text += "\n[délai de %d s dépassé : commande interrompue]" % timeout
        rc = proc.returncode if proc.returncode is not None else -1
        return {"ok": rc == 0 and not timed_out, "rc": rc, "secs": round(time.time() - t0, 1), "timed_out": timed_out, "tail": tail(text)}
