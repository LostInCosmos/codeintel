"""Phase 3a — the module registry.

One generated document per repo saying what each module is for, so whoever
adds code — a person or an agent — can find its home first.

Every word of description comes from the code itself, verbatim, with its
source shown. Nothing is summarised or invented: where a module or file says
nothing about itself, the registry says nothing either, and counts the gap.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from . import architecture, graph


@dataclass
class Symbol:
    kind: str
    name: str
    doc: str | None


@dataclass
class FileEntry:
    path: str
    lines: int
    purpose: str | None           # verbatim first doc line
    purpose_from: str | None      # "file" or the declaration it came from
    public: list[Symbol] = field(default_factory=list)


@dataclass
class Module:
    folder: str
    group: str
    statement: str | None         # the module's own word on its purpose (package __init__ docstring)
    files: list[FileEntry]
    depends_on: dict[str, int] = field(default_factory=dict)
    used_by: dict[str, int] = field(default_factory=dict)

    @property
    def public(self) -> list[Symbol]:
        return [s for f in self.files for s in f.public]


def _snake(name: str) -> str:
    """CaptureScreen / captureScreen / capture_screen -> capture_screen."""
    import re
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).replace("-", "_").lower()


def _main_declaration_doc(path: str, public: list[Symbol], top: list[Symbol]) -> tuple[str | None, str | None]:
    """Borrow a declaration's doc as the file's purpose only when that
    declaration IS the file: the one public declaration it is named after
    (capture_screen.dart / CaptureScreen — Dart's own convention), or the
    file's only top-level declaration of any kind.

    "Only *public* declaration" was not enough: invoice_items.py's one public
    function is a helper, while the file is really its private validator
    class. Borrowing would misstate what the file is for, so it says nothing.
    """
    stem = _snake(path.rsplit("/", 1)[-1].split(".")[0])
    named = [s for s in public if _snake(s.name) == stem]
    main = named[0] if named else (top[0] if len(top) == 1 else None)
    if main and main.doc:
        return main.doc, main.name
    return None, None


def _names_cell(names: list[str], limit: int = 5) -> str:
    """Distinct names, repeats counted: thirteen scripts each defining `run`
    read as `run` ×13, not as `run, run, run, run, run`."""
    counts: dict[str, int] = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    items = [f"`{n}`" + (f" ×{c}" if c > 1 else "") for n, c in counts.items()]
    return ", ".join(items[:limit]) + (f" +{len(items) - limit}" if len(items) > limit else "")


def _folder(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "."


def modules(conn: sqlite3.Connection, repo: str) -> list[Module]:
    """Every source folder of a repo as a module — tests excluded: the
    registry answers where new *code* goes."""
    level = architecture.DEFAULT_LEVEL.get(repo, 2)
    files = conn.execute("""SELECT id, path, lines, doc FROM files
                            WHERE repo = ? AND is_test = 0 ORDER BY path""", (repo,)).fetchall()
    by_folder: dict[str, list[FileEntry]] = defaultdict(list)
    statements: dict[str, str] = {}
    for f in files:
        syms = conn.execute("""SELECT kind, name, doc, is_private FROM symbols
                               WHERE file_id = ? AND parent IS NULL ORDER BY start_line""", (f["id"],)).fetchall()
        top = [Symbol(s["kind"], s["name"], s["doc"]) for s in syms]
        public = [Symbol(s["kind"], s["name"], s["doc"]) for s in syms if not s["is_private"]]
        purpose, source = (f["doc"], "file") if f["doc"] else _main_declaration_doc(f["path"], public, top)
        folder = _folder(f["path"])
        if f["path"].endswith("/__init__.py") and f["doc"]:
            statements[folder] = f["doc"]
        by_folder[folder].append(FileEntry(f["path"], f["lines"], purpose, source, public))

    # module-to-module dependencies at folder granularity, design view
    deps: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    users: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for (s, d) in graph.edges(conn, repo, "design", include_tests=False):
        fs, fd = _folder(s), _folder(d)
        if fs != fd:
            deps[fs][fd] += 1
            users[fd][fs] += 1

    out = []
    for folder, entries in sorted(by_folder.items()):
        # A folder holding nothing but an empty __init__.py is scaffolding
        # that makes a package importable, not a module anyone puts code in.
        if all(e.path.endswith("__init__.py") and not e.public and not e.purpose for e in entries):
            continue
        out.append(Module(folder, graph.package_of(entries[0].path, level), statements.get(folder),
                          entries, dict(deps.get(folder, {})), dict(users.get(folder, {}))))
    return out


def coverage(mods: list[Module]) -> dict[str, int]:
    files = [f for m in mods for f in m.files]
    return {
        "modules": len(mods),
        "modules_with_statement": sum(1 for m in mods if m.statement),
        "files": len(files),
        "files_with_purpose": sum(1 for f in files if f.purpose),
        "files_with_own_doc": sum(1 for f in files if f.purpose_from == "file"),
    }


# ------------------------------------------------------------------ render

def _anchor(folder: str) -> str:
    return folder


def render(repo: str, mods: list[Module]) -> str:
    """The registry as Markdown: an index first, then one section per
    module, every section the same shape so it reads like a table of
    contents rather than a wall of text."""
    cov = coverage(mods)
    L = [
        f"# Module registry — {repo}",
        "",
        "> **Generated by `codeintel registry` — do not edit by hand; regenerate after changes.**",
        "> Every description below is quoted from the code, with its source in brackets.",
        "> Nothing is summarised or invented, so a blank means the code says nothing about it.",
        "",
        "## Where things live",
        "",
        "Find the module whose names fit what you are adding, then read its section.",
        "",
        "| module | what is in it | files | used by |",
        "|---|---|---|---|",
    ]
    for group in sorted({m.group for m in mods}):
        for m in (x for x in mods if x.group == group):
            shown = _names_cell([s.name for s in m.public])
            L.append(f"| [`{m.folder}`](#{_slug(m.folder)}) | {shown or '—'} | {len(m.files)} | {len(m.used_by)} |")
    L += [
        "",
        f"**Coverage:** {cov['modules_with_statement']} of {cov['modules']} modules state their purpose; "
        f"{cov['files_with_purpose']} of {cov['files']} files have a purpose line "
        f"({cov['files_with_own_doc']} from their own doc, the rest from the one declaration the file is named after).",
        "",
    ]
    current_group = None
    group_sizes: dict[str, int] = defaultdict(int)
    for m in mods:
        group_sizes[m.group] += 1
    for m in sorted(mods, key=lambda x: (x.group, x.folder)):
        if m.group != current_group:
            current_group = m.group
            L += ["---", ""]
            # a group that is just this one module would repeat its heading
            if not (group_sizes[m.group] == 1 and m.folder == m.group):
                L += [f"## {m.group}", ""]
        L += [f"### `{m.folder}`", ""]
        if m.statement:
            L += [f"> {m.statement}  *[package docstring]*", ""]
        L += ["| file | lines | purpose |", "|---|---|---|"]
        for f in m.files:
            name = f.path.rsplit("/", 1)[-1]
            purpose = "—" if not f.purpose else (
                f.purpose if f.purpose_from == "file" else f"{f.purpose}  *[{f.purpose_from}]*")
            cell = purpose.replace("|", "\\|")     # one backslash: Markdown's escaped pipe
            L.append(f"| `{name}` | {f.lines} | {cell} |")
        if m.public:
            L += ["", f"**Public:** {_names_cell([s.name for s in m.public], limit=40)}"]
        if m.depends_on:
            L += ["", "**Depends on:** " + ", ".join(f"`{d}`" for d in sorted(m.depends_on))]
        if m.used_by:
            L += ["", "**Used by:** " + ", ".join(f"`{u}`" for u in sorted(m.used_by))]
        L.append("")
    return "\n".join(L) + "\n"


def _slug(folder: str) -> str:
    """GitHub-style heading anchor for `### `folder``: lowercased, spaces to
    hyphens, punctuation other than - and _ dropped."""
    import re
    return re.sub(r"[^a-z0-9 _-]", "", folder.lower()).replace(" ", "-")
