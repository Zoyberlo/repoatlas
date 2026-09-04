"""The on-disk shape of an index.

One SQLite file, no server, no daemon. That choice is not only about
convenience: an index an agent consults has to open in milliseconds from a
cold process, because it is read far more often than it is written.

Two design notes worth stating up front.

Everything a file produced is keyed by that file's path, and deleting the
row cascades. Re-indexing one file is therefore a delete followed by an
insert, which cannot leave a half-updated file behind however the process
dies mid-write.

Unresolved references are stored alongside resolved edges. They are not
claims about the code and never leave the store as edges, but keeping them
means a changed file can be re-resolved against the rest of the repository
without re-parsing every other file.
"""

from __future__ import annotations

__all__ = ["PRAGMAS", "SCHEMA", "SCHEMA_VERSION"]

SCHEMA_VERSION = 2
"""Bumped whenever the shape below changes.

A mismatch discards the store and rebuilds rather than migrating. Early
enough that the cost is seconds, and a wrong migration would produce an
index that looks fine and answers wrongly.
"""

PRAGMAS = (
    # A reader must not block behind the writer: an agent querying while a
    # watcher re-indexes is the normal case, not the exception.
    "PRAGMA journal_mode=WAL",
    # The index is derived data. Losing the last transaction to a power cut
    # costs one re-index, so paying for a full fsync per commit is waste.
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA temp_store=MEMORY",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per parsed file. `digest` is the content hash; `size` and
-- `mtime_ns` are the fast path that avoids reading a file to hash it.
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    language    TEXT NOT NULL,
    digest      TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    has_errors  INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    namespace   TEXT
);

CREATE TABLE IF NOT EXISTS symbols (
    id             TEXT PRIMARY KEY,
    path           TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL,
    qualified_name TEXT,
    container_id   TEXT,
    language       TEXT,
    signature      TEXT,
    documentation  TEXT,
    is_local       INTEGER NOT NULL DEFAULT 0,
    is_synthetic   INTEGER NOT NULL DEFAULT 0,
    name_start_line INTEGER NOT NULL,
    name_start_char INTEGER NOT NULL,
    name_end_line   INTEGER NOT NULL,
    name_end_char   INTEGER NOT NULL,
    full_start_line INTEGER,
    full_start_char INTEGER,
    full_end_line   INTEGER,
    full_end_char   INTEGER
);

CREATE INDEX IF NOT EXISTS symbols_by_path ON symbols(path);
CREATE INDEX IF NOT EXISTS symbols_by_name ON symbols(name);
CREATE INDEX IF NOT EXISTS symbols_by_container ON symbols(container_id);

-- Resolved edges. `site_path` carries the ON DELETE CASCADE because an edge
-- belongs to the file whose text evidences it, not to either endpoint.
CREATE TABLE IF NOT EXISTS edges (
    site_path       TEXT REFERENCES files(path) ON DELETE CASCADE,
    src_id          TEXT NOT NULL,
    dst_id          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    tier            TEXT NOT NULL,
    confidence      REAL NOT NULL,
    site_start_line INTEGER,
    site_start_char INTEGER,
    site_end_line   INTEGER,
    site_end_char   INTEGER
);

CREATE INDEX IF NOT EXISTS edges_by_src ON edges(src_id);
CREATE INDEX IF NOT EXISTS edges_by_dst ON edges(dst_id);
CREATE INDEX IF NOT EXISTS edges_by_site ON edges(site_path);
CREATE INDEX IF NOT EXISTS edges_by_site_position
    ON edges(site_path, site_start_line, site_start_char);

-- Names a file uses that resolution has yet to place. Kept so one changed
-- file can be re-resolved against the repository without re-parsing it all.
CREATE TABLE IF NOT EXISTS refs (
    path          TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    container_id  TEXT,
    start_line    INTEGER NOT NULL,
    start_char    INTEGER NOT NULL,
    end_line      INTEGER NOT NULL,
    end_char      INTEGER NOT NULL,
    -- The plain name a member was read through, and the type the
    -- extractor could put on it. Both are what makes `x.greet()` resolve to
    -- the class of `x` rather than to any `greet` in the repository.
    receiver      TEXT,
    receiver_type TEXT
);

CREATE INDEX IF NOT EXISTS refs_by_path ON refs(path);
-- A re-index re-resolves the references whose name a change touched,
-- so they have to be findable by name without a scan.
CREATE INDEX IF NOT EXISTS refs_by_name ON refs(name);

CREATE TABLE IF NOT EXISTS imports (
    id             INTEGER PRIMARY KEY,
    path           TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    module         TEXT NOT NULL,
    relative_level INTEGER NOT NULL DEFAULT 0,
    start_line     INTEGER NOT NULL,
    start_char     INTEGER NOT NULL,
    end_line       INTEGER NOT NULL,
    end_char       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS imports_by_path ON imports(path);

CREATE TABLE IF NOT EXISTS import_bindings (
    import_id  INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    local      TEXT NOT NULL,
    original   TEXT,
    start_line INTEGER NOT NULL,
    start_char INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    end_char   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS bindings_by_import ON import_bindings(import_id);

-- Trigram so a partial name matches: an agent searching for `Resolver`
-- should find `ModuleResolver`, which a prefix index would miss.
--
-- `detail` stays at its default. Setting it to 'none' halves the index but
-- makes FTS5 reject a quoted phrase, and quoting is what stops a symbol
-- name containing a hyphen or a colon being read as query syntax.
-- The global ranking, written when resolution runs so the first map after
-- a restart costs no power iteration. Additive: an older store simply has
-- an empty table and the ranking is computed on demand.
CREATE TABLE IF NOT EXISTS ranks (
    symbol_id TEXT PRIMARY KEY,
    score     REAL NOT NULL,
    in_degree INTEGER NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS symbol_search USING fts5(
    name,
    qualified_name,
    content='symbols',
    content_rowid='rowid',
    tokenize='trigram'
);
"""
