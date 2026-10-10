"""Phase 2 — the dependency graph and the architecture (package) graph.

Everything here is a query over the `imports` table; nothing is stored.

Two views of the same imports, because they answer different questions:

* **design** (default) — edges follow `defined_in`. `from ...models import
  Tenant` is an edge to `models/tenant.py`. Answers: what does this code
  really depend on?
* **runtime** — edges follow `resolved`, the file the statement literally
  names. The same import is an edge to `models/__init__.py`, which loads every
  model. Answers: what executes when this is imported — where can a Python
  circular import actually bite?

Not modelled: Python running a package's `__init__.py` before any submodule.
As an edge it would make every package that re-exports its submodules look
cyclic — false alarms by construction.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass, field

VIEWS = ("design", "runtime")


@dataclass
class Edge:
    src: str
    dst: str
    lines: list[int] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    scopes: set[str] = field(default_factory=set)
    kinds: set[str] = field(default_factory=set)

    @property
    def is_part(self) -> bool:
        """A Dart `part`: the destination is a piece of the source's own
        library, not a separate module it depends on."""
        return self.kinds == {"part"}

    @property
    def module_level(self) -> bool:
        """True when at least one import behind this edge runs at import time."""
        return "module" in self.scopes or "try" in self.scopes


def repo_of(conn: sqlite3.Connection, path: str) -> tuple[str, str] | None:
    """(repo, path) for a path given bare or as repo/path."""
    row = conn.execute("SELECT repo, path FROM files WHERE path=? OR repo||'/'||path=? LIMIT 1",
                       (path, path)).fetchone()
    return (row["repo"], row["path"]) if row else None


def edges(conn: sqlite3.Connection, repo: str, view: str = "design",
          include_tests: bool = True) -> dict[tuple[str, str], Edge]:
    """Every internal file -> file edge in a repo, one Edge per pair."""
    if view not in VIEWS:
        raise ValueError(view)
    col = "defined_in" if view == "design" else "resolved"
    test_filter = "" if include_tests else "AND f.is_test = 0"
    out: dict[tuple[str, str], Edge] = {}
    for r in conn.execute(f"""
            SELECT f.path src, i.{col} dst, i.line, i.name, i.scope, i.kind
            FROM imports i JOIN files f ON f.id = i.file_id
            WHERE f.repo = ? AND i.{col} IS NOT NULL {test_filter}
            ORDER BY f.path, i.line""", (repo,)):
        if r["src"] == r["dst"]:
            continue                      # `from . import x` inside x/__init__ re-exporting itself
        e = out.setdefault((r["src"], r["dst"]), Edge(r["src"], r["dst"]))
        e.lines.append(r["line"])
        if r["name"]:
            e.names.append(r["name"])
        e.scopes.add(r["scope"])
        e.kinds.add(r["kind"])
    return out


def adjacency(es: dict[tuple[str, str], Edge], module_only: bool = False) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = defaultdict(set)
    for (s, d), e in es.items():
        if module_only and not e.module_level:
            continue
        adj[s].add(d)
        adj.setdefault(d, set())
    return adj


def reach(adj: dict[str, set[str]], start: str) -> dict[str, int]:
    """Every node reachable from `start`, with its distance in hops."""
    dist = {start: 0}
    q = deque([start])
    while q:
        n = q.popleft()
        for m in adj.get(n, ()):
            if m not in dist:
                dist[m] = dist[n] + 1
                q.append(m)
    dist.pop(start)
    return dist


def reverse(adj: dict[str, set[str]]) -> dict[str, set[str]]:
    rev: dict[str, set[str]] = defaultdict(set)
    for s, ds in adj.items():
        rev.setdefault(s, set())
        for d in ds:
            rev[d].add(s)
    return rev


# ------------------------------------------------------------------ cycles

def sccs(adj: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components with more than one node (Tarjan,
    iterative so a deep graph cannot hit Python's recursion limit)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0

    for root in sorted(adj):
        if root in index:
            continue
        work = [(root, iter(sorted(adj[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(adj.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    m = stack.pop()
                    on_stack.discard(m)
                    comp.append(m)
                    if m == node:
                        break
                if len(comp) > 1:
                    result.append(sorted(comp))
    return sorted(result, key=lambda c: (-len(c), c))


def shortest_cycle(adj: dict[str, set[str]], comp: list[str]) -> list[str]:
    """One concrete loop through a component, shortest first, so a reader
    can follow it file by file and confirm it in the code."""
    members = set(comp)
    best: list[str] | None = None
    for start in comp:
        prev = {start: None}
        q = deque([start])
        found = None
        while q and found is None:
            n = q.popleft()
            for m in sorted(adj.get(n, ())):
                if m not in members:
                    continue
                if m == start:
                    found = n
                    break
                if m not in prev:
                    prev[m] = n
                    q.append(m)
        if found is None:
            continue
        path = [found]
        while prev[path[-1]] is not None:
            path.append(prev[path[-1]])
        path.reverse()
        path.append(start)
        if best is None or len(path) < len(best):
            best = path
    return best or comp


@dataclass
class Cycle:
    files: list[str]          # the whole strongly connected component
    loop: list[str]           # one concrete path around it, start == end
    module_level: bool        # True: closes on import-time edges alone


def cycles(conn: sqlite3.Connection, repo: str, view: str = "design") -> list[Cycle]:
    """Import cycles, split by whether they are live.

    module_level=True: the cycle closes using only imports that run when the
    module loads. In Python this is where a circular import can fail.
    module_level=False: it only closes through a function-local import — a
    cycle someone already broke on purpose. Reported so it stays visible,
    not as a problem.
    """
    es = edges(conn, repo, view)
    live_adj = adjacency(es, module_only=True)
    all_adj = adjacency(es)
    out = []
    live = sccs(live_adj)
    live_sets = [set(c) for c in live]
    for comp in live:
        out.append(Cycle(comp, shortest_cycle(live_adj, comp), True))
    for comp in sccs(all_adj):
        # anything not already explained by a live cycle exists only because
        # of a function-local import
        if any(set(comp) <= s for s in live_sets):
            continue
        out.append(Cycle(comp, shortest_cycle(all_adj, comp), False))
    return out


# ----------------------------------------------------------- package graph

def package_of(path: str, level: int | None = None, nested: set[str] | None = None) -> str:
    """The folder a file belongs to, optionally cut to its first `level`
    path segments (`app/services/invoice` at level 2 is `app/services`).

    A file sitting loose in a folder that also holds sub-packages — `app/main.py`,
    `src/App.jsx`, `src/api.js` — is its own unit, not part of a package named
    after the folder. Lumping them made `src/` look tangled with its own
    sub-folders: the shell (App.jsx) and a shared helper (api.js) are not one
    thing. `nested` is the set of folders that have sub-folders.
    """
    parts = path.split("/")[:-1]
    folder = "/".join(parts)
    if nested is not None and folder in nested and (level is None or len(parts) < level):
        return path
    if level:
        parts = parts[:level]
    return "/".join(parts) or "."


def nested_folders(paths) -> set[str]:
    """Folders that contain sub-folders with indexed files."""
    out = set()
    for p in paths:
        parts = p.split("/")[:-1]
        for i in range(1, len(parts)):
            out.add("/".join(parts[:i]))
    return out


def package_edges(conn: sqlite3.Connection, repo: str, level: int | None = None,
                  view: str = "design", include_tests: bool = False) -> dict[tuple[str, str], int]:
    """Edges between packages, weighted by how many file edges make them up.
    Edges inside one package are left out — they are its own business."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    es = edges(conn, repo, view, include_tests)
    nested = nested_folders({p for pair in es for p in pair})
    for (s, d) in es:
        ps, pd = package_of(s, level, nested), package_of(d, level, nested)
        if ps != pd:
            counts[(ps, pd)] += 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def to_dot(weighted: dict[tuple[str, str], int], title: str) -> str:
    """Graphviz text. Render with `dot -Tsvg`; no Python dependency."""
    lines = [f'digraph "{title}" {{', "  rankdir=LR;", '  node [shape=box, fontname="Helvetica"];']
    for (s, d), n in weighted.items():
        lines.append(f'  "{s}" -> "{d}" [label="{n}", penwidth={min(1 + n / 4, 6):.1f}];')
    lines.append("}")
    return "\n".join(lines)


Link = tuple[str, int, str, bool, bool]  # (src file, line, dst file, runs at import time, is a Dart part)


@dataclass
class Knot:
    """Two packages that import each other, or a longer loop of them.

    Reported as pairs first because a pair is something a person can act on:
    `schemas` and `services` depend on each other, and here are the imports
    that make it so. A loop is reported only when no pair explains it.
    """
    packages: list[str]
    live: bool                                     # closes on import-time imports alone
    links: dict[tuple[str, str], list[Link]]       # every import between the packages

    def thinnest(self) -> tuple[tuple[str, str], list[Link]]:
        """The direction with the fewest imports — usually the one that should
        not be there, and the cheapest place to cut."""
        return min(self.links.items(), key=lambda kv: (len(kv[1]), kv[0]))


def package_knots(conn: sqlite3.Connection, repo: str, level: int | None = None,
                  include_tests: bool = False) -> list[Knot]:
    """Packages that depend on each other, even where no single file does.

    File cycles miss these: `schemas` importing one class from `services`
    while `services` imports `schemas` throughout is no file cycle, but it is
    two packages tangled together.
    """
    es = edges(conn, repo, "design", include_tests)
    nested = nested_folders({p for pair in es for p in pair})
    links: dict[tuple[str, str], list[Link]] = defaultdict(list)
    for (s, d), e in es.items():
        ps, pd = package_of(s, level, nested), package_of(d, level, nested)
        if ps != pd:
            links[(ps, pd)].append((s, e.lines[0], d, e.module_level, e.is_part))
    links = {k: sorted(v) for k, v in links.items()}

    knots: list[Knot] = []
    cut: set[tuple[str, str]] = set()
    for (a, b) in sorted(links):
        if a < b and (b, a) in links:
            pair = {(a, b): links[(a, b)], (b, a): links[(b, a)]}
            live = all(any(l[3] for l in ls) for ls in pair.values())
            knot = Knot([a, b], live, pair)
            knots.append(knot)
            cut.add(knot.thinnest()[0])

    # Longer loops that no pair explains: cut each pair's thinnest direction
    # and see what is still circular.
    adj: dict[str, set[str]] = defaultdict(set)
    for (a, b) in links:
        if (a, b) not in cut:
            adj[a].add(b)
            adj.setdefault(b, set())
    for comp in sccs(adj):
        members = set(comp)
        sub = {k: v for k, v in links.items() if k[0] in members and k[1] in members and k not in cut}
        live = any(any(l[3] for l in ls) for ls in sub.values())
        knots.append(Knot(shortest_cycle(adj, comp)[:-1], live, sub))

    # biggest tangles first: by how many imports run the thin way
    return sorted(knots, key=lambda k: (-len(k.packages), k.packages))
