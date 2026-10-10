"""Phase 3b — the duplicate implementation detector.

The question is not "do these look alike?" but "how many parameters would a
shared version need?". Two bodies are aligned token by token, names and
string literals kept, and judged by two measurements together — see
`compare()` for why one alone is not enough:

* **how many places differ** (MAX_PARAMS) — 0 is an identical copy; 1-3 is
  one function with that many parameters serving both, with the differences
  listed (`claim_from_db`: the model class and the status a claim sets); 4+
  is not reported — the shapes coincide but the content does not (two
  `toJson` methods over different fields).
* **how much of the body matches** (MIN_SHARED) — catches what the count
  alone misses: a short function reading as "close" to a much longer,
  unrelated one because its little content sits inside one oversized
  substitution (`Corner.__init__`, two fields, matched three unrelated
  exception constructors this way at "3 parameters" each).

Both thresholds were measured, not chosen, on the 15 structural-hash groups in
this codebase: every real duplicate or repeated pattern needed at most 3
parameters and shared at least 0.80 of its tokens; the coincidences needed 4
params / 0.87 shared (`dispose` overriding different controllers) and 14
params / 0.53 shared (`toJson` over different fields). MAX_PARAMS=3 and
MIN_SHARED=0.75 sit in that gap. The sample is 15 groups from one codebase —
re-measure before moving either. See the vault note for the full table.

Groups of three or more are CLIQUES (`cliques_from`), not connected
components: a plain union-find merge, tried first, chained 26 unrelated route
handlers into one "duplicate" through a few genuinely close pairs inside it,
several others needing 4-5 parameters the rule above would have refused. A
group is reported only when every pair within it independently passed both
measurements.

Candidates come from three places, all judged by the same two measurements:
* **same structure** — identical `body_hash` (names erased), 5+ lines
* **same name** in different files — no size floor, since a shared name
  is not a coincidence (two copies of a 4-line `formatTokens`)
* **similar** — overlapping token sequences, for copies that drifted
"""

from __future__ import annotations

import difflib
import itertools
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

MAX_PARAMS = 3
# A pair must also share this much of their content, token for token — see
# `compare()`. Measured floor on every real match in this codebase was 0.80;
# kept at 0.75 for a small margin on a 15-group sample. Without it, a tiny
# function can read as "close" to a much larger, unrelated one whenever the
# little it has happens to sit inside a shared subsequence.
MIN_SHARED = 0.75
MIN_LINES = 5            # floor for structure/similarity candidates
MIN_TOKENS = 8           # floor for same-name candidates: skip `return null`-sized bodies
# Shingle overlap to admit a pair as a "similar" candidate. This threshold is
# deliberately loose — 0.3 to 0.7 changed the candidate count from 53 to 32
# on this codebase but changed nothing MAX_PARAMS went on to keep, because
# that second measured cut is what actually decides. Keep it low; let the
# parameter count filter.
SIMILAR_JACCARD = 0.4
SHINGLE = 4

_TOK = re.compile(r"'''[\s\S]*?'''|\"\"\"[\s\S]*?\"\"\"|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`[^`]*`"
                  r"|[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|\S")


@dataclass(frozen=True)
class Loc:
    repo: str
    path: str
    qualname: str
    start: int
    end: int
    is_test: bool

    def __str__(self) -> str:
        return f"{self.repo}/{self.path}:{self.start}  {self.qualname}"


@dataclass
class Duplicate:
    members: list[Loc]
    differences: list[tuple[str, str]]      # from the most different pair
    found_by: set[str] = field(default_factory=set)

    @property
    def identical(self) -> bool:
        return not self.differences

    @property
    def lines(self) -> int:
        return max(m.end - m.start + 1 for m in self.members)

    @property
    def in_tests(self) -> bool:
        return all(m.is_test for m in self.members)


def body_tokens(text: str, start: int, end: int, language: str) -> list[str]:
    """The body's tokens, names and string literals kept, comments and a
    leading docstring dropped. The signature line is skipped: renaming a
    function is not a difference in what it does."""
    lines = text.splitlines()[start:end]
    body = "\n".join(lines)
    body = re.sub(r"#[^\n]*" if language == "python" else r"//[^\n]*", "", body)
    toks = _TOK.findall(body)
    if language == "python" and toks and toks[0].startswith(('"""', "'''", '"', "'")):
        toks = toks[1:]
    return toks


def compare(a: list[str], b: list[str]) -> tuple[list[tuple[str, str]], float]:
    """(distinct substitutions, shared fraction) turning one body into the
    other. A change made the same way in several places counts as one
    substitution — that is one parameter.

    The count alone is not enough: it counts how many EDIT REGIONS there are,
    not their size, so a tiny function can still read as "close" to a much
    larger, unrelated one if the little it has happens to sit inside a common
    subsequence — `Corner.__init__` (two fields) matched three different
    custom exceptions' `__init__` this way, each at 3 "parameters" with most
    of the longer body swallowed into one of them. The shared fraction catches
    it: 0.43, against a measured floor of 0.80 on every real match. Both
    checks are required; see MAX_PARAMS and MIN_SHARED.
    """
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    seen: dict[tuple[str, str], None] = {}
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "equal":
            seen[(" ".join(a[i1:i2]), " ".join(b[j1:j2]))] = None
    shared = sum(block.size for block in sm.get_matching_blocks())
    return list(seen), shared / max(len(a), len(b), 1)


def _shingles(toks: list[str]) -> set[tuple[str, ...]]:
    return {tuple(toks[i:i + SHINGLE]) for i in range(max(len(toks) - SHINGLE + 1, 0))}


def cliques_from(adj: dict[int, set[int]]) -> list[set[int]]:
    """Group nodes connected by `adj` into CLIQUES, not connected components.
    "A is close to B, B is close to C" does not mean A and C belong together
    — a plain union-find merge chained unrelated route handlers into one
    26-member group this way, several pairs inside it needing 4-5 parameters
    the direct rule would have refused. A group is only returned when every
    pair within it is itself an edge in `adj`, which is what makes whatever a
    caller reports about the group (e.g. "these differ by only N tokens")
    true of the whole group, not just the two members that happened to be
    compared first.

    Finding the true maximum clique cover is NP-hard in general; these
    neighbourhoods are small (tens of nodes), so a greedy grow pass is
    sufficient. Every node is tried as a seed, not only unclaimed ones: a
    node can sit at the hub of two unrelated pairs (A-B an edge, C-B an edge,
    A-C not), and claiming B for whichever clique was grown first would
    silently drop the other pair. Trying every seed and de-duplicating the
    resulting cliques finds both.
    """
    seen: set[frozenset[int]] = set()
    cliques: list[set[int]] = []
    for i in sorted(adj):
        clique = {i}
        for j in sorted(adj[i]):
            if all(j in adj[k] for k in clique):
                clique.add(j)
        if len(clique) > 1 and frozenset(clique) not in seen:
            seen.add(frozenset(clique))
            cliques.append(clique)
    return cliques


def find(conn: sqlite3.Connection, root: Path, include_tests: bool = True,
         similar: float = SIMILAR_JACCARD) -> list[Duplicate]:
    rows = conn.execute("""
        SELECT f.repo, f.path, f.language, f.is_test, s.qualname, s.name, s.start_line, s.end_line, s.body_hash
        FROM symbols s JOIN files f ON f.id = s.file_id
        WHERE s.kind != 'class' AND s.end_line > s.start_line""").fetchall()
    texts: dict[tuple[str, str], str] = {}
    syms = []
    for r in rows:
        if r["is_test"] and not include_tests:
            continue
        key = (r["repo"], r["path"])
        if key not in texts:
            texts[key] = (root / r["repo"] / r["path"]).read_text()
        toks = body_tokens(texts[key], r["start_line"], r["end_line"], r["language"])
        loc = Loc(r["repo"], r["path"], r["qualname"], r["start_line"], r["end_line"], bool(r["is_test"]))
        syms.append((loc, r["name"], r["body_hash"], toks))

    candidates: dict[tuple[int, int], set[str]] = defaultdict(set)
    big = [i for i, s in enumerate(syms) if s[0].end - s[0].start + 1 >= MIN_LINES]

    by_hash = defaultdict(list)
    for i in big:
        if syms[i][2]:
            by_hash[(syms[i][0].repo, syms[i][2])].append(i)
    for group in by_hash.values():
        for i, j in itertools.combinations(group, 2):
            candidates[(i, j)].add("same structure")

    by_name = defaultdict(list)
    for i, s in enumerate(syms):
        if len(s[3]) >= MIN_TOKENS:
            by_name[(s[0].repo, s[1])].append(i)
    for group in by_name.values():
        for i, j in itertools.combinations(group, 2):
            if syms[i][0].path != syms[j][0].path:
                candidates[(i, j)].add("same name")

    shingles = {i: _shingles(syms[i][3]) for i in big}
    index = defaultdict(set)
    for i, sh in shingles.items():
        for s in sh:
            index[s].add(i)
    for i, sh in shingles.items():
        if not sh:
            continue
        overlap = defaultdict(int)
        for s in sh:
            for j in index[s]:
                if j > i and syms[j][0].repo == syms[i][0].repo:
                    overlap[j] += 1
        for j, n in overlap.items():
            if n / len(sh | shingles[j]) >= similar:
                candidates[(i, j)].add("similar")

    # Judge every candidate pair by two rules together — see `compare()` for
    # why the parameter count alone is not enough.
    kept: dict[tuple[int, int], list[tuple[str, str]]] = {}
    adj: dict[int, set[int]] = defaultdict(set)
    for (i, j) in candidates:
        d, shared = compare(syms[i][3], syms[j][3])
        if len(d) <= MAX_PARAMS and shared >= MIN_SHARED:
            kept[(i, j)] = d
            adj[i].add(j)
            adj[j].add(i)

    cliques = cliques_from(adj)
    out = []
    for clique in cliques:
        members = sorted(clique)
        # Every pair within a found clique satisfies an `adj` edge by
        # construction, so it must already be in `kept` — the fallback is
        # only a safety net against that invariant ever drifting.
        worst = max((kept.get((a, b)) or kept.get((b, a)) or compare(syms[a][3], syms[b][3])[0]
                    for a, b in itertools.combinations(members, 2)), key=len)
        found_by: set[str] = set()
        for a, b in itertools.combinations(members, 2):
            found_by |= candidates.get((a, b), candidates.get((b, a), set()))
        c = Duplicate([syms[k][0] for k in members], worst, found_by)
        c.members.sort(key=lambda m: (m.path, m.start))
        out.append(c)
    return sorted(out, key=lambda c: (c.in_tests, len(c.differences), -c.lines * len(c.members)))
