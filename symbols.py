"""Plan et lecture ciblée du code, SANS IA : l'application analyse les fichiers à la place de l'agent (économie de tokens).

Fonctions pures (aucun accès réseau ni disque) : l'appelant fournit les textes.
- `outline(path, text)` : liste des symboles d'un fichier (classes, fonctions, méthodes, constantes, sections Markdown, blocs <script>/<style>)
  avec leurs lignes de début et de fin ; None si le langage n'est pas reconnu. Python : module `ast` (exact) ; autres langages : heuristiques.
- `find(items, requete)` : retrouve un symbole par nom (« Classe.méthode », « méthode »…).
- `format_outline`, `repo_map` : textes compacts pour l'agent.
- `compact_diff(ancien, nouveau)` : différences d'un extrait déjà lu.
Bibliothèque standard uniquement."""
import ast
import difflib
import re

MAX_FILE = 2000000                     # au-delà, le fichier n'est pas analysé (l'analyse est locale et rapide : engine.py dépasse 200 000 caractères)
SKIP_DIRS = {".git", ".github", ".agents", ".myteam", "node_modules", "__pycache__", "venv", ".venv", "dist", "build", "data", "vendor", ".idea", ".vscode", ".pytest_cache"}
EXT_KIND = {".py": "py", ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".html": "html", ".htm": "html",
            ".md": "md", ".markdown": "md", ".go": "go", ".rs": "rs", ".java": "c", ".cs": "c", ".kt": "c", ".swift": "c", ".c": "c", ".h": "c",
            ".cpp": "c", ".hpp": "c", ".cc": "c", ".php": "c"}
KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "else", "new", "throw", "case", "yield", "await", "goto", "using", "delete",
            "foreach", "lock", "synchronized", "sizeof", "function", "typeof", "do", "try"}


def kind_of(path):
    """Langage d'un fichier d'après son extension (py, js, html, md, go, rs, c) ou None."""
    m = re.search(r"(\.[A-Za-z0-9]+)$", str(path or ""))
    return EXT_KIND.get(m.group(1).lower()) if m else None


def skip_dir(name):
    return name in SKIP_DIRS


# ---------------------------------------------------------------- Python (ast)

def _py(text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines = text.splitlines()
    out = []

    def sig(n, w=110):
        return lines[n.lineno - 1].strip()[:w] if 0 < n.lineno <= len(lines) else ""

    def walk(body, depth, inclass):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([n.lineno] + [d.lineno for d in n.decorator_list])
                kind = "class" if isinstance(n, ast.ClassDef) else ("method" if inclass else "def")
                out.append({"name": n.name, "kind": kind, "start": start, "end": n.end_lineno or n.lineno, "sig": sig(n)})
                if isinstance(n, ast.ClassDef):
                    walk(n.body, depth + 1, True)
            elif depth == 0 and isinstance(n, (ast.Assign, ast.AnnAssign)):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    if isinstance(t, ast.Name) and len(t.id) > 1 and t.id.isupper():
                        out.append({"name": t.id, "kind": "const", "start": n.lineno, "end": n.end_lineno or n.lineno, "sig": re.sub(r"\s+#.*$", "", sig(n, 200))[:70]})
                        break
    walk(tree.body, 0, False)
    return out


# ---------------------------------------------------------------- langages à accolades (JS/TS, Go, Rust, C…)

def _brace_end(lines, i):
    """Numéro (1-based) de la ligne où se ferme le premier bloc { } ouvert à partir de la ligne i (0-based). Chaînes et commentaires ignorés.
    Une déclaration terminée par « ; » avant toute accolade s'arrête à cette ligne. Sans fermeture trouvée : la ligne de départ."""
    depth, opened, quote, block = 0, False, "", False
    for j in range(i, min(len(lines), i + 4000)):
        s, k = lines[j], 0
        while k < len(s):
            c = s[k]
            if block:
                if s.startswith("*/", k):
                    block, k = False, k + 1
            elif quote:
                if c == "\\":
                    k += 1
                elif c == quote:
                    quote = ""
            elif s.startswith("//", k):
                break
            elif s.startswith("/*", k):
                block, k = True, k + 1
            elif c in "\"'`":
                quote = c
            elif c == "{":
                depth, opened = depth + 1, True
            elif c == "}":
                depth -= 1
                if opened and depth <= 0:
                    return j + 1
            elif c == ";" and not opened and depth == 0:
                return j + 1
            k += 1
        if quote in "\"'":
            quote = ""                                           # une chaîne simple ne continue pas à la ligne suivante
    return i + 1


JS_PATS = (
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*(?::\s*[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Z][A-Z0-9_]{2,})\s*="), "const"),
    (re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)"), "type"),
)
JS_METHOD = re.compile(r"^\s{2,}(?:(?:static|async|get|set|public|private|protected|readonly)\s+)*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::\s*[^{]+)?\{\s*$")


def _js(text):
    lines = text.splitlines()
    out = []
    for i, s in enumerate(lines):
        for rx, kind in JS_PATS:
            m = rx.match(s)
            if m:
                out.append({"name": m.group(1), "kind": kind, "start": i + 1, "end": _brace_end(lines, i), "sig": s.strip().rstrip("{").strip()[:110]})
                break
    classes = [it for it in out if it["kind"] == "class"]
    for i, s in enumerate(lines):                                    # méthodes : seulement à l'intérieur d'une classe
        m = JS_METHOD.match(s)
        if m and m.group(1) not in KEYWORDS and any(c["start"] < i + 1 <= c["end"] for c in classes):
            out.append({"name": m.group(1), "kind": "method", "start": i + 1, "end": _brace_end(lines, i), "sig": s.strip().rstrip("{").strip()[:110]})
    return out


GO_FUNC = re.compile(r"^func\s+(?:\(\s*(?:\w+\s+)?\*?(\w+)(?:\[[^\]]*\])?\s*\)\s*)?(\w+)")
GO_TYPE = re.compile(r"^type\s+(\w+)\s+(struct|interface)\b")
RS_ITEM = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(fn|struct|enum|trait|impl|mod)\s+([A-Za-z_]\w*)")


def _go(text):
    lines, out = text.splitlines(), []
    for i, s in enumerate(lines):
        m = GO_FUNC.match(s)
        if m:
            out.append({"name": (m.group(1) + "." + m.group(2)) if m.group(1) else m.group(2), "kind": "method" if m.group(1) else "func", "start": i + 1,
                        "end": _brace_end(lines, i), "sig": s.strip().rstrip("{").strip()[:110]})
            continue
        m = GO_TYPE.match(s)
        if m:
            out.append({"name": m.group(1), "kind": m.group(2), "start": i + 1, "end": _brace_end(lines, i), "sig": s.strip().rstrip("{").strip()[:110]})
    return out


def _rs(text):
    lines, out = text.splitlines(), []
    for i, s in enumerate(lines):
        m = RS_ITEM.match(s)
        if m:
            out.append({"name": m.group(2), "kind": m.group(1), "start": i + 1, "end": _brace_end(lines, i), "sig": s.strip().rstrip("{").strip()[:110]})
    return out


C_CLASS = re.compile(r"^\s*(?:(?:public|private|protected|internal|abstract|final|static|sealed|data|open|partial)\s+)*(class|interface|enum|struct|record|trait|object|namespace)\s+([A-Za-z_]\w*)")
C_FUNC = re.compile(r"^\s*(?:[\w<>\[\],.?*&:]+\s+)+([A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:const\s*)?(?:throws\s+[\w., ]+)?\s*\{?\s*$")
C_SPECIAL = re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract|override|open)\s+)*(?:function|fun|func)\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?(\w+)")


def _c(text):
    lines, out = text.splitlines(), []
    for i, s in enumerate(lines):
        m = C_CLASS.match(s)
        if m:
            out.append({"name": m.group(2), "kind": m.group(1), "start": i + 1, "end": _brace_end(lines, i), "sig": s.strip().rstrip("{").strip()[:110]})
            continue
        m = C_SPECIAL.match(s)
        if not m:
            m = C_FUNC.match(s)
            if m:
                nxt = next((x for x in lines[i + 1:i + 3] if x.strip()), "")
                first = s.strip().split(None, 1)[0]
                if not (s.rstrip().endswith("{") or nxt.lstrip().startswith("{")) or first in KEYWORDS or m.group(1) in KEYWORDS:
                    m = None
        if m:
            out.append({"name": m.group(1), "kind": "function", "start": i + 1, "end": _brace_end(lines, i), "sig": s.strip().rstrip("{").strip()[:110]})
    return out


# ---------------------------------------------------------------- Markdown et HTML

def _md(text):
    lines, heads, fence = text.splitlines(), [], False
    for i, s in enumerate(lines):
        if s.lstrip().startswith(("```", "~~~")):
            fence = not fence
        elif not fence:
            m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", s)
            if m:
                heads.append((i + 1, len(m.group(1)), m.group(2)))
    out = []
    for k, (ln, lvl, title) in enumerate(heads):
        end = len(lines)
        for ln2, lvl2, _ in heads[k + 1:]:
            if lvl2 <= lvl:
                end = ln2 - 1
                break
        out.append({"name": title, "kind": "section", "start": ln, "end": max(end, ln), "sig": "#" * lvl + " " + title})
    return out


def _html(text):
    out = []
    for m in re.finditer(r"<(script|style)\b([^>]*)>(.*?)</\1\s*>", text, re.S | re.I):
        tag, attrs = m.group(1).lower(), m.group(2)
        start = text.count("\n", 0, m.start()) + 1
        end = text.count("\n", 0, m.end()) + 1
        out.append({"name": tag + (" (externe)" if "src=" in attrs else ""), "kind": "bloc", "start": start, "end": end, "sig": "<%s>" % tag})
        if tag == "script" and "src=" not in attrs and m.group(3).strip():
            off = text.count("\n", 0, m.start(3))
            for it in _js(m.group(3)):
                it["start"] += off
                it["end"] += off
                out.append(it)
    return out


def _qualify(items):
    """Imbrication par inclusion de lignes : `depth` et `qual` (Classe.méthode)."""
    items.sort(key=lambda i: (i["start"], -i["end"]))
    stack = []
    for it in items:
        while stack and not (it["start"] >= stack[-1]["start"] and it["end"] <= stack[-1]["end"]):
            stack.pop()
        it["depth"] = len(stack)
        it["qual"] = (stack[-1]["qual"] + "." if stack else "") + it["name"]
        stack.append(it)
    return items


def outline(path, text):
    """Symboles de `text` (fichier `path`), triés par ligne : [{name, qual, kind, start, end, sig, depth}] ; None si le langage n'est pas reconnu
    (ou fichier trop gros). Un fichier Python à erreur de syntaxe donne None : l'agent lit le fichier normalement."""
    k = kind_of(path)
    if not k or len(text) > MAX_FILE:
        return None
    items = {"py": _py, "js": _js, "html": _html, "md": _md, "go": _go, "rs": _rs, "c": _c}[k](text)
    return None if items is None else _qualify(items)


def find(items, query):
    """Symboles correspondant à `query` : nom qualifié exact, puis nom simple ou suffixe « Classe.méthode », puis sans tenir compte de la casse."""
    q = str(query or "").strip()
    if not q:
        return []
    for ok in (lambda i: i["qual"] == q,
               lambda i: i["name"] == q or i["qual"].endswith("." + q),
               lambda i: i["qual"].lower() == q.lower() or i["name"].lower() == q.lower() or i["qual"].lower().endswith("." + q.lower())):
        r = [i for i in items if ok(i)]
        if r:
            return r
    return []


def close_names(items, query, n=5):
    """Noms proches (faute de frappe) pour aider l'agent quand le symbole demandé n'existe pas."""
    names = sorted({i["qual"] for i in items} | {i["name"] for i in items})
    return difflib.get_close_matches(str(query or ""), names, n=n, cutoff=0.5)


def format_outline(items, total_lines, maxdepth=2, limit=220):
    """Une ligne par symbole : « L12-48  def f(a, b): », indentée selon l'imbrication."""
    shown = [i for i in items if i["depth"] <= maxdepth]
    rows = ["%sL%d-%d  %s" % ("  " * min(i["depth"], maxdepth), i["start"], i["end"], i["sig"] or i["name"]) for i in shown[:limit]]
    if len(shown) > limit:
        rows.append("[… %d symboles de plus : lis la zone voulue avec from_line/to_line, ou cherche avec find_symbol]" % (len(shown) - limit))
    hid = len(items) - len(shown)
    if hid > 0:
        rows.append("[%d symbole(s) imbriqué(s) plus profondément non listé(s) : read_symbol les retrouve par leur nom]" % hid)
    return "\n".join(rows) or "(aucun symbole repéré)"


def repo_map(entries, others=(), budget=9000):
    """Plan compact du dépôt. entries : [(chemin, items|None, nb_lignes)] ; others : noms des fichiers non analysables.
    Une ligne par fichier : « chemin (N l.) : classe A{3}, def f, g… »."""
    lines, used, skipped = [], 0, 0
    for path, items, nl in entries:
        top = [i for i in (items or []) if i["depth"] == 0]
        names = []
        for i in top:
            kids = sum(1 for j in items if j["depth"] == 1 and j["qual"].startswith(i["qual"] + ".")) if i["kind"] == "class" else 0
            names.append(("%s{%d}" % (i["name"], kids)) if kids else i["name"])
        txt = ", ".join(names)
        if len(txt) > 260:
            cut = txt[:260].rsplit(", ", 1)[0]
            txt = cut + ", … (+%d)" % (len(names) - cut.count(", ") - 1)
        row = "%s (%d l.)%s" % (path, nl, (" : " + txt) if txt else "")
        if used + len(row) > budget:
            skipped += 1
            continue
        lines.append(row)
        used += len(row) + 1
    if skipped:
        lines.append("[… %d fichier(s) de plus non listés : demande le plan d'un sous-dossier avec outline path=\"<dossier>\"]" % skipped)
    if others:
        o = list(others)
        lines.append("Autres fichiers : " + ", ".join(o[:30]) + ((" … (+%d)" % (len(o) - 30)) if len(o) > 30 else ""))
    return "\n".join(lines) or "(aucun fichier source reconnu)"


def compact_diff(old, new, context=2):
    """Différences (diff unifié, lignes -ancien/+nouveau) entre deux extraits ; '' si identiques."""
    d = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "avant", "après", lineterm="", n=context))
    return "\n".join(d)
