"""Keeping a store current without rebuilding it.

Parsing only what changed is the easy half. The other half is which
references to resolve again, and the tempting answer, only the ones in the
changed files, is wrong: a file nobody touched still refers to a symbol
that has just moved or vanished.

There is a rule that is both narrow and right. A reference outside the
changed files resolves through the cascade, and every rung of the cascade
asks about symbols *by name*: the import rung looks the imported name up
in its target file, the member and same-file rungs look it up in one
scope, and the bottom rungs look it up across the repository. So a
reference's answer can only change if a symbol with its name was added,
removed or moved. The set to revisit is therefore every reference in a
changed file, plus every reference whose name is among the names the
change touched, plus the framework-convention references when a file
appeared or vanished, since those name files rather than symbols. That is
usually a small fraction of the repository, and a test holds it to the
same edges a full rebuild produces.

Two things still fall back to the whole repository: a rebuild the toolchain
forced, and a change in which framework plugins are active, which nothing
in the files can predict.
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
from ..parse.extract import Reference, extract_source
from ..parse.languages import SUPPORTED, LanguageUnavailable, query_source
from ..parse.walk import SourceFile, Tree, WalkStats, WorkingTree
from ..plugins import active_plugins, frameworks_source
from ..rank.pagerank import SymbolGraph, rank_symbols
from ..resolve.cascade import ResolutionStats
from ..resolve.derived import derive_inheritance
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
    scoped: bool = False
    """Whether resolution revisited only what the change could reach."""

    revisited: int = 0
    """How many references a scoped resolution looked at again."""

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
            "scoped": self.scoped,
            "revisited": self.revisited,
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


class _FromDisk:
    """Read a file through the path it carries, for callers with no tree.

    ``detect_changes`` is public and older callers pass a list of files and
    nothing else. Those files came off a disk, so this is what they meant.
    """

    @staticmethod
    def _located(source: SourceFile) -> Path:
        if source.absolute is None:
            raise ValueError(
                f"{source.path} has no location on disk; pass the tree it came from"
            )
        return source.absolute

    def read(self, source: SourceFile) -> bytes:
        return self._located(source).read_bytes()

    def stamp(self, source: SourceFile) -> tuple[int, int] | None:
        stat = self._located(source).stat()
        return stat.st_size, stat.st_mtime_ns


def detect_changes(
    files: Iterable[SourceFile],
    store: IndexStore,
    *,
    toolchain: str | None = None,
    trust_mtime: bool = True,
    tree: Tree | None = None,
) -> ChangeSet:
    """Compare the source against the store.

    ``trust_mtime`` uses size and modification time to skip hashing, which
    is what makes a no-op re-index fast. Turning it off hashes every file,
    for the case where a checkout has rewritten timestamps or a build step
    has touched files without changing them.

    A tree that has no timestamps to offer — a git revision — says so, and
    then every file is hashed regardless of ``trust_mtime``. That is not an
    oversight to optimise away later: a commit has no mtimes, and any
    constant stood in for them would make every file look untouched.
    """
    toolchain = toolchain or current_toolchain()
    stored = store.file_records()
    changes = ChangeSet(
        full_rebuild=not store.is_compatible(toolchain),
        was_empty=not stored,
    )
    known = {} if changes.full_rebuild else stored
    seen: set[str] = set()
    access: Tree | _FromDisk = tree if tree is not None else _FromDisk()

    for source in files:
        seen.add(source.path)
        record = known.get(source.path)
        if record is None:
            changes.added.append(source)
            continue
        try:
            stamp = access.stamp(source)
        except OSError:
            changes.modified.append(source)
            continue
        if trust_mtime and stamp is not None and record.unchanged_by_stat(*stamp):
            changes.stat_hits += 1
            changes.unchanged.append(source)
            continue
        try:
            digest = content_digest(access.read(source))
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
    root: Path | str | None,
    store: IndexStore,
    *,
    use_git: bool = True,
    trust_mtime: bool = True,
    resolve: bool = True,
    tree: Tree | None = None,
) -> UpdateResult:
    """Bring ``store`` up to date with its source, parsing only what changed.

    ``tree`` says where that source is. Left out, it is the working tree at
    ``root``, which is what it has always been. Passed a
    :class:`~repoatlas.parse.gitobjects.RevisionTree`, the whole build runs
    against git's object database and never needs the files on disk — the
    setting the diff-review result was measured in.
    """
    if tree is None:
        if root is None:
            raise ValueError("update_store needs either a root or a tree")
        tree = WorkingTree(Path(root).resolve(), use_git=use_git)
    walk_stats = WalkStats()
    source_files = list(tree.files(walk_stats))
    toolchain = current_toolchain()
    changes = detect_changes(
        source_files, store, toolchain=toolchain, trust_mtime=trust_mtime, tree=tree
    )
    result = UpdateResult(changes=changes, walk=walk_stats)

    if changes.full_rebuild:
        store.reset()

    # What the changed files defined *before* this update, per name, in the
    # form resolution sees it. Compared with what they define afterwards,
    # this says which names an untouched reference could resolve
    # differently for. A body edit that moves no definition changes no
    # name, and revisits only the file's own references.
    definitions_before = store.definitions_by_name(
        [source.path for source in changes.dirty] + list(changes.removed)
    )

    started = time.perf_counter()
    with store.transaction():
        for path in changes.removed:
            store.remove_file(path)
        unavailable: set[str] = set()
        for source in changes.dirty:
            if source.language.name in unavailable:
                continue
            try:
                content = tree.read(source)
                stamp = tree.stamp(source)
            except OSError as exc:
                result.failures.append((source.path, f"unreadable: {exc}"))
                continue
            # A revision has no timestamps. Zero is not a lie here the way
            # it would be in change detection: the stamp is only ever
            # trusted when the tree offered one, and this tree did not.
            size, mtime_ns = stamp if stamp is not None else (len(content), 0)
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
                    size=size,
                    mtime_ns=mtime_ns,
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
        store.set_meta("project_root", tree.label)
        store.set_meta("producer", f"repoatlas {__version__} (tree-sitter)")
        for key, value in tree.provenance().items():
            store.set_meta(key, value)
    result.parse_seconds = time.perf_counter() - started

    # Resolution is global, so it runs whenever anything moved. When
    # nothing did, the stored edges are already the answer: re-running would
    # burn the one cost a no-op re-index is supposed to avoid.
    if resolve and not changes.is_empty:
        started = time.perf_counter()
        paths = {source.path for source in source_files}
        # Framework detection reads manifests, so it needs a directory even
        # when the index does not. A working tree hands over its own root;
        # a revision writes out the few manifests that could matter. The
        # alternative was for a no-checkout index to silently lose every
        # Blade view and auto-imported component, which is the half of the
        # PHP result that a parser cannot recover.
        with tree.manifests(paths) as conventions_root:
            plugins_now = ",".join(
                sorted(plugin.name for plugin in active_plugins(conventions_root, paths))
            )
            plugins_before = store.get_meta("plugins")
            scoped = (
                not changes.full_rebuild
                and not changes.was_empty
                and plugins_before == plugins_now
            )
            if scoped:
                touched = [source.path for source in changes.dirty]
                definitions_after = store.definitions_by_name(touched)
                changed_names = {
                    name
                    for name in definitions_before.keys() | definitions_after.keys()
                    if definitions_before.get(name) != definitions_after.get(name)
                }
                _resolve_scoped(
                    store,
                    conventions_root,
                    source_files,
                    result,
                    touched=touched,
                    removed=list(changes.removed),
                    changed_names=changed_names,
                    files_changed=bool(changes.added or changes.removed),
                )
            else:
                _resolve_into(store, conventions_root, source_files, result)
        with store.transaction():
            store.set_meta("plugins", plugins_now)
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


def _resolve_scoped(
    store: IndexStore,
    root: Path,
    files: list[SourceFile],
    result: UpdateResult,
    *,
    touched: list[str],
    removed: list[str],
    changed_names: set[str],
    files_changed: bool,
) -> None:
    """Resolve only what the change can have affected, and patch the edges.

    The symbol table is loaded whole, because every rung of the cascade
    needs the repository's definitions by name; that is the one term that
    still grows with the repository rather than with the change. The
    references, imports and edges are the change's own.
    """
    from ..parse.build import BuildResult
    from ..resolve.cascade import CONVENTION_KINDS

    snapshot = IndexSnapshot()
    for symbol in store.symbols():
        snapshot.symbols[symbol.id] = symbol

    stale = set(touched) | set(removed)
    affected = store.references(
        paths=touched,
        names=changed_names,
        kinds=CONVENTION_KINDS if files_changed else (),
    )
    affected = [(path, reference) for path, reference in affected if path not in removed]
    build = BuildResult(
        snapshot=snapshot,
        references=affected,
        imports=store.imports(paths={path for path, _ in affected}),
        resolution=result.resolution,
    )
    # Every base clause in the store, so a typed receiver's member lookup
    # can walk a chain through files this change never touched.
    base_sources: dict[str, list[Reference]] = {}
    for base_path, base_reference in store.references(kinds=["class"]):
        base_sources.setdefault(base_path, []).append(base_reference)
    _resolve_references(build, root, files, base_sources=base_sources, derive=False)

    # Containment for the re-parsed files is rebuilt from their symbols,
    # exactly as the full path does for every file.
    for symbol in snapshot.symbols.values():
        if symbol.path in stale and symbol.container_id in snapshot.symbols:
            build.snapshot.add_edge(
                Edge(
                    src_id=symbol.container_id,
                    dst_id=symbol.id,
                    kind=EdgeKind.CONTAINS,
                    tier=ResolutionTier.ORACLE,
                    site_path=symbol.path,
                    site_range=symbol.name_range,
                )
            )

    with store.transaction():
        store.delete_edges_in(stale)
        store.delete_edges_at(
            (path, reference.span.start.line, reference.span.start.character)
            for path, reference in affected
            if path not in stale
        )
        store.add_edges(build.snapshot.edges)
        # Derived edges are recomputed whole: the set is small, and which
        # derivations a change invalidates is harder to know than to redo.
        store.delete_derived_edges()
        store.add_edges(derive_inheritance(snapshot.symbols, store.inheritance_edges()))
        graph = SymbolGraph.from_rows(snapshot.symbols, store.edge_rows())
        ranked = rank_symbols(snapshot, graph=graph)
        store.replace_ranks(
            (item.symbol.id, item.score, item.in_degree) for item in ranked
        )
        store.bump_generation()
    result.scoped = True
    result.revisited = len(affected)


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
