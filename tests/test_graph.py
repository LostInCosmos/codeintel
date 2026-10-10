"""Phase 2: the dependency graph over a throwaway repo, where every edge and
cycle is known in advance."""

import sqlite3
import textwrap
from pathlib import Path

import pytest

from codeintel import graph, store
from codeintel.scanner import Repo, scan


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    root = tmp_path / "backend"
    for pkg in ("app", "app/models", "app/services", "app/services/invoice", "app/api", "app/workers"):
        write(root, f"{pkg}/__init__.py", "")
    write(root, "app/models/__init__.py", "from .tenant import Tenant\nfrom .invoice import Invoice\n")
    write(root, "app/models/tenant.py", "class Tenant: pass\n")
    write(root, "app/models/invoice.py", "from .tenant import Tenant\nclass Invoice: pass\n")
    write(root, "app/services/invoice/handlers.py", """\
        from ...models import Tenant, Invoice
        def save(): pass
        def later():
            from ...workers import worker      # deliberately local: worker imports us
        """)
    write(root, "app/workers/worker.py", "from ..services.invoice import handlers\n")
    write(root, "app/api/invoice.py", "from ..services.invoice import handlers\nfrom ..models import Tenant\n")
    # a live cycle: a <-> b at module level
    write(root, "app/services/a.py", "from . import b\n")
    write(root, "app/services/b.py", "from . import a\n")
    write(root, "tests/test_api.py", "from app.api import invoice\n")
    c = store.connect(tmp_path / "index.db")
    scan(c, [Repo("backend", root, "python", ("app",), ("tests",))])
    return c


def test_design_edges_follow_reexports_runtime_edges_do_not(conn):
    design = graph.edges(conn, "backend", "design")
    runtime = graph.edges(conn, "backend", "runtime")
    src = "app/api/invoice.py"
    assert {d for s, d in design if s == src} == {"app/services/invoice/handlers.py", "app/models/tenant.py"}
    assert {d for s, d in runtime if s == src} == {"app/services/invoice/handlers.py", "app/models/__init__.py"}


def test_an_edge_knows_whether_it_runs_at_import_time(conn):
    es = graph.edges(conn, "backend")
    assert es[("app/services/invoice/handlers.py", "app/workers/worker.py")].module_level is False
    assert es[("app/workers/worker.py", "app/services/invoice/handlers.py")].module_level is True


def test_reach_gives_transitive_dependents_with_distance(conn):
    adj = graph.adjacency(graph.edges(conn, "backend"))
    dependents = graph.reach(graph.reverse(adj), "app/models/tenant.py")
    assert dependents["app/models/invoice.py"] == 1
    assert dependents["app/api/invoice.py"] == 1
    assert dependents["tests/test_api.py"] == 2          # via app/api/invoice.py
    assert "app/services/a.py" not in dependents


def test_a_live_cycle_and_a_deliberately_broken_one_are_told_apart(conn):
    found = {tuple(c.files): c for c in graph.cycles(conn, "backend")}
    live = found[("app/services/a.py", "app/services/b.py")]
    assert live.module_level is True
    assert live.loop[0] == live.loop[-1] and len(live.loop) == 3
    broken = found[("app/services/invoice/handlers.py", "app/workers/worker.py")]
    assert broken.module_level is False, "closes only through a function-local import"


def test_package_rollup_counts_file_edges_and_drops_internal_ones(conn):
    pk = graph.package_edges(conn, "backend")
    assert pk[("app/api", "app/services/invoice")] == 1
    assert pk[("app/api", "app/models")] == 1
    assert ("app/models", "app/models") not in pk
    assert all(not s.startswith("tests") for s, _ in pk), "tests excluded by default"
    coarse = graph.package_edges(conn, "backend", level=2)
    assert ("app/services", "app/workers") in coarse


def test_cycle_detection_survives_a_long_chain():
    """Iterative Tarjan: a deep graph must not hit the recursion limit."""
    n = 5000
    adj = {f"m{i}": {f"m{i + 1}"} for i in range(n)}
    adj[f"m{n}"] = {"m0"}
    comps = graph.sccs(adj)
    assert len(comps) == 1 and len(comps[0]) == n + 1


def test_dot_output_is_well_formed():
    dot = graph.to_dot({("app/api", "app/services"): 3}, "backend")
    assert dot.startswith('digraph "backend" {') and dot.rstrip().endswith("}")
    assert '"app/api" -> "app/services" [label="3"' in dot


# ---- package knots: pairs, loops, and loose root files ----

def test_package_knots_are_reported_as_pairs_with_their_thin_side(conn):
    knots = {tuple(k.packages): k for k in graph.package_knots(conn, "backend", level=2)}
    pair = knots[("app/services", "app/workers")]
    (a, b), thin = pair.thinnest()
    assert (a, b) == ("app/services", "app/workers")
    assert pair.live is False, "services reaches workers only inside a function"


def test_a_loop_no_pair_explains_is_still_reported(tmp_path):
    root = tmp_path / "r"
    write(root, "app/x/one.py", "from ..y import two\n")
    write(root, "app/y/two.py", "from ..z import three\n")
    write(root, "app/z/three.py", "from ..x import one\n")
    for pkg in ("app", "app/x", "app/y", "app/z"):
        write(root, f"{pkg}/__init__.py", "")
    c = store.connect(tmp_path / "i.db")
    scan(c, [Repo("backend", root, "python", ("app",), ())])
    knots = graph.package_knots(c, "backend", level=2)
    assert len(knots) == 1
    assert sorted(knots[0].packages) == ["app/x", "app/y", "app/z"] and knots[0].live


def test_loose_root_files_are_their_own_unit_not_a_fake_package():
    """Lumping src/App.jsx (the shell) with src/api.js (a helper) made src/
    look tangled with its own sub-folders."""
    paths = {"src/App.jsx", "src/api.js", "src/components/Card.jsx", "scripts/a.py", "scripts/b.py"}
    nested = graph.nested_folders(paths)
    assert graph.package_of("src/App.jsx", 2, nested) == "src/App.jsx"
    assert graph.package_of("src/components/Card.jsx", 2, nested) == "src/components"
    assert graph.package_of("scripts/a.py", 2, nested) == "scripts", "a flat folder is still one package"


def test_dart_part_links_are_marked(tmp_path):
    root = tmp_path / "mobile"
    write(root, "lib/core/api/client.dart", "import '../../features/a/models/m.dart';\npart '../../features/a/data/a_api.dart';\n")
    write(root, "lib/features/a/models/m.dart", "class M {}\n")
    write(root, "lib/features/a/data/a_api.dart", "part of '../../../core/api/client.dart';\n")
    write(root, "lib/features/a/screen.dart", "import '../../core/api/client.dart';\n")
    c = store.connect(tmp_path / "i.db")
    scan(c, [Repo("mobile", root, "dart", ("lib",), ())])
    es = graph.edges(c, "mobile")
    assert es[("lib/core/api/client.dart", "lib/features/a/data/a_api.dart")].is_part
    assert not es[("lib/core/api/client.dart", "lib/features/a/models/m.dart")].is_part


# ---- documented rules: a planted breach must be caught ----

def test_the_route_rule_holds_on_clean_code_and_catches_a_planted_breach(tmp_path):
    from codeintel import architecture
    root = tmp_path / "backend"
    for pkg in ("app", "app/api", "app/services"):
        write(root, f"{pkg}/__init__.py", "")
    write(root, "app/api/invoice.py", "def route(): pass\n")
    write(root, "app/main.py", "from .api import invoice\n")            # allowed: the entry point
    write(root, "tests/test_routes.py", "from app.api import invoice\n")  # allowed: tests
    c = store.connect(tmp_path / "i.db")
    repo = Repo("backend", root, "python", ("app",), ("tests",))
    scan(c, [repo])
    assert architecture.review(c, "backend").rules[0].holds

    write(root, "app/services/bad.py", "from ..api import invoice\n")     # the breach
    scan(c, [repo])
    result = architecture.review(c, "backend").rules[0]
    assert not result.holds
    assert result.breaches == [("app/services/bad.py", 1, "app/api/invoice.py")]


def test_a_repo_without_documented_rules_judges_nothing(conn):
    from codeintel import architecture
    assert architecture.review(conn, "mobile").documented is False
