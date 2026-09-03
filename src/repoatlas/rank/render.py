"""Turning a ranking into a map that fits a token budget.

The budget is the whole constraint. A map of a large repository will not
fit in a context window, so the question is never "what is in this
repository" but "which two thousand tokens of it are worth spending". Every
symbol included pushes another out.

The output is a file header followed by the declaration lines that
matter, each prefixed with its line number and indented by nesting. That
departs from aider's repo map, which marks omitted lines with ``⋮`` and
shown ones with a bar, for two reasons. This map only ever shows
declaration lines, so a marker on every entry carried no information and
the elisions outnumbered the content. And a line number is both cheaper
in tokens than an elision mark and more useful: every entry is already
the ``path:line`` an agent hands to a file reader.

Fitting the budget is a greedy selection by gain per token. Rank is the
gain, and the gain of the *k*-th symbol taken from one file is divided by
the square root of *k*, so a file whose thirty methods all rank well cannot
fill the budget with thirty lines of itself while a second file that would
have told the agent something new gets nothing. That is a submodular
objective, coverage with diminishing returns, and the lazy greedy that
maximises it carries the usual constant-factor guarantee; the literature on
budgeted context selection (PACMS, AdaGReS) reaches the same shape from
embeddings rather than from a graph. Cost is the rendered line, so a long
signature has to earn its length, and a symbol pays for the class and the
file header it brings with it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ..model import Symbol, SymbolKind
from .pagerank import RankedSymbol
from .tokens import TokenEstimator, estimate_tokens

__all__ = ["MapOptions", "RepoMap", "render_map"]

@dataclass(frozen=True, slots=True)
class MapOptions:
    """How to render, and how much of it to render."""

    budget: int = 2000
    """Target size in tokens.

    Two thousand is aider's default multiplied out for symbol-level rather
    than file-level entries. It is a starting point, not a finding.
    """

    spread: float = 0.5
    """How quickly a file's symbols stop earning their place.

    The *k*-th symbol chosen from one file is worth its rank divided by
    ``k ** spread``. At 0 there is no penalty and the map is the top of the
    ranking; at 1 the second symbol from a file is worth half its rank. The
    square root is the middle of that range and a judgement, not a
    measurement; the localisation benchmark is what would settle it.
    """

    max_files: int = 0
    """Cap on files listed; 0 means no cap."""

    show_kinds: bool = False
    """Prefix each entry with its symbol kind."""

    show_scores: bool = False
    """Append each entry's rank, for debugging a ranking rather than using it."""

    min_score: float = 0.0


@dataclass(slots=True)
class RepoMap:
    """A rendered map, with what it cost and what it left out."""

    text: str
    tokens: int
    included: int
    total: int
    files: int

    @property
    def coverage(self) -> float:
        return self.included / self.total if self.total else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "tokens": self.tokens,
            "included": self.included,
            "total": self.total,
            "files": self.files,
            "coverage": round(self.coverage, 4),
        }

    def __str__(self) -> str:
        return self.text


def _entry_line(item: RankedSymbol, options: MapOptions, depth: int) -> str:
    symbol = item.symbol
    body = symbol.signature or _synthetic_signature(symbol)
    prefix = f"{symbol.kind.value} " if options.show_kinds else ""
    suffix = f"  [{item.score:.5f}]" if options.show_scores else ""
    line = symbol.name_range.start.line + 1
    # Width 5 keeps columns aligned up to 99,999 lines, which covers any
    # file a person wrote by hand.
    indent = "  " * depth
    return f"{line:>5}  {indent}{prefix}{body}{suffix}"


def _synthetic_signature(symbol: Symbol) -> str:
    """A stand-in for a symbol extracted without its declaration line.

    Only reachable for an index built before signatures were captured, or
    for a language whose query captures a node the source does not spell on
    one line. Better than an empty entry, which would read as a bug.
    """
    if symbol.kind in (SymbolKind.CLASS, SymbolKind.INTERFACE, SymbolKind.TRAIT):
        return f"{symbol.kind.value} {symbol.name}"
    if symbol.kind.is_callable:
        return f"{symbol.name}(...)"
    return symbol.name


def _with_ancestors(
    selected: Sequence[RankedSymbol], by_id: Mapping[str, RankedSymbol]
) -> list[RankedSymbol]:
    """Add the containers of everything chosen.

    A method shown without its class is a line of code with no address. The
    class costs one line and turns a list of names into a structure, so it
    is pulled in even when its own rank did not earn a place.

    ``by_id`` is every candidate, built once by the caller: the budget
    search calls this a dozen times, and rebuilding a hundred-thousand-entry
    table on each was most of what a warm map cost.
    """
    chosen: dict[str, RankedSymbol] = {item.symbol.id: item for item in selected}
    queue = list(selected)
    while queue:
        item = queue.pop()
        container = item.symbol.container_id
        if not container or container in chosen:
            continue
        parent = by_id.get(container)
        if parent is None:
            continue
        chosen[container] = parent
        queue.append(parent)
    return list(chosen.values())


def _render(selected: Sequence[RankedSymbol], options: MapOptions) -> tuple[str, int]:
    """Render a chosen set of symbols, grouped by file and in source order."""
    by_file: dict[str, list[RankedSymbol]] = {}
    for item in selected:
        by_file.setdefault(item.symbol.path, []).append(item)

    # Files ordered by their best symbol, so the most relevant file leads.
    ordered_files = sorted(
        by_file,
        key=lambda path: (-max(item.score for item in by_file[path]), path),
    )
    if options.max_files:
        ordered_files = ordered_files[: options.max_files]

    lines: list[str] = []
    for path in ordered_files:
        entries = sorted(by_file[path], key=lambda item: item.symbol.name_range)
        shown = {item.symbol.id: item.symbol for item in entries}
        lines.append(f"{path}:")
        for item in entries:
            depth = _depth(item.symbol, shown)
            lines.append(_entry_line(item, options, depth))
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n" if lines else ""
    return text, len(ordered_files)


def _top_files(items: Sequence[RankedSymbol], count: int) -> list[RankedSymbol]:
    """Keep only the symbols of the ``count`` files whose best symbol ranks highest.

    ``items`` arrive highest first, so the first symbol seen for a file is
    its best, and the file order this produces is the one the renderer
    would have chosen anyway.
    """
    best: dict[str, float] = {}
    for item in items:
        best.setdefault(item.symbol.path, item.score)
    keep = set(sorted(best, key=lambda path: (-best[path], path))[:count])
    return [item for item in items if item.symbol.path in keep]


def _depth(symbol: Symbol, shown: Mapping[str, Symbol]) -> int:
    """How far to indent, counting containers that are themselves shown.

    Indenting by the true nesting depth would leave a method dangling under
    a class the budget excluded, which reads as a rendering fault rather
    than as an omission.
    """
    depth = 0
    container = symbol.container_id
    seen: set[str] = set()
    while container and container in shown and container not in seen:
        seen.add(container)
        depth += 1
        container = shown[container].container_id
    return depth


def render_map(
    ranked: Iterable[RankedSymbol],
    options: MapOptions | None = None,
    *,
    estimator: TokenEstimator | None = None,
) -> RepoMap:
    """Render the symbols that best spend the budget.

    Selection is a lazy greedy over gain per token (see the module note);
    rendering then groups the selection by file and orders it by position,
    so the map reads as structure rather than as a ranked list.
    """
    options = options or MapOptions()
    estimate = estimator or estimate_tokens
    items = [item for item in ranked if item.score >= options.min_score]
    total = len(items)
    if not items:
        return RepoMap(text="", tokens=0, included=0, total=0, files=0)
    if options.max_files:
        # Cap the files first, then fit the budget within them. Capping
        # after the fit threw away symbols the search had already paid for
        # and left the map well under budget with the files it kept.
        items = _top_files(items, options.max_files)

    by_id = {item.symbol.id: item for item in items}
    chosen = _select(items, by_id, options, estimate)
    text, files = _render(_with_ancestors(chosen, by_id), options)
    tokens = estimate(text)
    # Per-line costs round up against the joined text, so the sum is a
    # ceiling; still, a chosen set is only kept if the rendered whole fits.
    while tokens > options.budget and chosen:
        chosen.pop()
        text, files = _render(_with_ancestors(chosen, by_id), options)
        tokens = estimate(text)
    return RepoMap(text=text, tokens=tokens, included=len(chosen), total=total, files=files)


def _select(
    items: Sequence[RankedSymbol],
    by_id: Mapping[str, RankedSymbol],
    options: MapOptions,
    estimate: TokenEstimator,
) -> list[RankedSymbol]:
    """Choose what to show: lazy greedy by gain per token.

    Every candidate starts in a heap keyed by its best possible ratio, rank
    over its own line. Taking a candidate lowers the gain of every later
    symbol from the same file and can only raise costs, never lower them,
    so a stale key is an upper bound: when the top of the heap, recomputed,
    still beats the next key, it is the true best and is taken. Most
    candidates are never recomputed at all, which is what makes this a
    single pass rather than a dozen renders.

    The pool is the top of the ranking, a few times the budget deep. A
    symbol ranked below that would need every file above it to be
    exhausted before its ratio came into play, and the budget is spent long
    before then.
    """
    import heapq
    import math

    pool = items[: max(1, options.budget * 4)]
    line_cost = {
        item.symbol.id: estimate(_entry_line(item, options, 0) + "\n") for item in pool
    }
    # Ancestors outside the pool still cost their line when pulled in.
    for item in pool:
        container = item.symbol.container_id
        while container and container not in line_cost:
            parent = by_id.get(container)
            if parent is None:
                break
            line_cost[container] = estimate(_entry_line(parent, options, 0) + "\n")
            container = parent.symbol.container_id

    taken: dict[str, RankedSymbol] = {}
    per_file: dict[str, int] = {}
    open_files: set[str] = set()
    spent = 0
    budget = options.budget

    def cost_of(item: RankedSymbol) -> int:
        """The line, the file header if new, and any ancestor not yet shown."""
        total = line_cost[item.symbol.id]
        if item.symbol.path not in open_files:
            total += estimate(f"{item.symbol.path}:\n") + 1
        container = item.symbol.container_id
        while container and container not in taken:
            parent = by_id.get(container)
            if parent is None:
                break
            total += line_cost[container]
            container = parent.symbol.container_id
        return total

    def gain_of(item: RankedSymbol) -> float:
        k = per_file.get(item.symbol.path, 0)
        return float(item.score / math.pow(k + 1, options.spread))

    # (negated ratio, position in ranking) so ties fall to the higher rank
    # and the order is reproducible.
    heap = [
        (-(item.score / line_cost[item.symbol.id]), position, item)
        for position, item in enumerate(pool)
        if line_cost[item.symbol.id] > 0
    ]
    heapq.heapify(heap)
    order: list[RankedSymbol] = []

    while heap and spent < budget:
        stale, position, item = heapq.heappop(heap)
        if item.symbol.id in taken:
            continue
        cost = cost_of(item)
        ratio = gain_of(item) / cost if cost else math.inf
        if heap and -ratio > heap[0][0] + 1e-12 and -ratio != stale:
            # Worse than the next candidate's upper bound: re-key and retry.
            heapq.heappush(heap, (-ratio, position, item))
            continue
        if spent + cost > budget:
            # Too expensive at this point; a cheaper candidate may still
            # fit, so keep going without it.
            continue
        # Take it, and everything it brings.
        container = item.symbol.container_id
        while container and container not in taken:
            parent = by_id.get(container)
            if parent is None:
                break
            taken[container] = parent
            order.append(parent)
            container = parent.symbol.container_id
        taken[item.symbol.id] = item
        order.append(item)
        spent += cost
        open_files.add(item.symbol.path)
        per_file[item.symbol.path] = per_file.get(item.symbol.path, 0) + 1
    return order
