"""Tests for the SCIP reader.

Every structural assertion runs against both input forms. The binary reader
encodes this module's belief about SCIP field numbers; the JSON reader does
not. When they agree on a fixture built independently for each, the field
numbers are corroborated.
"""

from __future__ import annotations

import json

import pytest

from repoatlas.model import EdgeKind, IndexSnapshot, PositionEncoding, SymbolKind
from repoatlas.oracle import protobuf as pb
from repoatlas.oracle.scip import (
    ScipError,
    SymbolRole,
    parse_symbol,
    read_scip,
    read_scip_binary,
    read_scip_json,
)

from .conftest import (
    PACKAGE,
    SYM_GREET,
    SYM_MAIN,
    SYM_USER,
    IndexSpec,
    OccurrenceSpec,
    SymbolSpec,
)


@pytest.fixture(params=["binary", "json"])
def demo_snapshot(request: pytest.FixtureRequest, demo_index: IndexSpec) -> IndexSnapshot:
    """The demo index, read through whichever reader the parameter names."""
    if request.param == "binary":
        return read_scip_binary(demo_index.to_binary())
    return read_scip_json(demo_index.to_json())


class TestSymbolParsing:
    def test_parses_a_method_symbol(self) -> None:
        parsed = parse_symbol(SYM_GREET)
        assert not parsed.is_local
        assert parsed.scheme == "scip-typescript"
        assert parsed.package_manager == "npm"
        assert parsed.package_name == "demo"
        assert parsed.version == "1.0.0"
        assert parsed.descriptors == ("src/", "`user.ts`/", "User#", "greet().")
        assert parsed.name == "greet"
        assert parsed.kind is SymbolKind.METHOD

    def test_parses_a_type_symbol(self) -> None:
        parsed = parse_symbol(SYM_USER)
        assert parsed.name == "User"
        assert parsed.kind is SymbolKind.CLASS
        assert parsed.qualified_name == "src/`user.ts`/User#"

    def test_unescapes_a_backticked_path_segment(self) -> None:
        parsed = parse_symbol(f"{PACKAGE} src/`user.ts`/")
        assert parsed.name == "user.ts"

    def test_recognises_local_symbols(self) -> None:
        parsed = parse_symbol("local 12")
        assert parsed.is_local
        assert parsed.raw == "local 12"

    def test_treats_a_term_inside_a_type_as_a_field(self) -> None:
        parsed = parse_symbol("scip-java maven org.example 1.0 com/example/User#name.")
        assert parsed.kind is SymbolKind.FIELD
        assert parsed.name == "name"

    def test_treats_a_top_level_term_as_a_variable(self) -> None:
        parsed = parse_symbol("scip-python pip demo 1.0 settings/DEBUG.")
        assert parsed.kind is SymbolKind.VARIABLE

    def test_strips_a_method_disambiguator(self) -> None:
        parsed = parse_symbol("scip-java maven org.example 1.0 com/A#overloaded(+1).")
        assert parsed.name == "overloaded"
        assert parsed.kind is SymbolKind.METHOD

    def test_unescapes_backtick_quoted_names(self) -> None:
        parsed = parse_symbol("scip-php composer demo 1.0 `My Space`/thing.")
        assert parsed.descriptors[0] == "`My Space`/"
        assert parsed.name == "thing"

    def test_rejects_an_empty_symbol(self) -> None:
        with pytest.raises(ScipError, match="empty symbol"):
            parse_symbol("")

    def test_tolerates_a_truncated_header(self) -> None:
        parsed = parse_symbol("weird symbol")
        assert parsed.raw == "weird symbol"
        assert parsed.descriptors == ()


class TestReadingDefinitions:
    def test_finds_every_definition(self, demo_snapshot: IndexSnapshot) -> None:
        real = {sid for sid, s in demo_snapshot.symbols.items() if not s.synthetic}
        assert real == {SYM_USER, SYM_GREET, SYM_MAIN}

    def test_anchors_a_symbol_on_its_identifier(self, demo_snapshot: IndexSnapshot) -> None:
        user = demo_snapshot.symbols[SYM_USER]
        assert user.path == "src/user.ts"
        assert user.name_range.to_scip() == [0, 13, 17]
        assert user.name == "User"

    def test_uses_the_enclosing_range_as_the_body(self, demo_snapshot: IndexSnapshot) -> None:
        user = demo_snapshot.symbols[SYM_USER]
        assert user.full_range is not None
        assert user.full_range.to_scip() == [0, 0, 4, 1]

    def test_carries_documentation_through(self, demo_snapshot: IndexSnapshot) -> None:
        assert "class User" in (demo_snapshot.symbols[SYM_USER].documentation or "")

    def test_records_the_language(self, demo_snapshot: IndexSnapshot) -> None:
        assert demo_snapshot.symbols[SYM_MAIN].language == "TypeScript"


class TestReadingEdges:
    def test_attributes_a_reference_to_its_enclosing_function(
        self, demo_snapshot: IndexSnapshot
    ) -> None:
        calls = [
            e
            for e in demo_snapshot.edges
            if e.src_id == SYM_MAIN and e.dst_id == SYM_GREET
        ]
        assert len(calls) == 1
        assert calls[0].kind is EdgeKind.REFERENCES
        assert calls[0].site_path == "src/app.ts"
        assert calls[0].site_range is not None
        assert calls[0].site_range.to_scip() == [3, 4, 9]

    def test_marks_import_occurrences_as_import_edges(
        self, demo_snapshot: IndexSnapshot
    ) -> None:
        imports = [e for e in demo_snapshot.edges if e.kind is EdgeKind.IMPORTS]
        assert len(imports) == 1
        assert imports[0].dst_id == SYM_USER
        assert imports[0].site_range is not None
        assert imports[0].site_range.to_scip() == [0, 9, 13]

    def test_hangs_a_top_level_reference_off_a_synthetic_module(
        self, demo_snapshot: IndexSnapshot
    ) -> None:
        imports = [e for e in demo_snapshot.edges if e.kind is EdgeKind.IMPORTS]
        source = demo_snapshot.symbols[imports[0].src_id]
        assert source.synthetic
        assert source.kind is SymbolKind.MODULE
        assert source.path == "src/app.ts"

    def test_does_not_emit_an_edge_for_a_definition(
        self, demo_snapshot: IndexSnapshot
    ) -> None:
        # `User` is defined once and used twice; only the uses become edges.
        to_user = [e for e in demo_snapshot.edges if e.dst_id == SYM_USER]
        assert len(to_user) == 2
        assert {e.kind for e in to_user} == {EdgeKind.IMPORTS, EdgeKind.REFERENCES}

    def test_oracle_edges_carry_full_confidence(
        self, demo_snapshot: IndexSnapshot
    ) -> None:
        assert all(edge.score == 1.0 for edge in demo_snapshot.edges)

    def test_drops_references_to_symbols_defined_outside_the_index(
        self, demo_index: IndexSpec
    ) -> None:
        external = "scip-typescript npm @types/node 1.0 process."
        demo_index.documents[1].occurrences.append(
            OccurrenceSpec(external, [2, 2, 9])
        )
        snapshot = read_scip_binary(demo_index.to_binary())
        assert all(edge.dst_id != external for edge in snapshot.edges)


class TestRelationships:
    @staticmethod
    def _with_interface(demo_index: IndexSpec) -> str:
        """Add a `Greeter` interface that `User` declares it implements."""
        base = "scip-typescript npm demo 1.0.0 src/user.ts/Greeter#"
        user_doc = demo_index.documents[0]
        user_doc.occurrences.append(
            OccurrenceSpec(base, [0, 30, 37], SymbolRole.DEFINITION, [0, 25, 0, 40])
        )
        user_doc.symbols.append(SymbolSpec(base, "Greeter"))
        user_doc.symbols[0].relationships = [{"symbol": base, "isImplementation": True}]
        return base

    def test_reads_implementation_relationships(self, demo_index: IndexSpec) -> None:
        base = self._with_interface(demo_index)
        snapshot = read_scip_json(demo_index.to_json())
        implements = [e for e in snapshot.edges if e.kind is EdgeKind.IMPLEMENTS]
        assert len(implements) == 1
        assert implements[0].src_id == SYM_USER
        assert implements[0].dst_id == base

    def test_relationship_edges_agree_across_readers(self, demo_index: IndexSpec) -> None:
        self._with_interface(demo_index)

        def implements(snapshot: IndexSnapshot) -> set[tuple[str, str]]:
            return {
                (e.src_id, e.dst_id)
                for e in snapshot.edges
                if e.kind is EdgeKind.IMPLEMENTS
            }

        assert implements(read_scip_binary(demo_index.to_binary())) == implements(
            read_scip_json(demo_index.to_json())
        )

    def test_ignores_relationships_pointing_outside_the_index(
        self, demo_index: IndexSpec
    ) -> None:
        demo_index.documents[0].symbols[0].relationships = [
            {"symbol": "scip-typescript npm elsewhere 1.0 Unknown#", "isImplementation": True}
        ]
        snapshot = read_scip_json(demo_index.to_json())
        assert not [e for e in snapshot.edges if e.kind is EdgeKind.IMPLEMENTS]


class TestReaderAgreement:
    def test_both_readers_produce_the_same_symbols(self, demo_index: IndexSpec) -> None:
        from_binary = read_scip_binary(demo_index.to_binary())
        from_json = read_scip_json(demo_index.to_json())
        assert set(from_binary.symbols) == set(from_json.symbols)
        for symbol_id, left in from_binary.symbols.items():
            right = from_json.symbols[symbol_id]
            assert (left.path, left.name_range) == (right.path, right.name_range)
            assert left.kind is right.kind

    def test_both_readers_produce_the_same_edges(self, demo_index: IndexSpec) -> None:
        def keys(snapshot: IndexSnapshot) -> set[tuple[str, str, str, str]]:
            return {
                (e.src_id, e.dst_id, e.kind.value, str(e.site_range)) for e in snapshot.edges
            }

        assert keys(read_scip_binary(demo_index.to_binary())) == keys(
            read_scip_json(demo_index.to_json())
        )

    def test_both_readers_agree_on_encoding(self, demo_index: IndexSpec) -> None:
        assert (
            read_scip_binary(demo_index.to_binary()).encoding
            is read_scip_json(demo_index.to_json()).encoding
            is PositionEncoding.UTF8
        )


class TestJsonTolerance:
    def test_accepts_snake_case_keys(self, demo_index: IndexSpec) -> None:
        payload = json.loads(demo_index.to_json())
        for document in payload["documents"]:
            document["relative_path"] = document.pop("relativePath")
            for occurrence in document["occurrences"]:
                if "symbolRoles" in occurrence:
                    occurrence["symbol_roles"] = occurrence.pop("symbolRoles")
                if "enclosingRange" in occurrence:
                    occurrence["enclosing_range"] = occurrence.pop("enclosingRange")
        snapshot = read_scip_json(json.dumps(payload))
        assert SYM_USER in snapshot.symbols

    def test_accepts_named_symbol_roles(self, demo_index: IndexSpec) -> None:
        payload = json.loads(demo_index.to_json())
        for document in payload["documents"]:
            for occurrence in document["occurrences"]:
                if occurrence.get("symbolRoles") == 1:
                    occurrence["symbolRoles"] = "Definition"
                elif occurrence.get("symbolRoles") == 2:
                    occurrence["symbolRoles"] = "Import"
        snapshot = read_scip_json(json.dumps(payload))
        real = {sid for sid, s in snapshot.symbols.items() if not s.synthetic}
        assert real == {SYM_USER, SYM_GREET, SYM_MAIN}

    def test_rejects_a_non_object_payload(self) -> None:
        with pytest.raises(ScipError, match="object at the top level"):
            read_scip_json("[]")

    def test_rejects_a_document_without_a_path(self) -> None:
        with pytest.raises(ScipError, match="relativePath"):
            read_scip_json(json.dumps({"documents": [{"occurrences": []}]}))


class TestReadScipDispatch:
    def test_sniffs_json_content_regardless_of_extension(
        self, tmp_path, demo_index: IndexSpec
    ) -> None:
        path = tmp_path / "index.scip"
        path.write_text(demo_index.to_json(), encoding="utf-8")
        assert SYM_USER in read_scip(path).symbols

    def test_reads_binary_by_extension(self, tmp_path, demo_index: IndexSpec) -> None:
        path = tmp_path / "index.scip"
        path.write_bytes(demo_index.to_binary())
        assert SYM_USER in read_scip(path).symbols

    def test_reports_a_corrupt_index_clearly(self, tmp_path) -> None:
        path = tmp_path / "index.scip"
        path.write_bytes(b"\x0a\xff\xff\xff")
        with pytest.raises(ScipError, match="not a valid SCIP index"):
            read_scip(path)


class TestSnapshotSummary:
    def test_summarises_counts_by_edge_kind(self, demo_snapshot: IndexSnapshot) -> None:
        summary = demo_snapshot.summary()
        assert summary["files"] == 2
        assert summary["edges.imports"] == 1
        assert summary["edges.references"] == 2

    def test_records_the_producing_tool(self, demo_snapshot: IndexSnapshot) -> None:
        assert "scip-typescript" in (demo_snapshot.producer or "")


def test_encoder_and_decoder_round_trip_a_document(demo_index: IndexSpec) -> None:
    """Guards the fixture builder itself, which the reader tests depend on."""
    raw = demo_index.to_binary()
    fields = pb.decode_message(raw)
    documents = list(pb.submessages(fields, 2))
    assert len(documents) == 2
    assert pb.as_str(documents[0], 1) == "src/user.ts"
