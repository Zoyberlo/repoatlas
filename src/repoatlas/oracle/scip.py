"""Reader for SCIP indexes, used as ground truth for edge accuracy.

SCIP is the index format produced by compiler-backed indexers such as
``scip-typescript``, ``scip-python``, ``scip-java``/``scip-kotlin`` and
``scip-php``. Because those indexers use a real compiler front end, their
definition and reference sets are the closest thing to an oracle available
without writing a type checker.

Two input forms are supported:

``.scip`` binary
    Decoded with :mod:`repoatlas.oracle.protobuf`. Field numbers are
    declared in :class:`Field` so a schema change is a one-line fix.

``scip print --json`` output
    The authoritative form when the two disagree, because it is produced by
    the SCIP CLI itself rather than by this module's understanding of the
    schema. :func:`cross_check` compares both readings of one index.

Reference: https://github.com/scip-code/scip
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..model import (
    Edge,
    EdgeKind,
    IndexSnapshot,
    PositionEncoding,
    ResolutionTier,
    SourceRange,
    Symbol,
    SymbolKind,
)
from . import protobuf as pb

__all__ = [
    "ParsedSymbol",
    "ScipError",
    "SymbolRole",
    "cross_check",
    "parse_symbol",
    "read_scip",
    "read_scip_binary",
    "read_scip_json",
]


class ScipError(ValueError):
    """Raised when an index cannot be interpreted as SCIP."""


class Field:
    """SCIP protobuf field numbers, grouped by message.

    Kept in one place because they are the only part of this reader that can
    drift with the upstream schema.
    """

    # Index
    INDEX_METADATA = 1
    INDEX_DOCUMENTS = 2
    INDEX_EXTERNAL_SYMBOLS = 3

    # Metadata
    META_VERSION = 1
    META_TOOL_INFO = 2
    META_PROJECT_ROOT = 3
    META_TEXT_ENCODING = 4

    # ToolInfo
    TOOL_NAME = 1
    TOOL_VERSION = 2

    # Document
    DOC_RELATIVE_PATH = 1
    DOC_OCCURRENCES = 2
    DOC_SYMBOLS = 3
    DOC_LANGUAGE = 4
    DOC_TEXT = 5
    DOC_POSITION_ENCODING = 6

    # Occurrence
    OCC_RANGE = 1
    OCC_SYMBOL = 2
    OCC_SYMBOL_ROLES = 3
    OCC_SYNTAX_KIND = 5
    OCC_ENCLOSING_RANGE = 7

    # SymbolInformation
    SYM_SYMBOL = 1
    SYM_DOCUMENTATION = 3
    SYM_RELATIONSHIPS = 4
    SYM_KIND = 5
    SYM_DISPLAY_NAME = 6
    SYM_ENCLOSING_SYMBOL = 8

    # Relationship
    REL_SYMBOL = 1
    REL_IS_REFERENCE = 2
    REL_IS_IMPLEMENTATION = 3
    REL_IS_TYPE_DEFINITION = 4
    REL_IS_DEFINITION = 5


class SymbolRole:
    """Bit flags on ``Occurrence.symbol_roles``."""

    DEFINITION = 0x1
    IMPORT = 0x2
    WRITE_ACCESS = 0x4
    READ_ACCESS = 0x8
    GENERATED = 0x10
    TEST = 0x20
    FORWARD_DEFINITION = 0x40


# SCIP ``TextEncoding`` and ``PositionEncoding`` enums both use 1/2/3 for
# UTF-8/16/32, so one table serves both.
_ENCODING_BY_NUMBER = {
    0: PositionEncoding.UTF8,  # unspecified; UTF-8 is the practical default
    1: PositionEncoding.UTF8,
    2: PositionEncoding.UTF16,
    3: PositionEncoding.UTF32,
}
_ENCODING_BY_NAME = {
    "utf8": PositionEncoding.UTF8,
    "utf-8": PositionEncoding.UTF8,
    "utf8codeunitoffsetfromlinestart": PositionEncoding.UTF8,
    "utf16": PositionEncoding.UTF16,
    "utf-16": PositionEncoding.UTF16,
    "utf16codeunitoffsetfromlinestart": PositionEncoding.UTF16,
    "utf32": PositionEncoding.UTF32,
    "utf32codeunitoffsetfromlinestart": PositionEncoding.UTF32,
}

# SCIP descriptors encode where a symbol sits in the syntax, not what it is.
# A `#` suffix covers every named type alike, so a class, an interface and a
# type alias are indistinguishable from the symbol string; `#` is reported as
# CLASS as the commonest case, and comparisons that care should treat the
# type-like kinds as one family rather than expect an exact match.
#
# Two things the descriptor chain *can* settle are handled in `parse_symbol`:
# whether a `().` is a method or a free function, and whether a `.` term is a
# field or a variable, both of which follow from the preceding descriptor.
_DESCRIPTOR_KIND = {
    "/": SymbolKind.NAMESPACE,
    "#": SymbolKind.CLASS,
    ".": SymbolKind.VARIABLE,
    "().": SymbolKind.FUNCTION,
    ":": SymbolKind.UNKNOWN,
    "!": SymbolKind.MACRO,
    "[]": SymbolKind.PARAMETER,
    "()": SymbolKind.PARAMETER,
}

# scip-typescript spells a constructor this way inside a type descriptor.
_CONSTRUCTOR_DESCRIPTOR_NAMES = frozenset({"<constructor>", "constructor", "__init__"})

_KIND_BY_NAME = {
    "class": SymbolKind.CLASS,
    "interface": SymbolKind.INTERFACE,
    "trait": SymbolKind.TRAIT,
    "enum": SymbolKind.ENUM,
    "struct": SymbolKind.CLASS,
    "object": SymbolKind.CLASS,
    "type": SymbolKind.TYPE_ALIAS,
    "typealias": SymbolKind.TYPE_ALIAS,
    "function": SymbolKind.FUNCTION,
    "method": SymbolKind.METHOD,
    "staticmethod": SymbolKind.METHOD,
    "constructor": SymbolKind.CONSTRUCTOR,
    "property": SymbolKind.PROPERTY,
    "field": SymbolKind.FIELD,
    "constant": SymbolKind.CONSTANT,
    "variable": SymbolKind.VARIABLE,
    "parameter": SymbolKind.PARAMETER,
    "namespace": SymbolKind.NAMESPACE,
    "package": SymbolKind.NAMESPACE,
    "module": SymbolKind.MODULE,
    "file": SymbolKind.MODULE,
    "macro": SymbolKind.MACRO,
}


@dataclass(frozen=True, slots=True)
class ParsedSymbol:
    """A SCIP symbol string broken into its parts."""

    raw: str
    is_local: bool
    scheme: str = ""
    package_manager: str = ""
    package_name: str = ""
    version: str = ""
    descriptors: tuple[str, ...] = ()
    kind: SymbolKind = SymbolKind.UNKNOWN

    @property
    def name(self) -> str:
        """The last descriptor, stripped of its suffix."""
        if not self.descriptors:
            return self.raw
        return _descriptor_name(self.descriptors[-1])

    @property
    def qualified_name(self) -> str:
        return "".join(self.descriptors) if self.descriptors else self.raw


def _split_escaped(text: str) -> list[str]:
    """Split on spaces, honouring SCIP backtick escaping."""
    parts: list[str] = []
    current: list[str] = []
    in_backticks = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "`":
            # A doubled backtick inside an escaped name is a literal one.
            if in_backticks and index + 1 < len(text) and text[index + 1] == "`":
                current.append("`")
                index += 2
                continue
            in_backticks = not in_backticks
            current.append(char)
        elif char == " " and not in_backticks:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def _descriptor_suffix(descriptor: str) -> str:
    # A method may carry an overload disambiguator, as in `render(+1).`, so
    # the test is "ends with a closing paren then a dot", not a literal `().`.
    if descriptor.endswith(").") and "(" in descriptor:
        return "()."
    if descriptor.endswith(("/", "#", ".", ":", "!")):
        return descriptor[-1]
    if descriptor.startswith("[") and descriptor.endswith("]"):
        return "[]"
    if descriptor.startswith("(") and descriptor.endswith(")"):
        return "()"
    return ""


def _descriptor_name(descriptor: str) -> str:
    suffix = _descriptor_suffix(descriptor)
    if suffix == "().":
        body = descriptor[:-1]  # drop the trailing dot
        body = body[: body.rindex("(")]
    elif suffix in ("[]", "()"):
        body = descriptor[1:-1]
    elif suffix:
        body = descriptor[: -len(suffix)]
    else:
        body = descriptor
    if body.startswith("`") and body.endswith("`") and len(body) >= 2:
        body = body[1:-1].replace("``", "`")
    return body


def _split_descriptors(text: str) -> list[str]:
    """Split the descriptor suffix of a symbol into individual descriptors."""
    descriptors: list[str] = []
    current: list[str] = []
    in_backticks = False
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        current.append(char)
        if char == "`":
            if in_backticks and index + 1 < len(text) and text[index + 1] == "`":
                current.append("`")
                index += 2
                continue
            in_backticks = not in_backticks
        elif not in_backticks:
            if char in "([":
                depth += 1
            elif char in ")]":
                depth = max(0, depth - 1)
                # `(params)` and `[type]` are complete descriptors on their own,
                # but `foo().` continues with a trailing dot.
                if depth == 0 and not (
                    index + 1 < len(text) and text[index + 1] == "."
                ):
                    descriptors.append("".join(current))
                    current = []
            elif depth == 0 and char in "/#.:!":
                descriptors.append("".join(current))
                current = []
        index += 1
    if current:
        descriptors.append("".join(current))
    return descriptors


def parse_symbol(symbol: str) -> ParsedSymbol:
    """Parse a SCIP symbol string.

    Local symbols (``local 3``) are file-scoped and carry no package, so they
    are flagged rather than parsed further.
    """
    if not symbol:
        raise ScipError("empty symbol string")
    if symbol.startswith("local "):
        return ParsedSymbol(raw=symbol, is_local=True, kind=SymbolKind.VARIABLE)

    header = _split_escaped(symbol)
    if len(header) < 5:
        # Tolerate indexers that omit trailing header fields.
        return ParsedSymbol(raw=symbol, is_local=False)
    scheme, manager, package, version = header[:4]
    descriptor_text = " ".join(header[4:])
    descriptors = tuple(d for d in _split_descriptors(descriptor_text) if d)
    kind = SymbolKind.UNKNOWN
    if descriptors:
        kind = _DESCRIPTOR_KIND.get(_descriptor_suffix(descriptors[-1]), SymbolKind.UNKNOWN)
        inside_type = any(_descriptor_suffix(d) == "#" for d in descriptors[:-1])
        # A term directly inside a type is a field, not a loose variable, and
        # a callable inside one is a method rather than a free function.
        if kind is SymbolKind.VARIABLE and inside_type:
            kind = SymbolKind.FIELD
        elif kind is SymbolKind.FUNCTION and inside_type:
            kind = (
                SymbolKind.CONSTRUCTOR
                if _descriptor_name(descriptors[-1]) in _CONSTRUCTOR_DESCRIPTOR_NAMES
                else SymbolKind.METHOD
            )
    return ParsedSymbol(
        raw=symbol,
        is_local=False,
        scheme=scheme,
        package_manager=manager,
        package_name=package,
        version=version,
        descriptors=descriptors,
        kind=kind,
    )


@dataclass(slots=True)
class _Occurrence:
    """One occurrence, normalised out of either input form."""

    symbol: str
    span: SourceRange
    roles: int
    enclosing: SourceRange | None = None

    @property
    def is_definition(self) -> bool:
        return bool(self.roles & SymbolRole.DEFINITION)

    @property
    def is_import(self) -> bool:
        return bool(self.roles & SymbolRole.IMPORT)


@dataclass(slots=True)
class _Document:
    path: str
    language: str = ""
    occurrences: list[_Occurrence] = field(default_factory=list)
    symbol_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    encoding: PositionEncoding | None = None


def _coerce_encoding(value: object, default: PositionEncoding) -> PositionEncoding:
    if value is None:
        return default
    if isinstance(value, int):
        return _ENCODING_BY_NUMBER.get(value, default)
    if isinstance(value, str):
        return _ENCODING_BY_NAME.get(value.strip().lower().replace("_", ""), default)
    return default


# --- binary reader --------------------------------------------------------


def _read_binary_documents(
    fields: pb.Fields,
) -> tuple[list[_Document], PositionEncoding, str, str]:
    metadata = next(pb.submessages(fields, Field.INDEX_METADATA), {})
    project_root = pb.as_str(metadata, Field.META_PROJECT_ROOT)
    # Metadata carries the *text* encoding of the files, which says nothing
    # about how column offsets are counted. Only a document's own
    # position_encoding does, so this default is an assumption and the
    # snapshot records it as one.
    encoding = PositionEncoding.UTF8
    tool = next(pb.submessages(metadata, Field.META_TOOL_INFO), {})
    producer = " ".join(
        part
        for part in (pb.as_str(tool, Field.TOOL_NAME), pb.as_str(tool, Field.TOOL_VERSION))
        if part
    )

    documents: list[_Document] = []
    for raw_doc in pb.submessages(fields, Field.INDEX_DOCUMENTS):
        doc = _Document(
            path=pb.as_str(raw_doc, Field.DOC_RELATIVE_PATH),
            language=pb.as_str(raw_doc, Field.DOC_LANGUAGE),
        )
        doc_encoding = pb.as_int(raw_doc, Field.DOC_POSITION_ENCODING, -1)
        if doc_encoding >= 0:
            doc.encoding = _coerce_encoding(doc_encoding, encoding)
        if not doc.path:
            raise ScipError("document without a relative_path")

        for raw_occ in pb.submessages(raw_doc, Field.DOC_OCCURRENCES):
            span_values = pb.int_array(raw_occ, Field.OCC_RANGE)
            if not span_values:
                continue
            enclosing_values = pb.int_array(raw_occ, Field.OCC_ENCLOSING_RANGE)
            doc.occurrences.append(
                _Occurrence(
                    symbol=pb.as_str(raw_occ, Field.OCC_SYMBOL),
                    span=SourceRange.from_scip(span_values),
                    roles=pb.as_int(raw_occ, Field.OCC_SYMBOL_ROLES),
                    enclosing=(
                        SourceRange.from_scip(enclosing_values) if enclosing_values else None
                    ),
                )
            )

        for raw_sym in pb.submessages(raw_doc, Field.DOC_SYMBOLS):
            name = pb.as_str(raw_sym, Field.SYM_SYMBOL)
            if not name:
                continue
            relationships: list[dict[str, Any]] = []
            for raw_rel in pb.submessages(raw_sym, Field.SYM_RELATIONSHIPS):
                relationships.append(
                    {
                        "symbol": pb.as_str(raw_rel, Field.REL_SYMBOL),
                        "isReference": bool(pb.as_int(raw_rel, Field.REL_IS_REFERENCE)),
                        "isImplementation": bool(
                            pb.as_int(raw_rel, Field.REL_IS_IMPLEMENTATION)
                        ),
                        "isTypeDefinition": bool(
                            pb.as_int(raw_rel, Field.REL_IS_TYPE_DEFINITION)
                        ),
                        "isDefinition": bool(pb.as_int(raw_rel, Field.REL_IS_DEFINITION)),
                    }
                )
            doc.symbol_info[name] = {
                "displayName": pb.as_str(raw_sym, Field.SYM_DISPLAY_NAME),
                "documentation": pb.strings(raw_sym, Field.SYM_DOCUMENTATION),
                "relationships": relationships,
                "enclosingSymbol": pb.as_str(raw_sym, Field.SYM_ENCLOSING_SYMBOL),
            }
        documents.append(doc)
    return documents, encoding, project_root, producer


def read_scip_binary(data: bytes | Path | str) -> IndexSnapshot:
    """Read a binary ``.scip`` index into a snapshot."""
    if isinstance(data, (str, Path)):
        data = Path(data).read_bytes()
    try:
        fields = pb.decode_message(data)
    except pb.ProtobufError as exc:
        raise ScipError(f"not a valid SCIP index: {exc}") from exc
    documents, encoding, project_root, producer = _read_binary_documents(fields)
    return _build_snapshot(documents, encoding, project_root, producer or "scip (binary)")


# --- JSON reader ----------------------------------------------------------


def _get(mapping: dict[str, Any], *names: str, default: Any = None) -> Any:
    """Look a key up under any of its spellings.

    ``scip print --json`` emits camelCase, but hand-written fixtures and some
    tooling use snake_case, so both are accepted.
    """
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _roles_from_json(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return SymbolRole.DEFINITION if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        names = [part.strip() for part in value.replace("|", ",").split(",")]
        roles = 0
        for name in names:
            key = name.upper().replace(" ", "_")
            roles |= getattr(SymbolRole, key, 0)
        return roles
    if isinstance(value, list):
        roles = 0
        for item in value:
            roles |= _roles_from_json(item)
        return roles
    return 0


def read_scip_json(data: str | bytes | Path | dict[str, Any]) -> IndexSnapshot:
    """Read ``scip print --json`` output into a snapshot."""
    if isinstance(data, Path):
        payload = json.loads(Path(data).read_text(encoding="utf-8"))
    elif isinstance(data, (str, bytes)):
        payload = json.loads(data)
    else:
        payload = data
    if not isinstance(payload, dict):
        raise ScipError("SCIP JSON must be an object at the top level")

    metadata = _get(payload, "metadata", default={}) or {}
    project_root = _get(metadata, "projectRoot", "project_root", default="") or ""
    # See the binary reader: text encoding is not column encoding.
    encoding = PositionEncoding.UTF8
    tool = _get(metadata, "toolInfo", "tool_info", default={}) or {}
    producer = " ".join(
        str(part)
        for part in (_get(tool, "name", default=""), _get(tool, "version", default=""))
        if part
    )

    documents: list[_Document] = []
    for raw_doc in _get(payload, "documents", default=[]) or []:
        path = _get(raw_doc, "relativePath", "relative_path", default="")
        if not path:
            raise ScipError("document without a relativePath")
        doc = _Document(path=path, language=_get(raw_doc, "language", default="") or "")
        doc_encoding = _get(raw_doc, "positionEncoding", "position_encoding")
        if doc_encoding is not None:
            doc.encoding = _coerce_encoding(doc_encoding, encoding)

        for raw_occ in _get(raw_doc, "occurrences", default=[]) or []:
            span_values = _get(raw_occ, "range", default=None)
            if not span_values:
                continue
            enclosing_values = _get(raw_occ, "enclosingRange", "enclosing_range", default=None)
            doc.occurrences.append(
                _Occurrence(
                    symbol=_get(raw_occ, "symbol", default="") or "",
                    span=SourceRange.from_scip(span_values),
                    roles=_roles_from_json(
                        _get(raw_occ, "symbolRoles", "symbol_roles", default=0)
                    ),
                    enclosing=(
                        SourceRange.from_scip(enclosing_values) if enclosing_values else None
                    ),
                )
            )

        for raw_sym in _get(raw_doc, "symbols", default=[]) or []:
            name = _get(raw_sym, "symbol", default="")
            if not name:
                continue
            relationships = []
            for raw_rel in _get(raw_sym, "relationships", default=[]) or []:
                relationships.append(
                    {
                        "symbol": _get(raw_rel, "symbol", default=""),
                        "isReference": bool(
                            _get(raw_rel, "isReference", "is_reference", default=False)
                        ),
                        "isImplementation": bool(
                            _get(raw_rel, "isImplementation", "is_implementation", default=False)
                        ),
                        "isTypeDefinition": bool(
                            _get(raw_rel, "isTypeDefinition", "is_type_definition", default=False)
                        ),
                        "isDefinition": bool(
                            _get(raw_rel, "isDefinition", "is_definition", default=False)
                        ),
                    }
                )
            doc.symbol_info[name] = {
                "displayName": _get(raw_sym, "displayName", "display_name", default=""),
                "documentation": _get(raw_sym, "documentation", default=[]) or [],
                "relationships": relationships,
                "enclosingSymbol": _get(
                    raw_sym, "enclosingSymbol", "enclosing_symbol", default=""
                ),
            }
        documents.append(doc)
    return _build_snapshot(documents, encoding, project_root, producer or "scip (json)")


def read_scip(source: Path | str) -> IndexSnapshot:
    """Read either form, choosing by file extension then by content sniffing."""
    path = Path(source)
    if path.suffix.lower() == ".json":
        return read_scip_json(path)
    raw = path.read_bytes()
    stripped = raw.lstrip()
    if stripped[:1] in (b"{", b"["):
        return read_scip_json(raw.decode("utf-8"))
    return read_scip_binary(raw)


# --- snapshot construction ------------------------------------------------


def _enclosing_symbol_id(
    definitions: Sequence[tuple[SourceRange, str]], site: SourceRange
) -> str | None:
    """Find the innermost definition whose body contains ``site``."""
    best: tuple[SourceRange, str] | None = None
    for span, symbol_id in definitions:
        if not span.contains(site):
            continue
        if best is None or best[0].contains(span):
            best = (span, symbol_id)
    return best[1] if best else None


def _add_module_symbol(snapshot: IndexSnapshot, doc: _Document) -> str:
    """Create the stand-in symbol representing a whole file."""
    module_id = f"repoatlas-module {doc.path}"
    if module_id not in snapshot.symbols:
        origin = SourceRange.of(0, 0, 0, 0)
        snapshot.add_symbol(
            Symbol(
                id=module_id,
                name=doc.path.rsplit("/", 1)[-1],
                kind=SymbolKind.MODULE,
                path=doc.path,
                name_range=origin,
                full_range=origin,
                language=doc.language or None,
                synthetic=True,
            )
        )
    return module_id


def _build_snapshot(
    documents: Iterable[_Document],
    encoding: PositionEncoding,
    project_root: str,
    producer: str,
) -> IndexSnapshot:
    documents = list(documents)
    declared = [doc.encoding for doc in documents if doc.encoding is not None]
    snapshot = IndexSnapshot(
        encoding=declared[0] if declared else encoding,
        encoding_declared=bool(declared),
        project_root=project_root or None,
        producer=producer,
    )

    # Pass one: every definition becomes a symbol. Symbol ids are the SCIP
    # symbol strings, so relationships resolve without a second lookup table.
    definition_scopes: dict[str, list[tuple[SourceRange, str]]] = {}
    for doc in documents:
        scopes: list[tuple[SourceRange, str]] = []
        for occ in doc.occurrences:
            if not occ.is_definition or not occ.symbol:
                continue
            parsed = parse_symbol(occ.symbol)
            info = doc.symbol_info.get(occ.symbol, {})
            display = info.get("displayName") or parsed.name
            docs = info.get("documentation") or []
            kind = parsed.kind
            if kind is SymbolKind.UNKNOWN:
                kind = _KIND_BY_NAME.get(str(info.get("kind", "")).lower(), SymbolKind.UNKNOWN)
            body = occ.enclosing if occ.enclosing is not None else occ.span
            if not body.contains(occ.span):
                body = occ.span
            snapshot.add_symbol(
                Symbol(
                    id=occ.symbol,
                    name=display,
                    kind=kind,
                    path=doc.path,
                    name_range=occ.span,
                    full_range=body,
                    qualified_name=parsed.qualified_name or None,
                    language=doc.language or None,
                    documentation="\n".join(docs) or None,
                    # SCIP spells a scope-local binding `local 4`, with no
                    # package or descriptors, precisely because nothing
                    # outside the file can refer to it.
                    local=parsed.is_local,
                )
            )
            scopes.append((body, occ.symbol))
        definition_scopes[doc.path] = scopes

    # Pass two: non-definition occurrences become edges from whichever symbol
    # encloses the reference site. Occurrences whose target has no definition
    # in this index point outside the project and are dropped, matching the
    # "filter non-repo targets" rule from RepoGraph.
    for doc in documents:
        scopes = definition_scopes.get(doc.path, [])
        module_id: str | None = None
        for occ in doc.occurrences:
            if occ.is_definition or not occ.symbol:
                continue
            if occ.symbol not in snapshot.symbols:
                continue
            source_id = _enclosing_symbol_id(scopes, occ.span)
            if source_id is None:
                # A top-level reference, typically an import statement. Hang
                # it off a synthetic module symbol so the edge survives with
                # an honest source rather than being dropped.
                if module_id is None:
                    module_id = _add_module_symbol(snapshot, doc)
                source_id = module_id
            if source_id == occ.symbol:
                continue
            edge_kind = EdgeKind.IMPORTS if occ.is_import else EdgeKind.REFERENCES
            snapshot.add_edge(
                Edge(
                    src_id=source_id,
                    dst_id=occ.symbol,
                    kind=edge_kind,
                    tier=ResolutionTier.ORACLE,
                    site_path=doc.path,
                    site_range=occ.span,
                )
            )

    # Pass three: declared relationships give inheritance and implementation
    # edges that no occurrence carries.
    for doc in documents:
        for symbol_id, info in doc.symbol_info.items():
            if symbol_id not in snapshot.symbols:
                continue
            for rel in info.get("relationships", []):
                target = rel.get("symbol")
                if not target or target not in snapshot.symbols:
                    continue
                if rel.get("isImplementation"):
                    edge_kind = EdgeKind.IMPLEMENTS
                elif rel.get("isTypeDefinition"):
                    edge_kind = EdgeKind.USES_TYPE
                elif rel.get("isReference"):
                    edge_kind = EdgeKind.REFERENCES
                else:
                    continue
                snapshot.add_edge(
                    Edge(
                        src_id=symbol_id,
                        dst_id=target,
                        kind=edge_kind,
                        tier=ResolutionTier.ORACLE,
                    )
                )
    return snapshot


def cross_check(binary: Path | str, json_dump: Path | str) -> list[str]:
    """Compare both readings of one index, returning human-readable differences.

    Run this once per indexer version before trusting the binary reader:
    ``scip print --json index.scip > index.json`` then cross-check. An empty
    result means this module's field numbers agree with the SCIP CLI.
    """
    from_binary = read_scip_binary(Path(binary))
    from_json = read_scip_json(Path(json_dump))
    problems: list[str] = []

    if from_binary.encoding is not from_json.encoding:
        problems.append(
            f"encoding differs: binary={from_binary.encoding.value} json={from_json.encoding.value}"
        )

    binary_symbols = set(from_binary.symbols)
    json_symbols = set(from_json.symbols)
    only_binary = binary_symbols - json_symbols
    only_json = json_symbols - binary_symbols
    if only_binary:
        problems.append(f"{len(only_binary)} symbols only in binary, e.g. {sorted(only_binary)[:3]}")
    if only_json:
        problems.append(f"{len(only_json)} symbols only in json, e.g. {sorted(only_json)[:3]}")

    for symbol_id in sorted(binary_symbols & json_symbols):
        left, right = from_binary.symbols[symbol_id], from_json.symbols[symbol_id]
        if left.path != right.path or left.name_range != right.name_range:
            problems.append(
                f"{symbol_id}: binary at {left.path}:{left.name_range} "
                f"but json at {right.path}:{right.name_range}"
            )
            if len(problems) > 50:
                problems.append("... further differences suppressed")
                return problems

    def edge_key(edge: Edge) -> tuple[str, str, str, str, str]:
        return (
            edge.src_id,
            edge.dst_id,
            edge.kind.value,
            edge.site_path or "",
            str(edge.site_range or ""),
        )

    binary_edges = {edge_key(e) for e in from_binary.edges}
    json_edges = {edge_key(e) for e in from_json.edges}
    if binary_edges != json_edges:
        problems.append(
            f"edge sets differ: {len(binary_edges - json_edges)} only in binary, "
            f"{len(json_edges - binary_edges)} only in json"
        )
    return problems
