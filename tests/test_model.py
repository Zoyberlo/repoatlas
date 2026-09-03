"""Tests for the core data model.

The model is where invariants are cheap to enforce and expensive to skip: a
symbol whose identifier sits outside its own body, or an edge claiming a
confidence of 1.4, corrupts every number downstream.
"""

from __future__ import annotations

import pytest

from repoatlas.model import (
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


class TestPosition:
    def test_orders_by_line_then_character(self) -> None:
        assert Position(1, 5) < Position(2, 0)
        assert Position(1, 5) < Position(1, 6)

    def test_rejects_negative_coordinates(self) -> None:
        with pytest.raises(ValueError, match="negative position"):
            Position(-1, 0)


class TestSourceRange:
    def test_rejects_an_inverted_range(self) -> None:
        with pytest.raises(ValueError, match="inverted range"):
            SourceRange(Position(2, 0), Position(1, 0))

    @pytest.mark.parametrize(
        ("values", "expected"),
        [([3, 4, 9], [3, 4, 9]), ([1, 0, 4, 1], [1, 0, 4, 1])],
    )
    def test_round_trips_the_scip_compact_form(
        self, values: list[int], expected: list[int]
    ) -> None:
        assert SourceRange.from_scip(values).to_scip() == expected

    def test_expands_the_three_element_form_to_one_line(self) -> None:
        span = SourceRange.from_scip([7, 2, 8])
        assert span.start == Position(7, 2)
        assert span.end == Position(7, 8)
        assert span.is_single_line

    @pytest.mark.parametrize("values", [[], [1], [1, 2], [1, 2, 3, 4, 5]])
    def test_rejects_a_malformed_scip_range(self, values: list[int]) -> None:
        with pytest.raises(ValueError, match="3 or 4 elements"):
            SourceRange.from_scip(values)

    def test_detects_overlap_on_the_same_line(self) -> None:
        assert SourceRange.of(0, 0, 0, 5).overlaps(SourceRange.of(0, 4, 0, 9))
        assert not SourceRange.of(0, 0, 0, 5).overlaps(SourceRange.of(0, 5, 0, 9))

    def test_treats_a_zero_width_range_as_touching(self) -> None:
        caret = SourceRange.of(2, 3, 2, 3)
        assert caret.overlaps(SourceRange.of(2, 0, 2, 6))
        assert SourceRange.of(2, 0, 2, 6).overlaps(caret)

    def test_containment_is_not_symmetric(self) -> None:
        outer = SourceRange.of(0, 0, 9, 0)
        inner = SourceRange.of(1, 2, 1, 8)
        assert outer.contains(inner)
        assert not inner.contains(outer)


class TestSymbol:
    def test_rejects_an_identifier_outside_its_body(self) -> None:
        with pytest.raises(ValueError, match="escapes full_range"):
            Symbol(
                id="s",
                name="s",
                kind=SymbolKind.FUNCTION,
                path="a.py",
                name_range=SourceRange.of(9, 0, 9, 3),
                full_range=SourceRange.of(0, 0, 3, 0),
            )

    def test_rejects_an_empty_id(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            Symbol(
                id="",
                name="s",
                kind=SymbolKind.FUNCTION,
                path="a.py",
                name_range=SourceRange.of(0, 0, 0, 1),
            )

    def test_rejects_a_missing_path(self) -> None:
        with pytest.raises(ValueError, match="no path"):
            Symbol(
                id="s",
                name="s",
                kind=SymbolKind.FUNCTION,
                path="",
                name_range=SourceRange.of(0, 0, 0, 1),
            )

    def test_display_uses_one_based_lines_for_humans(self) -> None:
        symbol = Symbol(
            id="s",
            name="run",
            kind=SymbolKind.FUNCTION,
            path="a.py",
            name_range=SourceRange.of(4, 4, 4, 7),
        )
        assert symbol.display == "a.py:5:run"


class TestKinds:
    def test_callable_kinds_are_grouped(self) -> None:
        assert SymbolKind.METHOD.is_callable
        assert SymbolKind.CONSTRUCTOR.is_callable
        assert not SymbolKind.FIELD.is_callable

    def test_type_like_kinds_are_grouped(self) -> None:
        assert SymbolKind.INTERFACE.is_type_like
        assert not SymbolKind.FUNCTION.is_type_like

    def test_containment_is_the_only_structural_edge(self) -> None:
        structural = [kind for kind in EdgeKind if kind.is_structural]
        assert structural == [EdgeKind.CONTAINS]

    def test_resolved_edges_are_the_ones_needing_name_resolution(self) -> None:
        assert EdgeKind.CALLS.is_resolved
        assert EdgeKind.IMPORTS.is_resolved
        assert not EdgeKind.CONTAINS.is_resolved
        assert not EdgeKind.CO_CHANGED.is_resolved


class TestResolutionTier:
    def test_confidence_decreases_down_the_cascade(self) -> None:
        ladder = [
            ResolutionTier.ORACLE,
            ResolutionTier.IMPORT_MAP,
            ResolutionTier.SAME_MODULE,
            ResolutionTier.IMPORT_SUFFIX,
            ResolutionTier.UNIQUE_NAME,
            ResolutionTier.SUFFIX,
            ResolutionTier.FUZZY,
        ]
        values = [tier.default_confidence for tier in ladder]
        assert values == sorted(values, reverse=True)

    def test_looks_up_a_tier_by_label(self) -> None:
        assert ResolutionTier.from_label("fuzzy") is ResolutionTier.FUZZY

    def test_rejects_an_unknown_label(self) -> None:
        with pytest.raises(ValueError, match="unknown resolution tier"):
            ResolutionTier.from_label("vibes")


class TestEdge:
    def test_takes_its_confidence_from_its_tier(self) -> None:
        edge = Edge("a", "b", EdgeKind.CALLS, ResolutionTier.SUFFIX)
        assert edge.score == pytest.approx(0.55)

    def test_an_explicit_confidence_wins(self) -> None:
        edge = Edge("a", "b", EdgeKind.CALLS, ResolutionTier.SUFFIX, confidence=0.9)
        assert edge.score == pytest.approx(0.9)

    @pytest.mark.parametrize("value", [-0.1, 1.5])
    def test_rejects_confidence_outside_the_unit_interval(self, value: float) -> None:
        with pytest.raises(ValueError, match="confidence out of range"):
            Edge("a", "b", EdgeKind.CALLS, confidence=value)

    def test_requires_a_site_path_and_range_together(self) -> None:
        with pytest.raises(ValueError, match="must be given together"):
            Edge("a", "b", EdgeKind.CALLS, site_range=SourceRange.of(0, 0, 0, 1))


class TestIndexSnapshot:
    @staticmethod
    def _symbol(symbol_id: str, path: str = "a.py", line: int = 0) -> Symbol:
        return Symbol(
            id=symbol_id,
            name=symbol_id,
            kind=SymbolKind.FUNCTION,
            path=path,
            name_range=SourceRange.of(line, 0, line, len(symbol_id)),
        )

    def test_adding_the_same_symbol_twice_is_idempotent(self) -> None:
        snapshot = IndexSnapshot()
        symbol = self._symbol("f")
        snapshot.add_symbol(symbol)
        snapshot.add_symbol(symbol)
        assert len(snapshot) == 1

    def test_rejects_two_different_symbols_with_one_id(self) -> None:
        snapshot = IndexSnapshot()
        snapshot.add_symbol(self._symbol("f", line=0))
        with pytest.raises(ValueError, match="duplicate symbol id"):
            snapshot.add_symbol(self._symbol("f", line=5))

    def test_lists_symbols_of_a_file_in_source_order(self) -> None:
        snapshot = IndexSnapshot()
        snapshot.add_symbol(self._symbol("late", line=9))
        snapshot.add_symbol(self._symbol("early", line=1))
        snapshot.add_symbol(self._symbol("other", path="b.py", line=0))
        assert [s.id for s in snapshot.symbols_in("a.py")] == ["early", "late"]

    def test_paths_include_edge_sites_in_other_files(self) -> None:
        snapshot = IndexSnapshot()
        snapshot.add_symbol(self._symbol("f"))
        snapshot.add_edge(
            Edge(
                "f",
                "f",
                EdgeKind.CALLS,
                site_path="caller.py",
                site_range=SourceRange.of(0, 0, 0, 1),
            )
        )
        assert snapshot.paths == {"a.py", "caller.py"}

    def test_summary_counts_edges_by_kind(self) -> None:
        snapshot = IndexSnapshot()
        snapshot.add_symbol(self._symbol("f"))
        snapshot.add_edge(Edge("f", "f", EdgeKind.CALLS))
        snapshot.add_edge(Edge("f", "f", EdgeKind.CALLS))
        snapshot.add_edge(Edge("f", "f", EdgeKind.IMPORTS))
        summary = snapshot.summary()
        assert summary["edges"] == 3
        assert summary["edges.calls"] == 2
        assert summary["edges.imports"] == 1
        assert "edges.inherits" not in summary


class TestPositionEncoding:
    @pytest.mark.parametrize("value", ["UTF8", "utf-8", "utf8"])
    def test_coerces_common_spellings(self, value: str) -> None:
        assert PositionEncoding.coerce(value) is PositionEncoding.UTF8

    def test_passes_through_an_enum_member(self) -> None:
        assert PositionEncoding.coerce(PositionEncoding.UTF16) is PositionEncoding.UTF16

    def test_rejects_nonsense(self) -> None:
        with pytest.raises(ValueError, match="unknown position encoding"):
            PositionEncoding.coerce("ebcdic")
