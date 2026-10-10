"""Per-language extraction: source text -> symbols and imports.

Each language implements the same small interface (`extract(text) ->
Extracted`), so a better parser can replace one without anything else
noticing.

* Python uses the standard-library `ast` — exact.
* Dart and JavaScript have no stdlib parser, so those are line-based
  heuristics. The Dart one It is
  reliable for what Dart code in this repo actually looks like (imports,
  classes, top-level and member functions declared on one line) and knowingly
  misses signatures split across lines before the `(`, and anything inside
  string literals that looks like code. Swap it for tree-sitter-dart if the
  gaps start to matter; `test_extractors.py` pins the cases it must handle.
  The JavaScript one covers the admin panel's React/JSX style — ES imports,
  `function` declarations, `const X = (...) =>` components and hooks, and
  classes — and misses the same multi-line-signature shapes.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field


@dataclass
class Symbol:
    kind: str
    name: str
    qualname: str
    parent: str | None
    start_line: int
    end_line: int
    signature: str | None = None
    doc: str | None = None
    body_hash: str | None = None

    @property
    def is_private(self) -> bool:
        return self.name.startswith("_")


@dataclass
class Import:
    target: str
    line: int
    is_relative: bool
    level: int = 0          # python relative-import depth
    name: str | None = None  # python: the `name` in `from M import name`
    alias: str | None = None  # python: `as alias`
    scope: str = "module"    # python: module | function | type_checking | try
    kind: str = "import"     # import | export | part (dart part directive)


@dataclass
class Extracted:
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    error: str | None = None
    doc: str | None = None    # the file's own opening doc, first line


# Comment lines at the top of a file that are tooling, not documentation.
_PRAGMA = re.compile(r"^(//|#|/\*+)\s*(ignore|ignore_for_file|GENERATED|coverage:|eslint|@ts-|prettier|noqa|type:|-\*-)", re.I)


def _file_doc(text: str) -> str | None:
    """The file's own doc — the opening comment, unless that comment sits
    directly on the first declaration, in which case it documents that
    declaration (and is already recorded on its symbol), not the file."""
    doc = leading_comment(text)
    if doc is None:
        return None
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].strip().startswith(("//", "/*", "*")) or _PRAGMA.match(lines[i].strip())):
        i += 1
    # the comment block ended; if code follows with no blank line, it was the declaration's doc
    if i < len(lines) and i > 0 and lines[i - 1].strip() and not lines[i].lstrip().startswith(("import", "export", "part", "library")):
        return None
    return doc


def leading_comment(text: str) -> str | None:
    """First line of the comment block that opens a Dart or JavaScript file,
    before any code. Lint pragmas and generated-file banners are skipped:
    they describe the tooling, not the file."""
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if _PRAGMA.match(s):
            continue
        if s.startswith(("///", "//")):
            body = s.lstrip("/").strip()
            if body:
                return body[:200]
            continue
        if s.startswith(("/*", "*")):
            body = s.lstrip("/*").rstrip("*/").strip()
            if body:
                return body[:200]
            continue
        return None
    return None


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- python

class _Normaliser(ast.NodeTransformer):
    """Strips the parts of a function body that don't change what it does —
    names, docstrings, constants' values — so two copies of the same logic
    hash identically even after a rename. Used by duplicate detection."""

    def visit_Name(self, node):
        return ast.copy_location(ast.Name(id="_", ctx=node.ctx), node)

    def visit_arg(self, node):
        node.arg = "_"
        node.annotation = None
        return node

    def visit_Constant(self, node):
        return ast.copy_location(ast.Constant(value=type(node.value).__name__), node)

    def visit_Attribute(self, node):
        self.generic_visit(node)
        node.attr = "_" if node.attr.startswith("_") else node.attr
        return node


def _python_body_hash(fn: ast.AST) -> str | None:
    body = list(getattr(fn, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]              # drop the docstring
    if not body:
        return None
    try:
        module = ast.Module(body=[_Normaliser().visit(ast.parse(ast.unparse(s)).body[0]) for s in body],
                            type_ignores=[])
        return _hash(ast.dump(module, annotate_fields=False))
    except Exception:
        return None


def _first_doc_line(node) -> str | None:
    doc = ast.get_docstring(node)
    return doc.strip().splitlines()[0][:200] if doc else None


def extract_python(text: str) -> Extracted:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return Extracted(error=f"SyntaxError line {exc.lineno}: {exc.msg}")

    out = Extracted()
    module_doc = ast.get_docstring(tree)
    out.doc = module_doc.strip().splitlines()[0][:200] if module_doc and module_doc.strip() else None

    def visit(body, parent: str | None):
        for node in body:
            if isinstance(node, ast.ClassDef):
                out.symbols.append(Symbol(
                    "class", node.name, node.name if not parent else f"{parent}.{node.name}", parent,
                    node.lineno, node.end_lineno or node.lineno, None, _first_doc_line(node)))
                visit(node.body, node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if parent else "function"
                out.symbols.append(Symbol(
                    kind, node.name, f"{parent}.{node.name}" if parent else node.name, parent,
                    node.lineno, node.end_lineno or node.lineno,
                    ast.unparse(node.args), _first_doc_line(node), _python_body_hash(node)))

    visit(tree.body, None)

    def walk_imports(node, scope: str) -> None:
        """Record every import with the context it sits in. Imports that are
        not at module level matter to the dependency graph: they are real
        dependencies, but usually placed inside a function on purpose, to
        break an import cycle (see extraction/jobs.py)."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    out.imports.append(Import(alias.name, child.lineno, False, scope=scope))
            elif isinstance(child, ast.ImportFrom):
                # One row per imported name: `from . import reconciler, shutdown`
                # depends on reconciler.py and shutdown.py, not on __init__.py.
                for alias in child.names:
                    out.imports.append(Import(child.module or "", child.lineno, child.level > 0,
                                              child.level, alias.name, alias.asname, scope))
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                inner = "function"
            elif scope == "module" and isinstance(child, ast.If) and "TYPE_CHECKING" in ast.unparse(child.test):
                inner = "type_checking"
            elif scope == "module" and isinstance(child, ast.Try):
                inner = "try"
            walk_imports(child, inner)

    walk_imports(tree, "module")
    out.imports.sort(key=lambda i: i.line)
    return out


# ------------------------------------------- shared heuristic machinery

def sanitize(text: str, backticks: bool = False) -> list[str]:
    """Source lines with string contents and comments replaced by spaces,
    column positions preserved. Every heuristic below reads these, so a
    brace in a comment or `foo() {` inside a string can never look like code.

    Handles '...', "...", triple-quoted strings, // and /* */ comments,
    escapes, and (for JavaScript) multi-line template literals. A template
    literal's `${...}` is blanked with it — its braces are balanced, so
    dropping them leaves block matching unchanged.
    """
    out, state, quote = [], None, ""
    for line in text.splitlines():
        buf, i, n = list(line), 0, len(line)
        while i < n:
            ch = line[i]
            if state == "block":
                if line.startswith("*/", i):
                    buf[i] = buf[i + 1] = " "
                    state, i = None, i + 2
                    continue
                buf[i] = " "
            elif state == "str":
                if ch == "\\":
                    buf[i] = " "
                    if i + 1 < n:
                        buf[i + 1] = " "
                    i += 2
                    continue
                if line.startswith(quote, i):
                    i += len(quote)
                    state = None
                    continue
                buf[i] = " "
            else:
                if line.startswith("//", i):
                    for k in range(i, n):
                        buf[k] = " "
                    break
                if line.startswith("/*", i):
                    buf[i] = buf[i + 1] = " "
                    state, i = "block", i + 2
                    continue
                if ch in "'\"" or (backticks and ch == "`"):
                    # JavaScript only: a string literal can never directly
                    # follow an identifier character (`foo'x'` is a syntax
                    # error), so a quote that does is JSX text — `can't`,
                    # `it's`. Reading it as a string blanked the rest of the
                    # line, braces included, and unbalanced a whole component.
                    # Not applied to Dart, where r'...' raw strings put a
                    # quote right after a letter on purpose.
                    if backticks and ch != "`" and i > 0 and (line[i - 1].isalnum() or line[i - 1] == "_"):
                        i += 1
                        continue
                    quote = ch * 3 if line.startswith(ch * 3, i) and ch != "`" else ch
                    state, i = "str", i + len(quote)
                    continue
            i += 1
        # a single-quoted string cannot cross a line; only triple quotes,
        # backticks and block comments carry state onward
        if state == "str" and len(quote) == 1 and quote != "`":
            state = None
        out.append("".join(buf))
    return out


_OPEN, _CLOSE = "([{", ")]}"


def match_paren(clean: list[str], line: int, col: int) -> tuple[int, int] | None:
    """Position just after the bracket matching the one at (line, col), or
    None if it never closes. Deliberately unbounded: an earlier 60-line cap
    made every body longer than that "run to end of file", which silently
    hid every declaration after it."""
    depth = 0
    for li in range(line, len(clean)):
        row = clean[li]
        for ci in range(col if li == line else 0, len(row)):
            if row[ci] in _OPEN:
                depth += 1
            elif row[ci] in _CLOSE:
                depth -= 1
                if depth == 0:
                    return li, ci + 1
    return None


def _skip_ws(clean: list[str], line: int, col: int) -> tuple[int, int]:
    while line < len(clean):
        row = clean[line]
        while col < len(row) and row[col].isspace():
            col += 1
        if col < len(row):
            return line, col
        line, col = line + 1, 0
    return line, col


def body_end(clean: list[str], line: int, col: int) -> tuple[str, int] | None:
    """Read what follows a declaration's closing paren.

    Returns (kind, end_line) where kind is 'block' | 'expr' | 'decl', or None
    when what follows is not a body at all — which is how a call such as
    `setState(() {...})` or a function-typed field is told apart from a
    declaration.
    """
    line, col = _skip_ws(clean, line, col)
    for mod in ("async*", "async", "sync*"):
        if line < len(clean) and clean[line].startswith(mod, col):
            line, col = _skip_ws(clean, line, col + len(mod))
            break
    if line >= len(clean):
        return None
    row = clean[line]
    if row.startswith("{", col):
        m = match_paren(clean, line, col)
        # Unmatched: claim just this line. Failing safe matters more than
        # being complete — claiming "to end of file" hides everything after.
        return ("block", m[0]) if m else ("block", line)
    if row.startswith("=>", col) or row.startswith(":", col):
        # expression body or initializer list: runs to the `;` at depth 0,
        # unless an initializer list hands over to a `{` block
        depth, initializer = 0, row.startswith(":", col)
        for li in range(line, len(clean)):
            r = clean[li]
            for ci in range(col + 1 if li == line else 0, len(r)):
                ch = r[ci]
                if ch in _OPEN:
                    if ch == "{" and depth == 0 and initializer:
                        m = match_paren(clean, li, ci)
                        return ("block", m[0]) if m else ("block", li)
                    depth += 1
                elif ch in _CLOSE:
                    depth -= 1
                elif ch == ";" and depth == 0:
                    return ("expr", li)
        return ("expr", line)          # never terminated: fail safe, as above
    if row.startswith(";", col):
        return ("decl", line)
    return None


def _norm_sig(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip()[1:-1]).strip().rstrip(",").strip()


def _body_hash(clean: list[str], start: int, end: int) -> str | None:
    body = "\n".join(clean[start + 1:end])
    if not body.strip():
        return None
    return _hash(re.sub(r"\s+", "", re.sub(r"\b[A-Za-z_$][\w$]*\b", "_", body)))


# ------------------------------------------------------------------ dart

_DART_IMPORT = re.compile(r"""^\s*(?P<kind>import|export|part)\s+['"]([^'"]+)['"]""")
_DART_CLASS = re.compile(
    r"^\s*(?:abstract\s+|sealed\s+|base\s+|final\s+|interface\s+)*"
    r"(class|mixin|enum|extension)\s+([A-Za-z_]\w*)")
_DART_EXT_ON = re.compile(r"^\s*extension\s+([A-Za-z_]\w*)?\s*on\s+")
# `ReturnType name(` — a declaration needs a type in front of its name.
# That is the line between `Widget build(BuildContext c) {` and the call
# `setState(() {`, which have the same shape otherwise.
_DART_FUNC_HEAD = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:static|external|abstract|covariant)\s+)*"
    r"(?P<type>[A-Za-z_][\w<>?,\s\[\].]*?[\w>?\]])\s+(?P<name>[A-Za-z_]\w*)\s*(?P<open>\()")
_DART_GETTER = re.compile(
    r"^\s*(?:@\w+\s+)*(?:static\s+)?(?:[A-Za-z_][\w<>?,\s\[\].]*?[\w>?\]]\s+)?(?P<acc>get|set)\s+(?P<name>[A-Za-z_]\w*)\b")
_DART_MAIN = re.compile(r"^\s*(?P<name>main)\s*(?P<open>\()")
_DART_OPERATOR = re.compile(
    r"^\s*(?:@\w+\s+)*(?:[A-Za-z_][\w<>?,\s\[\].]*?[\w>?\]]\s+)?operator\s*(?P<op>==|<=|>=|\[\]=|\[\]|[-+*/%<>~^|&]|~/|<<|>>>?|unary-)\s*(?P<open>\()")
_DART_KEYWORDS = {"return", "await", "else", "new", "throw", "case", "final", "var", "const",
                  "yield", "if", "for", "while", "switch", "catch", "do", "try", "assert", "in",
                  "is", "as", "late", "required", "super", "this", "default", "import", "export"}


def _dart_doc(lines: list[str], i: int) -> str | None:
    j = i - 1
    while j >= 0 and lines[j].strip().startswith("@"):
        j -= 1
    doc = []
    while j >= 0 and lines[j].strip().startswith("///"):
        doc.append(lines[j].strip()[3:].strip())
        j -= 1
    doc.reverse()
    return doc[0][:200] if doc and doc[0] else None


def _raw_sig(lines: list[str], line: int, open_col: int, close: tuple[int, int]) -> str:
    """The parameter list exactly as written, joined onto one line."""
    if close[0] == line:
        return lines[line][open_col:close[1]]
    middle = " ".join(ln.strip() for ln in lines[line + 1:close[0]])
    return f"{lines[line][open_col:]} {middle} {lines[close[0]][:close[1]].strip()}"


def _dart_class_end(clean: list[str], i: int) -> int:
    """A class header may wrap (`extends ...` / `with ...` on later lines)
    before its `{`; look a few lines ahead for it."""
    for hl in range(i, min(i + 10, len(clean))):
        hc = clean[hl].find("{")
        if hc >= 0:
            m = match_paren(clean, hl, hc)
            return m[0] if m else hl
        if clean[hl].rstrip().endswith(";"):     # e.g. `mixin X on Y;` / typedef-like
            return hl
    return i


def _dart_declaration(row: str, parent: str | None) -> tuple[str, str, int] | None:
    """(name, kind, column of the opening paren) for a declaration head on
    this line, or None. Constructors are checked first inside a class, since
    `factory Foo(` would otherwise read as a method of return type `factory`."""
    if parent:
        ctor = re.match(rf"^\s*(?:const\s+|factory\s+|external\s+)*(?P<name>{re.escape(parent)}(?:\.\w+)?)\s*\(", row)
        if ctor:
            return ctor.group("name"), "constructor", ctor.end() - 1
    if parent and (om := _DART_OPERATOR.match(row)):
        return f"operator{om.group('op')}", "method", om.start("open")
    fm = _DART_FUNC_HEAD.match(row)
    if fm:
        words = fm.group("type").split()
        if words[0] not in _DART_KEYWORDS and words[-1] not in _DART_KEYWORDS \
                and fm.group("name") not in _DART_KEYWORDS:
            return fm.group("name"), "method" if parent else "function", fm.start("open")
    if not parent and (mm := _DART_MAIN.match(row)):
        return "main", "function", mm.start("open")
    return None


def extract_dart(text: str) -> Extracted:
    out = Extracted(doc=_file_doc(text))
    lines = text.splitlines()
    clean = sanitize(text)
    classes: list[tuple[str, int]] = []      # (name, end line index)
    i = 0
    while i < len(lines):
        while classes and i > classes[-1][1]:
            classes.pop()
        parent = classes[-1][0] if classes else None
        line, row = lines[i], clean[i]

        if m := _DART_IMPORT.match(line):
            target = m.group(2)
            out.imports.append(Import(target, i + 1, not target.startswith(("package:", "dart:")),
                                      kind=m.group("kind")))
            i += 1
            continue
        if not row.strip():
            i += 1
            continue

        if cm := _DART_CLASS.match(row):
            name = cm.group(2)
            if cm.group(1) == "extension" and (em := _DART_EXT_ON.match(row)):
                name = em.group(1) or f"extension@{i + 1}"     # unnamed extension
            end = _dart_class_end(clean, i)
            out.symbols.append(Symbol("class", name, f"{parent}.{name}" if parent else name, parent,
                                      i + 1, end + 1, None, _dart_doc(lines, i)))
            classes.append((name, end))
            i += 1
            continue

        if decl := _dart_declaration(row, parent):
            name, kind, open_col = decl
            close = match_paren(clean, i, open_col)
            tail = body_end(clean, *close) if close else None
            if tail:
                if kind == "constructor":
                    qual = name                          # Foo / Foo.fromJson
                    name = name.split(".")[-1]
                else:
                    qual = f"{parent}.{name}" if parent else name
                out.symbols.append(Symbol(
                    kind, name, qual, parent, i + 1, tail[1] + 1,
                    _norm_sig(_raw_sig(lines, i, open_col, close)), _dart_doc(lines, i),
                    _body_hash(clean, close[0], tail[1]) if tail[0] != "decl" else None))
                # Skip the body: Dart has no nested classes, and functions
                # local to a body are not indexed (the Python extractor skips
                # them too, so both languages index the same shape).
                i = tail[1] + 1
                continue

        if gm := _DART_GETTER.match(row):
            if gm.group("acc") == "set":
                po = row.find("(", gm.end())
                close = match_paren(clean, i, po) if po >= 0 else None
                tail = body_end(clean, *close) if close else None
            else:
                tail = body_end(clean, i, gm.end())
            if tail:
                name = gm.group("name")
                out.symbols.append(Symbol(
                    "method" if parent else "function", name, f"{parent}.{name}" if parent else name,
                    parent, i + 1, tail[1] + 1, None, _dart_doc(lines, i),
                    _body_hash(clean, i, tail[1]) if tail[0] != "decl" else None))
                i = tail[1] + 1
                continue
        i += 1
    return out


# ------------------------------------------------------------ javascript

_JS_IMPORT = re.compile(r"""^\s*import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""")
_JS_REEXPORT = re.compile(r"""^\s*export\s+[^'"]*?\s+from\s+['"]([^'"]+)['"]""")
_JS_FUNC_HEAD = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)\s*(?P<open>\()")
_JS_ARROW_HEAD = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?P<open>\()")
_JS_ARROW_ONEARG = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?[A-Za-z_$][\w$]*\s*=>")
_JS_CLASS = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")
_JS_METHOD_HEAD = re.compile(r"^\s*(?:static\s+)?(?:async\s+)?(?P<name>[A-Za-z_$#][\w$]*)\s*(?P<open>\()")
_JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "else", "do", "try", "super",
                "this", "new", "await", "throw", "typeof", "function"}


def _js_doc(lines: list[str], i: int) -> str | None:
    j = i - 1
    if j >= 0 and lines[j].strip().endswith("*/"):
        while j >= 0 and not lines[j].strip().startswith("/*"):
            j -= 1
        if j >= 0:
            for ln in lines[j:i]:
                s = ln.strip().lstrip("/*").strip().rstrip("*/").strip()
                if s:
                    return s[:200]
    if j >= 0 and lines[j].strip().startswith("//"):
        return lines[j].strip()[2:].strip()[:200] or None
    return None


def _js_arrow_tail(clean: list[str], line: int, col: int):
    """After an arrow's parameter list: `=>` then a block or an expression."""
    line, col = _skip_ws(clean, line, col)
    if line >= len(clean) or not clean[line].startswith("=>", col):
        return None
    line, col = _skip_ws(clean, line, col + 2)
    if line >= len(clean):
        return None
    if clean[line].startswith("{", col):
        m = match_paren(clean, line, col)
        return ("block", m[0]) if m else None
    # expression body (`=> (<div/>)` or a value): to the end of the statement
    # No semicolons in this codebase's style, so a statement also ends at a
    # line where brackets balance and the next line does not continue it.
    depth = 0
    for li in range(line, len(clean)):
        r = clean[li]
        for ci in range(col if li == line else 0, len(r)):
            ch = r[ci]
            if ch in _OPEN:
                depth += 1
            elif ch in _CLOSE:
                depth -= 1
                if depth < 0:
                    return ("expr", li)
            elif ch == ";" and depth == 0:
                return ("expr", li)
        nxt = clean[li + 1].strip() if li + 1 < len(clean) else ""
        if depth == 0 and not nxt.startswith((".", "?", ":", "&&", "||", "+", "-", "*", "/")):
            return ("expr", li)
    return ("expr", line)


def extract_javascript(text: str) -> Extracted:
    out = Extracted(doc=_file_doc(text))
    lines = text.splitlines()
    clean = sanitize(text, backticks=True)
    classes: list[tuple[str, int]] = []
    i = 0
    while i < len(lines):
        while classes and i > classes[-1][1]:
            classes.pop()
        parent = classes[-1][0] if classes else None
        line, row = lines[i], clean[i]

        if m := (_JS_IMPORT.match(line) or _JS_REEXPORT.match(line)):
            target = m.group(1)
            out.imports.append(Import(target, i + 1, target.startswith("."),
                                      kind="import" if line.lstrip().startswith("import") else "export"))
            i += 1
            continue
        if not row.strip():
            i += 1
            continue

        if m := _JS_CLASS.match(row):
            hc = row.find("{")
            mm = match_paren(clean, i, hc) if hc >= 0 else None
            end = mm[0] if mm else i
            out.symbols.append(Symbol("class", m.group(1), m.group(1), None, i + 1, end + 1,
                                      None, _js_doc(lines, i)))
            classes.append((m.group(1), end))
            i += 1
            continue

        # Each form yields: name, kind, signature, the line its params end on, body tail.
        hit = None
        if fm := _JS_FUNC_HEAD.match(row):
            close = match_paren(clean, i, fm.start("open"))
            if close and (tail := body_end(clean, *close)) and tail[0] == "block":
                hit = (fm.group("name"), "function", _raw_sig(lines, i, fm.start("open"), close), close[0], tail)
        elif am := _JS_ARROW_HEAD.match(row):
            close = match_paren(clean, i, am.start("open"))
            if close and (tail := _js_arrow_tail(clean, *close)):
                hit = (am.group("name"), "function", _raw_sig(lines, i, am.start("open"), close), close[0], tail)
        elif om := _JS_ARROW_ONEARG.match(row):
            arrow = row.find("=>", om.start("name"))
            if tail := _js_arrow_tail(clean, i, arrow):
                param = row[:arrow].split("=", 1)[1].replace("async", "").strip()
                hit = (om.group("name"), "function", f"({param})", i, tail)
        elif parent and (mm := _JS_METHOD_HEAD.match(row)) and mm.group("name") not in _JS_KEYWORDS:
            close = match_paren(clean, i, mm.start("open"))
            if close and (tail := body_end(clean, *close)) and tail[0] == "block":
                hit = (mm.group("name"), "method", _raw_sig(lines, i, mm.start("open"), close), close[0], tail)

        if hit and hit[0] not in _JS_KEYWORDS:
            name, kind, raw, params_end, tail = hit
            out.symbols.append(Symbol(
                kind, name, f"{parent}.{name}" if parent else name, parent, i + 1, tail[1] + 1,
                _norm_sig(raw), _js_doc(lines, i), _body_hash(clean, params_end, tail[1])))
            # Same rule as Dart and Python: index declarations, not the
            # handlers and helpers defined inside a component's body.
            i = tail[1] + 1 if not parent else i + 1
            continue
        i += 1
    return out


EXTRACTORS = {"python": extract_python, "dart": extract_dart, "javascript": extract_javascript}
