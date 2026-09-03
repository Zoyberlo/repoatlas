"""RepoAtlas: a universal, oracle-verified code index for LLM agents.

The project is built in the order its evaluation demands. Correctness comes
first: an index that claims a call edge which does not exist is worse than no
index, because an agent will follow it. So the oracle comparison harness
exists before the extractor it will judge.

Layout:

``repoatlas.model``
    The vocabulary. Symbols, edges, confidence tiers, source ranges.

``repoatlas.oracle``
    Readers for compiler-backed indexes that serve as ground truth.

``repoatlas.eval``
    Projection into location facts, scoring, calibration, reporting.
"""

from __future__ import annotations

from .model import (
    Edge,
    EdgeKind,
    IndexSnapshot,
    Position,
    PositionEncoding,
    ResolutionTier,
    SourceRange,
    Symbol,
    SymbolKind,
)

__version__ = "0.1.0"

__all__ = [
    "Edge",
    "EdgeKind",
    "IndexSnapshot",
    "Position",
    "PositionEncoding",
    "ResolutionTier",
    "SourceRange",
    "Symbol",
    "SymbolKind",
    "__version__",
]
