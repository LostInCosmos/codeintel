"""Phase 4: the change impact analyser.

The diff parser is checked against REAL `git diff` output (captured from an
actual throwaway repo exercising every shape: a multi-position edit, a
multi-hunk edit, an add, a delete, a rename-with-edit, and binary files) —
not hand-typed guesses at the format. Two real bugs were found this way and
are pinned below:

1. A binary file has no `---`/`+++`/hunk lines, so nothing in the first
   version of the parser ever finalised it — it was silently dropped from
   the changed-file list entirely.
2. A deletion hunk's `-` lines never advance the new-file line counter (they
   are not IN the new file), so every deleted line collapsed to the same
   spot near the top of the hunk. Reporting "nothing overlapped" for a file
   being removed entirely would be actively misleading, not just imprecise.
"""

import textwrap
from pathlib import Path

import pytest

from codeintel import impact, store
from codeintel.scanner import Repo, scan


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


# ----------------------------------------------- real diffs, captured verbatim
# via: git init; make each kind of change; `git diff --no-color [--cached] -M30%`

REAL_MODIFIED_MULTI_HUNK = """\
diff --git a/modme.py b/modme.py
index b3c5a95..ea50874 100644
--- a/modme.py
+++ b/modme.py
@@ -1,5 +1,6 @@
 line1
-line2
+line2-CHANGED
 line3
 line4
+NEWLINE-after-4
 line5
"""

REAL_TWO_HUNK_FILE = """\
diff --git a/multihunk.py b/multihunk.py
index 4083766..1526684 100644
--- a/multihunk.py
+++ b/multihunk.py
@@ -1,4 +1,4 @@
-line1
+line1-CHANGED
 line2
 line3
 line4
@@ -7,4 +7,4 @@ line6
 line7
 line8
 line9
-line10
+line10-CHANGED
"""

REAL_ADDED = """\
diff --git a/newfile.py b/newfile.py
new file mode 100644
index 0000000..1cb3c2c
--- /dev/null
+++ b/newfile.py
@@ -0,0 +1,2 @@
+brand new
+content
"""

REAL_DELETED = """\
diff --git a/deleteme.py b/deleteme.py
deleted file mode 100644
index 83db48f..0000000
--- a/deleteme.py
+++ /dev/null
@@ -1,3 +0,0 @@
-line1
-line2
-line3
"""

REAL_RENAMED_WITH_EDIT = """\
diff --git a/renameme.py b/renamed.py
similarity index 37%
rename from renameme.py
rename to renamed.py
index 55c1833..5e596a5 100644
--- a/renameme.py
+++ b/renamed.py
@@ -1,2 +1,2 @@
-old content
+old content CHANGED
 second line
"""

REAL_BINARY_MODIFIED = """\
diff --git a/image.png b/image.png
index 5e86d5a..f4fe830 100644
Binary files a/image.png and b/image.png differ
"""

REAL_BINARY_ADDED = """\
diff --git a/new.bin b/new.bin
new file mode 100644
index 0000000..3f2504e
Binary files /dev/null and b/new.bin differ
"""

REAL_MIXED = REAL_DELETED + REAL_MODIFIED_MULTI_HUNK + REAL_BINARY_MODIFIED + REAL_RENAMED_WITH_EDIT


# --------------------------------------------------------------- diff parsing

def test_a_multi_position_edit_reports_each_changed_line_in_the_new_file():
    cf, = impact.parse_unified_diff(REAL_MODIFIED_MULTI_HUNK)
    assert cf.status == "modified" and cf.path == "modme.py"
    # line 2 -> "line2-CHANGED"; a line inserted after old line 4 lands at new line 5
    assert cf.merge_lines() == [(2, 2), (5, 5)]


def test_two_hunks_far_apart_are_not_merged_into_one_giant_range():
    cf, = impact.parse_unified_diff(REAL_TWO_HUNK_FILE)
    assert cf.merge_lines() == [(1, 1), (10, 10)]


def test_an_added_file_reports_its_whole_content_as_changed():
    cf, = impact.parse_unified_diff(REAL_ADDED)
    assert cf.status == "added" and cf.path == "newfile.py"
    assert cf.merge_lines() == [(1, 2)]


def test_a_deleted_file_is_still_reported():
    cf, = impact.parse_unified_diff(REAL_DELETED)
    assert cf.status == "deleted" and cf.path == "deleteme.py"


def test_a_rename_with_an_edit_keeps_the_old_path_and_notes_the_new_one():
    cf, = impact.parse_unified_diff(REAL_RENAMED_WITH_EDIT)
    assert cf.status == "renamed"
    assert cf.path == "renameme.py", "impact must be looked up on the path still in the index"
    assert cf.new_path == "renamed.py"
    assert cf.merge_lines() == [(1, 1)]


def test_a_binary_file_is_not_silently_dropped():
    """Real bug: no ---/+++/hunk lines follow a binary diff, so nothing used
    to finalise it and it vanished from the changed-file list entirely."""
    cf, = impact.parse_unified_diff(REAL_BINARY_MODIFIED)
    assert cf.status == "modified" and cf.path == "image.png" and cf.merge_lines() == []


def test_a_binary_file_being_added_is_reported_as_added():
    cf, = impact.parse_unified_diff(REAL_BINARY_ADDED)
    assert cf.status == "added" and cf.path == "new.bin"


def test_a_mixed_diff_accounts_for_every_file_in_order():
    files = impact.parse_unified_diff(REAL_MIXED)
    assert [(f.status, f.path) for f in files] == [
        ("deleted", "deleteme.py"), ("modified", "modme.py"),
        ("modified", "image.png"), ("renamed", "renameme.py")]


def test_an_empty_diff_parses_to_nothing():
    assert impact.parse_unified_diff("") == []


# ------------------------------------------------------- analyse(): the wiring

@pytest.fixture()
def conn(tmp_path):
    return store.connect(tmp_path / "i.db")


@pytest.fixture()
def repo(tmp_path, conn):
    root = tmp_path / "backend"
    write(root, "app/__init__.py", "")
    write(root, "app/core/__init__.py", "")
    write(root, "app/core/queue.py", "def push(): pass\n")
    write(root, "app/workers/__init__.py", "")
    write(root, "app/workers/reconciler.py", "def claim(): pass\ndef mark_for_retry(): pass\n")
    write(root, "app/workers/extraction_worker.py", "from .reconciler import claim\n")
    write(root, "app/workers/voice_worker.py", "from .reconciler import claim\n")
    write(root, "app/services/__init__.py", "")
    write(root, "app/services/handlers.py",
         "def f():\n    from ..workers import reconciler\n    reconciler.mark_for_retry()\n")
    write(root, "tests/test_reconciler.py", "from app.workers import reconciler\n")
    write(root, "tests/test_unrelated.py", "from app.core import queue\n")
    r = Repo("backend", root, "python", ("app",), ("tests",))
    scan(conn, [r])
    return r


def test_whole_file_impact_matches_the_known_dependents_of_reconciler(conn, repo):
    """The real shape DAS-17 needed: changing reconciler.py must surface both
    workers, the services handler and its test -- everything that imports it,
    directly or transitively -- and nothing that does not."""
    reports = impact.from_paths(conn, ["app/workers/reconciler.py"])
    report = reports["backend"]
    assert {d.path for d in report.dependents} == {
        "app/workers/extraction_worker.py", "app/workers/voice_worker.py",
        "app/services/handlers.py", "tests/test_reconciler.py"}
    assert "tests/test_unrelated.py" not in {d.path for d in report.dependents}


def test_a_transitive_dependent_gets_a_higher_hop_count_than_a_direct_one(conn, repo, tmp_path):
    """hops must reflect real distance, not just 'reachable': an indirect
    dependent (only reachable through another changed-adjacent file) must be
    strictly farther than a file that imports the changed file directly."""
    root = tmp_path / "backend"
    write(root, "app/routes.py", "from app.services import handlers\n")
    scan(conn, [Repo("backend", root, "python", ("app",), ("tests",))])
    report = impact.from_paths(conn, ["app/workers/reconciler.py"])["backend"]
    by_path = {d.path: d.hops for d in report.dependents}
    assert by_path["app/services/handlers.py"] == 1
    assert by_path["app/routes.py"] == 2


def test_tests_and_non_tests_are_split(conn, repo):
    report = impact.from_paths(conn, ["app/workers/reconciler.py"])["backend"]
    assert {d.path for d in report.tests} == {"tests/test_reconciler.py"}
    assert "tests/test_reconciler.py" not in {d.path for d in report.non_test_dependents}


def test_a_never_scanned_path_inside_a_known_repo_is_still_resolved(conn, repo):
    """The whole point of whole-file mode: a brand new file that has never
    been scanned must not vanish just because it is not in the index yet."""
    reports = impact.from_paths(conn, ["app/does/not/exist.py"])
    assert reports["backend"].unresolved == ["app/does/not/exist.py"]
    assert reports["backend"].dependents == []


def test_a_path_matching_no_known_repo_is_reported_not_dropped(conn, repo):
    reports = impact.from_paths(conn, ["some/other/thing.py"])
    assert reports["?"].unresolved == ["some/other/thing.py"]


def test_analyse_reports_a_within_repo_unresolved_path_explicitly(conn, repo):
    r = impact.analyse(conn, "backend", {"app/does/not/exist.py": ("modified", [])})
    assert r.unresolved == ["app/does/not/exist.py"]
    assert r.dependents == []


def test_a_deleted_file_reports_every_symbol_it_held(conn, repo):
    """Real bug: a plain overlap check against the parser's degenerate
    deletion ranges found nothing. Deleting a file removes ALL its symbols,
    and the report must say so."""
    r = impact.analyse(conn, "backend", {"app/workers/reconciler.py": ("deleted", [(1, 1)])})
    names = {s.qualname for s in r.changed["app/workers/reconciler.py"].symbols}
    assert names == {"claim", "mark_for_retry"}


def test_changed_files_do_not_list_each_other_as_dependents(conn, repo):
    """If both a file and its only importer are in the changed set, the
    importer is not a 'surprise' dependent -- it is already known-changed."""
    r = impact.analyse(conn, "backend", {
        "app/workers/reconciler.py": ("modified", []),
        "app/workers/extraction_worker.py": ("modified", []),
    })
    assert "app/workers/extraction_worker.py" not in {d.path for d in r.dependents}
    assert "app/services/handlers.py" in {d.path for d in r.dependents}


def test_an_added_file_already_scanned_can_still_show_real_dependents(conn, repo, tmp_path):
    """An added file is not hard-coded to 'no dependents': if it has already
    been wired up elsewhere in the same uncommitted change and rescanned,
    that is a real dependent and must not be suppressed."""
    root = tmp_path / "backend"
    write(root, "app/workers/brand_new.py", "def helper(): pass\n")
    write(root, "app/workers/extraction_worker.py",
         "from .reconciler import claim\nfrom .brand_new import helper\n")
    scan(conn, [Repo("backend", root, "python", ("app",), ("tests",))])
    r = impact.analyse(conn, "backend", {"app/workers/brand_new.py": ("added", [(1, 1)])})
    assert "app/workers/extraction_worker.py" in {d.path for d in r.dependents}


def test_from_diff_text_end_to_end_on_a_real_captured_diff(conn, repo):
    """The full pipeline: real diff text in, a usable report out, for a file
    that genuinely exists in this fixture repo."""
    diff = """\
diff --git a/app/workers/reconciler.py b/app/workers/reconciler.py
index aaa..bbb 100644
--- a/app/workers/reconciler.py
+++ b/app/workers/reconciler.py
@@ -1,2 +1,2 @@
-def claim(): pass
+def claim(worker_id): pass
 def mark_for_retry(): pass
"""
    report = impact.from_diff_text(conn, "backend", diff)
    assert report.unresolved == []
    assert {d.path for d in report.dependents} >= {
        "app/workers/extraction_worker.py", "app/workers/voice_worker.py"}
    changed_names = {s.qualname for s in report.changed["app/workers/reconciler.py"].symbols}
    assert changed_names == {"claim"}, "only the edited function, not the untouched one"
