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

Fitting the budget is a binary search over how many ranked symbols to keep.
Rendering is cheap and estimating tokens is cheaper, so a dozen renders to
land within a few percent of the target costs nothing and beats guessing a
symbol count that would be wrong on every repository but one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
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

    tolerance: float = 0.15
    """How far under the budget a result may land before it is good enough.

    Binary search cannot usually hit a budget exactly, because one symbol
    is an indivisible step. Accepting anything within fifteen percent stops
    the search wasting iterations on a gap it cannot close.
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
    selected: Sequence[RankedSymbol], everything: Sequence[RankedSymbol]
) -> list[RankedSymbol]:
    """Add the containers of everything chosen.

    A method shown without its class is a line of code with no address. The
    class costs one line and turns a list of names into a structure, so it
    is pulled in even when its own rank did not earn a place.
    """
    by_id = {item.symbol.id: item for item in everything}
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
        lines.append(f"{path}:")
        for item in entries:
            depth = _depth(item.symbol, by_file[path])
            lines.append(_entry_line(item, options, depth))
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n" if lines else ""
    return text, len(ordered_files)


def _depth(symbol: Symbol, siblings: Sequence[RankedSymbol]) -> int:
    """How far to indent, counting containers that are themselves shown.

    Indenting by the true nesting depth would leave a method dangling under
    a class the budget excluded, which reads as a rendering fault rather
    than as an omission.
    """
    shown = {item.symbol.id for item in siblings}
    depth = 0
    container = symbol.container_id
    seen: set[str] = set()
    by_id = {item.symbol.id: item.symbol for item in siblings}
    while container and container in shown and container not in seen:
        seen.add(container)
        depth += 1
        parent = by_id.get(container)
        container = parent.container_id if parent else None
    return depth


def render_map(
    ranked: Iterable[RankedSymbol],
    options: MapOptions | None = None,
    *,
    estimator: TokenEstimator | None = None,
) -> RepoMap:
    """Render the highest-ranked symbols that fit the budget.

    Binary search over how many to keep. The alternative, adding symbols
    until the budget is spent, produces a map that stops mid-file wherever
    the count happened to run out; searching for the count first lets the
    renderer group and order the whole selection.
    """
    options = options or MapOptions()
    estimate = estimator or estimate_tokens
    items = [item for item in ranked if item.score >= options.min_score]
    total = len(items)
    if not items:
        return RepoMap(text="", tokens=0, included=0, total=0, files=0)

    def attempt(count: int) -> tuple[str, int, int]:
        text, files = _render(_with_ancestors(items[:count], items), options)
        return text, estimate(text), files

    whole_text, whole_tokens, whole_files = attempt(total)
    if whole_tokens <= options.budget:
        return RepoMap(
            text=whole_text,
            tokens=whole_tokens,
            included=total,
            total=total,
            files=whole_files,
        )

    floor = int(options.budget * (1 - options.tolerance))
    low, high = 0, total
    best: tuple[str, int, int, int] = ("", 0, 0, 0)
    while low <= high:
        middle = (low + high) // 2
        if middle == 0:
            low = 1
            continue
        text, tokens, files = attempt(middle)
        if tokens <= options.budget:
            best = (text, tokens, middle, files)
            if tokens >= floor:
                break
            low = middle + 1
        else:
            high = middle - 1

    text, tokens, included, files = best
    return RepoMap(text=text, tokens=tokens, included=included, total=total, files=files)
