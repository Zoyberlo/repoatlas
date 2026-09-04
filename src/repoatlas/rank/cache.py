"""Keeping the graph and its ranking between calls.

A map used to be built from nothing on every call: load every symbol and
edge from the store, build the graph, run the power iteration, render.
Measured on a synthetic index of a hundred thousand symbols and half a
million edges that was six and a half seconds, and the agent asks for a
map more than once.

Nothing in that work depends on the question. The graph and its transition
table are the same for every call until the index changes, and the global
ranking is the same for every unfocused call. So they are kept, keyed by
the store's generation, which the store bumps whenever resolution runs. A
focused map reuses the graph and pays only for its own power iteration; an
unfocused one pays for nothing but the render.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..model import IndexSnapshot
from .pagerank import RankedSymbol, RankOptions, SymbolGraph, rank_symbols

if TYPE_CHECKING:  # pragma: no cover - the store imports this package's tokens
    from ..store.database import IndexStore

__all__ = ["MIN_COMPONENT", "RankCache", "mention_keys"]

MIN_COMPONENT = 4
"""Shortest word of a name that a mention may match on its own.

Below this a component matches half the names in any project: `id`, `api`
and `get` say nothing about which files a task touches.
"""

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")


def mention_keys(name: str) -> set[str]:
    """Everything a mention may be matched against, for one name.

    The whole name, and the words it is made of. A task says "the client
    report", and the file that answers it is `ClientsReportExport`; exact
    matching sent that word to whatever local variable happened to be
    spelled `client` instead, which measured worse than not steering at
    all. See docs/benchmarks/steering.md.
    """
    keys = {name.lower()}
    spaced = _CAMEL_BOUNDARY.sub(" ", name)
    for piece in _SEPARATORS.split(spaced):
        if len(piece) >= MIN_COMPONENT:
            keys.add(piece.lower())
    return keys


@dataclass(slots=True)
class _Loaded:
    """Everything one generation of the index needs to be ranked."""

    generation: str
    snapshot: IndexSnapshot
    graph: SymbolGraph
    global_ranking: list[RankedSymbol] | None = None
    by_name: dict[str, list[str]] = field(default_factory=dict)
    """Mention key to symbol ids: the whole name and the words in it."""

    by_stem: dict[str, list[str]] = field(default_factory=dict)
    """Mention key to paths, so a mention can name a file too."""


@dataclass(slots=True)
class RankCache:
    """The snapshot, graph and global ranking of one store, kept current.

    One per server process. Safe to share between the worker threads the
    MCP SDK runs tools on, because a reload replaces the whole loaded
    object under a lock rather than mutating it.
    """

    options: RankOptions = field(default_factory=RankOptions)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _loaded: _Loaded | None = field(default=None, repr=False)
    loads: int = 0
    """How many times the graph was rebuilt, for tests and for status."""

    def _current(self, store: IndexStore) -> _Loaded:
        generation = store.generation()
        with self._lock:
            loaded = self._loaded
            if loaded is not None and loaded.generation == generation:
                return loaded
            # Symbols in full, edges as bare rows: the graph needs four
            # columns of an edge's ten, and building objects for half a
            # million of them was most of a cold start.
            snapshot = IndexSnapshot(
                project_root=store.get_meta("project_root"),
                producer=store.get_meta("producer"),
            )
            for symbol in store.symbols():
                snapshot.symbols[symbol.id] = symbol
            graph = SymbolGraph.from_rows(snapshot.symbols, store.edge_rows(), self.options)
            loaded = _Loaded(generation=generation, snapshot=snapshot, graph=graph)
            for symbol in snapshot.symbols.values():
                if symbol.synthetic or symbol.local:
                    continue
                for key in mention_keys(symbol.name):
                    loaded.by_name.setdefault(key, []).append(symbol.id)
            for path in {symbol.path for symbol in snapshot.symbols.values()}:
                stem = path.rsplit("/", 1)[-1].split(".", 1)[0]
                for key in mention_keys(stem):
                    loaded.by_stem.setdefault(key, []).append(path)
            self._loaded = loaded
            self.loads += 1
            return loaded

    def snapshot(self, store: IndexStore) -> IndexSnapshot:
        return self._current(store).snapshot

    def graph(self, store: IndexStore) -> SymbolGraph:
        return self._current(store).graph

    def seeds_for(self, store: IndexStore, mentions: Iterable[str]) -> tuple[set[str], set[str], set[str]]:
        """Turn words into the symbols and files they name.

        A mention matches a symbol whose name it is, or whose name is
        partly made of it, and a file the same way by stem: `invoice`
        reaches `InvoiceExporter` and `invoice.ts` alike. Matching only
        whole names measured *worse* than not steering at all on a real
        project, because a task's word is usually a part of the name that
        matters rather than the whole of it.

        What matched nothing is returned as well, so the answer can say so
        instead of quietly ranking the whole repository as if nothing had
        been asked.
        """
        loaded = self._current(store)
        symbols: set[str] = set()
        paths: set[str] = set()
        unmatched: set[str] = set()
        for mention in mentions:
            key = mention.strip().lower()
            if not key:
                continue
            hit = False
            for symbol_id in loaded.by_name.get(key, ()):
                symbols.add(symbol_id)
                hit = True
            for path in loaded.by_stem.get(key, ()):
                paths.add(path)
                hit = True
            if not hit:
                unmatched.add(mention)
        return symbols, paths, unmatched

    def ranking(
        self,
        store: IndexStore,
        *,
        focus_paths: set[str] | None = None,
        focus_symbols: set[str] | None = None,
    ) -> list[RankedSymbol]:
        """Rank the index, reusing everything that does not depend on the focus.

        An unfocused ranking is served from the store when the index
        recorded one at resolution time, so the first call after a restart
        costs no power iteration either; failing that it is computed once
        and kept for the generation.
        """
        loaded = self._current(store)
        if focus_paths or focus_symbols:
            return rank_symbols(
                loaded.snapshot,
                focus_paths=focus_paths,
                focus_symbols=focus_symbols,
                options=self.options,
                graph=loaded.graph,
            )
        with self._lock:
            if loaded.global_ranking is None:
                loaded.global_ranking = _stored_or_computed(store, loaded, self.options)
            return loaded.global_ranking


def _stored_or_computed(
    store: IndexStore, loaded: _Loaded, options: RankOptions
) -> list[RankedSymbol]:
    stored = store.ranks()
    if stored:
        ranked = [
            RankedSymbol(symbol=loaded.snapshot.symbols[symbol_id], score=score, in_degree=degree)
            for symbol_id, (score, degree) in stored.items()
            if symbol_id in loaded.snapshot.symbols
        ]
        # The same order the ranker itself produces, so a map from stored
        # ranks is byte-identical to one from a fresh power iteration.
        ranked.sort(key=lambda item: (-item.score, item.symbol.path, item.symbol.name_range))
        if ranked:
            return ranked
    return rank_symbols(loaded.snapshot, options=options, graph=loaded.graph)
