"""Ranking symbols by how much the rest of the code depends on them.

The question a map has to answer is which fifty of five thousand symbols
belong in two thousand tokens. Counting references is the obvious answer and
the wrong one: it treats a call from a test helper as worth the same as a
call from the module everything else routes through.

PageRank fixes that by making importance recursive. A symbol matters when
things that matter refer to it. Personalising it steers the whole
calculation toward whatever the agent is looking at: seed the walk at the
files in the working set, and rank concentrates on what those files reach
rather than on whatever is globally popular.

The implementation is power iteration over the sparse graph, with no
dependency. NetworkX would do this in one call, but the core of this
project installs with nothing, and the algorithm is thirty lines.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..model import Edge, EdgeKind, IndexSnapshot, Symbol, SymbolKind

__all__ = ["RankOptions", "RankedSymbol", "SymbolGraph", "rank_symbols"]

# Restart mass by symbol kind. A map answers "where does the behaviour
# live", so the things that hold behaviour outrank the things that hold
# state, and both outrank a bare alias.
_KIND_PRIOR = {
    SymbolKind.CLASS: 1.0,
    SymbolKind.INTERFACE: 1.0,
    SymbolKind.TRAIT: 1.0,
    SymbolKind.ENUM: 0.9,
    SymbolKind.FUNCTION: 0.9,
    SymbolKind.METHOD: 0.8,
    SymbolKind.CONSTRUCTOR: 0.5,
    SymbolKind.TYPE_ALIAS: 0.7,
    SymbolKind.MODULE: 0.5,
    SymbolKind.NAMESPACE: 0.5,
    SymbolKind.CONSTANT: 0.4,
    SymbolKind.PROPERTY: 0.3,
    SymbolKind.FIELD: 0.25,
    SymbolKind.VARIABLE: 0.2,
    SymbolKind.MACRO: 0.6,
    SymbolKind.PARAMETER: 0.1,
    SymbolKind.UNKNOWN: 0.3,
}

# Keywords with which a language declares that something is not part of its
# public surface.
_PRIVATE_KEYWORDS = ("private ", "protected ", "private static ", "protected static ")


def _is_private(symbol: Symbol) -> bool:
    """Whether the language or the convention marks this symbol private.

    A leading underscore is the convention, `#name` is how JavaScript
    spells it, and `private` is how the typed languages do. A dunder is the
    exception to the underscore rule: `__init__` and `__str__` are the most
    public thing a Python class has.
    """
    name = symbol.name
    if name.startswith("#"):
        return True
    if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
        return True
    signature = (symbol.signature or "").lstrip()
    return signature.startswith(_PRIVATE_KEYWORDS)


# How much each kind of edge carries rank. Ordered by what the retrieval
# literature found actually predicts relevance: expanding along call edges
# beat expanding along containment by roughly two to one (SpIDER, 2025).
#
# Containment runs the other way from the rest, reversed where the graph is
# built: a member lends rank to the type that holds it. On this project that
# is the difference between a map led by `Symbol`, `IndexStore` and
# `Comparison` and one led by `Position`, `SourceRange.of` and
# `_symbol_from`. A class is worth naming because the things in it are used;
# pushing rank downward instead inflates whichever private field happens to
# be touched twice.
_EDGE_WEIGHT = {
    EdgeKind.CALLS: 1.0,
    EdgeKind.IMPORTS: 0.9,
    EdgeKind.INHERITS: 0.8,
    EdgeKind.IMPLEMENTS: 0.8,
    EdgeKind.REFERENCES: 0.7,
    EdgeKind.USES_TYPE: 0.6,
    EdgeKind.TESTS: 0.3,
    EdgeKind.CONTAINS: 0.15,
    EdgeKind.CO_CHANGED: 0.2,
    EdgeKind.BUILD_DEPENDS: 0.1,
}


@dataclass(frozen=True, slots=True)
class RankOptions:
    """Knobs for one ranking run."""

    damping: float = 0.85
    """Probability the walk follows an edge rather than restarting.

    The conventional 0.85. Lower concentrates rank on the seeds, higher
    spreads it toward whatever is globally central.
    """

    tolerance: float = 1e-6
    max_iterations: int = 100

    focus_weight: float = 50.0
    """How much more restart mass a focused file gets than an ordinary one.

    Taken from aider's repo map, where a file already in the conversation is
    weighted fifty times a file that is not. It is the difference between a
    map of the repository and a map of what the agent is working on.
    """

    private_penalty: float = 0.25
    """Multiplier for a symbol the language marks private.

    A leading underscore, or a `private` or `protected` keyword, is a
    request not to be looked at from outside, and a map is a list of things
    worth looking at from outside. The keyword matters as much as the
    convention: TypeScript, PHP and Java say it in a word rather than in
    the name, so penalising only underscores would leave every private
    TypeScript field competing with the classes.
    """

    use_kind_prior: bool = True
    """Weight the restart by what kind of symbol it is.

    A stated bias, not a discovery. A map is read to find where behaviour
    lives, so a class or a function earns more restart mass than a field.
    Turn it off to rank purely on how the graph is shaped.
    """

    include_local: bool = False
    include_synthetic: bool = False


@dataclass(frozen=True, slots=True)
class RankedSymbol:
    """A symbol with its score and the evidence behind it."""

    symbol: Symbol
    score: float
    in_degree: int
    """How many distinct symbols refer to this one."""

    def __str__(self) -> str:
        return f"{self.score:.5f} {self.symbol.display}"


@dataclass(slots=True)
class SymbolGraph:
    """A weighted directed graph over symbols, ready to rank.

    Edges point from the symbol that refers to the symbol referred to, so
    rank flows toward definitions. That direction is the whole point: a
    function called from everywhere should end up important, not the file
    with the longest list of imports.
    """

    out_edges: dict[str, dict[str, float]] = field(default_factory=dict)
    in_degree: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    nodes: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls, snapshot: IndexSnapshot, options: RankOptions | None = None
    ) -> SymbolGraph:
        options = options or RankOptions()
        graph = cls()
        keep = {
            symbol.id
            for symbol in snapshot.symbols.values()
            if (options.include_synthetic or not symbol.synthetic)
            and (options.include_local or not symbol.local)
        }
        graph.nodes = sorted(keep)
        incoming: dict[str, set[str]] = defaultdict(set)
        for edge in snapshot.edges:
            weight = _edge_weight(edge)
            if weight <= 0:
                continue
            if edge.src_id not in keep or edge.dst_id not in keep:
                continue
            source, target = edge.src_id, edge.dst_id
            if edge.kind is EdgeKind.CONTAINS:
                source, target = target, source
            if source == target:
                continue
            targets = graph.out_edges.setdefault(source, {})
            # Several references from one symbol to another are more
            # evidence than one, but not proportionally more: a loop calling
            # a helper twenty times does not make it twenty times as
            # important. The square root is aider's damping of the same
            # effect.
            targets[target] = targets.get(target, 0.0) + weight
            incoming[target].add(source)
        for source, targets in graph.out_edges.items():
            graph.out_edges[source] = {
                target: total**0.5 for target, total in targets.items()
            }
        graph.in_degree = {node: len(sources) for node, sources in incoming.items()}
        return graph

    def __len__(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return sum(len(targets) for targets in self.out_edges.values())


def _edge_weight(edge: Edge) -> float:
    """How much rank one edge carries.

    Confidence is a multiplier rather than a filter. A fuzzy guess is still
    weak evidence of importance, and dropping it entirely would make the map
    of a language with no module resolver look emptier than it is.
    """
    return _EDGE_WEIGHT.get(edge.kind, 0.5) * edge.score


def _personalisation(
    graph: SymbolGraph,
    snapshot: IndexSnapshot,
    focus_paths: set[str],
    focus_symbols: set[str],
    options: RankOptions,
) -> dict[str, float]:
    """Where the random walk restarts, which is what steers the ranking."""
    weights: dict[str, float] = {}
    for node in graph.nodes:
        symbol = snapshot.symbols.get(node)
        if symbol is None:
            continue
        weight = 1.0
        if options.use_kind_prior:
            weight *= _KIND_PRIOR.get(symbol.kind, 0.3)
        if symbol.path in focus_paths or symbol.id in focus_symbols:
            weight *= options.focus_weight
        if _is_private(symbol):
            weight *= options.private_penalty
        weights[node] = weight
    total = sum(weights.values())
    if total <= 0:
        # Every node was penalised to nothing, which means the focus matched
        # nothing either. An even restart is the honest fallback.
        even = 1.0 / len(graph.nodes) if graph.nodes else 0.0
        return dict.fromkeys(graph.nodes, even)
    return {node: weight / total for node, weight in weights.items()}


def rank_symbols(
    snapshot: IndexSnapshot,
    *,
    focus_paths: set[str] | None = None,
    focus_symbols: set[str] | None = None,
    options: RankOptions | None = None,
    graph: SymbolGraph | None = None,
) -> list[RankedSymbol]:
    """Rank every symbol, highest first.

    ``focus_paths`` and ``focus_symbols`` steer the result: with neither,
    the ranking is global and answers "what is this repository built
    around"; with them, it answers "what does this work touch".
    """
    options = options or RankOptions()
    graph = graph or SymbolGraph.build(snapshot, options)
    if not graph.nodes:
        return []

    restart = _personalisation(
        graph, snapshot, focus_paths or set(), focus_symbols or set(), options
    )
    scores = dict(restart)
    damping = options.damping

    # Normalise once: transition probabilities do not change between
    # iterations, and dividing inside the loop would be the hot path.
    transitions: dict[str, list[tuple[str, float]]] = {}
    for source, targets in graph.out_edges.items():
        total = sum(targets.values())
        if total > 0:
            transitions[source] = [
                (target, weight / total) for target, weight in targets.items()
            ]

    for _ in range(options.max_iterations):
        updated = {node: (1.0 - damping) * restart.get(node, 0.0) for node in graph.nodes}
        # A node with no outgoing edge would otherwise leak its rank out of
        # the system. Its mass is redistributed the way a restart would.
        dangling = sum(
            score for node, score in scores.items() if node not in transitions
        )
        if dangling:
            share = damping * dangling
            for node in graph.nodes:
                updated[node] += share * restart.get(node, 0.0)
        for source, moves in transitions.items():
            source_score = scores.get(source, 0.0)
            if not source_score:
                continue
            contribution = damping * source_score
            for target, probability in moves:
                updated[target] += contribution * probability
        delta = sum(abs(updated[node] - scores.get(node, 0.0)) for node in graph.nodes)
        scores = updated
        if delta < options.tolerance:
            break

    ranked = [
        RankedSymbol(
            symbol=snapshot.symbols[node],
            score=scores.get(node, 0.0),
            in_degree=graph.in_degree.get(node, 0),
        )
        for node in graph.nodes
        if node in snapshot.symbols
    ]
    # Ties broken by position rather than by dictionary order, so two runs
    # over the same index produce the same map.
    ranked.sort(key=lambda item: (-item.score, item.symbol.path, item.symbol.name_range))
    return ranked
