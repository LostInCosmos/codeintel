"""Phase 3a: the module registry quotes the code and never invents."""

import textwrap
from pathlib import Path

import pytest

from codeintel import registry, store
from codeintel.scanner import Repo, scan


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


@pytest.fixture()
def mods(tmp_path):
    root = tmp_path / "backend"
    write(root, "app/__init__.py", "")
    write(root, "app/api/__init__.py", "")                                   # scaffolding only
    write(root, "app/models/__init__.py", '"""ORM models, one file per aggregate."""\nfrom .tenant import Tenant\n')
    write(root, "app/models/tenant.py", 'class Tenant:\n    """A shop."""\n')    # named after its class
    write(root, "app/services/__init__.py", "")
    write(root, "app/services/mixed.py", '''\
        def first():
            """Only one of several unrelated helpers."""
        def second(): pass
        ''')
    write(root, "app/services/helper_host.py", '''\
        def helper():
            """A small public helper."""
        class _RealThing:
            pass
        ''')
    write(root, "app/services/documented.py", '"""Writes exports."""\ndef run(): pass\n')
    write(root, "scripts/a.py", "def run(): pass\n")
    write(root, "scripts/b.py", "def run(): pass\n")
    write(root, "tests/test_x.py", "def test_x(): pass\n")
    c = store.connect(tmp_path / "i.db")
    scan(c, [Repo("backend", root, "python", ("app", "scripts"), ("tests",))])
    return {m.folder: m for m in registry.modules(c, "backend")}


def purpose(mods, path):
    return next(f for m in mods.values() for f in m.files if f.path == path)


def test_scaffolding_and_tests_are_not_modules(mods):
    assert "app/api" not in mods and "app/services" in mods and "tests" not in mods


def test_a_package_docstring_is_the_modules_statement(mods):
    assert mods["app/models"].statement == "ORM models, one file per aggregate."
    assert mods["app/services"].statement is None


def test_a_files_own_doc_wins(mods):
    f = purpose(mods, "app/services/documented.py")
    assert (f.purpose, f.purpose_from) == ("Writes exports.", "file")


def test_a_declaration_the_file_is_named_after_lends_its_doc(mods):
    f = purpose(mods, "app/models/tenant.py")
    assert (f.purpose, f.purpose_from) == ("A shop.", "Tenant")


def test_one_helper_among_several_does_not_speak_for_the_file(mods):
    assert purpose(mods, "app/services/mixed.py").purpose is None


def test_the_only_public_declaration_is_not_enough_when_a_private_one_is_the_file(mods):
    """invoice_items.py's case: a public helper beside the private class that
    is really the file."""
    assert purpose(mods, "app/services/helper_host.py").purpose is None


def test_repeated_names_are_collapsed():
    assert registry._names_cell(["run", "run", "main"]) == "`run` ×2, `main`"


def test_render_has_an_index_linking_every_module(mods):
    out = registry.render("backend", list(mods.values()))
    assert "## Where things live" in out
    for folder in mods:
        assert f"](#{registry._slug(folder)})" in out and f"### `{folder}`" in out
    assert "do not edit by hand" in out


def test_coverage_counts_the_gaps(mods):
    cov = registry.coverage(list(mods.values()))
    assert cov["modules_with_statement"] == 1
    assert cov["files_with_purpose"] < cov["files"]


def test_a_pipe_in_a_purpose_line_is_escaped_once():
    f = registry.FileEntry("a/x.py", 3, "a | b", "file")
    row = [l for l in registry.render("r", [registry.Module("a", "a", None, [f])]).splitlines() if "x.py" in l][-1]
    assert row == "| `x.py` | 3 | a \\| b |"


def test_output_is_deterministic(mods):
    """No timestamp: regenerating unchanged code must produce no diff."""
    a = registry.render("backend", list(mods.values()))
    b = registry.render("backend", list(mods.values()))
    assert a == b and "Generated 20" not in a
