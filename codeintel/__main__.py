"""codeintel — a local codebase intelligence index for GSTApp.

  Phase 1 — the index
    scan [--full]               build / refresh the index
    stats                       what is indexed
    find NAME                   where a symbol is defined (substring, case-insensitive)
    file PATH                   one file's symbols and imports

  Phase 2 — dependencies and architecture
    deps PATH [--all]           what a file depends on, and what depends on it
    cycles [REPO]               import cycles, live vs already broken
    arch [REPO]                 documented rules + packages tangled together
    packages REPO [--all]       the package-to-package dependency list
    dot REPO [-o FILE]          the package graph as Graphviz, for a picture

  Phase 3 — registry and duplicates
    registry [REPO] [--write]   what each module is for, quoted from the code
    duplicates [REPO] [--tests] [--all]
                                 code that could be one shared implementation

  Phase 4 — change impact
    impact PATH... [--all]      what changing these files affects, and which tests cover it
    impact --diff REPO [--all]  the same, for a repo's current uncommitted changes

  Phase 5 — automated review
    review [REPO] [--all]       deterministic findings against the codebase's own rules

Run from the codeintel/ directory. The index lives in .data/ and never
leaves this machine.
"""

from __future__ import annotations

import argparse
import sys

from . import architecture, duplicates, graph, impact, registry, review, store
from .scanner import scan


def _stats(conn) -> None:
    last = conn.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone()
    print(f"last scan: {last[0] if last else 'never'}\n")
    print(f"{'repo':<12} {'lang':<11} {'files':>6} {'tests':>6} {'lines':>8} {'classes':>8} {'funcs':>7} {'errors':>7}")
    for r in conn.execute("""
        SELECT f.repo, f.language, COUNT(DISTINCT f.id) files, SUM(f.is_test) tests,
               (SELECT SUM(lines) FROM files x WHERE x.repo=f.repo) lines,
               (SELECT COUNT(*) FROM symbols s JOIN files y ON y.id=s.file_id
                 WHERE y.repo=f.repo AND s.kind='class') classes,
               (SELECT COUNT(*) FROM symbols s JOIN files y ON y.id=s.file_id
                 WHERE y.repo=f.repo AND s.kind!='class') funcs,
               SUM(f.parse_error IS NOT NULL) errors
        FROM files f GROUP BY f.repo, f.language ORDER BY f.repo"""):
        print(f"{r['repo']:<12} {r['language']:<11} {r['files']:>6} {r['tests']:>6} {r['lines']:>8} "
              f"{r['classes']:>8} {r['funcs']:>7} {r['errors']:>7}")
    own = conn.execute("SELECT COUNT(*) FROM imports WHERE resolved IS NOT NULL").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
    print(f"\nimports: {total} total, {own} resolved to our own code")


def _find(conn, name: str) -> None:
    rows = conn.execute("""
        SELECT f.repo, f.path, s.kind, s.qualname, s.start_line, s.end_line, s.signature, s.doc
        FROM symbols s JOIN files f ON f.id=s.file_id
        WHERE s.name LIKE ? COLLATE NOCASE ORDER BY s.name, f.repo, f.path""", (f"%{name}%",)).fetchall()
    if not rows:
        print(f"no symbol matching {name!r}")
        return
    for r in rows:
        sig = f"({r['signature']})" if r["kind"] != "class" and r["signature"] is not None else ""
        print(f"{r['repo']}/{r['path']}:{r['start_line']}-{r['end_line']}  {r['kind']:<8} {r['qualname']}{sig}")
        if r["doc"]:
            print(f"    {r['doc']}")
    print(f"\n{len(rows)} match(es)")


def _file(conn, path: str) -> None:
    f = conn.execute("SELECT * FROM files WHERE path=? OR repo||'/'||path=? LIMIT 1", (path, path)).fetchone()
    if not f:
        print(f"not indexed: {path}")
        return
    print(f"{f['repo']}/{f['path']}  ({f['language']}, {f['lines']} lines{', test' if f['is_test'] else ''})")
    print(f"module: {f['module']}")
    if f["parse_error"]:
        print(f"PARSE ERROR: {f['parse_error']}")
    print("\nsymbols:")
    for s in conn.execute("SELECT * FROM symbols WHERE file_id=? ORDER BY start_line", (f["id"],)):
        indent = "    " if s["parent"] else "  "
        print(f"{indent}{s['start_line']:>5}  {s['kind']:<8} {s['name']}")
    print("\nimports:")
    for i in conn.execute("SELECT * FROM imports WHERE file_id=? ORDER BY line, id", (f["id"],)):
        what = i["target"]
        if i["name"]:
            what = f"from {i['target']} import {i['name']}" + (f" as {i['alias']}" if i["alias"] else "")
        if not i["resolved"]:
            where = "(external)"
        elif i["defined_in"] and i["defined_in"] != i["resolved"]:
            where = f"-> {i['defined_in']}  (via {i['resolved']})"
        else:
            where = f"-> {i['resolved']}"
        print(f"  {i['line']:>5}  {what:<50} {where}")


# ------------------------------------------------------------ phase 2
# Every report below leads with the answer and keeps the detail behind a
# flag: easy to read at a glance, nothing hidden that you cannot ask for.

REPOS = ("backend", "mobile", "admin_panel")
SHOW = 12                                   # list length before "--all"


def _listing(items: list[str], show_all: bool, indent: str = "    ") -> None:
    for x in items if show_all else items[:SHOW]:
        print(f"{indent}{x}")
    if not show_all and len(items) > SHOW:
        print(f"{indent}… {len(items) - SHOW} more (--all)")


def _deps(conn, path: str, show_all: bool, view: str) -> None:
    found = graph.repo_of(conn, path)
    if not found:
        print(f"not indexed: {path}")
        return
    repo, rel = found
    es = graph.edges(conn, repo, view)
    adj = graph.adjacency(es)
    out_direct = sorted(adj.get(rel, ()))
    in_direct = sorted(s for (s, d) in es if d == rel)
    out_all = graph.reach(adj, rel)
    in_all = graph.reach(graph.reverse(adj), rel)

    def label(other: str, edge) -> str:
        return other + ("   (inside a function)" if edge and not edge.module_level else "")

    print(f"{repo}/{rel}   [{view} view]\n")
    print(f"  depends on   {len(out_direct)} directly, {len(out_all)} in total")
    _listing([label(d, es.get((rel, d))) for d in out_direct], show_all)
    if show_all and len(out_all) > len(out_direct):
        print("    — indirectly:")
        _listing([f"{d}  ({n} hops)" for d, n in sorted(out_all.items(), key=lambda kv: (kv[1], kv[0]))
                  if n > 1], True, "      ")
    print(f"\n  used by      {len(in_direct)} directly, {len(in_all)} in total")
    _listing([label(s, es.get((s, rel))) for s in in_direct], show_all)
    if show_all and len(in_all) > len(in_direct):
        print("    — indirectly:")
        _listing([f"{s}  ({n} hops)" for s, n in sorted(in_all.items(), key=lambda kv: (kv[1], kv[0]))
                  if n > 1], True, "      ")


def _cycles(conn, repos: list[str], view: str) -> None:
    for repo in repos:
        found = graph.cycles(conn, repo, view)
        live = [c for c in found if c.module_level]
        broken = [c for c in found if not c.module_level]
        lang = conn.execute("SELECT language FROM files WHERE repo=? LIMIT 1", (repo,)).fetchone()
        verdict = "none" if not live else f"{len(live)}"
        print(f"{repo}: live cycles {verdict}" + (f", already broken {len(broken)}" if broken else ""))
        for c in live:
            print(f"    LIVE    {' → '.join(c.loop)}")
        for c in broken:
            print(f"    broken  {' → '.join(c.loop)}   (closes only inside a function)")
        if live and lang and lang[0] != "python":
            print("            Dart/JavaScript load cyclic imports fine — a design question, not a crash.")
    print()


def _arch(conn, repos: list[str]) -> None:
    for repo in repos:
        r = architecture.review(conn, repo)
        knots = r.knots
        print(f"{repo}")
        if r.documented:
            held = sum(x.holds for x in r.rules)
            print(f"  documented rules   {held}/{len(r.rules)} hold")
            for x in r.rules:
                print(f"    {'✓' if x.holds else '✗'} {x.rule.name}")
                print(f"        source: {x.rule.source}")
                for f, line, d in x.breaches:
                    print(f"        breach: {f}:{line} imports {d}")
        else:
            print("  documented rules   none — this repo's layering is not written down anywhere,")
            print("                     so nothing is judged; the knots below are facts for you to weigh")
        print(f"  package knots      {len(knots)}  (packages that import each other, level {r.level})")

        # A hub: one file on the thin side of several knots. Shown once,
        # rather than as near-identical blocks that bury the point.
        hubs, rest = architecture.group_knots(knots)

        for src, ks in hubs.items():
            print(f"    {src} reaches back into {len(ks)} packages that all import it")
            parts = 0
            for k in ks:
                (_, b), thin = k.thinnest()
                bits = []
                for _, line, d, _, is_part in thin:
                    bits.append(f"{d.removeprefix(b + '/')}{' (part)' if is_part else ''}")
                    parts += is_part
                print(f"        {b:<26} {' · '.join(bits)}")
            if parts:
                print(f"        (part) = a Dart `part`: that file is a piece of {src.rsplit('/', 1)[-1]}'s own")
                print(f"        library, stored in another folder — one library spread over {len(ks) + 1} folders")
        for k in rest:
            (a, b), thin = k.thinnest()
            shape = " ⇄ ".join(k.packages) if len(k.packages) == 2 else " → ".join(k.packages + k.packages[:1])
            tag = "" if k.live else "   (only through a function-local import)"
            print(f"    {shape}{tag}")
            print(f"        thin side {a} → {b}: {len(thin)} import(s)")
            for s, line, d, _, is_part in thin[:3]:
                print(f"          {s}:{line} → {d}{'  (part)' if is_part else ''}")
            if len(thin) > 3:
                print(f"          … {len(thin) - 3} more")
        print()


def _packages(conn, repo: str, level: int | None, show_all: bool, view: str) -> None:
    level = level or architecture.DEFAULT_LEVEL.get(repo, 2)
    pk = graph.package_edges(conn, repo, level, view)
    print(f"{repo}: {len(pk)} package-to-package dependencies (level {level}, {view} view, tests excluded)\n")
    _listing([f"{s:<32} → {d:<32} {n:>3}" for (s, d), n in pk.items()], show_all, "  ")



# ------------------------------------------------------------ phase 3

def _registry(conn, repos: list[str], write: bool) -> None:
    from .scanner import default_repos
    roots = {r.name: r.root for r in default_repos()}
    for repo in repos:
        mods = registry.modules(conn, repo)
        cov = registry.coverage(mods)
        target = roots[repo] / "MODULE_REGISTRY.md"
        print(f"{repo}: {cov['modules']} modules · {cov['modules_with_statement']} state their purpose · "
              f"{cov['files_with_purpose']}/{cov['files']} files have a purpose line")
        if write:
            target.write_text(registry.render(repo, mods))
            print(f"    wrote {target}")
        else:
            print(f"    would write {target}  (--write)")


def _duplicates(conn, repos: list[str], include_tests: bool, show_all: bool) -> None:
    from .scanner import default_repos
    root = default_repos()[0].root.parent
    found = duplicates.find(conn, root, include_tests=include_tests)

    def tag(d) -> str:
        return "identical" if d.identical else f"{len(d.differences)} param(s)"

    def diff_line(d) -> str:
        return "varies: " + "; ".join(f"{a[:40]!r} → {b[:40]!r}" for a, b in d.differences)

    for repo in repos:
        mine = [d for d in found if d.members[0].repo == repo]
        # Pairs are concrete and actionable — "these two could be one
        # function". Three or more of the same shape is usually a repeated
        # IDIOM (every route handler, every migration script) rather than
        # something to merge, and listing all of its members buries the
        # pairs under one giant block — so it is summarized, not expanded.
        pairs = [d for d in mine if len(d.members) == 2]
        patterns = [d for d in mine if len(d.members) > 2]
        ident = sum(d.identical for d in pairs)
        print(f"{repo}: {len(pairs)} pair(s) ({ident} identical), {len(patterns)} repeated pattern(s) across 3+ places"
              + ("" if include_tests else "  — test files excluded, --tests to include"))

        for d in pairs if show_all else pairs[:SHOW]:
            print(f"\n  [{tag(d)}] {d.lines} lines   found by: {', '.join(sorted(d.found_by))}")
            for m in d.members:
                print(f"      {m}")
            if d.differences:
                print(f"      {diff_line(d)}")
        if not show_all and len(pairs) > SHOW:
            print(f"\n  … {len(pairs) - SHOW} more pair(s) (--all)")

        if patterns:
            print("\n  repeated patterns (not pairs to merge — one shape used everywhere):")
            for d in patterns:
                print(f"\n    [{tag(d)}] {d.lines} lines × {len(d.members)} places   found by: {', '.join(sorted(d.found_by))}")
                shown_members = d.members if show_all else d.members[:3]
                for m in shown_members:
                    print(f"        {m}")
                if not show_all and len(d.members) > len(shown_members):
                    print(f"        … {len(d.members) - len(shown_members)} more (--all)")
                if d.differences:
                    print(f"        {diff_line(d)}")
        print()


# ------------------------------------------------------------ phase 4

def _impact(report: impact.ImpactReport, show_all: bool) -> None:
    if report.repo == "?":
        for p in report.unresolved:
            print(f"not part of any known repo: {p}")
        return

    print(f"{report.repo}")
    for path, fi in report.changed.items():
        tag = f"[{fi.status}" + (f" -> {fi.new_path}]" if fi.new_path else "]")
        print(f"  {path}  {tag}")
        for s in fi.symbols:
            print(f"      {s.kind:<8} {s.qualname:<40} {s.start_line}-{s.end_line}")
    for p in report.unresolved:
        print(f"  {p}  [not in the index — never scanned, or outside the source/test dirs]")

    tests, other = report.tests, report.non_test_dependents
    print(f"\n  affects {len(report.dependents)} file(s): {len(tests)} test(s), {len(other)} other")
    if tests:
        print("\n  tests to run:")
        _listing([f"{d.path}  ({d.hops} hop{'s' if d.hops != 1 else ''})" for d in tests], show_all)
    if other:
        print("\n  other dependents:")
        _listing([f"{d.path}  ({d.hops} hop{'s' if d.hops != 1 else ''})" for d in other], show_all)
    print()


# ------------------------------------------------------------ phase 5

def _review(repo: str, findings: list[review.Finding], show_all: bool) -> None:
    counts = {s: sum(1 for f in findings if f.severity == s) for s in ("error", "warn", "info")}
    summary = ", ".join(f"{counts[s]} {s}" for s in ("error", "warn", "info") if counts[s])
    print(f"{repo}: {len(findings)} finding(s)" + (f" — {summary}" if summary else " — clean"))

    # error/warn are rare enough to always show in full; info (duplicates,
    # misplaced Dart parts) is the bulk of the volume and goes behind --all,
    # same convention as every other report here.
    actionable = [f for f in findings if f.severity != "info"]
    info = [f for f in findings if f.severity == "info"]
    for f in actionable:
        where = f"{f.file}:{f.line}" if f.line is not None else f.file
        print(f"\n  [{f.severity}] {f.rule}  {where}")
        print(f"      {f.message}")
        if f.source:
            print(f"      source: {f.source}")
    if info:
        print(f"\n  info-level ({len(info)}):")
        _listing([f"[{f.rule}] {(f'{f.file}:{f.line}' if f.line is not None else f.file)}  {f.message}"
                  for f in info], show_all)
    print()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="codeintel", description=__doc__.splitlines()[0])
    p.add_argument("--db", default=str(store.DEFAULT_DB), help="index file (default: .data/index.db)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="build or refresh the index")
    s.add_argument("--full", action="store_true", help="re-parse every file, not just changed ones")
    sub.add_parser("stats", help="summary of what is indexed")
    f = sub.add_parser("find", help="locate a symbol by name")
    f.add_argument("name")
    fi = sub.add_parser("file", help="show one file's symbols and imports")
    fi.add_argument("path")

    def view_flag(sp):
        sp.add_argument("--runtime", action="store_true",
                        help="follow imports as written (through __init__.py), not to where names are defined")

    d = sub.add_parser("deps", help="what a file depends on, and what depends on it")
    d.add_argument("path")
    d.add_argument("--all", action="store_true", help="list everything, including indirect")
    view_flag(d)
    c = sub.add_parser("cycles", help="import cycles, live vs already broken")
    c.add_argument("repo", nargs="?", choices=REPOS)
    view_flag(c)
    a = sub.add_parser("arch", help="documented rules, and packages tangled together")
    a.add_argument("repo", nargs="?", choices=REPOS)
    pk = sub.add_parser("packages", help="package-to-package dependencies")
    pk.add_argument("repo", choices=REPOS)
    pk.add_argument("--level", type=int, help="how many path segments make a package")
    pk.add_argument("--all", action="store_true")
    view_flag(pk)
    dt = sub.add_parser("dot", help="the package graph as Graphviz text")
    dt.add_argument("repo", choices=REPOS)
    dt.add_argument("--level", type=int)
    dt.add_argument("-o", "--output", help="write here instead of stdout")
    view_flag(dt)
    rg = sub.add_parser("registry", help="what each module is for, quoted from the code")
    rg.add_argument("repo", nargs="?", choices=REPOS)
    rg.add_argument("--write", action="store_true", help="write MODULE_REGISTRY.md into the repo")
    du = sub.add_parser("duplicates", help="code that could be one shared implementation")
    du.add_argument("repo", nargs="?", choices=REPOS)
    du.add_argument("--tests", action="store_true", help="include test files (excluded by default)")
    du.add_argument("--all", action="store_true", help="list every group, not just the first ones")
    im = sub.add_parser("impact", help="what changing these files affects, and which tests cover it")
    im.add_argument("paths", nargs="*", help="files to treat as fully changed")
    im.add_argument("--diff", choices=REPOS, help="use this repo's own uncommitted changes instead of PATHS")
    im.add_argument("--all", action="store_true", help="list every dependent, not just the first ones")
    rv = sub.add_parser("review", help="deterministic findings against the codebase's own rules")
    rv.add_argument("repo", nargs="?", choices=REPOS)
    rv.add_argument("--all", action="store_true", help="list every info-level finding, not just the first ones")
    args = p.parse_args(argv)

    conn = store.connect(args.db)
    if args.cmd == "scan":
        result = scan(conn, full=args.full)
        print(" ".join(f"{k}={v}" for k, v in result.items()))
    elif args.cmd == "stats":
        _stats(conn)
    elif args.cmd == "find":
        _find(conn, args.name)
    elif args.cmd == "file":
        _file(conn, args.path)
    elif args.cmd in ("deps", "cycles", "packages", "dot"):
        view = "runtime" if args.runtime else "design"
        if args.cmd == "deps":
            _deps(conn, args.path, args.all, view)
        elif args.cmd == "cycles":
            _cycles(conn, [args.repo] if args.repo else list(REPOS), view)
        elif args.cmd == "packages":
            _packages(conn, args.repo, args.level, args.all, view)
        else:
            level = args.level or architecture.DEFAULT_LEVEL.get(args.repo, 2)
            dot = graph.to_dot(graph.package_edges(conn, args.repo, level, view), args.repo)
            if args.output:
                with open(args.output, "w") as fh:
                    fh.write(dot + "\n")
                print(f"wrote {args.output} — render with: dot -Tsvg {args.output} -o graph.svg")
            else:
                print(dot)
    elif args.cmd == "arch":
        _arch(conn, [args.repo] if args.repo else list(REPOS))
    elif args.cmd == "registry":
        _registry(conn, [args.repo] if args.repo else list(REPOS), args.write)
    elif args.cmd == "duplicates":
        _duplicates(conn, [args.repo] if args.repo else list(REPOS), args.tests, args.all)
    elif args.cmd == "impact":
        if args.diff:
            from .scanner import default_repos
            root = next(r.root for r in default_repos() if r.name == args.diff)
            diff_text = impact.live_diff(root)
            if not diff_text.strip():
                print(f"{args.diff}: no uncommitted changes")
            else:
                _impact(impact.from_diff_text(conn, args.diff, diff_text), args.all)
        elif args.paths:
            for report in impact.from_paths(conn, args.paths).values():
                _impact(report, args.all)
        else:
            print("give one or more PATHS, or --diff REPO")
    elif args.cmd == "review":
        from .scanner import default_repos
        root = default_repos()[0].root.parent
        for repo in ([args.repo] if args.repo else list(REPOS)):
            _review(repo, review.review(conn, root, repo), args.all)
    return 0


if __name__ == "__main__":
    sys.exit(main())
