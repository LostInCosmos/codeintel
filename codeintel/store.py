"""The one index every codeintel command reads.

A single SQLite file under `.data/`, local to this machine and never
deployed. Everything in it is regenerable by `codeintel scan`, so it carries
no migrations: a schema change bumps SCHEMA_VERSION and the next scan
rebuilds from scratch.

All later phases (dependency graph, duplicates, impact, review) are queries
over these tables rather than separate stores — one source of truth.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 6
DEFAULT_DB = Path(__file__).resolve().parent.parent / ".data" / "index.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    repo        TEXT NOT NULL,          -- 'backend', 'mobile', ...
    path        TEXT NOT NULL,          -- relative to the repo root
    language    TEXT NOT NULL,          -- 'python' | 'dart'
    module      TEXT NOT NULL,          -- importable name: app.core.queue / package:x/y.dart
    lines       INTEGER NOT NULL,
    sha1        TEXT NOT NULL,          -- content hash, for incremental rescans
    is_test     INTEGER NOT NULL,
    parse_error TEXT,                   -- set when the extractor could not read it
    doc         TEXT,                   -- first line of the file's own doc: Python module
                                        -- docstring, or the comment block opening a Dart/JS file
    UNIQUE (repo, path)
);

CREATE TABLE IF NOT EXISTS symbols (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,          -- 'class' | 'function' | 'method'
    name        TEXT NOT NULL,
    qualname    TEXT NOT NULL,          -- Class.method, or name for top level
    parent      TEXT,                   -- enclosing class, if any
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    signature   TEXT,                   -- parameter list as written
    doc         TEXT,                   -- first line of the docstring / doc comment
    is_private  INTEGER NOT NULL,       -- leading underscore
    body_hash   TEXT                    -- structural hash for duplicate detection (phase 3)
);

CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    target      TEXT NOT NULL,          -- module as written in the source
    name        TEXT,                   -- python `from M import name`; null otherwise
    alias       TEXT,                   -- python `... import name as alias`
    resolved    TEXT,                   -- the file the import statement names (literal)
    defined_in  TEXT,                   -- where the name is really defined, after following
                                        -- re-exports through __init__.py; = resolved otherwise
    line        INTEGER NOT NULL,
    is_relative INTEGER NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'module',
                                        -- 'module' | 'function' | 'type_checking' | 'try'
                                        -- an import inside a function or a guard is still a
                                        -- dependency, but usually one placed there to break a cycle
    kind        TEXT NOT NULL DEFAULT 'import'
                                        -- 'import' | 'export' | 'part' (Dart). A Dart `part` is
                                        -- not a dependency between libraries: the file IS part of
                                        -- the library that names it, wherever it sits on disk
);

CREATE INDEX IF NOT EXISTS ix_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS ix_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS ix_imports_file ON imports(file_id);
CREATE INDEX IF NOT EXISTS ix_imports_resolved ON imports(resolved);
CREATE INDEX IF NOT EXISTS ix_imports_defined ON imports(defined_in);
"""


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    version = None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = int(row[0]) if row else None
    except sqlite3.OperationalError:
        pass
    if version not in (None, SCHEMA_VERSION):
        # Regenerable by design — rebuild rather than migrate.
        conn.executescript("DROP TABLE IF EXISTS imports; DROP TABLE IF EXISTS symbols; "
                           "DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS meta;")
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()
    return conn
