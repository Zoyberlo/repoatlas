"""Reading and writing the index store.

The store holds what a file produced, keyed by that file. Replacing a file
is a delete followed by an insert inside one transaction, so a crash
mid-write leaves the previous version intact rather than half of both.

Version stamps decide whether the store can be trusted at all. A parser
upgrade or an edit to a tag query changes what extraction would produce, so
both are hashed into the store and a mismatch marks everything dirty. This
is the failure mode that matters: a stale index does not announce itself,
it just answers with yesterday's code.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..model import (
    Edge,
    EdgeKind,
    IndexSnapshot,
    ResolutionTier,
    SourceRange,
    Symbol,
    SymbolKind,
)
from ..parse.extract import Reference
from ..parse.imports import FileImports, ImportBinding, ImportStatement
from ..rank.tokens import TokenEstimator, estimate_tokens, make_estimator
from .schema import PRAGMAS, SCHEMA, SCHEMA_VERSION

__all__ = ["FileRecord", "IndexStore", "StoreError", "content_digest", "toolchain_version"]


class StoreError(RuntimeError):
    """Raised when a store cannot be opened or is not usable."""


@dataclass(frozen=True, slots=True)
class FileRecord:
    """What the store remembers about one file, without its contents."""

    path: str
    language: str
    digest: str
    size: int
    mtime_ns: int
    has_errors: bool = False
    error_count: int = 0

    def unchanged_by_stat(self, size: int, mtime_ns: int) -> bool:
        """Whether size and mtime alone say the file is untouched.

        The fast path. Reading a file to hash it is the expensive part of
        change detection, and a file whose size and modification time both
        match has almost certainly not changed. Almost: an edit within the
        same timestamp tick that preserves length would slip through, which
        is why a caller that must be certain can hash unconditionally.
        """
        return self.size == size and self.mtime_ns == mtime_ns


def content_digest(data: bytes) -> str:
    """Hash a file's contents.

    BLAKE2b from the standard library rather than a faster third-party
    hash, because the evaluation harness and the store both have to run
    with no dependencies installed, and hashing is not the bottleneck:
    parsing costs roughly sixty times as much.
    """
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def toolchain_version(queries: Iterable[tuple[str, str]]) -> str:
    """A stamp covering everything that decides what extraction produces.

    The tree-sitter version and every tag query, hashed together. Editing a
    query changes the symbols a file yields, and an index that kept the old
    ones would be wrong in a way nothing else would notice.
    """
    digest = hashlib.blake2b(digest_size=16)
    try:
        import tree_sitter

        digest.update(getattr(tree_sitter, "__version__", "unknown").encode())
    except ImportError:  # pragma: no cover - depends on the extra
        digest.update(b"no-parser")
    for name, source in sorted(queries):
        digest.update(name.encode())
        digest.update(source.encode())
    return digest.hexdigest()


def _range_columns(span: SourceRange | None) -> tuple[int | None, ...]:
    if span is None:
        return (None, None, None, None)
    return (
        span.start.line,
        span.start.character,
        span.end.line,
        span.end.character,
    )


def _range_from(row: Sequence[Any], offset: int) -> SourceRange | None:
    values = row[offset : offset + 4]
    if any(value is None for value in values):
        return None
    return SourceRange.of(*values)


class IndexStore:
    """A SQLite-backed index.

    Use as a context manager. Every write happens inside a transaction the
    caller controls through :meth:`transaction`, so a batch of file updates
    either lands whole or not at all.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.name != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # The server runs synchronous tool functions on a worker thread, so
        # the connection outlives the thread that opened it. Sharing one is
        # safe only because this SQLite is built serialized, which is
        # checked rather than assumed; writes still take the lock below,
        # because the explicit BEGIN and COMMIT here are a transaction
        # protocol that serialized mode does not make atomic on its own.
        if sqlite3.threadsafety < 3:  # pragma: no cover - depends on the build
            raise StoreError(
                "this Python's SQLite is not built for shared connections; "
                f"threadsafety is {sqlite3.threadsafety}, 3 is required"
            )
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            ":memory:" if self.path.name == ":memory:" else str(self.path),
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = None
        # Anything that fails from here on leaves a connection open unless it
        # is closed on the way out, and a leaked handle keeps a lock on the
        # database file, which on Windows stops the caller even deleting it.
        try:
            for pragma in PRAGMAS:
                try:
                    self._connection.execute(pragma)
                except sqlite3.DatabaseError as exc:  # pragma: no cover - old SQLite
                    raise StoreError(f"{pragma} failed: {exc}") from exc
            try:
                self._connection.executescript(SCHEMA)
            except sqlite3.DatabaseError as exc:
                raise StoreError(
                    f"cannot create the index schema, most likely because this "
                    f"Python's SQLite lacks FTS5: {exc}"
                ) from exc
            self._connection.execute("BEGIN")
            self._connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.close()
            raise

    # --- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> IndexStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def transaction(self) -> Any:
        """A context manager wrapping one atomic batch of writes."""
        return _Transaction(self._connection, self._lock)

    # --- metadata ----------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @property
    def schema_version(self) -> int:
        return int(self.get_meta("schema_version") or 0)

    def chars_per_token(self) -> float | None:
        """The calibrated token constant, if a calibration was recorded."""
        raw = self.get_meta("chars_per_token")
        if raw is None:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if value > 0 else None

    def calibrated_model(self) -> str | None:
        """Which model the recorded constant was counted for."""
        return self.get_meta("chars_per_token_model")

    def estimator(self) -> TokenEstimator:
        """The estimator every tool over this store should use.

        Calibrated when a calibration exists, the default otherwise. Kept
        on the store rather than passed around because the constant is a
        property of the index and the model reading it, not of any call.
        """
        constant = self.chars_per_token()
        return make_estimator(constant) if constant else estimate_tokens

    def generation(self) -> str:
        """A value that changes whenever the resolved index does.

        Bumped by every resolution and every reset. Anything derived from
        the whole index, a graph, a ranking, can be kept until this moves.
        """
        return self.get_meta("generation") or "0"

    def bump_generation(self) -> None:
        self.set_meta("generation", str(int(self.generation()) + 1))

    def replace_ranks(self, ranked: Iterable[tuple[str, float, int]]) -> None:
        """Swap the stored global ranking for a fresh one."""
        self._connection.execute("DELETE FROM ranks")
        self._connection.executemany(
            "INSERT INTO ranks(symbol_id, score, in_degree) VALUES(?, ?, ?)",
            list(ranked),
        )

    def ranks(self) -> dict[str, tuple[float, int]]:
        """The stored global ranking, empty if none was recorded."""
        rows = self._connection.execute(
            "SELECT symbol_id, score, in_degree FROM ranks"
        ).fetchall()
        return {row[0]: (float(row[1]), int(row[2])) for row in rows}

    def most_referenced(self, *, limit: int = 1) -> list[str]:
        """Ids of the symbols with the most distinct users, most first."""
        rows = self._connection.execute(
            "SELECT e.dst_id FROM edges e JOIN symbols s ON s.id = e.dst_id "
            "WHERE s.is_synthetic = 0 GROUP BY e.dst_id "
            "ORDER BY count(DISTINCT e.src_id) DESC, e.dst_id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row[0] for row in rows]

    def is_compatible(self, toolchain: str) -> bool:
        """Whether the stored index was produced by this toolchain.

        A mismatch is not an error; it means every file is dirty.
        """
        return (
            self.schema_version == SCHEMA_VERSION
            and self.get_meta("toolchain") == toolchain
        )

    def reset(self) -> None:
        """Empty the store, keeping the file and its schema."""
        with self.transaction():
            for table in (
                "import_bindings",
                "imports",
                "refs",
                "edges",
                "ranks",
                "symbols",
                "files",
            ):
                self._connection.execute(f"DELETE FROM {table}")
            self._connection.execute("DELETE FROM symbol_search")
            # The generation survives a reset, and moves, so a cache keyed
            # on it notices that everything it held is gone.
            generation = self.generation()
            self._connection.execute(
                "DELETE FROM meta WHERE key != 'schema_version'"
            )
            self.set_meta("generation", str(int(generation) + 1))

    # --- writing -----------------------------------------------------------

    def remove_file(self, path: str) -> None:
        """Drop a file and everything derived from it."""
        self._connection.execute(
            "DELETE FROM symbol_search WHERE rowid IN "
            "(SELECT rowid FROM symbols WHERE path = ?)",
            (path,),
        )
        self._connection.execute("DELETE FROM files WHERE path = ?", (path,))

    def put_file(
        self,
        record: FileRecord,
        symbols: Sequence[Symbol],
        references: Sequence[Reference],
        imports: FileImports | None = None,
    ) -> None:
        """Replace everything the store holds for one file."""
        self.remove_file(record.path)
        self._connection.execute(
            "INSERT INTO files(path, language, digest, size, mtime_ns, "
            "has_errors, error_count, namespace) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.path,
                record.language,
                record.digest,
                record.size,
                record.mtime_ns,
                int(record.has_errors),
                record.error_count,
                imports.namespace if imports else None,
            ),
        )
        if symbols:
            self._connection.executemany(
                "INSERT INTO symbols(id, path, name, kind, qualified_name, "
                "container_id, language, signature, documentation, is_local, "
                "is_synthetic, name_start_line, name_start_char, name_end_line, "
                "name_end_char, full_start_line, full_start_char, full_end_line, "
                "full_end_char) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?)",
                [
                    (
                        symbol.id,
                        symbol.path,
                        symbol.name,
                        symbol.kind.value,
                        symbol.qualified_name,
                        symbol.container_id,
                        symbol.language,
                        symbol.signature,
                        symbol.documentation,
                        int(symbol.local),
                        int(symbol.synthetic),
                        *_range_columns(symbol.name_range),
                        *_range_columns(symbol.full_range),
                    )
                    for symbol in symbols
                ],
            )
            # The search index is external-content, so it is fed by rowid
            # rather than by trigger: a trigger would fire during the delete
            # above as well and cost a second pass over every row.
            self._connection.execute(
                "INSERT INTO symbol_search(rowid, name, qualified_name) "
                "SELECT rowid, name, COALESCE(qualified_name, name) "
                "FROM symbols WHERE path = ?",
                (record.path,),
            )
        if references:
            self._connection.executemany(
                "INSERT INTO refs(path, name, kind, container_id, start_line, "
                "start_char, end_line, end_char) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        record.path,
                        reference.name,
                        reference.kind,
                        reference.container_id,
                        *_range_columns(reference.span),
                    )
                    for reference in references
                ],
            )
        if imports is not None:
            for statement in imports.statements:
                cursor = self._connection.execute(
                    "INSERT INTO imports(path, module, relative_level, "
                    "start_line, start_char, end_line, end_char) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.path,
                        statement.module,
                        statement.relative_level,
                        *_range_columns(statement.span),
                    ),
                )
                import_id = cursor.lastrowid
                if statement.bindings:
                    self._connection.executemany(
                        "INSERT INTO import_bindings(import_id, local, original, "
                        "start_line, start_char, end_line, end_char) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                import_id,
                                binding.local,
                                binding.original,
                                *_range_columns(binding.span),
                            )
                            for binding in statement.bindings
                        ],
                    )

    def replace_edges(self, edges: Iterable[Edge]) -> None:
        """Swap every resolved edge for a freshly resolved set.

        Resolution is global and cheap, roughly one part in sixty of the
        cost of parsing, so it is redone whole rather than patched. That
        removes the subtle failure this stage would otherwise invite: an
        edge from an untouched file into a file that just changed is stale,
        and finding every such edge is harder than recomputing them all.
        """
        self._connection.execute("DELETE FROM edges")
        self._connection.executemany(
            "INSERT INTO edges(site_path, src_id, dst_id, kind, tier, "
            "confidence, site_start_line, site_start_char, site_end_line, "
            "site_end_char) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    edge.site_path,
                    edge.src_id,
                    edge.dst_id,
                    edge.kind.value,
                    edge.tier.label,
                    edge.score,
                    *_range_columns(edge.site_range),
                )
                for edge in edges
            ],
        )

    # --- reading -----------------------------------------------------------

    def file_records(self) -> dict[str, FileRecord]:
        rows = self._connection.execute(
            "SELECT path, language, digest, size, mtime_ns, has_errors, error_count "
            "FROM files"
        ).fetchall()
        return {
            row[0]: FileRecord(
                path=row[0],
                language=row[1],
                digest=row[2],
                size=row[3],
                mtime_ns=row[4],
                has_errors=bool(row[5]),
                error_count=row[6],
            )
            for row in rows
        }

    def symbols(self, *, path: str | None = None) -> list[Symbol]:
        query = (
            "SELECT id, path, name, kind, qualified_name, container_id, language, "
            "signature, documentation, is_local, is_synthetic, name_start_line, "
            "name_start_char, name_end_line, name_end_char, full_start_line, "
            "full_start_char, full_end_line, full_end_char FROM symbols"
        )
        parameters: tuple[Any, ...] = ()
        if path is not None:
            query += " WHERE path = ?"
            parameters = (path,)
        return [_symbol_from(row) for row in self._connection.execute(query, parameters)]

    def edge_rows(self) -> list[tuple[str, str, str, float]]:
        """Every edge as ``(src, dst, kind, confidence)``, nothing more.

        The graph needs four columns of the ten, and building an ``Edge``
        for each of half a million rows was five times the cost of reading
        them. This is what the ranking cache loads.
        """
        return self._connection.execute(
            "SELECT src_id, dst_id, kind, confidence FROM edges"
        ).fetchall()

    def edges(self) -> list[Edge]:
        rows = self._connection.execute(
            "SELECT site_path, src_id, dst_id, kind, tier, confidence, "
            "site_start_line, site_start_char, site_end_line, site_end_char FROM edges"
        ).fetchall()
        return [_edge_from(row) for row in rows]

    def references(self) -> list[tuple[str, Reference]]:
        rows = self._connection.execute(
            "SELECT path, name, kind, container_id, start_line, start_char, "
            "end_line, end_char FROM refs"
        ).fetchall()
        return [
            (
                row[0],
                Reference(
                    name=row[1],
                    kind=row[2],
                    span=SourceRange.of(row[4], row[5], row[6], row[7]),
                    container_id=row[3],
                ),
            )
            for row in rows
        ]

    def imports(self) -> dict[str, FileImports]:
        result: dict[str, FileImports] = {}
        for path, namespace in self._connection.execute(
            "SELECT path, namespace FROM files"
        ):
            result[path] = FileImports(namespace=namespace)
        bindings: dict[int, list[ImportBinding]] = {}
        for row in self._connection.execute(
            "SELECT import_id, local, original, start_line, start_char, "
            "end_line, end_char FROM import_bindings"
        ):
            bindings.setdefault(row[0], []).append(
                ImportBinding(
                    local=row[1],
                    original=row[2],
                    span=SourceRange.of(row[3], row[4], row[5], row[6]),
                )
            )
        for row in self._connection.execute(
            "SELECT id, path, module, relative_level, start_line, start_char, "
            "end_line, end_char FROM imports ORDER BY id"
        ):
            file_imports = result.setdefault(row[1], FileImports())
            file_imports.statements.append(
                ImportStatement(
                    module=row[2],
                    bindings=tuple(bindings.get(row[0], [])),
                    span=SourceRange.of(row[4], row[5], row[6], row[7]),
                    relative_level=row[3],
                )
            )
        return result

    def snapshot(self) -> IndexSnapshot:
        """Rebuild the whole index as an in-memory snapshot."""
        snapshot = IndexSnapshot(
            project_root=self.get_meta("project_root"),
            producer=self.get_meta("producer"),
        )
        for symbol in self.symbols():
            snapshot.symbols[symbol.id] = symbol
        snapshot.edges = self.edges()
        return snapshot

    def languages(self) -> dict[str, str]:
        return dict(self._connection.execute("SELECT path, language FROM files"))

    # --- search ------------------------------------------------------------

    def search(self, query: str, *, limit: int = 50, kinds: Sequence[str] = ()) -> list[Symbol]:
        """Find symbols whose name or qualified name contains ``query``.

        Substring rather than prefix matching, because an agent looking for
        ``Resolver`` should find ``ModuleResolver``. Synthetic and local
        symbols are excluded: neither is somewhere to navigate to.
        """
        cleaned = query.strip()
        source, parameters = self._search_source(cleaned, kinds)
        # Ranked by how directly the symbol answers the query. A match on
        # the name itself beats one that only landed in the qualified name,
        # which otherwise lets a short private member of a matching class
        # outrank the class. Among equals, shorter wins, so `User` comes
        # before `UserRepositoryFactory`.
        sql = (
            f"SELECT {_SYMBOL_COLUMNS} {source}"
            " ORDER BY (lower(s.name) = lower(?)) DESC,"
            " (instr(lower(s.name), lower(?)) > 0) DESC,"
            " length(s.name) ASC, s.path ASC, s.name_start_line ASC LIMIT ?"
        )
        parameters = [*parameters, cleaned, cleaned, limit]
        return [_symbol_from(row) for row in self._connection.execute(sql, parameters)]

    def search_count(self, query: str, *, kinds: Sequence[str] = ()) -> int:
        """How many symbols :meth:`search` would match without a limit.

        A search result that says how much it left out has to know, and
        over-fetching by one only ever knows "at least one more".
        """
        source, parameters = self._search_source(query.strip(), kinds)
        row = self._connection.execute(f"SELECT count(*) {source}", parameters).fetchone()
        return int(row[0]) if row else 0

    def _search_source(self, cleaned: str, kinds: Sequence[str]) -> tuple[str, list[Any]]:
        """The FROM and WHERE of a search, shared by the query and its count."""
        if len(cleaned) < 3:
            # Trigram indexes cannot answer a shorter query, so fall back to
            # a scan, which is cheap because it is bounded by the limit.
            source = (
                "FROM symbols s WHERE s.is_synthetic = 0 AND s.is_local = 0 "
                "AND instr(lower(s.name), lower(?)) > 0"
            )
            parameters: list[Any] = [cleaned]
        else:
            source = (
                "FROM symbol_search JOIN symbols s ON s.rowid = symbol_search.rowid "
                "WHERE symbol_search MATCH ? AND s.is_synthetic = 0 AND s.is_local = 0"
            )
            parameters = [_fts_query(cleaned)]
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            source += f" AND s.kind IN ({placeholders})"
            parameters.extend(kinds)
        return source, parameters

    def symbol(self, symbol_id: str) -> Symbol | None:
        row = self._connection.execute(
            "SELECT id, path, name, kind, qualified_name, container_id, language, "
            "signature, documentation, is_local, is_synthetic, name_start_line, "
            "name_start_char, name_end_line, name_end_char, full_start_line, "
            "full_start_char, full_end_line, full_end_char FROM symbols WHERE id = ?",
            (symbol_id,),
        ).fetchone()
        return _symbol_from(row) if row else None

    def edges_from(self, symbol_id: str) -> list[Edge]:
        """Edges whose source is this symbol: what it uses."""
        return self._edges_where("src_id = ?", symbol_id)

    def edges_to(self, symbol_id: str) -> list[Edge]:
        """Edges whose target is this symbol: what uses it."""
        return self._edges_where("dst_id = ?", symbol_id)

    def _edges_where(self, clause: str, *parameters: Any) -> list[Edge]:
        rows = self._connection.execute(
            "SELECT site_path, src_id, dst_id, kind, tier, confidence, "
            "site_start_line, site_start_char, site_end_line, site_end_char "
            f"FROM edges WHERE {clause}",
            parameters,
        ).fetchall()
        return [_edge_from(row) for row in rows]

    def reference_counts(self, symbol_ids: Sequence[str]) -> dict[str, int]:
        """How many distinct symbols use each of these, in one query.

        One query rather than one per symbol: a search returning twenty hits
        would otherwise make twenty round trips to decorate its own output.
        """
        if not symbol_ids:
            return {}
        placeholders = ", ".join("?" for _ in symbol_ids)
        rows = self._connection.execute(
            f"SELECT dst_id, count(DISTINCT src_id) FROM edges "
            f"WHERE dst_id IN ({placeholders}) GROUP BY dst_id",
            tuple(symbol_ids),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def counts(self) -> dict[str, int]:
        with closing(self._connection.cursor()) as cursor:
            return {
                name: cursor.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in ("files", "symbols", "edges", "refs", "imports")
            }

    def size_bytes(self) -> int:
        row = self._connection.execute(
            "SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()"
        ).fetchone()
        return int(row[0]) if row else 0


_SYMBOL_COLUMNS = (
    "s.id, s.path, s.name, s.kind, s.qualified_name, s.container_id, "
    "s.language, s.signature, s.documentation, s.is_local, s.is_synthetic, "
    "s.name_start_line, s.name_start_char, s.name_end_line, s.name_end_char, "
    "s.full_start_line, s.full_start_char, s.full_end_line, s.full_end_char"
)


def _fts_query(text: str) -> str:
    """Quote a user string so FTS5 reads it as a literal, not as syntax."""
    return '"' + text.replace('"', '""') + '"'


def _edge_from(row: Sequence[Any]) -> Edge:
    return Edge(
        src_id=row[1],
        dst_id=row[2],
        kind=EdgeKind(row[3]),
        tier=ResolutionTier.from_label(row[4]),
        confidence=row[5],
        site_path=row[0],
        site_range=_range_from(row, 6),
    )


def _symbol_from(row: Sequence[Any]) -> Symbol:
    return Symbol(
        id=row[0],
        path=row[1],
        name=row[2],
        kind=SymbolKind(row[3]),
        qualified_name=row[4],
        container_id=row[5],
        language=row[6],
        signature=row[7],
        documentation=row[8],
        local=bool(row[9]),
        synthetic=bool(row[10]),
        name_range=SourceRange.of(row[11], row[12], row[13], row[14]),
        full_range=_range_from(row, 15),
    )


class _Transaction:
    """One atomic batch of writes."""

    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock) -> None:
        self._connection = connection
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        # Held for the whole transaction. Two threads interleaving their
        # statements between one BEGIN and one COMMIT would commit each
        # other's half-finished work.
        self._lock.acquire()
        try:
            self._connection.execute("BEGIN")
        except BaseException:
            self._lock.release()
            raise
        return self._connection

    def __exit__(self, exc_type: object, *_: object) -> None:
        try:
            if exc_type is None:
                self._connection.execute("COMMIT")
            else:
                self._connection.execute("ROLLBACK")
        finally:
            self._lock.release()
