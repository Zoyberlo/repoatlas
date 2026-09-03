"""Tests for projecting an index into location-keyed facts and matching them.

The projection is what makes two indexes comparable at all, so its edge
cases matter more than they look: a producer that anchors symbols on the
whole declaration instead of the identifier, or that emits Windows path
separators, would otherwise score zero for reasons that have nothing to do
with how well it understands the code.
"""

from __future__ import annotations

import pytest

from repoatlas.eval.facts import (
    DefFact,
    FactSet,
    RefFact,
    definition_facts,
    match_facts,
    normalise_path,
    reference_facts,
)
from repoatlas.model import (
    Edge,
    EdgeKind,
    IndexSnapshot,
    ResolutionTier,
    SourceRange,
    Symbol,
    SymbolKind,
)


def make_symbol(
    symbol_id: str,
    path: str = "src/a.py",
    line: int = 0,
    start: int = 0,
    kind: SymbolKind = SymbolKind.FUNCTION,
    synthetic: bool = False,
) -> Symbol:
    return Symbol(
        id=symbol_id,
        name=symbol_id,
        kind=kind,
        path=path,
        name_range=SourceRange.of(line, start, line, start + len(symbol_id)),
        synthetic=synthetic,
    )


def snapshot_with(*symbols: Symbol) -> IndexSnapshot:
    snapshot = IndexSnapshot()
    for symbol in symbols:
        snapshot.add_symbol(symbol)
    return snapshot


class TestNormalisePath:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("src\\a.py", "src/a.py"),
            ("./src/a.py", "src/a.py"),
            (".//src/a.py", "src/a.py"),
            ("/src/a.py", "src/a.py"),
            ("  src/a.py  ", "src/a.py"),
        ],
    )
    def test_normalises_separators_and_prefixes(self, raw: str, expected: str) -> None:
        assert normalise_path(raw) == expected

    def test_keeps_case_by_default_because_git_is_case_sensitive(self) -> None:
        assert normalise_path("Src/A.py") == "Src/A.py"

    def test_folds_case_on_request(self) -> None:
        assert normalise_path("Src/A.py", case_fold=True) == "src/a.py"


class TestDefinitionFacts:
    def test_projects_each_symbol_once(self) -> None:
        snapshot = snapshot_with(make_symbol("f"), make_symbol("g", line=3))
        assert len(definition_facts(snapshot)) == 2

    def test_omits_synthetic_module_symbols(self) -> None:
        snapshot = snapshot_with(
            make_symbol("f"), make_symbol("mod", synthetic=True, kind=SymbolKind.MODULE)
        )
        assert [fact.line for fact in definition_facts(snapshot)] == [0]

    def test_ignores_kind_unless_asked_to_compare_it(self) -> None:
        snapshot = snapshot_with(make_symbol("f"))
        assert definition_facts(snapshot)[0].kind == ""
        assert definition_facts(snapshot, include_kind=True)[0].kind == "function"

    def test_restricts_to_the_requested_paths(self) -> None:
        snapshot = snapshot_with(
            make_symbol("f", path="src/a.py"), make_symbol("g", path="src/b.py")
        )
        facts = definition_facts(snapshot, paths={"src/a.py"})
        assert [fact.path for fact in facts] == ["src/a.py"]


class TestReferenceFacts:
    def test_projects_an_edge_with_a_site(self) -> None:
        snapshot = snapshot_with(make_symbol("f"), make_symbol("g", line=5))
        snapshot.add_edge(
            Edge(
                "f",
                "g",
                EdgeKind.CALLS,
                site_path="src/a.py",
                site_range=SourceRange.of(2, 4, 2, 5),
            )
        )
        facts, unprojectable = reference_facts(snapshot)
        assert not unprojectable
        assert facts[0].site_span.to_scip() == [2, 4, 5]
        assert facts[0].target_span.to_scip() == [5, 0, 1]

    def test_reports_a_dangling_edge_instead_of_dropping_it(self) -> None:
        snapshot = snapshot_with(make_symbol("f"))
        snapshot.add_edge(
            Edge(
                "f",
                "missing",
                EdgeKind.CALLS,
                site_path="src/a.py",
                site_range=SourceRange.of(1, 0, 1, 1),
            )
        )
        facts, unprojectable = reference_facts(snapshot)
        assert not facts
        assert len(unprojectable) == 1

    def test_anchors_a_siteless_edge_on_its_source_symbol(self) -> None:
        snapshot = snapshot_with(make_symbol("Child", line=1), make_symbol("Base", line=7))
        snapshot.add_edge(Edge("Child", "Base", EdgeKind.INHERITS))
        facts, unprojectable = reference_facts(snapshot, require_site=False)
        assert not unprojectable
        assert facts[0].site_span.to_scip() == [1, 0, 5]

    def test_rejects_a_siteless_edge_when_a_site_is_required(self) -> None:
        snapshot = snapshot_with(make_symbol("Child"), make_symbol("Base", line=7))
        snapshot.add_edge(Edge("Child", "Base", EdgeKind.INHERITS))
        facts, unprojectable = reference_facts(snapshot, require_site=True)
        assert not facts
        assert len(unprojectable) == 1

    def test_collapses_reference_like_kinds_by_default(self) -> None:
        snapshot = snapshot_with(make_symbol("f"), make_symbol("g", line=5))
        for kind in (EdgeKind.CALLS, EdgeKind.REFERENCES, EdgeKind.USES_TYPE):
            snapshot.add_edge(
                Edge(
                    "f",
                    "g",
                    kind,
                    site_path="src/a.py",
                    site_range=SourceRange.of(2, 0, 2, 1),
                )
            )
        facts, _ = reference_facts(snapshot)
        assert {fact.kind for fact in facts} == {"reference-like"}

    def test_keeps_kinds_apart_when_asked(self) -> None:
        snapshot = snapshot_with(make_symbol("f"), make_symbol("g", line=5))
        snapshot.add_edge(
            Edge(
                "f",
                "g",
                EdgeKind.CALLS,
                site_path="src/a.py",
                site_range=SourceRange.of(2, 0, 2, 1),
            )
        )
        facts, _ = reference_facts(snapshot, collapse_kinds=False)
        assert facts[0].kind == "calls"

    def test_imports_stay_their_own_group(self) -> None:
        snapshot = snapshot_with(make_symbol("f"), make_symbol("g", line=5))
        snapshot.add_edge(
            Edge(
                "f",
                "g",
                EdgeKind.IMPORTS,
                site_path="src/a.py",
                site_range=SourceRange.of(0, 0, 0, 1),
            )
        )
        facts, _ = reference_facts(snapshot)
        assert facts[0].kind == "imports"


class TestFactSet:
    def test_indexes_facts_by_file_and_line(self) -> None:
        facts = FactSet.build(
            defs=[
                DefFact("a.py", SourceRange.of(0, 0, 0, 1)),
                DefFact("a.py", SourceRange.of(0, 5, 0, 6)),
                DefFact("b.py", SourceRange.of(3, 0, 3, 1)),
            ]
        )
        assert len(facts.definitions[("a.py", 0)]) == 2
        assert facts.paths == {"a.py", "b.py"}

    def test_iterates_every_fact(self) -> None:
        facts = FactSet.build(
            defs=[
                DefFact("a.py", SourceRange.of(0, 0, 0, 1)),
                DefFact("b.py", SourceRange.of(1, 0, 1, 1)),
            ]
        )
        assert len(list(facts.all_definitions())) == 2


class TestMatching:
    @staticmethod
    def _def(path: str, line: int, start: int, end: int, kind: str = "") -> DefFact:
        return DefFact(path, SourceRange.of(line, start, line, end), kind)

    def test_identical_sets_match_completely(self) -> None:
        facts = [self._def("a.py", 0, 0, 4), self._def("a.py", 5, 2, 7)]
        result = match_facts(facts, list(facts))
        assert result.true_positive_count == 2
        assert not result.false_positives
        assert not result.false_negatives

    def test_an_extra_prediction_is_a_false_positive(self) -> None:
        result = match_facts(
            [self._def("a.py", 0, 0, 4), self._def("a.py", 9, 0, 4)],
            [self._def("a.py", 0, 0, 4)],
        )
        assert result.true_positive_count == 1
        assert len(result.false_positives) == 1

    def test_a_missing_prediction_is_a_false_negative(self) -> None:
        result = match_facts(
            [self._def("a.py", 0, 0, 4)],
            [self._def("a.py", 0, 0, 4), self._def("a.py", 9, 0, 4)],
        )
        assert len(result.false_negatives) == 1

    def test_overlap_absorbs_a_column_shift(self) -> None:
        # The kind of difference a UTF-8 versus UTF-16 offset produces.
        result = match_facts(
            [self._def("a.py", 0, 12, 16)], [self._def("a.py", 0, 13, 17)]
        )
        assert result.true_positive_count == 1
        assert result.tolerant_matches == 1

    def test_exact_policy_refuses_the_same_shift(self) -> None:
        result = match_facts(
            [self._def("a.py", 0, 12, 16)],
            [self._def("a.py", 0, 13, 17)],
            policy="exact",
        )
        assert result.true_positive_count == 0
        assert result.tolerant_matches == 0

    def test_line_policy_ignores_columns_entirely(self) -> None:
        result = match_facts(
            [self._def("a.py", 0, 0, 2)], [self._def("a.py", 0, 40, 44)], policy="line"
        )
        assert result.true_positive_count == 1

    def test_a_different_line_never_matches(self) -> None:
        result = match_facts([self._def("a.py", 0, 0, 4)], [self._def("a.py", 1, 0, 4)])
        assert result.true_positive_count == 0

    def test_a_different_file_never_matches(self) -> None:
        result = match_facts([self._def("a.py", 0, 0, 4)], [self._def("b.py", 0, 0, 4)])
        assert result.true_positive_count == 0

    def test_kinds_must_agree_when_both_state_one(self) -> None:
        result = match_facts(
            [self._def("a.py", 0, 0, 4, "class")],
            [self._def("a.py", 0, 0, 4, "function")],
        )
        assert result.true_positive_count == 0

    def test_an_unstated_kind_matches_anything(self) -> None:
        result = match_facts(
            [self._def("a.py", 0, 0, 4)], [self._def("a.py", 0, 0, 4, "function")]
        )
        assert result.true_positive_count == 1

    def test_one_oracle_fact_can_only_be_claimed_once(self) -> None:
        # Two overlapping predictions, one oracle fact: exactly one is right.
        result = match_facts(
            [self._def("a.py", 0, 0, 6), self._def("a.py", 0, 2, 8)],
            [self._def("a.py", 0, 1, 7)],
        )
        assert result.true_positive_count == 1
        assert len(result.false_positives) == 1

    def test_two_empty_sets_produce_an_empty_result(self) -> None:
        result = match_facts([], [])
        assert result.true_positive_count == 0
        assert not result.false_positives
        assert not result.false_negatives

    def test_reference_matching_requires_the_target_to_agree(self) -> None:
        site = SourceRange.of(2, 0, 2, 5)
        predicted = RefFact("a.py", site, "b.py", SourceRange.of(9, 0, 9, 3), "calls")
        wrong_target = RefFact("a.py", site, "c.py", SourceRange.of(9, 0, 9, 3), "calls")
        assert match_facts([predicted], [wrong_target]).true_positive_count == 0
        assert match_facts([predicted], [predicted]).true_positive_count == 1

    def test_reference_matching_requires_the_target_line_to_agree(self) -> None:
        site = SourceRange.of(2, 0, 2, 5)
        predicted = RefFact("a.py", site, "b.py", SourceRange.of(9, 0, 9, 3), "calls")
        wrong_line = RefFact("a.py", site, "b.py", SourceRange.of(4, 0, 4, 3), "calls")
        assert match_facts([predicted], [wrong_line]).true_positive_count == 0

    def test_per_path_counts_split_by_file(self) -> None:
        result = match_facts(
            [self._def("a.py", 0, 0, 4), self._def("b.py", 0, 0, 4)],
            [self._def("a.py", 0, 0, 4), self._def("c.py", 0, 0, 4)],
        )
        counts = result.by_path()
        assert counts["a.py"] == (1, 0, 0)
        assert counts["b.py"] == (0, 1, 0)
        assert counts["c.py"] == (0, 0, 1)


class TestOracleRoundTrip:
    """A snapshot compared with itself must score perfectly."""

    def test_definitions_match_themselves(self, demo_index) -> None:
        from repoatlas.oracle.scip import read_scip_binary

        snapshot = read_scip_binary(demo_index.to_binary())
        facts = definition_facts(snapshot)
        result = match_facts(facts, list(facts), policy="exact")
        assert not result.false_positives
        assert not result.false_negatives

    def test_references_match_themselves(self, demo_index) -> None:
        from repoatlas.oracle.scip import read_scip_binary

        snapshot = read_scip_binary(demo_index.to_binary())
        facts, unprojectable = reference_facts(snapshot, require_site=False)
        assert not unprojectable
        result = match_facts(facts, list(facts), policy="exact")
        assert not result.false_positives
        assert not result.false_negatives

    def test_edges_carry_full_confidence_from_an_oracle(self, demo_index) -> None:
        from repoatlas.oracle.scip import read_scip_binary

        snapshot = read_scip_binary(demo_index.to_binary())
        assert all(edge.tier is ResolutionTier.ORACLE for edge in snapshot.edges)
