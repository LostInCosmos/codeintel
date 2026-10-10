# codeintel

A local codebase-intelligence index for GSTApp: every source file, symbol and
import across `backend/` (Python), `mobile/` (Dart) and `admin_panel/`
(JavaScript), in one SQLite file.

**Local only.** The index lives in `.data/` (git-ignored), is rebuilt from
source on demand, and is never part of any deploy. Nothing here is imported
by the apps.

Design, verification and limits: the vault note **Codebase Intelligence**
(`~/Desktop/GSTApp/memory/GSTApp/14 - Tooling/`). Phases still to
build: **Codebase Intelligence Roadmap** in `12 - Roadmap/`.

## Use

```bash
cd ~/Desktop/GSTApp/codeintel
alias ci='.venv/bin/python -m codeintel'

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
```

Every report leads with the answer and keeps detail behind `--all`.
`--runtime` on `deps`/`cycles`/`packages`/`dot` follows imports as written
(through `__init__.py`) instead of to where names are really defined.

First run on a fresh checkout:

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
```

The tool itself needs nothing beyond the standard library; `pytest` is only
for its tests.

## Test

```bash
.venv/bin/python -m pytest tests/ -q
```

## Layout

| file | responsibility |
|---|---|
| `codeintel/store.py` | the SQLite schema; the one store every command reads |
| `codeintel/extractors.py` | source text → symbols + imports, one function per language |
| `codeintel/scanner.py` | walks the repos, incremental re-parse, import resolution |
| `codeintel/graph.py` | dependency graph: edges, reach, cycles, package knots, Graphviz |
| `codeintel/architecture.py` | documented layering rules, each citing its vault source |
| `codeintel/__main__.py` | the CLI, one section per phase |
