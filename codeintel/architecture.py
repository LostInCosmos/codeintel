"""Phase 2 — the architecture view: documented rules, and package knots.

Only rules the vault actually states are checked, and each names where it is
written. Anything else the graph shows is reported as a package cycle — a
fact — never as a "violation" of a rule nobody wrote down. Where a repo has
no documented layering, the report says so rather than inventing one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import graph

# The package granularity that means "a module" in each repo: backend layers
# (app/services), mobile features (lib/features/capture), admin folders.
DEFAULT_LEVEL = {"backend": 2, "mobile": 3, "admin_panel": 2}


@dataclass
class Rule:
    repo: str
    name: str
    source: str                       # the vault note that states it
    check: callable


@dataclass
class RuleResult:
    rule: Rule
    breaches: list[tuple[str, int, str]] = field(default_factory=list)   # (file, line, imported file)

    @property
    def holds(self) -> bool:
        return not self.breaches


def _routes_are_leaves(conn: sqlite3.Connection) -> list[tuple[str, int, str]]:
    """Route modules hold routes only, so nothing but the app entry point
    should import one. Tests may — exercising a route is what they are for."""
    out = []
    for (s, d), e in graph.edges(conn, "backend", include_tests=False).items():
        if d.startswith("app/api/") and not s.startswith("app/api/") and s != "app/main.py":
            out.append((s, e.lines[0], d))
    return out


RULES = [
    Rule("backend", "route modules are imported only by app/main.py",
         "03 - Architecture/Architecture & Data Model.md — three-layer pattern: "
         "\"routes only: FastAPI decorators, DI, nothing else\"",
         _routes_are_leaves),
]


@dataclass
class Report:
    repo: str
    level: int
    rules: list[RuleResult]
    knots: list[graph.Knot]

    @property
    def documented(self) -> bool:
        return bool(self.rules)


def review(conn: sqlite3.Connection, repo: str, level: int | None = None) -> Report:
    level = level or DEFAULT_LEVEL.get(repo, 2)
    results = [RuleResult(r, r.check(conn)) for r in RULES if r.repo == repo]
    return Report(repo, level, results, graph.package_knots(conn, repo, level))


def group_knots(knots: list[graph.Knot]) -> tuple[dict[str, list[graph.Knot]], list[graph.Knot]]:
    """Knots sharing one hub file on the thin side of 3+ of them are one
    story — "this file reaches back into N packages" — not N repeats of the
    same fact. Used by both `arch`'s display and the Phase 5 reviewer, so a
    hub is collapsed the same way everywhere it is reported.

    Returns (hub source path -> its knots, every other knot to report alone).
    """
    by_source: dict[str, list[graph.Knot]] = {}
    for k in knots:
        sources = {link[0] for link in k.thinnest()[1]}
        if len(sources) == 1:
            by_source.setdefault(next(iter(sources)), []).append(k)
    hubs = {src: ks for src, ks in by_source.items() if len(ks) >= 3}
    hubbed = {id(k) for ks in hubs.values() for k in ks}
    return hubs, [k for k in knots if id(k) not in hubbed]
