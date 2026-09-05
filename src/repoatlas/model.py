"""Core data model shared by extractors, oracles and the evaluation harness.

Everything in RepoAtlas is expressed with these types. A tree-sitter
extractor and a compiler-backed oracle (SCIP, LSP) both emit ``Symbol`` and
``Edge`` values, which lets the evaluation harness compare them directly.

Positions follow the SCIP/LSP convention: zero-based lines, zero-based
character offsets, half-open ranges. The character unit is recorded on the
snapshot because SCIP indexers disagree about it (UTF-8 bytes vs UTF-16 code
units); see :mod:`repoatlas.eval.facts` for how that is normalised away
before comparison.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

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
]


class PositionEncoding(enum.Enum):
    """Unit used to count the ``character`` component of a position."""

    UTF8 = "utf-8"
    UTF16 = "utf-16"
    UTF32 = "utf-32"

    @classmethod
    def coerce(cls, value: object) -> PositionEncoding:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            key = value.strip().lower().replace("_", "-")
            for member in cls:
                if key in (member.value, member.name.lower()):
                    return member
        raise ValueError(f"unknown position encoding: {value!r}")


class SymbolKind(enum.Enum):
    """Kinds of definition RepoAtlas records.

    Deliberately small. Language-specific kinds map onto the closest entry
    rather than growing the enum, so cross-language reports stay comparable.
    """

    MODULE = "module"
    NAMESPACE = "namespace"
    CLASS = "class"
    INTERFACE = "interface"
    TRAIT = "trait"
    ENUM = "enum"
    TYPE_ALIAS = "type_alias"
    COMPONENT = "component"
    """A UI component: a Vue single-file component's options object or setup script."""
    FUNCTION = "function"
    METHOD = "method"
    CONSTRUCTOR = "constructor"
    PROPERTY = "property"
    FIELD = "field"
    CONSTANT = "constant"
    VARIABLE = "variable"
    PARAMETER = "parameter"
    MACRO = "macro"
    UNKNOWN = "unknown"

    @property
    def is_callable(self) -> bool:
        return self in _CALLABLE_KINDS

    @property
    def is_type_like(self) -> bool:
        return self in _TYPE_LIKE_KINDS


_CALLABLE_KINDS = frozenset(
    {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CONSTRUCTOR, SymbolKind.MACRO}
)
_TYPE_LIKE_KINDS = frozenset(
    {
        SymbolKind.CLASS,
        SymbolKind.COMPONENT,
        SymbolKind.INTERFACE,
        SymbolKind.TRAIT,
        SymbolKind.ENUM,
        SymbolKind.TYPE_ALIAS,
    }
)


class EdgeKind(enum.Enum):
    """Relations between symbols, ordered roughly by retrieval value."""

    CONTAINS = "contains"
    CALLS = "calls"
    IMPORTS = "imports"
    INHERITS = "inherits"
    IMPLEMENTS = "implements"
    REFERENCES = "references"
    USES_TYPE = "uses_type"
    TESTS = "tests"
    CO_CHANGED = "co_changed"
    BUILD_DEPENDS = "build_depends"

    @property
    def is_structural(self) -> bool:
        """True when the edge is derivable from one file in isolation."""
        return self is EdgeKind.CONTAINS

    @property
    def is_resolved(self) -> bool:
        """True when producing the edge requires cross-file name resolution."""
        return self in _RESOLVED_EDGE_KINDS


_RESOLVED_EDGE_KINDS = frozenset(
    {
        EdgeKind.CALLS,
        EdgeKind.IMPORTS,
        EdgeKind.INHERITS,
        EdgeKind.IMPLEMENTS,
        EdgeKind.REFERENCES,
        EdgeKind.USES_TYPE,
    }
)


class ResolutionTier(enum.Enum):
    """How an edge's target was determined, with its default confidence.

    The ladder follows the cascade described in the Codebase-Memory paper
    (arXiv:2603.27277). ``ORACLE`` is added for edges imported from a
    compiler-backed index, which the harness treats as ground truth.
    """

    ORACLE = ("oracle", 1.00)
    TYPE_ENGINE = ("type_engine", 0.98)
    """A type inference engine agreed, without a compiler having compiled it.

    PHPStan with larastan knows what an untyped receiver holds and what an
    Eloquent model's columns are, which is more than any SCIP indexer on
    this stack sees. It is not ground truth the way a compiler's own index
    is — an inference can be wrong where a compilation cannot — so it sits
    a rung below ORACLE rather than sharing it, and a report can tell the
    two apart.
    """

    IMPORT_MAP = ("import_map", 0.95)
    SAME_MODULE = ("same_module", 0.90)
    IMPORT_SUFFIX = ("import_suffix", 0.85)
    UNIQUE_NAME = ("unique_name", 0.75)
    SUFFIX = ("suffix", 0.55)
    FUZZY = ("fuzzy", 0.35)

    def __init__(self, label: str, default_confidence: float) -> None:
        self.label = label
        self.default_confidence = default_confidence

    @classmethod
    def from_label(cls, label: str) -> ResolutionTier:
        for member in cls:
            if member.label == label:
                return member
        raise ValueError(f"unknown resolution tier: {label!r}")


@dataclass(frozen=True, order=True, slots=True)
class Position:
    """A zero-based cursor position."""

    line: int
    character: int

    def __post_init__(self) -> None:
        if self.line < 0 or self.character < 0:
            raise ValueError(f"negative position: {self.line}:{self.character}")

    def __str__(self) -> str:
        return f"{self.line}:{self.character}"


@dataclass(frozen=True, order=True, slots=True)
class SourceRange:
    """A half-open ``[start, end)`` span inside one file."""

    start: Position
    end: Position

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"inverted range: {self.start} > {self.end}")

    @property
    def is_single_line(self) -> bool:
        return self.start.line == self.end.line

    @classmethod
    def of(cls, start_line: int, start_char: int, end_line: int, end_char: int) -> SourceRange:
        return cls(Position(start_line, start_char), Position(end_line, end_char))

    @classmethod
    def from_scip(cls, values: Iterable[int]) -> SourceRange:
        """Build a range from SCIP's compact three- or four-element form."""
        nums = list(values)
        if len(nums) == 3:
            line, start_char, end_char = nums
            return cls.of(line, start_char, line, end_char)
        if len(nums) == 4:
            return cls.of(*nums)
        raise ValueError(
            f"SCIP range must have 3 or 4 elements, got {len(nums)}: {nums!r}"
        )

    def to_scip(self) -> list[int]:
        if self.is_single_line:
            return [self.start.line, self.start.character, self.end.character]
        return [self.start.line, self.start.character, self.end.line, self.end.character]

    def contains(self, other: SourceRange) -> bool:
        return self.start <= other.start and other.end <= self.end

    def overlaps(self, other: SourceRange) -> bool:
        """True when the two spans share at least one character.

        Zero-width ranges are treated as touching the position they sit on,
        so a caret marker still matches the token it points at.
        """
        if self.start == self.end or other.start == other.end:
            return self.start <= other.end and other.start <= self.end
        return self.start < other.end and other.start < self.end

    def __str__(self) -> str:
        if self.is_single_line:
            return f"{self.start.line}:{self.start.character}-{self.end.character}"
        return f"{self.start}-{self.end}"


@dataclass(frozen=True, slots=True)
class Symbol:
    """A definition site.

    ``name_range`` covers only the identifier, which is what oracles anchor
    on. ``full_range`` covers the whole construct including its body and is
    what a map renderer needs. Comparison against an oracle uses
    ``name_range`` exclusively.
    """

    id: str
    name: str
    kind: SymbolKind
    path: str
    name_range: SourceRange
    full_range: SourceRange | None = None
    container_id: str | None = None
    qualified_name: str | None = None
    signature: str | None = None
    language: str | None = None
    documentation: str | None = None
    local: bool = False
    """True when nothing outside this symbol's own scope can name it.

    A variable declared inside a function body is a definition, but not one
    anybody navigates to, and an index that lists them buries the symbols
    that matter. The distinction is scope rather than kind: an exported
    module-level constant and a loop counter are both bindings.
    """

    synthetic: bool = False
    """True for symbols invented to stand in for a file or module.

    A top-level import has no enclosing function or class, so its edge needs
    a source anyway. Synthetic symbols provide one without appearing in
    definition counts, where they would be scored against an oracle that
    never claimed them.
    """

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("symbol id must be non-empty")
        if not self.path:
            raise ValueError(f"symbol {self.id!r} has no path")
        if self.full_range is not None and not self.full_range.contains(self.name_range):
            raise ValueError(
                f"symbol {self.id!r}: name_range {self.name_range} "
                f"escapes full_range {self.full_range}"
            )

    @property
    def display(self) -> str:
        return f"{self.path}:{self.name_range.start.line + 1}:{self.name}"


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed relation between two symbols.

    ``site`` records where the evidence for the edge was found, which is what
    makes an edge auditable: a call edge points at the call expression, not
    at the definition of the callee.
    """

    src_id: str
    dst_id: str
    kind: EdgeKind
    tier: ResolutionTier = ResolutionTier.ORACLE
    confidence: float | None = None
    site_path: str | None = None
    site_range: SourceRange | None = None

    def __post_init__(self) -> None:
        if self.confidence is None:
            object.__setattr__(self, "confidence", self.tier.default_confidence)
        conf = self.confidence
        assert conf is not None
        if not 0.0 <= conf <= 1.0:
            raise ValueError(f"confidence out of range: {conf}")
        if (self.site_range is None) != (self.site_path is None):
            raise ValueError("site_path and site_range must be given together")

    @property
    def score(self) -> float:
        assert self.confidence is not None
        return self.confidence


@dataclass(slots=True)
class IndexSnapshot:
    """A view of one index, ready for comparison."""

    symbols: dict[str, Symbol] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    encoding: PositionEncoding = PositionEncoding.UTF8
    encoding_declared: bool = True
    """False when the producer never said how it counts columns.

    scip-typescript 0.4.0 leaves ``Document.position_encoding`` unset while
    the TypeScript compiler reports UTF-16 offsets, so on a line with
    non-ASCII text ahead of an identifier its columns differ from
    tree-sitter's UTF-8 byte offsets. The comparison absorbs that with
    overlap matching; this flag lets a report say the encoding was
    assumed rather than known.
    """
    project_root: str | None = None
    producer: str | None = None

    def add_symbol(self, symbol: Symbol) -> Symbol:
        existing = self.symbols.get(symbol.id)
        if existing is not None and existing != symbol:
            raise ValueError(f"duplicate symbol id with differing content: {symbol.id!r}")
        self.symbols[symbol.id] = symbol
        return symbol

    def add_edge(self, edge: Edge) -> Edge:
        self.edges.append(edge)
        return edge

    def symbols_in(self, path: str) -> list[Symbol]:
        return sorted(
            (s for s in self.symbols.values() if s.path == path),
            key=lambda s: s.name_range,
        )

    def edges_of_kind(self, kind: EdgeKind) -> Iterator[Edge]:
        return (e for e in self.edges if e.kind is kind)

    @property
    def paths(self) -> set[str]:
        paths = {s.path for s in self.symbols.values()}
        paths.update(e.site_path for e in self.edges if e.site_path)
        return paths

    def __len__(self) -> int:
        return len(self.symbols)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {
            "symbols": len(self.symbols),
            "edges": len(self.edges),
            "files": len(self.paths),
        }
        for kind in EdgeKind:
            n = sum(1 for e in self.edges if e.kind is kind)
            if n:
                counts[f"edges.{kind.value}"] = n
        return counts


_DECORATOR_PREFIX = re.compile(r"^(?:(?:@[\w.$]+|#\[[^\]]*\])\s+)+")


def declaration_of(signature: str) -> str:
    """A signature without the decorators the extractor put in front of it.

    `@property def name(self)` and `#[Route] private function show()` carry
    their decorators as a prefix so a map can show what kind of thing a
    member is; anything that reads the declaration itself, a visibility
    keyword or a return type, wants the text after them.
    """
    return _DECORATOR_PREFIX.sub("", signature, count=1)
