"""Shared fixtures.

The SCIP fixture is written twice, once as binary protobuf and once as the
JSON that ``scip print --json`` would produce for the same index. Every
reader test runs against both, so a field-number mistake in the binary path
shows up as a disagreement rather than as quietly missing edges.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from repoatlas.oracle import protobuf as pb
from repoatlas.oracle.scip import Field, SymbolRole

# A tiny two-file TypeScript project:
#
#   src/user.ts
#     0 | export class User {
#     1 |   greet(): string {
#     2 |     return "hi";
#     3 |   }
#     4 | }
#
#   src/app.ts
#     0 | import { User } from "./user";
#     1 | function main() {
#     2 |   const u = new User();
#     3 |   u.greet();
#     4 | }
#
# Column numbers below are the real ones for that source, so the fixture
# stays meaningful if someone re-derives it with a real indexer.

# Path segments containing a dot must be backtick-escaped: an unescaped dot
# is the descriptor separator for a term, so `user.ts/` would parse as two
# descriptors. Real indexers emit the escaped form and so does this fixture.
PACKAGE = "scip-typescript npm demo 1.0.0"
SYM_USER = f"{PACKAGE} src/`user.ts`/User#"
SYM_GREET = f"{PACKAGE} src/`user.ts`/User#greet()."
SYM_MAIN = f"{PACKAGE} src/`app.ts`/main()."


@dataclass(slots=True)
class OccurrenceSpec:
    symbol: str
    span: list[int]
    roles: int = 0
    enclosing: list[int] | None = None


@dataclass(slots=True)
class SymbolSpec:
    symbol: str
    display_name: str = ""
    documentation: list[str] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class DocumentSpec:
    path: str
    language: str = "TypeScript"
    occurrences: list[OccurrenceSpec] = field(default_factory=list)
    symbols: list[SymbolSpec] = field(default_factory=list)


@dataclass(slots=True)
class IndexSpec:
    documents: list[DocumentSpec]
    project_root: str = "file:///demo"
    tool_name: str = "scip-typescript"
    tool_version: str = "0.4.0"
    text_encoding: int = 1  # UTF-8

    def to_binary(self) -> bytes:
        tool = pb.encode_message(
            [(Field.TOOL_NAME, self.tool_name), (Field.TOOL_VERSION, self.tool_version)]
        )
        metadata = (
            pb.encode_field(Field.META_VERSION, 0)
            + pb.encode_field(Field.META_TOOL_INFO, tool)
            + pb.encode_field(Field.META_PROJECT_ROOT, self.project_root)
            + pb.encode_field(Field.META_TEXT_ENCODING, self.text_encoding)
        )
        payload = pb.encode_field(Field.INDEX_METADATA, metadata)
        for doc in self.documents:
            body = pb.encode_field(Field.DOC_RELATIVE_PATH, doc.path)
            body += pb.encode_field(Field.DOC_LANGUAGE, doc.language)
            for occ in doc.occurrences:
                encoded = pb.encode_packed(Field.OCC_RANGE, occ.span)
                encoded += pb.encode_field(Field.OCC_SYMBOL, occ.symbol)
                if occ.roles:
                    encoded += pb.encode_field(Field.OCC_SYMBOL_ROLES, occ.roles)
                if occ.enclosing:
                    encoded += pb.encode_packed(Field.OCC_ENCLOSING_RANGE, occ.enclosing)
                body += pb.encode_field(Field.DOC_OCCURRENCES, encoded)
            for sym in doc.symbols:
                encoded = pb.encode_field(Field.SYM_SYMBOL, sym.symbol)
                for line in sym.documentation:
                    encoded += pb.encode_field(Field.SYM_DOCUMENTATION, line)
                for rel in sym.relationships:
                    rel_body = pb.encode_field(Field.REL_SYMBOL, rel["symbol"])
                    if rel.get("isReference"):
                        rel_body += pb.encode_field(Field.REL_IS_REFERENCE, 1)
                    if rel.get("isImplementation"):
                        rel_body += pb.encode_field(Field.REL_IS_IMPLEMENTATION, 1)
                    if rel.get("isTypeDefinition"):
                        rel_body += pb.encode_field(Field.REL_IS_TYPE_DEFINITION, 1)
                    if rel.get("isDefinition"):
                        rel_body += pb.encode_field(Field.REL_IS_DEFINITION, 1)
                    encoded += pb.encode_field(Field.SYM_RELATIONSHIPS, rel_body)
                if sym.display_name:
                    encoded += pb.encode_field(Field.SYM_DISPLAY_NAME, sym.display_name)
                body += pb.encode_field(Field.DOC_SYMBOLS, encoded)
            payload += pb.encode_field(Field.INDEX_DOCUMENTS, body)
        return payload

    def to_json(self) -> str:
        return json.dumps(
            {
                "metadata": {
                    "version": "UnspecifiedProtocolVersion",
                    "toolInfo": {"name": self.tool_name, "version": self.tool_version},
                    "projectRoot": self.project_root,
                    "textDocumentEncoding": "UTF8",
                },
                "documents": [
                    {
                        "language": doc.language,
                        "relativePath": doc.path,
                        "occurrences": [
                            {
                                key: value
                                for key, value in (
                                    ("range", occ.span),
                                    ("symbol", occ.symbol),
                                    ("symbolRoles", occ.roles),
                                    ("enclosingRange", occ.enclosing),
                                )
                                # protojson omits zero values, so the JSON
                                # fixture must omit them too.
                                if value
                            }
                            for occ in doc.occurrences
                        ],
                        "symbols": [
                            {
                                key: value
                                for key, value in (
                                    ("symbol", sym.symbol),
                                    ("displayName", sym.display_name),
                                    ("documentation", sym.documentation),
                                    ("relationships", sym.relationships),
                                )
                                if value
                            }
                            for sym in doc.symbols
                        ],
                    }
                    for doc in self.documents
                ],
            },
            indent=2,
        )


def build_demo_index() -> IndexSpec:
    """The two-file project described at the top of this module."""
    user_doc = DocumentSpec(
        path="src/user.ts",
        occurrences=[
            OccurrenceSpec(SYM_USER, [0, 13, 17], SymbolRole.DEFINITION, [0, 0, 4, 1]),
            OccurrenceSpec(SYM_GREET, [1, 2, 7], SymbolRole.DEFINITION, [1, 2, 3, 3]),
        ],
        symbols=[
            SymbolSpec(SYM_USER, "User", ["```ts\nclass User\n```"]),
            SymbolSpec(SYM_GREET, "greet"),
        ],
    )
    app_doc = DocumentSpec(
        path="src/app.ts",
        occurrences=[
            OccurrenceSpec(SYM_USER, [0, 9, 13], SymbolRole.IMPORT),
            OccurrenceSpec(SYM_MAIN, [1, 9, 13], SymbolRole.DEFINITION, [1, 0, 4, 1]),
            OccurrenceSpec(SYM_USER, [2, 16, 20], SymbolRole.READ_ACCESS),
            OccurrenceSpec(SYM_GREET, [3, 4, 9], SymbolRole.READ_ACCESS),
        ],
        symbols=[SymbolSpec(SYM_MAIN, "main")],
    )
    return IndexSpec(documents=[user_doc, app_doc])


@pytest.fixture
def demo_index() -> IndexSpec:
    return build_demo_index()


@pytest.fixture
def demo_binary(demo_index: IndexSpec) -> bytes:
    return demo_index.to_binary()


@pytest.fixture
def demo_json(demo_index: IndexSpec) -> str:
    return demo_index.to_json()
