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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..model import SourceRange, Symbol, SymbolKind
from ..spans import ScopeIndex
from .embedded import embedded_regions, parse_embedded
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
    "to_range",
]

# Capture prefixes, in the tree-sitter tags convention.
_DEFINITION_PREFIX = "definition."
_REFERENCE_PREFIX = "reference."

# How much a reference kind tells the resolver, most first. A call names a
# callable; a bare member read could be anything.
_REFERENCE_PRECEDENCE = {
    # A string a framework reads as a filename is that, whatever else
    # a generic pattern would make of the same span.
    "view": 7,
    "extends": 7,
    "include": 7,
    "component": 7,
    "route": 7,
    "class": 6,
    "scope": 6,
    "import": 5,
    "construct": 4,
    "call": 3,
    "type": 2,
    "member": 1,
    "value": 0,
}

# Reference kinds a local can shadow. A member or a type is reached through
# something else and is never a bare use of a local's name.
_SHADOWED_KINDS = frozenset({"value", "call", "construct"})

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
    # A field defined by assignment inside a method, `self.x = ...`, or by
    # a promoted constructor parameter. Same kind as a field; what differs
    # is where it belongs, and `_assign_ids` hoists it to the class.
    "attribute": SymbolKind.FIELD,
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


def _unquote(text: str) -> str:
    """Strip one matching pair of quotes from a captured string literal.

    Template languages name their targets with strings, so the capture is
    `'layouts.app'` where the name is `layouts.app`. Only a matched pair is
    removed, so an identifier that merely starts with a quote is untouched.
    """
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _is_component_name(name: str) -> bool:
    """Whether a tag names a component rather than a native element.

    Vue's own rule, and the one every template language that mixes the two
    settles on: a custom component is written in PascalCase or contains a
    hyphen, because HTML reserves the bare lowercase names. Without this,
    every `div` and `span` in a template becomes a reference that can never
    resolve, burying the ones that can.
    """
    return "-" in name or (name[:1].isupper() if name else False)


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
    receiver: str | None = None
    """What a member was read through: `greeter` in `greeter.greet`.

    A plain name, `this.name` for a property of the enclosing class, or
    ``EXPRESSION_RECEIVER`` when the member hangs off an expression such
    as `make()->run()`, which no name describes.
    """

    receiver_type: str | None = None
    """What the extractor could tell about the receiver's type, by name.

    Filled from an annotation or a `new` on the same local within the
    enclosing function. It is a name, not a symbol: the resolver still has
    to find which `Greeter` is meant.
    """

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


def to_range(node: Node) -> SourceRange:
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


EXPRESSION_RECEIVER = "(expr)"
"""The receiver of a member read off an expression rather than a name."""

CALL_TYPE_PREFIX = "<call>"
"""Marks a receiver type that is whatever a call returns: `$g = $this->build()`.

The rest of the string is ``callee|receiver|receiver type``, the last two
empty when the call is bare. The resolver resolves the call and reads the
callee's declared return type.
"""


@dataclass(slots=True)
class _RawReference:
    name: str
    kind: str
    span: SourceRange
    receiver: str | None = None


@dataclass(slots=True)
class _Collected:
    """Everything one tag query found in one tree."""

    definitions: list[_RawDefinition]
    references: list[_RawReference]
    locals: list[tuple[str, SourceRange | None]]
    """Names bound inside a function, with the span of the function that binds them.

    ``None`` is the module: a name bound there is a symbol, not a local.
    """

    bindings: list[tuple[str, str, SourceRange | None]]
    """``(variable, type name, binding function)`` for every annotated or constructed local."""

    def shifted(self, adjust: Callable[[SourceRange], SourceRange]) -> _Collected:
        """The same findings with every span mapped through ``adjust``."""
        for definition in self.definitions:
            definition.name_span = adjust(definition.name_span)
            definition.full_span = adjust(definition.full_span)
        for reference in self.references:
            reference.span = adjust(reference.span)
        self.locals = [
            (name, adjust(span) if span is not None else None) for name, span in self.locals
        ]
        self.bindings = [
            (var, kind, adjust(span) if span is not None else None)
            for var, kind, span in self.bindings
        ]
        return self


@dataclass(slots=True)
class _RawDefinition:
    name: str
    kind: SymbolKind
    name_span: SourceRange
    full_span: SourceRange
    signature: str = ""
    hoist: bool = False
    """Belongs to the nearest enclosing type, not the nearest enclosing body."""


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


def _collect(tree: Tree, language: str, lines: list[str]) -> _Collected:
    """Run the tag query and sort its captures into what they say."""
    from tree_sitter import QueryCursor

    cursor = QueryCursor(_compiled_query(language))

    definitions: dict[tuple[int, int, int, int], _RawDefinition] = {}
    # One use site is one reference however many patterns matched it, so
    # references are keyed by span and the strongest kind wins.
    reference_kinds: dict[SourceRange, str] = {}
    reference_names: dict[SourceRange, str] = {}
    receivers: dict[SourceRange, str] = {}
    chained: set[SourceRange] = set()
    call_bindings: list[tuple[str, str, str, SourceRange | None, SourceRange]] = []
    found_locals: list[tuple[str, SourceRange | None]] = []
    bindings: list[tuple[str, str, SourceRange | None]] = []

    for _pattern_index, captures in cursor.matches(tree.root_node):
        local_nodes = captures.get("local")
        if local_nodes and "name" not in captures:
            node = local_nodes[0]
            if node.text is not None:
                found_locals.append((_text(node, language), _enclosing_function(node, language)))
            continue
        if "binding" in captures:
            var_nodes, type_nodes = captures.get("var"), captures.get("vtype")
            call_nodes = captures.get("vcall")
            if var_nodes and call_nodes and var_nodes[0].text and call_nodes[0].text:
                # The type is whatever the call returns; the resolver
                # finds out. The receiver's own type is looked up once
                # every binding is known, below.
                receiver_nodes = captures.get("vcall_receiver")
                call_receiver = (
                    _text(receiver_nodes[0], language)
                    if receiver_nodes and receiver_nodes[0].text is not None
                    else ""
                )
                call_bindings.append(
                    (
                        _text(var_nodes[0], language),
                        _text(call_nodes[0], language),
                        call_receiver,
                        _enclosing_function(captures["binding"][0], language),
                        to_range(captures["binding"][0]),
                    )
                )
                continue
            if var_nodes and type_nodes and var_nodes[0].text and type_nodes[0].text:
                binding_node = captures["binding"][0]
                var = _text(var_nodes[0], language)
                type_name = type_nodes[0].text.decode("utf-8", errors="replace")
                bindings.append((var, type_name, _enclosing_function(binding_node, language)))
                if _is_field_binding(binding_node):
                    # A typed property binds `this.name` for every method
                    # of the class: `$this->service->handle()` goes where
                    # `private Service $service` says.
                    bindings.append(
                        ("this." + var, type_name, _enclosing_class(binding_node, language))
                    )
            continue
        name_nodes = captures.get("name")
        if not name_nodes:
            continue
        name_node = name_nodes[0]
        name_span = to_range(name_node)
        name_text = name_node.text
        if name_text is None:
            continue
        name = _text(name_node, language)
        if not name_node.is_named:
            # The `class` keyword of an anonymous class is the only name
            # it has; PHP itself calls the type `class@anonymous`.
            name = f"{name}@anonymous"
        # A string-keyed reference such as `@extends('layouts.app')` captures
        # the literal, quotes and all. The name is what is inside them.
        name = _unquote(name)
        if not name:
            continue
        if "chained" in captures:
            chained.add(name_span)

        for capture_name, nodes in captures.items():
            if capture_name.startswith(_DEFINITION_PREFIX):
                kind = _KIND_BY_CAPTURE.get(
                    capture_name[len(_DEFINITION_PREFIX) :], SymbolKind.UNKNOWN
                )
                full_span = to_range(nodes[0])
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
                        hoist=capture_name == "definition.attribute",
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
                receiver_nodes = captures.get("receiver")
                if receiver_nodes and receiver_nodes[0].text is not None:
                    receivers[name_span] = _text(receiver_nodes[0], language)
                field_nodes = captures.get("receiver_field")
                if field_nodes and field_nodes[0].text is not None:
                    receivers[name_span] = "this." + _text(field_nodes[0], language)

    ordered = sorted(
        definitions.values(), key=lambda d: (d.name_span.start, d.name_span.end)
    )
    references = [
        _RawReference(
            reference_names[span],
            kind,
            span,
            receivers.get(span) or (EXPRESSION_RECEIVER if span in chained else None),
        )
        for span, kind in reference_kinds.items()
    ]
    # A call's receiver may itself be a typed local; say so, so that the
    # resolver can find the callee. This is why call bindings wait until
    # the declared ones are all in.
    scoping = _Scoping(found_locals, bindings)
    for var, callee, call_receiver, scope, at in call_bindings:
        receiver_type = scoping.type_of(call_receiver, at) if call_receiver else None
        marker = f"{CALL_TYPE_PREFIX}{callee}|{call_receiver}|{receiver_type or ''}"
        bindings.append((var, marker, scope))
    return _Collected(ordered, references, found_locals, bindings)


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
    # Fields already defined per type, so `self.x = ...` in a second method
    # is a use of the field rather than a second field.
    fields_seen: set[tuple[str, str]] = set()

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

        if definition.hoist:
            # `self.x = ...` sits inside a method; the field it defines
            # belongs to the class around it. Outside any class it defines
            # nothing an index should hold.
            owner_index = next(
                (
                    index
                    for _span, index in reversed(open_bodies)
                    if symbols[index].kind in _TYPE_CONTAINERS
                ),
                None,
            )
            if owner_index is None:
                continue
            container = symbols[owner_index]
            if (container.id, definition.name) in fields_seen:
                continue
            fields_seen.add((container.id, definition.name))
            qualified = _qualified_name(definition.name, container)
            symbols.append(
                Symbol(
                    id=f"{path}#{qualified}",
                    name=definition.name,
                    kind=SymbolKind.FIELD,
                    path=path,
                    name_range=definition.name_span,
                    full_range=definition.full_span,
                    container_id=container.id,
                    qualified_name=qualified,
                    signature=definition.signature or None,
                )
            )
            continue
        qualified = _qualified_name(definition.name, container)
        base_id = f"{path}#{qualified}"
        seen = used.get(base_id, 0) + 1
        used[base_id] = seen
        symbol_id = base_id if seen == 1 else f"{base_id}${seen}"

        # Locality is inherited: a closure inside a function is local, and so
        # is everything inside that closure. A type is not: a class declared
        # inside a function, PHP's `new class { ... }`, has members an agent
        # navigates to, and a compiler-backed index files them as
        # definitions like any other.
        is_local = (
            container is not None
            and (container.local or container.kind in _LOCAL_SCOPES)
            and not definition.kind.is_type_like
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
        if symbol.kind is SymbolKind.FIELD and container is not None:
            fields_seen.add((container.id, symbol.name))
        open_bodies.append((definition.full_span, len(symbols) - 1))
    return symbols


def _merge(into: _Collected, extra: _Collected) -> None:
    into.definitions.extend(extra.definitions)
    into.references.extend(extra.references)
    into.locals.extend(extra.locals)
    into.bindings.extend(extra.bindings)


class _Scoping:
    """Which names are local to which function, and what type each has.

    Built once per file from the `@local` and `@binding` captures, each of
    which the collector tagged with the span of the function that binds it.
    A local is visible in that function and in every closure nested inside
    it, which is the scoping rule shared by every language here, near
    enough that the differences do not reach a reference's resolution.

    The scope is the function *node*, not a symbol. Half of a front end's
    code sits inside anonymous callbacks that are nobody's symbol, and a
    `const` declared there shadows just as well as one in a named function.
    """

    __slots__ = ("_names", "_types")

    def __init__(
        self,
        found_locals: list[tuple[str, SourceRange | None]],
        bindings: list[tuple[str, str, SourceRange | None]],
    ) -> None:
        self._names: dict[str, list[SourceRange]] = {}
        self._types: dict[str, list[tuple[SourceRange | None, str]]] = {}
        for name, scope in found_locals:
            # A name bound at module level is a symbol, not a local; only a
            # function's own bindings shadow anything.
            if scope is not None:
                self._names.setdefault(name, []).append(scope)
        for var, type_name, scope in bindings:
            self._types.setdefault(var, []).append((scope, type_name))

    def is_local(self, name: str, span: SourceRange) -> bool:
        return any(_encloses(scope, span) for scope in self._names.get(name, ()))

    def type_of(self, receiver: str | None, span: SourceRange) -> str | None:
        """The innermost binding of ``receiver`` that is in scope at ``span``."""
        if not receiver:
            return None
        best: tuple[int, int] | None = None
        found: str | None = None
        module_level: str | None = None
        for scope, type_name in self._types.get(receiver, ()):
            if scope is None:
                module_level = module_level or type_name
            elif _encloses(scope, span):
                size = (
                    scope.end.line - scope.start.line,
                    scope.end.character - scope.start.character,
                )
                if best is None or size < best:
                    best, found = size, type_name
        return found if found is not None else module_level


def _encloses(outer: SourceRange, inner: SourceRange) -> bool:
    return (outer.start.line, outer.start.character) <= (
        inner.start.line,
        inner.start.character,
    ) and (inner.end.line, inner.end.character) <= (outer.end.line, outer.end.character)


# The node types that open a scope for locals, per grammar. A language not
# listed gets the union, which is harmless: an unknown node type never
# matches anything.
_JS_FUNCTION_NODES = frozenset(
    {
        "function_declaration",
        "function_expression",
        "function",
        "arrow_function",
        "method_definition",
        "generator_function",
        "generator_function_declaration",
    }
)
_FUNCTION_NODES: dict[str, frozenset[str]] = {
    "typescript": _JS_FUNCTION_NODES,
    "tsx": _JS_FUNCTION_NODES,
    "javascript": _JS_FUNCTION_NODES,
    "python": frozenset({"function_definition", "lambda"}),
    "php": frozenset(
        {
            "function_definition",
            "method_declaration",
            "anonymous_function",
            "anonymous_function_creation_expression",
            "arrow_function",
        }
    ),
}
_ANY_FUNCTION_NODES = frozenset().union(*_FUNCTION_NODES.values())

# Languages whose variables carry a sigil the name does without.
_SIGIL_LANGUAGES = frozenset({"php", "blade"})


def _text(node: Node, language: str) -> str:
    text = (node.text or b"").decode("utf-8", errors="replace")
    return text.lstrip("$") if language in _SIGIL_LANGUAGES else text


# The node types that are a class body's owner, for bindings of `this.x`.
_CLASS_NODES: dict[str, frozenset[str]] = {
    "typescript": frozenset({"class_declaration", "abstract_class_declaration", "class"}),
    "tsx": frozenset({"class_declaration", "abstract_class_declaration", "class"}),
    "javascript": frozenset({"class_declaration", "class"}),
    "python": frozenset({"class_definition"}),
    "php": frozenset(
        {"class_declaration", "anonymous_class", "trait_declaration", "enum_declaration"}
    ),
}
_ANY_CLASS_NODES = frozenset().union(*_CLASS_NODES.values())

# Binding nodes that declare a property rather than a local.
_FIELD_BINDING_NODES = frozenset(
    {"property_declaration", "property_promotion_parameter", "public_field_definition"}
)


def _is_field_binding(node: Node) -> bool:
    if node.type in _FIELD_BINDING_NODES:
        return True
    # TypeScript's `constructor(private svc: Svc)` promotes the parameter
    # to a property, and the only sign of it is the modifier.
    return node.type == "required_parameter" and any(
        child.type == "accessibility_modifier" for child in node.children
    )


def _enclosing(node: Node, kinds: frozenset[str]) -> SourceRange | None:
    """The span of the nearest ancestor of one of ``kinds``, skipping ``node``'s own.

    A function declared inside another is bound in the outer one, not in
    itself: the walk skips an ancestor when ``node`` is its own name.
    """
    current = node.parent
    while current is not None:
        if current.type in kinds and current.child_by_field_name("name") != node:
            return to_range(current)
        current = current.parent
    return None


def _enclosing_function(node: Node, language: str) -> SourceRange | None:
    """The function ``node`` is bound in, or ``None`` at module level."""
    return _enclosing(node, _FUNCTION_NODES.get(language, _ANY_FUNCTION_NODES))


def _enclosing_class(node: Node, language: str) -> SourceRange | None:
    """The class ``node`` is declared in, or ``None`` outside any."""
    return _enclosing(node, _CLASS_NODES.get(language, _ANY_CLASS_NODES))


def extract_source(
    path: str, source: bytes, spec: LanguageSpec
) -> FileExtraction:
    """Extract from source already in memory."""
    parser = get_parser(spec.name)
    tree = parser.parse(source)
    lines = source.decode("utf-8", errors="replace").splitlines()
    collected = _collect(tree, spec.name, lines)

    # A Vue component's definitions live in its script block, which is
    # another language. Parsing it here rather than in a second pass keeps
    # every symbol in one file's extraction, so the store's per-file
    # delete-and-insert still covers all of them.
    embedded_imports: list[FileImports] = []
    for region in embedded_regions(tree, spec.name):
        inner_tree = parse_embedded(source, region)
        if inner_tree is None:
            continue
        if region.is_prefixed:
            # The island was parsed with a prefix in front, so its tree's
            # positions are its own; collect against the island's lines
            # and move every span back onto the host file.
            island = region.prefix + source[region.node.start_byte : region.node.end_byte]
            island_lines = island.decode("utf-8", errors="replace").splitlines()
            inner = _collect(inner_tree, region.language, island_lines)
            prefix_text = region.prefix.decode("utf-8", errors="replace")
            for definition in inner.definitions:
                if definition.name_span.start.line == 0 and definition.signature.startswith(
                    prefix_text
                ):
                    definition.signature = definition.signature[len(prefix_text) :]
            _merge(collected, inner.shifted(region.adjust))
            continue
        _merge(collected, _collect(inner_tree, region.language, lines))
        embedded_imports.append(extract_imports(inner_tree, region.language))
    collected.definitions.sort(key=lambda d: (d.name_span.start, d.name_span.end))

    symbols = _assign_ids(path, collected.definitions)

    scopes: ScopeIndex[int] = ScopeIndex(
        (symbol.full_range or symbol.name_range, index)
        for index, symbol in enumerate(symbols)
    )
    definition_spans = {symbol.name_range for symbol in symbols}
    scoping = _Scoping(collected.locals, collected.bindings)

    references: list[Reference] = []
    for raw in collected.references:
        if raw.kind == "component" and not _is_component_name(raw.name):
            continue
        if raw.span in definition_spans:
            # The identifier in `class A(B)` is a definition of A and a
            # reference to B; a name that is its own definition site is not
            # a reference to anything.
            continue
        if raw.kind in _SHADOWED_KINDS and scoping.is_local(raw.name, raw.span):
            # A use of a parameter or a local, whether read, called or
            # constructed: the function's own business, and not a use of
            # any symbol this index defines.
            continue
        container_index = scopes.innermost(raw.span)
        references.append(
            Reference(
                name=raw.name,
                kind=raw.kind,
                span=raw.span,
                container_id=(
                    symbols[container_index].id if container_index is not None else None
                ),
                receiver=raw.receiver,
                receiver_type=scoping.type_of(raw.receiver, raw.span),
            )
        )
    references.sort(key=lambda reference: (reference.span.start, reference.name))

    file_imports = extract_imports(tree, spec.name)
    for extra in embedded_imports:
        file_imports.statements.extend(extra.statements)
        file_imports.namespace = file_imports.namespace or extra.namespace

    error_count = _count_errors(tree)
    return FileExtraction(
        path=path,
        language=spec.name,
        symbols=symbols,
        references=references,
        imports=file_imports,
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
