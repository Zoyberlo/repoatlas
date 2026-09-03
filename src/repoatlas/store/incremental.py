"""Keeping a store current without rebuilding it.

Two phases with very different costs. Parsing a file is expensive; resolving
names across the repository is not, by roughly sixty to one on the projects
measured. So the split is: parse only what changed, then resolve everything.

That asymmetry is worth being explicit about, because the tempting
optimisation is wrong. Patching only the edges of changed files leaves stale
edges *into* them: a file nobody touched still refers to a symbol that has
just moved or vanished. Finding every such edge is harder than recomputing
the lot, and getting it wrong produces an index that looks fine and points
at the wrong line.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from ..model import (
    Edge,
    EdgeKind,
    IndexSnapshot,
    ResolutionTier,
    SourceRange,
    Symbol,
    SymbolKind,
)
from ..parse.build import LanguageStats, _resolve_references
from ..parse.extract import extract_source
from ..parse.languages import SUPPORTED, LanguageUnavailable, query_source
from ..parse.walk import SourceFile, WalkStats, iter_source_files
from ..plugins import frameworks_source
from ..rank.pagerank import rank_symbols
from ..resolve.cascade import ResolutionStats
from .database import FileRecord, IndexStore, content_digest, toolchain_version

__all__ = ["ChangeSet", "UpdateResult", "detect_changes", "update_store"]


@dataclass(slots=True)
class ChangeSet:
    """Which files need re-parsing, and which the store can keep."""

    added: list[SourceFile] = field(default_factory=list)
    modified: list[SourceFile] = field(default_factory=list)
    unchanged: list[SourceFile] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    full_rebuild: bool = False
    """True when nothing stored can be trusted and everything is re-parsed.

    Either the store is new, or the parser or a tag query changed, which
    alters what extraction would produce from the very same bytes.
    """

    was_empty: bool = False
    """True when the store held nothing, so a rebuild is not a warning."""

    stat_hits: int = 0
    """Files ruled unchanged by size and mtime alone, without hashing."""

    @property
    def dirty(self) -> list[SourceFile]:
        return [*self.added, *self.modified]

    @property
    def is_empty(self) -> bool:
        return not self.dirty and not self.removed

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "added": len(self.added),
            "modified": len(self.modified),
            "unchanged": len(self.unchanged),
            "removed": len(self.removed),
            "stat_hits": self.stat_hits,
            "full_rebuild": self.full_rebuild,
        }


@dataclass(slots=True)
class UpdateResult:
    """What an update did, and how long each phase took."""

    changes: ChangeSet
    parsed: int = 0
    parse_seconds: float = 0.0
    resolve_seconds: float = 0.0
    by_language: dict[str, LanguageStats] = field(default_factory=dict)
    resolution: ResolutionStats = field(default_factory=ResolutionStats)
    failures: list[tuple[str, str]] = field(default_factory=list)
    walk: WalkStats = field(default_factory=WalkStats)

    @property
    def total_seconds(self) -> float:
        return self.parse_seconds + self.resolve_seconds

    def as_dict(self) -> dict[str, object]:
        return {
            "changes": self.changes.as_dict(),
            "parsed": self.parsed,
            "parse_seconds": round(self.parse_seconds, 3),
            "resolve_seconds": round(self.resolve_seconds, 3),
            "resolution": self.resolution.as_dict(),
            "by_language": {
                name: stats.as_dict() for name, stats in sorted(self.by_language.items())
            },
            "failures": len(self.failures),
        }


def current_toolchain() -> str:
    """The stamp for this parser, its tag queries and the convention rules.

    The framework rules belong in the stamp even though they are not
    queries. A no-op re-index skips resolution entirely, so editing a
    convention would otherwise leave every edge it used to produce in place
    and every edge it newly allows missing, with nothing to show that
    anything had changed.
    """
    queries: list[tuple[str, str]] = []
    for spec in SUPPORTED:
        try:
            queries.append((spec.name, query_source(spec.name)))
        except LanguageUnavailable:  # pragma: no cover - missing query file
            continue
    queries.append(("frameworks", frameworks_source()))
    return toolchain_version(queries)


def detect_changes(
    files: Iterable[SourceFile],
    store: IndexStore,
    *,
    toolchain: str | None = None,
    trust_mtime: bool = True,
) -> ChangeSet:
    """Compare the filesystem against the store.

    ``trust_mtime`` uses size and modification time to skip hashing, which
    is what makes a no-op re-index fast. Turning it off hashes every file,
    for the case where a checkout has rewritten timestamps or a build step
    has touched files without changing them.
    """
    toolchain = toolchain or current_toolchain()
    stored = store.file_records()
    changes = ChangeSet(
        full_rebuild=not store.is_compatible(toolchain),
        was_empty=not stored,
    )
    known = {} if changes.full_rebuild else stored
    seen: set[str] = set()

    for source in files:
        seen.add(source.path)
        record = known.get(source.path)
        if record is None:
            changes.added.append(source)
            continue
        try:
            stat = source.absolute.stat()
        except OSError:
            changes.modified.append(source)
            continue
        if trust_mtime and record.unchanged_by_stat(stat.st_size, stat.st_mtime_ns):
            changes.stat_hits += 1
            changes.unchanged.append(source)
            continue
        try:
            digest = content_digest(source.absolute.read_bytes())
        except OSError:
            changes.modified.append(source)
            continue
        if digest == record.digest:
            changes.unchanged.append(source)
        else:
            changes.modified.append(source)

    changes.removed = [path for path in known if path not in seen]
    return changes


def update_store(
    root: Path | str,
    store: IndexStore,
    *,
    use_git: bool = True,
    trust_mtime: bool = True,
    resolve: bool = True,
) -> UpdateResult:
    """Bring ``store`` up to date with ``root``, parsing only what changed."""
    root_path = Path(root).resolve()
    walk_stats = WalkStats()
    source_files = list(iter_source_files(root_path, use_git=use_git, stats=walk_stats))
    toolchain = current_toolchain()
    changes = detect_changes(
        source_files, store, toolchain=toolchain, trust_mtime=trust_mtime
    )
    result = UpdateResult(changes=changes, walk=walk_stats)

    if changes.full_rebuild:
        store.reset()

    started = time.perf_counter()
    with store.transaction():
        for path in changes.removed:
            store.remove_file(path)
        unavailable: set[str] = set()
        for source in changes.dirty:
            if source.language.name in unavailable:
                continue
            try:
                content = source.absolute.read_bytes()
                stat = source.absolute.stat()
            except OSError as exc:
                result.failures.append((source.path, f"unreadable: {exc}"))
                continue
            try:
                extraction = extract_source(source.path, content, source.language)
            except LanguageUnavailable as exc:
                unavailable.add(source.language.name)
                result.failures.append((source.path, str(exc)))
                continue
            except Exception as exc:  # pragma: no cover - grammar crash guard
                result.failures.append((source.path, f"{type(exc).__name__}: {exc}"))
                continue

            symbols = list(extraction.symbols)
            symbols.append(_module_symbol(extraction.path))
            store.put_file(
                FileRecord(
                    path=source.path,
                    language=source.language.name,
                    digest=content_digest(content),
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    has_errors=extraction.has_errors,
                    error_count=extraction.error_count,
                ),
                symbols,
                extraction.references,
                extraction.imports,
            )
            result.parsed += 1
            stats = result.by_language.setdefault(extraction.language, LanguageStats())
            stats.files += 1
            stats.symbols += len(extraction.symbols)
            stats.references += len(extraction.references)
            if extraction.has_errors:
                stats.files_with_errors += 1
                stats.error_nodes += extraction.error_count
        store.set_meta("toolchain", toolchain)
        store.set_meta("project_root", str(root_path))
        store.set_meta("producer", f"repoatlas {__version__} (tree-sitter)")
    result.parse_seconds = time.perf_counter() - started

    # Resolution is global, so it runs whenever anything moved. When
    # nothing did, the stored edges are already the answer: re-running would
    # burn the one cost a no-op re-index is supposed to avoid.
    if resolve and not changes.is_empty:
        started = time.perf_counter()
        _resolve_into(store, root_path, source_files, result)
        result.resolve_seconds = time.perf_counter() - started
    return result


def _module_symbol(path: str) -> Symbol:
    """The stand-in symbol for a file's top level.

    Built here rather than borrowed from the batch builder because the
    store persists per file, and a synthetic symbol has to be stored with
    the file it stands for so deleting that file removes it too.
    """
    origin = SourceRange.of(0, 0, 0, 0)
    return Symbol(
        id=f"{path}#<module>",
        name=Path(path).name,
        kind=SymbolKind.MODULE,
        path=path,
        name_range=origin,
        full_range=origin,
        synthetic=True,
    )


def _resolve_into(
    store: IndexStore,
    root: Path,
    files: list[SourceFile],
    result: UpdateResult,
) -> None:
    """Resolve every reference in the store and replace the edge set."""
    from ..parse.build import BuildResult

    snapshot = IndexSnapshot()
    for symbol in store.symbols():
        snapshot.symbols[symbol.id] = symbol
    # Containment is derived from the symbols themselves rather than stored
    # as edges, so it has to be rebuilt here. Without this the stored index
    # carried no containment at all, and a map built from a store ranked
    # differently from one built by parsing the same repository directly.
    for symbol in snapshot.symbols.values():
        if symbol.container_id and symbol.container_id in snapshot.symbols:
            snapshot.add_edge(
                Edge(
                    src_id=symbol.container_id,
                    dst_id=symbol.id,
                    kind=EdgeKind.CONTAINS,
                    tier=ResolutionTier.ORACLE,
                    site_path=symbol.path,
                    site_range=symbol.name_range,
                )
            )
    build = BuildResult(
        snapshot=snapshot,
        references=store.references(),
        imports=store.imports(),
        resolution=result.resolution,
    )
    _resolve_references(build, root, files)
    # Rank here, once per change, rather than on every map call. The
    # global ranking depends on nothing but the graph, and the graph is
    # settled the moment the edges are.
    ranked = rank_symbols(build.snapshot)
    with store.transaction():
        store.replace_edges(build.snapshot.edges)
        store.replace_ranks(
            (item.symbol.id, item.score, item.in_degree) for item in ranked
        )
        store.bump_generation()
