"""Cross-file name resolution: references become edges, with a confidence.

Split from parsing on purpose. Extraction needs one file; resolution needs
the whole repository, so re-parsing a changed file never invalidates another
file's symbols, only the edges that cross between them.
"""

from __future__ import annotations

from .cascade import ResolutionStats, Resolver, SymbolIndex
from .modules import (
    ComposerResolver,
    ModuleResolver,
    NodeResolver,
    PythonResolver,
    resolver_for,
)

__all__ = [
    "ComposerResolver",
    "ModuleResolver",
    "NodeResolver",
    "PythonResolver",
    "ResolutionStats",
    "Resolver",
    "SymbolIndex",
    "resolver_for",
]
