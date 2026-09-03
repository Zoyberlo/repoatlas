"""Turn one source file into symbols and unresolved references.

This is the half of indexing that needs no knowledge beyond the file in
front of it. Cross-file resolution, which turns a reference into an edge, is
a separate stage: it needs the whole repository and it is where confidence
tiers come from.

Keeping the two apart is what makes re-indexing cheap. Re-parsing one file
never invalidates another file's extraction, only the resolution that
crosses between them.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..model import SourceRange, Symbol, SymbolKind
from ..spans import ScopeIndex
from .imports import FileImports, extract_imports
from .languages import LanguageSpec, get_language, get_parser, query_source

if TYPE_CHECKING:  # pragma: no cover - imported only for type checking
    from tree_sitter import Node, Query, Tree

__all__ = [
    "FileExtraction",
    "Reference",
    "ReferenceKind",
    "extract_file",
    "extract_source",
]

# Capture prefixes, in the tree-sitter tags convention.
_DEFINITION_PREFIX = "definition."
_REFERENCE_PREFIX = "reference."

# How much a reference kind tells the resolver, most first. A call names a
# callable; a bare member read could be anything.
_REFERENCE_PRECEDENCE = {
    "class": 6,
    "import": 5,
    "construct": 4,
    "call": 3,
    "type": 2,
    "member": 1,
    "value": 0,
}

_KIND_BY_CAPTURE = {
    "class": SymbolKind.CLASS,
    "interface": SymbolKind.INTERFACE,
    "trait": SymbolKind.TRAIT,
    "enum": SymbolKind.ENUM,
    "type": SymbolKind.TYPE_ALIAS,
    "function": SymbolKind.FUNCTION,
    "method": SymbolKind.METHOD,
    "constructor": SymbolKind.CONSTRUCTOR,
    "field": SymbolKind.FIELD,
    "property": SymbolKind.PROPERTY,
    "constant": SymbolKind.CONSTANT,
    "variable": SymbolKind.VARIABLE,
    "module": SymbolKind.MODULE,
    "namespace": SymbolKind.NAMESPACE,
    "macro": SymbolKind.MACRO,
}

# When two patterns claim the same identifier, the higher number wins. An
# arrow function bound to a name is a function, not a constant, and query
# results arrive in tree order so pattern order cannot settle it.
_KIND_PRECEDENCE = {
    SymbolKind.UNKNOWN: 0,
    SymbolKind.VARIABLE: 1,
    SymbolKind.CONSTANT: 2,
    SymbolKind.FIELD: 3,
    SymbolKind.PROPERTY: 4,
    SymbolKind.MODULE: 5,
    SymbolKind.NAMESPACE: 5,
    SymbolKind.TYPE_ALIAS: 6,
    SymbolKind.ENUM: 7,
    SymbolKind.FUNCTION: 8,
    SymbolKind.METHOD: 9,
    SymbolKind.CONSTRUCTOR: 9,
    SymbolKind.TRAIT: 10,
    SymbolKind.INTERFACE: 10,
    SymbolKind.CLASS: 10,
    SymbolKind.MACRO: 10,
}

_TYPE_CONTAINERS = frozenset(
    {
        SymbolKind.CLASS,
        SymbolKind.INTERFACE,
        SymbolKind.TRAIT,
        SymbolKind.ENUM,
    }
)

# Names a language gives its constructor. Recognising them keeps the kind
# vocabulary comparable across languages, where the grammars do not: Python
# and PHP both spell a constructor as an ordinary function definition.
_CONSTRUCTOR_NAMES = frozenset({"__init__", "__construct", "constructor", "new"})

# A declaration line longer than this is wrapping or generated. Truncating
# keeps one map entry to roughly one line of a terminal.
_MAX_SIGNATURE = 120

# A definition inside one of these is scope-local: a variable in a function
# body, a nested helper closure. Real definitions, but not ones anybody
# navigates to, and listing them buries the symbols that matter.
_LOCAL_SCOPES = frozenset(
    {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CONSTRUCTOR}
)


def _refine_kind(kind: SymbolKind, name: str, container: Symbol | None) -> SymbolKind:
    """Adjust a kind using context the query could not see.

    Python and PHP write methods with the same node type as free functions,
    so only the enclosing symbol distinguishes them. Left uncorrected, every
    per-kind score would be incomparable between languages.
    """
    if kind not in (SymbolKind.FUNCTION, SymbolKind.METHOD):
        return kind
    if container is None or container.kind not in _TYPE_CONTAINERS:
        return kind
    if name in _CONSTRUCTOR_NAMES:
        return SymbolKind.CONSTRUCTOR
    return SymbolKind.METHOD


ReferenceKind = str
"""One of ``call``, ``class``, ``type``, ``import`` or another query suffix.

Left as a plain string rather than an enum because a language may need a
reference kind the shared vocabulary has no word for, and the resolver
treats unknown kinds as ordinary references rather than failing.
"""


@dataclass(frozen=True, slots=True)
class Reference:
    """A name used somewhere, before anyone knows what it refers to.

    ``container_id`` is the symbol whose body encloses the use site, which
    becomes the source of the edge once the target is resolved. It is
    ``None`` for a top-level reference such as an import, and the resolver
    substitutes the file's module symbol there.
    """

    name: str
    kind: ReferenceKind
    span: SourceRange
    container_id: str | None = None

    def __str__(self) -> str:
        return f"{self.name} [{self.kind}] at {self.span}"


@dataclass(slots=True)
class FileExtraction:
    """Everything one file yields on its own."""

    path: str
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    imports: FileImports = field(default_factory=FileImports)
    """What this file borrows, and from where.

    Collected during extraction because it comes from the same parse,
    and consumed during resolution, which needs the whole repository.
    """

    has_errors: bool = False
    """Whether tree-sitter had to recover from a syntax error.

    Tracked per file because the error rate is a headline quality measure
    per language: published rates run from 0.2% of files in Go to 53% in C.
    A language whose rate is high here needs its grammar questioned before
    its extraction numbers are believed.
    """

    error_count: int = 0

    @property
    def definitions_by_id(self) -> dict[str, Symbol]:
        return {symbol.id: symbol for symbol in self.symbols}


def _to_range(node: Node) -> SourceRange:
    """Convert a tree-sitter node span to a model range.

    tree-sitter reports a point's column in bytes, which is what SCIP calls
    UTF-8 offsets, so this needs no conversion for the common case. Files
    whose oracle counts UTF-16 units are handled by the comparison layer's
    column tolerance instead of by guessing here.
    """
    start_row, start_col = node.start_point
    end_row, end_col = node.end_point
    return SourceRange.of(start_row, start_col, end_row, end_col)


def _count_errors(tree: Tree) -> int:
    """Count error and missing nodes, visiting only subtrees that hold one.

    ``has_error`` is a cheap flag tree-sitter keeps on every node, so a
    clean subtree is skipped whole rather than walked.
    """
    if not tree.root_node.has_error:
        return 0
    total = 0
    cursor = tree.walk()
    visited_children = False
    while True:
        if not visited_children:
            node = cursor.node
            if node is not None and (node.is_error or node.is_missing):
                total += 1
                # Everything below an error node is unreliable, so do not
                # descend into it and inflate the count.
                if not cursor.goto_next_sibling():
                    visited_children = True
                    continue
                continue
            if node is not None and not node.has_error:
                visited_children = True
                continue
            if not cursor.goto_first_child():
                visited_children = True
        elif cursor.goto_next_sibling():
            visited_children = False
        elif not cursor.goto_parent():
            break
    return total


def _qualified_name(name: str, container: Symbol | None) -> str:
    if container is None or container.kind is SymbolKind.MODULE:
        return name
    parent = container.qualified_name or container.name
    return f"{parent}.{name}"


@dataclass(slots=True)
class _RawDefinition:
    name: str
    kind: SymbolKind
    name_span: SourceRange
    full_span: SourceRange
    signature: str = ""


@functools.cache
def _compiled_query(language: str) -> Query:
    """Compile a language's tag query once.

    Compiling is the expensive part of extraction by a wide margin: on an
    83-file TypeScript project it took 1.38 s against 0.05 s for parsing
    when it was redone per file. A Query is immutable once built, so
    sharing it is safe; the cursor that walks a tree is made per file.
    """
    from tree_sitter import Query

    return Query(get_language(language), query_source(language))


def _declaration_line(lines: list[str], span: SourceRange) -> str:
    """The source line a definition is declared on, trimmed.

    Stored so a map can be rendered from the index alone, without opening
    the file. One line rather than the whole signature: a multi-line
    parameter list adds bulk without telling a reader anything the name and
    the first line do not, and the map is spent in tokens.
    """
    index = span.start.line
    if index >= len(lines):
        return ""
    text = lines[index].strip()
    return text if len(text) <= _MAX_SIGNATURE else text[: _MAX_SIGNATURE - 1] + "…"


def _collect(
    tree: Tree, spec: LanguageSpec, lines: list[str]
) -> tuple[list[_RawDefinition], list[tuple[str, str, SourceRange]]]:
    """Run the tag query and split its captures into definitions and uses."""
    from tree_sitter import QueryCursor

    cursor = QueryCursor(_compiled_query(spec.name))

    definitions: dict[tuple[int, int, int, int], _RawDefinition] = {}
    # One use site is one reference however many patterns matched it, so
    # references are keyed by span and the strongest kind wins.
    reference_kinds: dict[SourceRange, str] = {}
    reference_names: dict[SourceRange, str] = {}

    for _pattern_index, captures in cursor.matches(tree.root_node):
        name_nodes = captures.get("name")
        if not name_nodes:
            continue
        name_node = name_nodes[0]
        name_span = _to_range(name_node)
        name_text = name_node.text
        if name_text is None:
            continue
        name = name_text.decode("utf-8", errors="replace")
        # PHP property names arrive with their sigil; the name people search
        # for does not include it.
        name = name.lstrip("$")
        if not name:
            continue

        for capture_name, nodes in captures.items():
            if capture_name.startswith(_DEFINITION_PREFIX):
                kind = _KIND_BY_CAPTURE.get(
                    capture_name[len(_DEFINITION_PREFIX) :], SymbolKind.UNKNOWN
                )
                full_span = _to_range(nodes[0])
                if not full_span.contains(name_span):
                    # A query that captures a node not containing its own
                    # identifier is a bug in the query, not in the source.
                    full_span = name_span
                key = (
                    name_span.start.line,
                    name_span.start.character,
                    name_span.end.line,
                    name_span.end.character,
                )
                existing = definitions.get(key)
                if existing is None or _KIND_PRECEDENCE[kind] > _KIND_PRECEDENCE[
                    existing.kind
                ]:
                    definitions[key] = _RawDefinition(
                        name,
                        kind,
                        name_span,
                        full_span,
                        _declaration_line(lines, full_span),
                    )
            elif capture_name.startswith(_REFERENCE_PREFIX):
                # Several patterns may capture one token. `user.greet()`
                # matches both the call pattern and the member-read pattern,
                # and it is one use site either way; the more specific kind
                # wins, because a call tells the resolver more than a read.
                reference_kind = capture_name[len(_REFERENCE_PREFIX) :]
                previous = reference_kinds.get(name_span)
                if previous is None or _REFERENCE_PRECEDENCE.get(
                    reference_kind, 0
                ) > _REFERENCE_PRECEDENCE.get(previous, 0):
                    reference_kinds[name_span] = reference_kind
                    reference_names[name_span] = name

    ordered = sorted(
        definitions.values(), key=lambda d: (d.name_span.start, d.name_span.end)
    )
    references = [
        (reference_names[span], kind, span) for span, kind in reference_kinds.items()
    ]
    return ordered, references


def _assign_ids(path: str, raw: list[_RawDefinition]) -> list[Symbol]:
    """Build symbols with containment and stable ids.

    An id is ``<path>#<qualified name>``. Two definitions can legitimately
    share one, through a conditional definition or an overload, so a repeat
    gets a ``$2`` suffix counted in source order. That keeps ids stable
    under edits elsewhere in the file, which a line number would not.
    """
    symbols: list[Symbol] = []
    used: dict[str, int] = {}
    # Definitions arrive in identifier order, which for every supported
    # grammar is a pre-order walk of the containment tree: a parent's name
    # precedes its members. A stack of open bodies therefore finds each
    # definition's container in amortised constant time.
    open_bodies: list[tuple[SourceRange, int]] = []

    for definition in raw:
        while open_bodies and (
            not open_bodies[-1][0].contains(definition.name_span)
            # Siblings that share one node, as in `public $a, $b;`, have
            # identical bodies and must not nest inside each other.
            or open_bodies[-1][0] == definition.full_span
        ):
            open_bodies.pop()
        container_index = open_bodies[-1][1] if open_bodies else None
        container = symbols[container_index] if container_index is not None else None
        qualified = _qualified_name(definition.name, container)
        base_id = f"{path}#{qualified}"
        seen = used.get(base_id, 0) + 1
        used[base_id] = seen
        symbol_id = base_id if seen == 1 else f"{base_id}${seen}"

        # Locality is inherited: a closure inside a function is local, and so
        # is everything inside that closure.
        is_local = container is not None and (
            container.local or container.kind in _LOCAL_SCOPES
        )
        symbol = Symbol(
            id=symbol_id,
            name=definition.name,
            kind=_refine_kind(definition.kind, definition.name, container),
            path=path,
            name_range=definition.name_span,
            full_range=definition.full_span,
            container_id=container.id if container is not None else None,
            qualified_name=qualified,
            signature=definition.signature or None,
            local=is_local,
        )
        symbols.append(symbol)
        open_bodies.append((definition.full_span, len(symbols) - 1))
    return symbols


def extract_source(
    path: str, source: bytes, spec: LanguageSpec
) -> FileExtraction:
    """Extract from source already in memory."""
    parser = get_parser(spec.name)
    tree = parser.parse(source)
    lines = source.decode("utf-8", errors="replace").splitlines()
    raw_definitions, raw_references = _collect(tree, spec, lines)
    symbols = _assign_ids(path, raw_definitions)

    scopes: ScopeIndex[int] = ScopeIndex(
        (symbol.full_range or symbol.name_range, index)
        for index, symbol in enumerate(symbols)
    )
    definition_spans = {symbol.name_range for symbol in symbols}

    references: list[Reference] = []
    for name, kind, span in raw_references:
        if span in definition_spans:
            # The identifier in `class A(B)` is a definition of A and a
            # reference to B; a name that is its own definition site is not
            # a reference to anything.
            continue
        container_index = scopes.innermost(span)
        references.append(
            Reference(
                name=name,
                kind=kind,
                span=span,
                container_id=(
                    symbols[container_index].id if container_index is not None else None
                ),
            )
        )
    references.sort(key=lambda reference: (reference.span.start, reference.name))

    error_count = _count_errors(tree)
    return FileExtraction(
        path=path,
        language=spec.name,
        symbols=symbols,
        references=references,
        imports=extract_imports(tree, spec),
        has_errors=error_count > 0,
        error_count=error_count,
    )


def extract_file(path: str, source_path: str | None = None) -> FileExtraction | None:
    """Extract from a file on disk, or ``None`` for an unhandled language.

    ``path`` is the repository-relative path recorded in the output;
    ``source_path`` is where to actually read from, when they differ.
    """
    from .languages import language_for_path

    spec = language_for_path(path)
    if spec is None:
        return None
    with open(source_path or path, "rb") as handle:
        source = handle.read()
    return extract_source(path, source, spec)
