"""Fast innermost-enclosing-range lookup.

Both the extractor and the SCIP reader need to answer "which definition's
body encloses this position" for every reference in a file. A linear scan
per query is quadratic in a file's symbol count, and files with thousands
of definitions exist in every real repository: generated code, large PHP
modules, one-file TypeScript bundles. An 8 000-symbol file took 15 seconds
that way against 36 milliseconds to parse.

:class:`ScopeIndex` answers the same question in logarithmic time plus a
short walk. Ranges here are tree-sitter nodes or SCIP enclosing ranges,
which nest or are disjoint but never partially overlap, and that property
is what makes the walk short.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from typing import Generic, TypeVar

from .model import SourceRange

__all__ = ["ScopeIndex"]

T = TypeVar("T")


class ScopeIndex(Generic[T]):
    """Ranges sorted by start, with a prefix maximum of their ends.

    To find the innermost range enclosing a span, bisect to the last range
    starting at or before the span and walk backwards. Ranges are ordered so
    that among equal starts the wider one comes first, which means the walk
    meets an inner range before its parent. The walk stops as soon as the
    prefix maximum of ends falls short of the span's end, since nothing
    earlier can reach it.
    """

    __slots__ = ("_ends_max", "_items", "_starts")

    def __init__(self, items: Iterable[tuple[SourceRange, T]]) -> None:
        ordered = sorted(items, key=lambda pair: (pair[0].start, _negate(pair[0].end)))
        self._items = ordered
        self._starts = [span.start for span, _ in ordered]
        running: list[tuple[int, int]] = []
        best = (-1, -1)
        for span, _ in ordered:
            end = (span.end.line, span.end.character)
            if end > best:
                best = end
            running.append(best)
        self._ends_max = running

    def __len__(self) -> int:
        return len(self._items)

    def innermost(self, span: SourceRange, *, exclude: SourceRange | None = None) -> T | None:
        """The value of the tightest range enclosing ``span``, or ``None``.

        ``exclude`` skips a range equal to it. Two sibling declarations that
        share one syntax node, such as PHP's ``public $a, $b;``, carry the
        same body range, and without this the second would be filed inside
        the first.
        """
        index = bisect.bisect_right(self._starts, span.start) - 1
        needed = (span.end.line, span.end.character)
        while index >= 0:
            if self._ends_max[index] < needed:
                return None
            candidate, value = self._items[index]
            if candidate.contains(span) and candidate != exclude:
                return value
            index -= 1
        return None


def _negate(position: object) -> tuple[int, int]:
    """Sort key placing the wider of two equal-start ranges first."""
    line, character = position.line, position.character  # type: ignore[attr-defined]
    return (-line, -character)
