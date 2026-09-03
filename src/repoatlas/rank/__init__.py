"""Deciding what belongs in a map, and rendering it under a budget.

An index of a large repository cannot be handed to an agent whole, so the
question is always which fraction of it to spend context on. Ranking
answers that with personalised PageRank over the symbol graph: importance
is recursive, and seeding the walk at the files being worked on turns a map
of the repository into a map of the task.
"""

from __future__ import annotations

from .pagerank import RankedSymbol, RankOptions, SymbolGraph, rank_symbols
from .render import MapOptions, RepoMap, render_map
from .tokens import (
    CHARS_PER_TOKEN,
    TokenEstimator,
    calibrate_constant,
    estimate_tokens,
    estimate_with,
    make_estimator,
)

__all__ = [
    "CHARS_PER_TOKEN",
    "MapOptions",
    "RankOptions",
    "RankedSymbol",
    "RepoMap",
    "SymbolGraph",
    "TokenEstimator",
    "calibrate_constant",
    "estimate_tokens",
    "estimate_with",
    "make_estimator",
    "rank_symbols",
    "render_map",
]
