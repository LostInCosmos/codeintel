"""Phase 5 — the automated architecture reviewer.

Deterministic, by design: every check here is a query over Phases 1-4's
already-verified index, nothing new is parsed or guessed. An LLM pass on top
is future, optional, and must never replace a check a query can already do
exactly — see the roadmap.

Five checks, each named in the roadmap's table:

| finding          | reuses                                   | severity |
|------------------|-------------------------------------------|---------|
| broken-import    | Phase 1's resolver                         | error   |
| layering         | Phase 2's `arch` (documented rules only)   | error   |
| orphan-module    | Phase 2's `deps`, minus entry points        | warn    |
| package-knot     | Phase 2's `arch` (package knots)           | warn    |
| mixed-file       | new: symbol/import line-range cohesion      | warn    |
| dart-part-misplaced | Phase 2's import `kind`                 | info    |
| duplicate        | Phase 3b, pairs only                       | info    |

`mixed-file` is the one genuinely new measurement, and the one with no
independent ground truth to check it against — unlike every earlier phase.
Its shape was calibrated, not assumed: see `mixed_files` for the two things
that had to be true before a single finding survived contact with the real
repos.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import architecture, duplicates, graph

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}

# A file started by the runtime or build tool, never imported by other
# source — true in every framework used here, not a per-file guess.
# `ops/` and `scripts/` are additionally documented as standalone, run-by-
# hand tooling: 04 - Backend/Backend Code Structure.md — "scripts ... run
# manually, in order"; ops/upload_logs.py is log rotation to R2, invoked by
# the deploy scripts, not imported.
ENTRY_POINT_DIRS = {"backend": ("ops", "scripts")}
ENTRY_POINT_NAMES = {"main.py", "main.dart", "main.jsx", "main.tsx"}

_JS_SOURCE_EXT = (".js", ".jsx", ".mjs", ".ts", ".tsx")


def _is_entry_point(repo: str, path: str) -> bool:
    top = path.split("/", 1)[0]
    return top in ENTRY_POINT_DIRS.get(repo, ()) or path.rsplit("/", 1)[-1] in ENTRY_POINT_NAMES


@dataclass
class Finding:
    repo: str
    rule: str
    severity: str          # "error" | "warn" | "info"
    file: str
    line: int | None
    message: str
    source: str | None = None     # the vault note stating the rule, when one exists

    def sort_key(self) -> tuple:
        return (SEVERITY_ORDER[self.severity], self.rule, self.file, self.line or 0)


def broken_imports(conn: sqlite3.Connection, repo: str) -> list[Finding]:
    """A relative import can only ever name code in this same repo — never a
    third-party package — so one that does not resolve is always broken,
    never a legitimate dependency wrongly flagged.

    One real exception, found while calibrating this check against the real
    index: JavaScript can relatively import a non-source asset (`./index.css`).
    This tool only reads source files, so such an import can never resolve
    here — not a defect, just outside what it was ever asked to track.
    Excluded by extension, not by path, so a genuinely broken relative
    import of a `.jsx` file two folders away is still caught.
    """
    out = []
    for r in conn.execute(
            """SELECT f.path, f.language, i.line, i.target FROM imports i
               JOIN files f ON f.id = i.file_id
               WHERE f.repo=? AND i.is_relative=1 AND i.resolved IS NULL
               ORDER BY f.path, i.line""", (repo,)):
        if r["language"] == "javascript":
            tail = r["target"].rsplit("/", 1)[-1]
            if "." in tail and not tail.endswith(_JS_SOURCE_EXT):
                continue
        out.append(Finding(repo, "broken-import", "error", r["path"], r["line"],
                           f"{r['target']!r} does not resolve to any indexed file"))
    return out


def orphan_modules(conn: sqlite3.Connection, repo: str) -> list[Finding]:
    """A file nothing in this repo imports, and that is not a known entry
    point. Measured raw rate before entry points are excluded: 60/136 files
    in the backend — almost all `__init__.py` (expected: nothing explicitly
    imports a package marker) or a test (expected: nothing imports a test).
    Excluding those plus entry points is what turns 60 into a number that
    means something: what's left is either dead code, or something only
    ever reached dynamically (string-built import, reflection) that this
    index cannot see either way.
    """
    imported = {d for (_, d) in graph.edges(conn, repo, "design", include_tests=True)}
    out = []
    for r in conn.execute("SELECT path FROM files WHERE repo=? AND is_test=0", (repo,)):
        path = r["path"]
        if path in imported or path.endswith("__init__.py") or _is_entry_point(repo, path):
            continue
        out.append(Finding(repo, "orphan-module", "warn", path, None,
                           "nothing in this repo imports it, and it is not a known entry point"))
    return out


def layering(conn: sqlite3.Connection, repo: str) -> list[Finding]:
    """Breaches of a rule the vault actually states — see `architecture.py`.
    A repo with no written layering produces nothing here, not an invented
    judgment."""
    out = []
    for rr in architecture.review(conn, repo).rules:
        for file, line, dst in rr.breaches:
            out.append(Finding(repo, "layering", "error", file, line,
                               f'breaks "{rr.rule.name}": imports {dst}', source=rr.rule.source))
    return out


def package_knots(conn: sqlite3.Connection, repo: str) -> list[Finding]:
    """Packages that import each other even where no single file does.
    Severity warn, not error: several confirmed knots are the documented
    three-layer pattern working as intended, not a defect — this is a fact
    to weigh, same as `arch`'s own framing. A hub (one file behind 3+ knots)
    is collapsed into one finding via `architecture.group_knots`, matching
    `arch`'s own display rule: one story, not N repeats.
    """
    level = architecture.DEFAULT_LEVEL.get(repo, 2)
    knots = graph.package_knots(conn, repo, level)
    hubs, rest = architecture.group_knots(knots)
    out = []
    for src, ks in hubs.items():
        packages = sorted({b for k in ks for b in k.packages if b != src})
        out.append(Finding(repo, "package-knot", "warn", src, None,
                           f"reaches back into {len(ks)} packages that all import it directly: "
                           + ", ".join(packages)))
    for k in rest:
        (a, b), thin = k.thinnest()
        shape = " <-> ".join(k.packages) if len(k.packages) == 2 else " -> ".join([*k.packages, k.packages[0]])
        s, line = thin[0][0], thin[0][1]
        out.append(Finding(repo, "package-knot", "warn", s, line,
                           f"{shape}: {len(thin)} import(s) the thin way, {a} -> {b}"))
    return out


def misplaced_dart_parts(conn: sqlite3.Connection, repo: str) -> list[Finding]:
    """A Dart `part` file stored outside its own library's folder — the
    library is spread across folders on disk, whether or not that also
    happens to form a 2-way package knot (`package_knots` only surfaces a
    part this way when it does)."""
    if repo != "mobile":
        return []
    level = architecture.DEFAULT_LEVEL.get(repo, 2)
    es = graph.edges(conn, repo, "design", include_tests=False)
    nested = graph.nested_folders({p for pair in es for p in pair})
    out = []
    for (s, d), e in es.items():
        if e.is_part and graph.package_of(s, level, nested) != graph.package_of(d, level, nested):
            out.append(Finding(repo, "dart-part-misplaced", "info", d, e.lines[0],
                               f"part of the {s} library, stored in a different folder"))
    return out


def duplicate_pairs(conn: sqlite3.Connection, root: Path, repo: str) -> list[Finding]:
    """Mergeable pairs only (two files). A group of 3+ is usually a repeated
    shape the documented architecture calls for (every route handler), not
    something to merge — see Phase 3's write-up — so patterns are left out
    of the reviewer entirely, not merely demoted."""
    out = []
    for d in duplicates.find(conn, root, include_tests=False):
        if len(d.members) != 2 or d.members[0].repo != repo:
            continue
        a, b = d.members
        out.append(Finding(repo, "duplicate", "info", a.path, a.start,
                           f"near-duplicate of {b}: could be one shared implementation"))
    return out


# -------------------------------------------------------------------- mixed

_NAME = re.compile(r"[A-Za-z_]\w*")


def _referenced(blob: str, candidates: set[str]) -> set[str]:
    return candidates & set(_NAME.findall(blob))


def mixed_files(conn: sqlite3.Connection, root: Path, repo: str) -> list[Finding]:
    """A file whose top-level symbols split into two or more groups that
    share no name — no shared import, and no reference to each other —
    with every group at least two symbols large.

    Deliberately conservative, and calibrated against the real repos rather
    than assumed: the index has no call graph, so a lone small helper that
    just does not happen to textually reference anything else in the file
    reads as its own "group" far too often to mean anything. Measured on
    this codebase: allowing a one-symbol group produced 14 findings, almost
    all well-organised files (`core/cache.py`, `core/security.py`, a mobile
    models file, ...) where a single tiny singleton — `reset_for_tests`,
    `hash_password` — sat beside one real cluster. Requiring every group to
    have 2+ members cut that to exactly one finding across all three repos
    (`model_pricing.py`: token/cost math vs a static per-model pricing
    table), confirmed true by reading the file — the only version of this
    check that held up, so the only version shipped.
    """
    out = []
    for f in conn.execute("SELECT id, path FROM files WHERE repo=? AND is_test=0", (repo,)):
        syms = conn.execute(
            "SELECT name, start_line, end_line FROM symbols WHERE file_id=? AND parent IS NULL "
            "ORDER BY start_line", (f["id"],)).fetchall()
        if len(syms) < 4:
            continue   # two groups of 2+ each need at least 4 symbols to begin with
        path = root / repo / f["path"]
        if not path.exists():
            continue
        text = path.read_text(errors="replace").splitlines()
        imports = {r[0] for r in conn.execute(
            "SELECT DISTINCT COALESCE(alias, name) FROM imports WHERE file_id=? AND name IS NOT NULL",
            (f["id"],)) if r[0]}
        names = [s["name"] for s in syms]
        vocab = imports | set(names)
        refs = [_referenced("\n".join(text[s["start_line"] - 1:s["end_line"]]), vocab - {s["name"]})
                for s in syms]

        parent = list(range(len(syms)))

        def find(x: int) -> int:
            while parent[x] != x:
                x = parent[x]
            return x

        for i in range(len(syms)):
            for j in range(i + 1, len(syms)):
                if refs[i] & refs[j] or names[j] in refs[i] or names[i] in refs[j]:
                    a, b = find(i), find(j)
                    if a != b:
                        parent[a] = b

        groups: dict[int, list[str]] = {}
        for i in range(len(syms)):
            groups.setdefault(find(i), []).append(names[i])
        big = [g for g in groups.values() if len(g) >= 2]
        if len(big) >= 2:
            out.append(Finding(repo, "mixed-file", "warn", f["path"], syms[0]["start_line"],
                               "splits into unrelated groups: " + " | ".join(", ".join(g) for g in big)))
    return out


def review(conn: sqlite3.Connection, root: Path, repo: str) -> list[Finding]:
    findings = [
        *broken_imports(conn, repo),
        *orphan_modules(conn, repo),
        *layering(conn, repo),
        *package_knots(conn, repo),
        *misplaced_dart_parts(conn, repo),
        *duplicate_pairs(conn, root, repo),
        *mixed_files(conn, root, repo),
    ]
    return sorted(findings, key=Finding.sort_key)
