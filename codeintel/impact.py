"""Phase 4 — the change impact analyser.

"I am changing this file — what else is affected, and which tests cover it?"

## Scope, stated up front (the one thing this must never get wrong)

This answers that question against the **current index** — the files as they
sit on disk right now, before a proposed edit is applied. That is deliberate,
not a shortcut: the question is prospective ("I am about to change this"),
and for exactly that question the current index already holds the true
dependents, including for a file about to be **deleted** — its dependents are
still in the index because the file hasn't been removed yet. Reconstructing
an arbitrary past commit's index is a different, much larger feature nothing
here asked for; it is not built.

## Why propagation is file-level, never symbol-level

A changed file's SYMBOLS are reported (which functions moved, for a human to
read), but impact — who might need to change too — propagates from the whole
**file**, always, regardless of which specific lines changed within it. A
change to a module-level constant, an import, or a class attribute sits
outside every function's line range; gating propagation on "a symbol's body
overlapped a changed line" would silently drop exactly that case. Phase 2's
stated rule carries over unchanged: over-reporting is acceptable here,
under-reporting is not.

## What "impact" means

For a set of changed files, the transitively-dependent files — built on
Phase 2's `graph.reach` over the reversed **design** view, already verified
89/89 against grimp — split into test files (the suite to run) and
everything else, with a risk summary: how many files and repos, and whether
a documented boundary (currently: the backend's route layer) is touched.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import graph


# --------------------------------------------------------------- diff parsing

@dataclass
class ChangedFile:
    status: str              # "added" | "modified" | "deleted" | "renamed"
    path: str                # the path impact is computed against (old path for a delete/rename)
    new_path: str | None = None    # set only when renamed
    lines: list[tuple[int, int]] = field(default_factory=list)  # changed ranges in the NEW file

    def merge_lines(self) -> list[tuple[int, int]]:
        """Adjacent/overlapping ranges collapsed, for a tidy report."""
        if not self.lines:
            return []
        out = [self.lines[0]]
        for s, e in self.lines[1:]:
            ls, le = out[-1]
            if s <= le + 1:
                out[-1] = (ls, max(le, e))
            else:
                out.append((s, e))
        return out


_DIFF_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(text: str) -> list[ChangedFile]:
    """Parse `git diff` output. Pure and dependency-free, so it can be tested
    against real diffs without a repo to hand.

    Deliberately narrow: this reads exactly what `git diff`/`git show` emit
    (unified format, `a/`/`b/`-prefixed paths), not arbitrary patch dialects.
    """
    out: list[ChangedFile] = []
    cur: ChangedFile | None = None
    new_line = 0
    fallback_a = fallback_b = None

    def flush():
        if cur is not None:
            out.append(cur)

    for line in text.splitlines():
        if m := _DIFF_HEADER.match(line):
            flush()
            fallback_a, fallback_b = m.group(1), m.group(2)
            cur = None   # finalised once --- / +++ / new file / deleted file is seen
            continue
        if line.startswith("new file mode"):
            cur = ChangedFile("added", fallback_b)
            continue
        if line.startswith("deleted file mode"):
            cur = ChangedFile("deleted", fallback_a)
            continue
        if line.startswith("rename from "):
            cur = ChangedFile("renamed", line[len("rename from "):])
            continue
        if line.startswith("rename to "):
            if cur is not None:
                cur.new_path = line[len("rename to "):]
            continue
        if line.startswith("--- "):
            # A plain modification never hits new-file/deleted-file/rename
            # above, so this is where it is first established.
            if cur is None:
                cur = ChangedFile("modified", fallback_a if fallback_a != "/dev/null" else fallback_b)
            continue
        if line.startswith("+++ "):
            continue
        if line.startswith("Binary files ") and line.endswith(" differ"):
            # No --- / +++ / hunks follow a binary diff, so nothing else here
            # would ever finalise it — the file was silently disappearing
            # from the changed-file list entirely. No ranges to report (a
            # binary has no line-based symbols), but the path itself must
            # still surface: this repo only indexes source files, so a
            # binary is reported `unresolved` downstream, never dropped
            # before the caller can see that it exists.
            if cur is None:
                status = ("added" if fallback_a == "/dev/null" else
                          "deleted" if fallback_b == "/dev/null" else "modified")
                cur = ChangedFile(status, fallback_b if fallback_a == "/dev/null" else fallback_a)
            continue
        if m := _HUNK.match(line):
            new_line = int(m.group(3))
            continue
        if cur is None:
            continue   # between the file header and its first hunk/marker
        if line.startswith("+") and not line.startswith("+++"):
            cur.lines.append((new_line, new_line))
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            # Not in the new file, so it advances nothing — but record the
            # position so a pure deletion still points near where it happened,
            # for the human-readable symbol report. Never a gate on impact.
            if cur.lines and cur.lines[-1][1] == new_line - 1:
                pass   # already adjacent to a '+' at this position
            else:
                cur.lines.append((max(new_line, 1), max(new_line, 1)))
        elif line.startswith(" ") or line == "":
            new_line += 1
        # a line starting with '\' ("No newline at end of file") is ignored
    flush()
    return [c for c in out if c is not None]


def live_diff(repo_root: Path) -> str:
    """The working tree's uncommitted changes against HEAD, tracked files
    only. A failure (not a git repo, no HEAD yet) returns no diff rather than
    raising — impact on an un-diffable repo is simply nothing to report."""
    try:
        r = subprocess.run(["git", "diff", "--no-color", "HEAD"], cwd=repo_root,
                           capture_output=True, timeout=15)
        # A stray non-UTF-8 byte in an otherwise-text diff (git's own binary
        # heuristic can miss a near-binary file) must not crash the whole
        # analysis; replace rather than fail, since this only ever affects
        # a file codeintel does not index as source anyway.
        return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ------------------------------------------------------------- changed symbols

@dataclass
class ChangedSymbol:
    kind: str
    qualname: str
    start_line: int
    end_line: int


def _symbols_for(conn: sqlite3.Connection, file_id: int, status: str,
                 ranges: list[tuple[int, int]]) -> list[ChangedSymbol]:
    """Symbols to report for one changed file. Informational only — see the
    module docstring for why this never gates what counts as impacted.

    `deleted` is special-cased to every symbol in the file, not an overlap
    check: a pure deletion hunk never advances the new-file line counter (the
    lines it removed do not exist there), so every deleted line collapses to
    the same spot near wherever the hunk started — reporting "nothing
    overlapped" for a file being removed entirely would be actively
    misleading rather than merely incomplete.
    """
    rows = conn.execute("SELECT kind, qualname, start_line, end_line FROM symbols "
                        "WHERE file_id=? ORDER BY start_line", (file_id,)).fetchall()
    if status == "deleted":
        return [ChangedSymbol(r["kind"], r["qualname"], r["start_line"], r["end_line"]) for r in rows]
    if not ranges:
        return []
    return [ChangedSymbol(r["kind"], r["qualname"], r["start_line"], r["end_line"]) for r in rows
            if any(r["start_line"] <= e and st <= r["end_line"] for st, e in ranges)]


# ---------------------------------------------------------------------- impact

@dataclass
class FileImpact:
    status: str
    symbols: list[ChangedSymbol]
    new_path: str | None = None   # set only when renamed


@dataclass
class Dependent:
    path: str
    hops: int
    is_test: bool


@dataclass
class ImpactReport:
    repo: str
    changed: dict[str, FileImpact]          # path -> what changed there
    dependents: list[Dependent]             # transitive, changed files excluded
    unresolved: list[str]                   # changed paths not found in the index

    @property
    def tests(self) -> list[Dependent]:
        return [d for d in self.dependents if d.is_test]

    @property
    def non_test_dependents(self) -> list[Dependent]:
        return [d for d in self.dependents if not d.is_test]


def analyse(conn: sqlite3.Connection, repo: str,
           changed: dict[str, tuple[str, list[tuple[int, int]]]]) -> ImpactReport:
    """`changed`: repo-relative path -> (status, changed line ranges).
    A `deleted`/`renamed` path's dependents all need attention — the thing
    they imported moved or stopped existing.
    """
    adj = graph.adjacency(graph.edges(conn, repo, "design", include_tests=True))
    rev = graph.reverse(adj)

    out_changed: dict[str, FileImpact] = {}
    unresolved: list[str] = []
    seen: dict[str, int] = {}   # dependent path -> best (lowest) hop count

    for rel, (status, ranges) in changed.items():
        row = conn.execute("SELECT id FROM files WHERE repo=? AND path=?", (repo, rel)).fetchone()
        if not row:
            # Nothing in the CURRENT index to reason about: an added file
            # that was never scanned, or one already deleted and rescanned
            # away. Reported, not silently skipped — a gap stays visible.
            unresolved.append(rel)
            continue
        out_changed[rel] = FileImpact(status, _symbols_for(conn, row[0], status, ranges))
        for dep, hops in graph.reach(rev, rel).items():
            if dep in changed:
                continue   # another changed file; not a dependent to flag
            if dep not in seen or hops < seen[dep]:
                seen[dep] = hops

    is_test = dict(conn.execute(f"SELECT path, is_test FROM files WHERE repo=? AND path IN "
                                f"({','.join('?' * len(seen))})", (repo, *seen)).fetchall()) if seen else {}
    dependents = sorted(
        (Dependent(p, h, bool(is_test.get(p, 0))) for p, h in seen.items()),
        key=lambda d: (d.hops, d.path))
    return ImpactReport(repo, out_changed, dependents, unresolved)


def from_diff_text(conn: sqlite3.Connection, repo: str, diff_text: str) -> ImpactReport:
    changed = {}
    renamed_to: dict[str, str] = {}
    for cf in parse_unified_diff(diff_text):
        changed[cf.path] = (cf.status, cf.merge_lines())
        if cf.new_path:
            renamed_to[cf.path] = cf.new_path
    report = analyse(conn, repo, changed)
    for rel, new_path in renamed_to.items():
        if rel in report.changed:
            report.changed[rel].new_path = new_path
    return report


def _resolve_repo(conn: sqlite3.Connection, path: str) -> tuple[str, str] | None:
    """(repo, rel) for a path, matched against each known repo's directory
    layout rather than requiring this exact path already be in the index.

    `graph.repo_of` only finds a path already in the `files` table — fine
    for `deps`, where a never-scanned file has no import edges to show
    anyway. Whole-file impact mode exists precisely FOR a brand new,
    not-yet-scanned file, so resolving only through an exact index hit would
    make that exact case vanish silently — the one thing this module's
    docstring says must never happen. A repo's top-level source/test
    directory is inferred from files already indexed for it, so this needs
    no knowledge of the real filesystem layout.
    """
    head, sep, rest = path.partition("/")
    repos = [r[0] for r in conn.execute("SELECT DISTINCT repo FROM files")]
    if sep and head in repos:
        return head, rest
    for repo in repos:
        if conn.execute("SELECT 1 FROM files WHERE repo=? AND path LIKE ? LIMIT 1",
                        (repo, f"{head}/%")).fetchone():
            return repo, path
    return None


def from_paths(conn: sqlite3.Connection, paths: list[str]) -> dict[str, ImpactReport]:
    """Whole-file mode: every given path is treated as fully changed.

    A path belonging to no known repo at all (typo, wrong argument) is
    reported under the "?" key rather than dropped — see `_resolve_repo`.
    """
    by_repo: dict[str, dict[str, tuple[str, list]]] = {}
    unknown: list[str] = []
    for p in paths:
        found = _resolve_repo(conn, p)
        if not found:
            unknown.append(p)
            continue
        repo, rel = found
        by_repo.setdefault(repo, {})[rel] = ("modified", [])
    reports = {repo: analyse(conn, repo, changed) for repo, changed in by_repo.items()}
    if unknown:
        reports["?"] = ImpactReport("?", {}, [], unknown)
    return reports
