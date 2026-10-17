"""The scanner against a small throwaway repo per language: what gets
indexed, how imports resolve, and that incremental rescans stay correct when
files are added, edited and deleted — the add/delete case was a real bug,
where unchanged importers kept stale resolutions.
"""

import sqlite3
import textwrap
from pathlib import Path

import pytest

from codeintel import store
from codeintel.scanner import Repo, scan


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    return store.connect(tmp_path / "index.db")


@pytest.fixture()
def py_repo(tmp_path) -> Repo:
    root = tmp_path / "backend"
    write(root, "app/__init__.py", "")
    write(root, "app/core/__init__.py", "")
    write(root, "app/core/queue.py", "def push(q, job_id):\n    return True\n")
    write(root, "app/workers/__init__.py", "")
    write(root, "app/workers/worker.py", """\
        from ..core import queue
        from app.core.queue import push
        import json

        def run():
            push("q", 1)
        """)
    write(root, "tests/test_queue.py", "from app.core.queue import push\n\ndef test_push():\n    assert push('q', 1)\n")
    write(root, ".venv/lib/junk.py", "def never_indexed(): pass\n")
    return Repo("backend", root, "python", ("app",), ("tests",))


def resolved(conn, path):
    return sorted((r["target"] + (f":{r['name']}" if r["name"] else ""), r["resolved"]) for r in conn.execute(
        "SELECT i.target, i.name, i.resolved FROM imports i JOIN files f ON f.id=i.file_id WHERE f.path=?", (path,)))


def test_python_repo_indexes_symbols_tests_and_resolves_imports(conn, py_repo):
    stats = scan(conn, [py_repo])
    assert stats["parsed"] == 6 and stats["errors"] == 0
    paths = {r["path"] for r in conn.execute("SELECT path FROM files")}
    assert ".venv/lib/junk.py" not in paths, "environment dirs must be skipped"
    assert conn.execute("SELECT is_test FROM files WHERE path='tests/test_queue.py'").fetchone()[0] == 1
    assert conn.execute("SELECT module FROM files WHERE path='app/core/__init__.py'").fetchone()[0] == "app.core"
    assert resolved(conn, "app/workers/worker.py") == [
        ("..core:queue", "app/core/queue.py"),          # the submodule, not core/__init__.py
        ("app.core.queue:push", "app/core/queue.py"),   # a symbol: falls back to its module
        ("json", None),                                 # external
    ]


def test_rescan_with_no_changes_parses_nothing(conn, py_repo):
    scan(conn, [py_repo])
    again = scan(conn, [py_repo])
    assert again["parsed"] == 0 and again["unchanged"] == 6


def test_an_edit_reparses_only_that_file(conn, py_repo):
    scan(conn, [py_repo])
    write(py_repo.root, "app/core/queue.py", "def push(q, job_id):\n    return True\n\ndef pop(q):\n    return None\n")
    again = scan(conn, [py_repo])
    assert again["parsed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM symbols WHERE name='pop'").fetchone()[0] == 1


def test_a_deleted_file_is_dropped_and_its_importers_unresolve(conn, py_repo):
    """Real bug: unchanged importers kept pointing at the deleted file."""
    scan(conn, [py_repo])
    (py_repo.root / "app/core/queue.py").unlink()
    again = scan(conn, [py_repo])
    assert again["removed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM symbols WHERE name='push'").fetchone()[0] == 0
    after = dict(resolved(conn, "app/workers/worker.py"))
    assert after["app.core.queue:push"] is None
    # `from ..core import queue` can now only mean a name defined in core/__init__.py
    assert after["..core:queue"] == "app/core/__init__.py"


def _labels(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT repo FROM files")}


def test_scanning_everything_drops_a_repo_no_longer_configured(conn, py_repo, monkeypatch):
    """DAS-89: renaming a label in KNOWN_REPOS left the old label's rows in
    the index for ever, reported by `stats` as a fifth repo."""
    from codeintel import scanner

    scan(conn, [Repo("old_label", py_repo.root, "python", ("app",), ())])
    monkeypatch.setattr(scanner, "default_repos", lambda: [py_repo])
    result = scan(conn)
    assert _labels(conn) == {"backend"}
    assert result["removed"] > 0
    assert conn.execute("SELECT COUNT(*) FROM symbols s JOIN files f ON f.id = s.file_id "
                        "WHERE f.repo = 'old_label'").fetchone()[0] == 0


def test_an_explicit_repo_list_leaves_other_repos_alone(conn, py_repo):
    scan(conn, [Repo("other", py_repo.root, "python", ("app",), ())])
    scan(conn, [py_repo])
    assert _labels(conn) == {"other", "backend"}


def test_a_new_file_resolves_its_unchanged_importers(conn, py_repo):
    """Real bug: an import written before its target existed stayed unresolved."""
    write(py_repo.root, "app/workers/later.py", "from app.core.cache import get\n")
    scan(conn, [py_repo])
    assert resolved(conn, "app/workers/later.py") == [("app.core.cache:get", None)]
    write(py_repo.root, "app/core/cache.py", "def get(k):\n    return None\n")
    scan(conn, [py_repo])
    assert resolved(conn, "app/workers/later.py") == [("app.core.cache:get", "app/core/cache.py")]


def test_a_syntax_error_is_recorded_and_does_not_stop_the_scan(conn, py_repo):
    write(py_repo.root, "app/broken.py", "def broken(:\n")
    stats = scan(conn, [py_repo])
    assert stats["errors"] == 1
    assert conn.execute("SELECT parse_error FROM files WHERE path='app/broken.py'").fetchone()[0].startswith("SyntaxError")
    assert conn.execute("SELECT COUNT(*) FROM symbols WHERE name='push'").fetchone()[0] == 1


def test_dart_package_and_relative_imports_resolve(conn, tmp_path):
    root = tmp_path / "mobile"
    write(root, "lib/core/api.dart", "class Api {}\n")
    write(root, "lib/features/screen.dart", """\
        import 'package:flutter/material.dart';
        import 'package:gst_bill_app/core/api.dart';
        import '../core/api.dart';
        class Screen {}
        """)
    scan(conn, [Repo("mobile", root, "dart", ("lib",), ("test",), "gst_bill_app")])
    assert resolved(conn, "lib/features/screen.dart") == [
        ("../core/api.dart", "lib/core/api.dart"),
        ("package:flutter/material.dart", None),
        ("package:gst_bill_app/core/api.dart", "lib/core/api.dart"),
    ]
    assert conn.execute("SELECT module FROM files WHERE path='lib/core/api.dart'").fetchone()[0] \
        == "package:gst_bill_app/core/api.dart"


def test_js_extensionless_and_index_imports_resolve(conn, tmp_path):
    root = tmp_path / "admin_panel"
    write(root, "src/api.js", "export function get() { return 1 }\n")
    write(root, "src/components/index.jsx", "export function Card() { return null }\n")
    write(root, "src/App.jsx", """\
        import React from 'react'
        import { get } from './api'
        import { Card } from './components'
        export default function App() { return null }
        """)
    scan(conn, [Repo("admin_panel", root, "javascript", ("src",), ())])
    assert resolved(conn, "src/App.jsx") == [
        ("./api", "src/api.js"), ("./components", "src/components/index.jsx"), ("react", None)]


def test_a_schema_change_rebuilds_instead_of_migrating(tmp_path, monkeypatch):
    db = tmp_path / "index.db"
    c = store.connect(db)
    c.execute("INSERT INTO meta VALUES ('marker', 'x')")
    c.commit()
    monkeypatch.setattr(store, "SCHEMA_VERSION", store.SCHEMA_VERSION + 1)
    c2 = store.connect(db)
    assert c2.execute("SELECT value FROM meta WHERE key='marker'").fetchone() is None


def test_from_dot_import_names_resolve_to_each_submodule(conn, py_repo):
    """The shape the real workers use. Resolving to the package __init__ lost
    the actual dependency edges."""
    write(py_repo.root, "app/workers/reconciler.py", "def reconcile(): pass\n")
    write(py_repo.root, "app/workers/shutdown.py", "stop = None\n")
    write(py_repo.root, "app/workers/main.py", "from . import reconciler, shutdown\n")
    scan(conn, [py_repo])
    assert resolved(conn, "app/workers/main.py") == [
        (".:reconciler", "app/workers/reconciler.py"), (".:shutdown", "app/workers/shutdown.py")]


def test_a_plain_import_of_a_missing_module_does_not_fall_back(conn, py_repo):
    """`import app.core.nothere` must not invent an edge to app/core/__init__.py."""
    write(py_repo.root, "app/workers/odd.py", "import app.core.nothere\n")
    scan(conn, [py_repo])
    assert resolved(conn, "app/workers/odd.py") == [("app.core.nothere", None)]


def defined(conn, path):
    return sorted((r["name"], r["defined_in"]) for r in conn.execute(
        "SELECT i.name, i.defined_in FROM imports i JOIN files f ON f.id=i.file_id WHERE f.path=?", (path,)))


def test_names_reexported_by_a_package_are_traced_to_their_real_file(conn, py_repo):
    """`from ...models import Tenant` names models/__init__.py, but Tenant lives
    in models/tenant.py — the dependency graph needs the latter."""
    write(py_repo.root, "app/models/__init__.py", "from .tenant import Tenant\nfrom .user import User as Member\nVERSION = 1\n")
    write(py_repo.root, "app/models/tenant.py", "class Tenant: pass\n")
    write(py_repo.root, "app/models/user.py", "class User: pass\n")
    write(py_repo.root, "app/api.py", "from .models import Tenant, Member, VERSION\n")
    scan(conn, [py_repo])
    assert resolved(conn, "app/api.py") == [
        (".models:Member", "app/models/__init__.py"),
        (".models:Tenant", "app/models/__init__.py"),
        (".models:VERSION", "app/models/__init__.py")]          # resolved stays literal
    assert defined(conn, "app/api.py") == [
        ("Member", "app/models/user.py"),       # followed through `as Member`
        ("Tenant", "app/models/tenant.py"),
        ("VERSION", "app/models/__init__.py")]  # really defined in __init__: stays


def test_reexport_chains_follow_through_nested_packages_and_stop_on_cycles(conn, py_repo):
    write(py_repo.root, "app/a/__init__.py", "from .b import Thing\n")
    write(py_repo.root, "app/a/b/__init__.py", "from .c import Thing\n")
    write(py_repo.root, "app/a/b/c.py", "class Thing: pass\n")
    write(py_repo.root, "app/loop/__init__.py", "from .inner import X\n")
    write(py_repo.root, "app/loop/inner/__init__.py", "from .. import X\n")
    write(py_repo.root, "app/use.py", "from .a import Thing\nfrom .loop import X\n")
    scan(conn, [py_repo])          # must terminate despite the cycle
    got = dict(defined(conn, "app/use.py"))
    assert got["Thing"] == "app/a/b/c.py"
    assert got["X"] in ("app/loop/__init__.py", "app/loop/inner/__init__.py")
