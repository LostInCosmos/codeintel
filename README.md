# codeintel

A local codebase-intelligence index for Dastavez: every source file, symbol and
import across four repos, in one SQLite file.

| label | folder (first found) | language |
|---|---|---|
| `backend` | `bahi_backend/` or `backend/` | Python |
| `mobile` | `bahi_app/` or `mobile/` | Dart |
| `admin_panel` | `admin_panel/` or `bahi_admin_frontend/` | JavaScript |
| `bahi_frontend` | `bahi_frontend/` or `webapp_frontend/` | JavaScript |

Labels are the names the vault uses. Repos are looked for beside this folder;
whichever are checked out are indexed, and `scan` drops any repo it no longer
finds. **Adding a repo is one entry in `scanner.KNOWN_REPOS`** — the CLI's
choices, module levels and entry points are derived from it.

**Local only.** The index lives in `.data/` (git-ignored), is rebuilt from
source on demand, and is never part of any deploy. Nothing here is imported
by the apps.

Design, verification and limits: the vault note **Codebase Intelligence**
(`~/Desktop/Dastavez/Dastavez-memory/GSTApp/14 - Tooling/`).

## Use

```bash
cd ~/Desktop/Dastavez/codeintel
alias ci='python3 -m codeintel'       # standard library only, no venv needed

# the index
ci scan                          # incremental; --full to re-parse everything
ci stats
ci find claim                    # where is a symbol defined, in any repo?
ci file app/workers/reconciler.py

# dependencies and architecture
ci deps app/workers/reconciler.py    # what it uses, what uses it (--all for indirect)
ci cycles                            # import cycles: live vs already broken
ci arch                              # documented rules + packages that import each other
ci packages backend                  # package-to-package list (--all, --level N)
ci dot backend -o backend.dot        # Graphviz; render with `dot -Tsvg`

# review
ci duplicates backend                # implementations that could be one (--all, --tests)
ci review backend                    # broken imports, orphans, layering, knots, mixed files, duplicates
ci impact app/core/cache.py          # what a change affects, and which tests cover it
ci registry backend --write          # regenerate that repo's MODULE_REGISTRY.md
```

Every report leads with the answer and keeps detail behind `--all`.
`--runtime` on `deps`/`cycles`/`packages`/`dot` follows imports as written
(through `__init__.py`) instead of to where names are really defined.

`registry --write` writes into each repo it is given — name the repo. With no
label it writes into all four, and each repo's owner regenerates their own.

**Duplicates.** "Identical" means identical body tokens: the `def` line,
comments and a leading docstring are not compared. A *thin delegate* — a
function whose whole body is one call with plain arguments (Python only) —
has no logic of its own, so a group made only of them is set aside rather
than reported as something to merge: the workers' `claim_from_db`, every API
route. So is a group of *markup-only* components — JSX that is nothing but
literal markup and `{...spread}` props, differing only in what it draws (the
public site's SVG icons); a bare `{name}` disqualifies one. Both are counted in
the header and listed with `--all`.

## Test

```bash
../.venv-backend/bin/python -m pytest tests/ -q     # 141 tests, under a second
```

Any Python with `pytest` installed works; the tool itself needs only the
standard library.

## Layout

| file | responsibility |
|---|---|
| `codeintel/store.py` | the SQLite schema; the one store every command reads |
| `codeintel/extractors.py` | source text → symbols + imports, one function per language |
| `codeintel/scanner.py` | the repo table (`KNOWN_REPOS`), walks the repos, incremental re-parse, import resolution |
| `codeintel/graph.py` | dependency graph: edges, reach, cycles, package knots, Graphviz |
| `codeintel/architecture.py` | documented layering rules, each citing its vault source |
| `codeintel/duplicates.py` | implementations that could share one body; thin delegates and markup set aside |
| `codeintel/review.py` | the reviewer: one list of findings per repo, by severity |
| `codeintel/impact.py` | what a change affects, and the tests that cover it |
| `codeintel/registry.py` | `MODULE_REGISTRY.md`: what each module is for, quoted from the code |
| `codeintel/__main__.py` | the CLI, one section per phase |
