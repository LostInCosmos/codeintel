"""Phase 1 — the repository scanner.

Walks each configured repo, hashes every source file, and re-extracts only
the ones whose content changed since the last scan. Files that disappeared
are dropped. Imports pointing at our own code are resolved to a repo path so
the dependency graph (phase 2) is a straight join.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .extractors import EXTRACTORS

GSTAPP_ROOT = Path(__file__).resolve().parents[2]

# Directories never worth reading: generated, vendored, or environment.
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", "build", ".dart_tool",
    ".idea", ".vscode", ".pytest_cache", ".ruff_cache", "storage", ".claude",
    "ios", "android", "macos", "linux", "windows", "web", ".gradle", "Pods",
}


@dataclass(frozen=True)
class Repo:
    name: str
    root: Path
    language: str
    source_dirs: tuple[str, ...]     # scanned relative to root
    test_dirs: tuple[str, ...]
    package: str | None = None       # dart package name, for package: imports

    @property
    def suffixes(self) -> tuple[str, ...]:
        return {"python": (".py",), "dart": (".dart",), "javascript": (".js", ".jsx", ".mjs", ".ts", ".tsx")}[self.language]


def _dart_package(root: Path) -> str | None:
    pubspec = root / "pubspec.yaml"
    if pubspec.exists():
        for line in pubspec.read_text().splitlines():
            if line.startswith("name:"):
                return line.split(":", 1)[1].strip()
    return None


def default_repos(root: Path = GSTAPP_ROOT) -> list[Repo]:
    repos = []
    if (root / "backend" / "app").is_dir():
        repos.append(Repo("backend", root / "backend", "python", ("app", "ops", "scripts"), ("tests",)))
    if (root / "mobile" / "lib").is_dir():
        repos.append(Repo("mobile", root / "mobile", "dart", ("lib",), ("test",),
                          _dart_package(root / "mobile")))
    if (root / "admin_panel" / "src").is_dir():
        repos.append(Repo("admin_panel", root / "admin_panel", "javascript", ("src",), ()))
    return repos


def _walk(repo: Repo):
    for top in (*repo.source_dirs, *repo.test_dirs):
        base = repo.root / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for fn in sorted(filenames):
                if fn.endswith(repo.suffixes):
                    path = Path(dirpath) / fn
                    yield path, top in repo.test_dirs


def _module_name(repo: Repo, rel: str) -> str:
    if repo.language == "python":
        mod = rel[:-3].replace("/", ".")
        return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod
    if repo.language == "dart" and rel.startswith("lib/") and repo.package:
        return f"package:{repo.package}/{rel[4:]}"
    return rel


def _resolve_python(target: str, level: int, rel: str, known: set[str], name: str | None = None) -> str | None:
    """Repo path of an imported module, or None when it is not our code.

    For `from M import name`, `name` may be a submodule (`from . import
    reconciler`) or a symbol defined in M (`from app.core.queue import push`).
    So try M.name as a module first and fall back to M — and only then: a
    plain `import a.b.c` names a module, and falling back from a missing one
    would invent an edge to its parent package.
    """
    if level:
        base = rel.split("/")[:-1]
        if level > 1:
            base = base[: len(base) - (level - 1)]
        parts = base + (target.split(".") if target else [])
    else:
        parts = target.split(".")

    def as_module(segments: list[str]) -> str | None:
        if not segments:
            return None
        stem = "/".join(segments)
        for cand in (f"{stem}.py", f"{stem}/__init__.py"):
            if cand in known:
                return cand
        return None

    if name and name != "*":
        if hit := as_module(parts + [name]):
            return hit
    return as_module(parts)


def _resolve_dart(target: str, rel: str, repo: Repo, known: set[str]) -> str | None:
    if target.startswith("dart:"):
        return None
    if target.startswith("package:"):
        if repo.package and target.startswith(f"package:{repo.package}/"):
            cand = "lib/" + target[len(f"package:{repo.package}/"):]
            return cand if cand in known else None
        return None
    cand = os.path.normpath(str(Path(rel).parent / target))
    return cand if cand in known else None


_JS_EXTS = ("", ".js", ".jsx", ".mjs", ".ts", ".tsx", "/index.js", "/index.jsx", "/index.ts", "/index.tsx")


def _resolve_js(target: str, rel: str, known: set[str]) -> str | None:
    """Only relative imports can be our own code — a bare specifier is a
    package from node_modules. Extensions are optional in ES imports."""
    if not target.startswith("."):
        return None
    stem = os.path.normpath(str(Path(rel).parent / target))
    for ext in _JS_EXTS:
        if stem + ext in known:
            return stem + ext
    return None


def _follow_reexports(conn: sqlite3.Connection, repo: str) -> None:
    """Point `defined_in` at the file that really defines an imported name.

    `from ...models import Tenant` resolves to app/models/__init__.py, which
    only re-exports it (`from .tenant import Tenant`). Stopping there makes
    every importer appear to depend on every model. Follow the chain through
    package __init__ files until the name is no longer a re-export. A name
    actually defined in __init__.py stays there; cycles stop where they loop.
    """
    # For each __init__.py: the names it re-exports, and where each comes from.
    reexports: dict[str, dict[str, str]] = {}
    for path, name, alias, defined in conn.execute("""
            SELECT f.path, i.name, i.alias, i.resolved FROM imports i JOIN files f ON f.id = i.file_id
            WHERE f.repo = ? AND f.path LIKE '%__init__.py' AND i.name IS NOT NULL
              AND i.name != '*' AND i.resolved IS NOT NULL""", (repo,)):
        reexports.setdefault(path, {})[alias or name] = defined

    def chase(path: str, name: str) -> str:
        seen = set()
        while path in reexports and name in reexports[path] and path not in seen:
            seen.add(path)
            nxt = reexports[path][name]
            if nxt == path:
                break
            path = nxt
        return path

    rows = conn.execute("""SELECT i.id, i.name, i.resolved FROM imports i JOIN files f ON f.id = i.file_id
                           WHERE f.repo = ? AND i.resolved LIKE '%__init__.py' AND i.name IS NOT NULL""",
                        (repo,)).fetchall()
    conn.executemany("UPDATE imports SET defined_in = ? WHERE id = ?",
                     [(chase(resolved, name), imp_id) for imp_id, name, resolved in rows])


def scan(conn: sqlite3.Connection, repos: list[Repo] | None = None, full: bool = False) -> dict:
    repos = repos if repos is not None else default_repos()
    t0 = time.monotonic()
    stats = {"files": 0, "parsed": 0, "unchanged": 0, "removed": 0, "errors": 0}

    for repo in repos:
        seen: set[str] = set()
        existing = {r["path"]: (r["id"], r["sha1"])
                    for r in conn.execute("SELECT id, path, sha1 FROM files WHERE repo=?", (repo.name,))}
        changed: list[tuple[int, str, object]] = []

        for path, is_test in _walk(repo):
            rel = path.relative_to(repo.root).as_posix()
            seen.add(rel)
            raw = path.read_bytes()
            sha1 = hashlib.sha1(raw).hexdigest()
            stats["files"] += 1
            if not full and rel in existing and existing[rel][1] == sha1:
                stats["unchanged"] += 1
                continue
            text = raw.decode("utf-8", errors="replace")
            extracted = EXTRACTORS[repo.language](text)
            if rel in existing:
                conn.execute("DELETE FROM files WHERE id=?", (existing[rel][0],))
            cur = conn.execute(
                "INSERT INTO files (repo, path, language, module, lines, sha1, is_test, parse_error, doc) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (repo.name, rel, repo.language, _module_name(repo, rel), text.count("\n") + 1,
                 sha1, int(is_test), extracted.error, extracted.doc))
            file_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO symbols (file_id, kind, name, qualname, parent, start_line, end_line, "
                "signature, doc, is_private, body_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(file_id, s.kind, s.name, s.qualname, s.parent, s.start_line, s.end_line,
                  s.signature, s.doc, int(s.is_private), s.body_hash) for s in extracted.symbols])
            changed.append((file_id, rel, extracted))
            stats["parsed"] += 1
            stats["errors"] += int(extracted.error is not None)

        for rel, (fid, _) in existing.items():
            if rel not in seen:
                conn.execute("DELETE FROM files WHERE id=?", (fid,))
                stats["removed"] += 1

        for file_id, rel, extracted in changed:
            conn.executemany(
                "INSERT INTO imports (file_id, target, name, alias, resolved, line, is_relative, scope, kind) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(file_id, ("." * imp.level + imp.target) if imp.level else imp.target, imp.name, imp.alias,
                  None, imp.line, int(imp.is_relative), imp.scope, imp.kind) for imp in extracted.imports])

        # Re-resolve EVERY import in the repo, not only the changed files'.
        # Otherwise a newly added file leaves its unchanged importers marked
        # unresolved, and a deleted one leaves them pointing at nothing.
        # It is string matching over ~1k rows, so doing all of it is free.
        rows = conn.execute("""SELECT i.id, i.target, i.name, f.path FROM imports i
                               JOIN files f ON f.id = i.file_id WHERE f.repo = ?""", (repo.name,)).fetchall()
        updates = []
        for imp_id, target, name, rel in rows:
            if repo.language == "python":
                level = len(target) - len(target.lstrip("."))
                resolved = _resolve_python(target[level:], level, rel, seen, name)
            elif repo.language == "dart":
                resolved = _resolve_dart(target, rel, repo, seen)
            else:
                resolved = _resolve_js(target, rel, seen)
            updates.append((resolved, imp_id))
        conn.executemany("UPDATE imports SET resolved = ?, defined_in = ? WHERE id = ?",
                         [(r, r, i) for r, i in updates])
        if repo.language == "python":
            _follow_reexports(conn, repo.name)

    conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_scan', ?)", (time.strftime("%Y-%m-%dT%H:%M:%S"),))
    conn.commit()
    stats["seconds"] = round(time.monotonic() - t0, 2)
    return stats
