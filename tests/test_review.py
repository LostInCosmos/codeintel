"""Phase 5: the automated architecture reviewer.

Most checks here wrap primitives Phases 1-3 already verified against
independent ground truths (grimp, ast, networkx, ...) — see test_graph.py and
test_duplicates.py for those. What these tests cover is the NEW judgment on
top: which of that already-correct data counts as a finding, at what
severity, and — for `mixed_files`, the one genuinely new measurement — the
calibration that kept it from flooding the report.

`mixed_files` has no independent ground truth to check it against, unlike
every earlier phase. Calibrating it against the real repos found that
allowing a lone one-symbol group produced 14 findings, almost all a small
self-contained helper sitting beside one real cluster — not evidence of
anything scattered, just the index's blindness to calls (it tracks
definitions and imports, never who calls whom). Requiring every group to
have 2+ members cut that to exactly one true finding. The regression test
for that exact shape — a cluster plus an unrelated singleton — is the most
important test in this file.
"""

import textwrap
from pathlib import Path

import pytest

from codeintel import review, store
from codeintel.scanner import Repo, scan


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


@pytest.fixture()
def conn(tmp_path):
    return store.connect(tmp_path / "i.db")


# --------------------------------------------------------------- broken imports

def test_an_unresolved_relative_import_is_always_broken(conn, tmp_path):
    root = tmp_path / "backend"
    write(root, "app/__init__.py", "")
    write(root, "scripts/__init__.py", "")
    write(root, "scripts/migrate.py", "from ..models import Thing\n")   # app/ has no 'models'
    scan(conn, [Repo("backend", root, "python", ("app", "scripts"), ())])
    found = review.broken_imports(conn, "backend")
    assert len(found) == 1
    assert found[0].file == "scripts/migrate.py" and found[0].severity == "error"


def test_a_resolving_relative_import_is_not_flagged(conn, tmp_path):
    root = tmp_path / "backend"
    write(root, "app/__init__.py", "")
    write(root, "app/models.py", "class Thing: pass\n")
    write(root, "app/user.py", "from .models import Thing\n")
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    assert review.broken_imports(conn, "backend") == []


def test_js_relative_import_of_a_non_source_asset_is_excluded(conn, tmp_path):
    """Real false positive found calibrating this check: a JS file can
    relatively import a non-source asset. This index only reads source
    files, so such an import can never resolve here -- that is outside what
    it tracks, not a defect."""
    root = tmp_path / "admin_panel"
    write(root, "src/main.jsx", "import './index.css';\n")
    scan(conn, [Repo("admin_panel", root, "javascript", ("src",), ())])
    assert review.broken_imports(conn, "admin_panel") == []


def test_js_relative_import_of_a_missing_source_file_is_still_caught(conn, tmp_path):
    root = tmp_path / "admin_panel"
    write(root, "src/main.jsx", "import Foo from './components/Foo.jsx';\n")
    scan(conn, [Repo("admin_panel", root, "javascript", ("src",), ())])
    found = review.broken_imports(conn, "admin_panel")
    assert len(found) == 1 and found[0].file == "src/main.jsx"


def test_a_non_relative_import_that_cannot_resolve_is_never_flagged(conn, tmp_path):
    """Could be a real external package -- only a relative import is
    guaranteed to name our own code."""
    root = tmp_path / "backend"
    write(root, "app/__init__.py", "")
    write(root, "app/user.py", "import some_external_package\n")
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    assert review.broken_imports(conn, "backend") == []


# --------------------------------------------------------------- orphan modules

@pytest.fixture()
def orphan_repo(tmp_path, conn):
    root = tmp_path / "backend"
    write(root, "app/__init__.py", "")
    write(root, "app/main.py", "from .api import x\n")
    write(root, "app/api/__init__.py", "")
    write(root, "app/api/x.py", "def route(): pass\n")
    write(root, "app/dead.py", "def never_imported(): pass\n")
    write(root, "ops/upload.py", "def run(): pass\n")
    write(root, "scripts/migrate.py", "def run(): pass\n")
    write(root, "tests/test_x.py", "from app.api import x\n")
    scan(conn, [Repo("backend", root, "python", ("app", "ops", "scripts"), ("tests",))])
    return root


def test_a_file_nothing_imports_is_an_orphan(conn, orphan_repo):
    found = {f.file for f in review.orphan_modules(conn, "backend")}
    assert found == {"app/dead.py"}


def test_init_files_are_never_orphans_even_when_unimported(conn, orphan_repo):
    assert "app/api/__init__.py" not in {f.file for f in review.orphan_modules(conn, "backend")}


def test_entry_points_are_excluded_main_ops_and_scripts(conn, orphan_repo):
    found = {f.file for f in review.orphan_modules(conn, "backend")}
    assert "app/main.py" not in found
    assert "ops/upload.py" not in found
    assert "scripts/migrate.py" not in found


def test_test_files_are_never_orphans(conn, orphan_repo):
    assert "tests/test_x.py" not in {f.file for f in review.orphan_modules(conn, "backend")}


# ---------------------------------------------------------------------- layering

def test_a_layering_breach_becomes_an_error_finding(conn, tmp_path):
    root = tmp_path / "backend"
    for pkg in ("app", "app/api", "app/services"):
        write(root, f"{pkg}/__init__.py", "")
    write(root, "app/api/invoice.py", "def route(): pass\n")
    write(root, "app/main.py", "from .api import invoice\n")
    write(root, "app/services/bad.py", "from ..api import invoice\n")   # the breach
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    found = review.layering(conn, "backend")
    assert len(found) == 1
    assert found[0].severity == "error" and found[0].file == "app/services/bad.py"
    assert found[0].source and "Architecture" in found[0].source


def test_clean_layering_produces_no_findings(conn, tmp_path):
    root = tmp_path / "backend"
    for pkg in ("app", "app/api"):
        write(root, f"{pkg}/__init__.py", "")
    write(root, "app/api/invoice.py", "def route(): pass\n")
    write(root, "app/main.py", "from .api import invoice\n")
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    assert review.layering(conn, "backend") == []


# ------------------------------------------------------------------ package knots

def test_a_plain_knot_is_reported_once_with_its_thin_side(conn, tmp_path):
    root = tmp_path / "backend"
    write(root, "app/a/one.py", "from ..b import two\n")
    write(root, "app/b/two.py", "from ..a import one\n")
    for pkg in ("app", "app/a", "app/b"):
        write(root, f"{pkg}/__init__.py", "")
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    found = review.package_knots(conn, "backend")
    assert len(found) == 1 and found[0].rule == "package-knot"


def test_a_hub_behind_three_or_more_knots_is_one_finding_not_several(conn, tmp_path):
    """hub.py sits loose in app/, which also holds sub-packages f0/f1/f2 --
    so hub.py is its own unit (the same 'loose root file' rule as graph.py),
    and importing/being-imported-by each of three sub-packages makes it the
    thin-side hub of three separate knots."""
    # The "thin side" is measured in FILE PAIRS crossing the package
    # boundary, not import statements. Each f{i} package has two files
    # importing hub (two file-pairs f{i}->hub) while hub imports back from
    # only one of them (one file-pair hub->f{i}) -- that asymmetry is what
    # makes app/hub.py the unambiguous thin side for every pair, so all
    # three knots share the same hub source instead of each picking
    # whichever side happens to sort first alphabetically.
    root = tmp_path / "backend"
    write(root, "app/__init__.py", "")
    write(root, "app/hub.py", "SHARED = 1\n" + "\n".join(f"from .f{i}.mod import x{i}" for i in range(3)))
    for i in range(3):
        write(root, f"app/f{i}/__init__.py", "")
        write(root, f"app/f{i}/mod.py", f"from ..hub import SHARED\ndef x{i}(): pass\n")
        write(root, f"app/f{i}/extra.py", "from ..hub import SHARED\n")
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    found = review.package_knots(conn, "backend")
    hub_findings = [f for f in found if f.file == "app/hub.py"]
    assert len(hub_findings) == 1, "a hub behind 3+ knots must collapse into one finding, not N repeats"
    assert "reaches back into 3" in hub_findings[0].message


# ------------------------------------------------------------ misplaced dart parts

def test_a_part_outside_its_librarys_folder_is_flagged(conn, tmp_path):
    root = tmp_path / "mobile"
    write(root, "lib/core/client.dart", "part '../features/a/a_part.dart';\n")
    write(root, "lib/features/a/a_part.dart", "part of '../../core/client.dart';\n")
    scan(conn, [Repo("mobile", root, "dart", ("lib",), ())])
    found = review.misplaced_dart_parts(conn, "mobile")
    assert len(found) == 1 and found[0].file == "lib/features/a/a_part.dart"
    assert found[0].severity == "info"


def test_a_part_in_the_same_folder_is_not_flagged(conn, tmp_path):
    root = tmp_path / "mobile"
    write(root, "lib/core/client.dart", "part 'client_part.dart';\n")
    write(root, "lib/core/client_part.dart", "part of 'client.dart';\n")
    scan(conn, [Repo("mobile", root, "dart", ("lib",), ())])
    assert review.misplaced_dart_parts(conn, "mobile") == []


def test_non_mobile_repos_have_no_dart_parts_to_report(conn, orphan_repo):
    assert review.misplaced_dart_parts(conn, "backend") == []


# -------------------------------------------------------------------- duplicates

def test_a_mergeable_pair_is_reported(conn, tmp_path):
    root = tmp_path / "backend"
    body = "x = 1\ny = 2\nz = x + y\nreturn z\n"
    write(root, "app/a.py", f"def calc():\n{textwrap.indent(body, '    ')}\n")
    write(root, "app/b.py", f"def calc():\n{textwrap.indent(body, '    ')}\n")
    write(root, "app/__init__.py", "")
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    found = review.duplicate_pairs(conn, root.parent, "backend")
    assert len(found) == 1 and found[0].severity == "info"


def test_a_repeated_pattern_of_three_or_more_is_not_a_reviewer_finding(conn, tmp_path):
    """3+ near-identical copies are usually a documented shape (every route
    handler), not something to merge -- Phase 3's own framing. The reviewer
    only reports mergeable pairs."""
    root = tmp_path / "backend"
    body = "x = 1\ny = 2\nz = x + y\nreturn z\n"
    for n in "abc":
        write(root, f"app/{n}.py", f"def calc():\n{textwrap.indent(body, '    ')}\n")
    write(root, "app/__init__.py", "")
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    assert review.duplicate_pairs(conn, root.parent, "backend") == []


# ------------------------------------------------------------------- mixed files

def _mixed_repo(tmp_path, conn, body: str) -> Path:
    root = tmp_path / "backend"
    write(root, "app/__init__.py", "")
    write(root, "app/m.py", body)
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    return root.parent


def test_two_genuinely_unrelated_clusters_are_flagged(tmp_path, conn):
    root = _mixed_repo(tmp_path, conn, """\
        def usage_a(): return cost_a()
        def cost_a(): return 1
        def pricing_b(): return table_b()
        def table_b(): return {}
        """)
    found = review.mixed_files(conn, root, "backend")
    assert len(found) == 1 and found[0].file == "app/m.py"


def test_a_cluster_plus_one_unrelated_singleton_is_not_flagged(tmp_path, conn):
    """The exact shape that produced 14 false positives before the 2+-per-
    group rule: a well-organised file with one small, self-contained helper
    that the index's call-blindness makes look 'disconnected'."""
    root = _mixed_repo(tmp_path, conn, """\
        def get_client(): return 1
        def get(k): return get_client()
        def set(k, v): return get_client()
        def reset_for_tests(): pass
        """)
    assert review.mixed_files(conn, root, "backend") == []


def test_symbols_linked_only_through_a_shared_import_count_as_one_group(tmp_path, conn):
    root = tmp_path / "backend"
    write(root, "app/__init__.py", "")
    write(root, "app/shared.py", "THING = 1\n")
    write(root, "app/m.py", """\
        from .shared import THING
        def one(): return THING
        def two(): return THING
        def three(): return one() + two()
        def four(): return THING
        """)
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    assert review.mixed_files(conn, root.parent, "backend") == []


def test_files_with_fewer_than_four_top_level_symbols_are_never_flagged(tmp_path, conn):
    root = _mixed_repo(tmp_path, conn, "def a(): return 1\ndef b(): return 2\n")
    assert review.mixed_files(conn, root, "backend") == []


# -------------------------------------------------------------------------- review()

def test_review_sorts_by_severity_then_rule_then_file(conn, tmp_path):
    root = tmp_path / "backend"
    write(root, "app/__init__.py", "")
    write(root, "app/scripts_dir_placeholder.py", "x = 1\n")   # keeps the repo non-trivial
    write(root, "app/bad_import.py", "from .nope import X\n")   # broken-import: error
    write(root, "app/dead.py", "def f(): pass\n")                # orphan-module: warn
    scan(conn, [Repo("backend", root, "python", ("app",), ())])
    found = review.review(conn, root.parent, "backend")
    severities = [f.severity for f in found]
    assert severities == sorted(severities, key=lambda s: review.SEVERITY_ORDER[s])
    assert found[0].rule == "broken-import", "errors must sort before warnings"
