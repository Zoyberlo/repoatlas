"""End-to-end tests for the oracle comparison harness.

The method here is to take a known-good index, break it in one specific way,
and assert the harness reports that break and nothing else. A harness that
cannot detect a deliberately planted error will not detect a real one.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from repoatlas.eval.compare import ComparisonOptions, compare_snapshots
from repoatlas.eval.report import to_json, to_markdown
from repoatlas.model import (
    Edge,
    EdgeKind,
    IndexSnapshot,
    PositionEncoding,
    ResolutionTier,
    SourceRange,
    Symbol,
    SymbolKind,
)
from repoatlas.oracle.scip import read_scip_binary

from .conftest import SYM_GREET, SYM_MAIN, SYM_USER, IndexSpec


@pytest.fixture
def oracle(demo_index: IndexSpec) -> IndexSnapshot:
    snapshot = read_scip_binary(demo_index.to_binary())
    snapshot.producer = "scip-typescript 0.4.0"
    return snapshot


@pytest.fixture
def candidate(oracle: IndexSnapshot) -> IndexSnapshot:
    """A copy of the oracle, standing in for a perfect extractor."""
    clone = copy.deepcopy(oracle)
    clone.producer = "repoatlas tree-sitter"
    clone.edges = [
        replace(edge, tier=ResolutionTier.IMPORT_MAP, confidence=None)
        for edge in clone.edges
    ]
    return clone


class TestPerfectCandidate:
    def test_scores_definitions_perfectly(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        result = compare_snapshots(candidate, oracle)
        assert result.definitions.f1 == pytest.approx(1.0)
        assert result.definitions.false_positives == 0

    def test_scores_references_perfectly(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        result = compare_snapshots(candidate, oracle)
        assert result.references.f1 == pytest.approx(1.0)

    def test_finds_no_dangling_edges(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        result = compare_snapshots(candidate, oracle)
        assert result.dangling_edges == []
        assert result.dangling_rate == 0.0

    def test_records_both_producers(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        result = compare_snapshots(candidate, oracle)
        assert result.candidate_producer == "repoatlas tree-sitter"
        assert result.oracle_producer == "scip-typescript 0.4.0"


class TestMissedEdges:
    def test_dropping_an_edge_costs_recall_not_precision(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.edges = candidate.edges[:-1]
        result = compare_snapshots(candidate, oracle)
        assert result.references.precision == pytest.approx(1.0)
        assert result.references.recall < 1.0
        assert result.references.false_negatives == 1

    def test_a_missed_edge_is_listed_as_an_example(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.edges = candidate.edges[:-1]
        result = compare_snapshots(candidate, oracle)
        assert len(result.sample_false_negatives()) == 1

    def test_finding_no_edges_at_all_scores_zero(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.edges = []
        result = compare_snapshots(candidate, oracle)
        assert result.references.recall == 0.0
        assert result.references.f1 == 0.0
        assert result.definitions.f1 == pytest.approx(1.0)


class TestInventedEdges:
    def test_an_invented_edge_costs_precision(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.add_edge(
            Edge(
                src_id=SYM_MAIN,
                dst_id=SYM_USER,
                kind=EdgeKind.CALLS,
                tier=ResolutionTier.FUZZY,
                site_path="src/app.ts",
                site_range=SourceRange.of(3, 20, 3, 24),
            )
        )
        result = compare_snapshots(candidate, oracle)
        assert result.references.false_positives == 1
        assert result.references.recall == pytest.approx(1.0)
        assert result.references.precision < 1.0

    def test_an_edge_to_an_unknown_symbol_is_dangling_not_wrong(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.add_edge(
            Edge(
                src_id=SYM_MAIN,
                dst_id="nowhere",
                kind=EdgeKind.CALLS,
                site_path="src/app.ts",
                site_range=SourceRange.of(3, 20, 3, 24),
            )
        )
        result = compare_snapshots(candidate, oracle)
        assert len(result.dangling_edges) == 1
        assert result.dangling_rate > 0
        assert result.references.false_positives == 0

    def test_an_invented_definition_costs_definition_precision(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.add_symbol(
            Symbol(
                id="ghost",
                name="ghost",
                kind=SymbolKind.FUNCTION,
                path="src/app.ts",
                name_range=SourceRange.of(2, 8, 2, 13),
            )
        )
        result = compare_snapshots(candidate, oracle)
        assert result.definitions.false_positives == 1


class TestWrongTargets:
    def test_pointing_an_edge_at_the_wrong_symbol_is_both_a_miss_and_a_mistake(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        # Resolve `u.greet()` to the class instead of the method: a classic
        # heuristic failure when the receiver type is unknown.
        rewritten = []
        for edge in candidate.edges:
            if edge.dst_id == SYM_GREET:
                rewritten.append(replace(edge, dst_id=SYM_USER))
            else:
                rewritten.append(edge)
        candidate.edges = rewritten
        result = compare_snapshots(candidate, oracle)
        assert result.references.false_positives == 1
        assert result.references.false_negatives == 1

    def test_labelling_an_import_as_a_call_shows_in_the_per_kind_table(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.edges = [
            replace(edge, kind=EdgeKind.CALLS) if edge.kind is EdgeKind.IMPORTS else edge
            for edge in candidate.edges
        ]
        result = compare_snapshots(candidate, oracle, ComparisonOptions())
        assert result.references_by_kind["imports"].recall == 0.0
        # Collapsed scoring still counts the edge, because the site and target
        # are right; only the label is wrong.
        assert result.references_by_kind["reference-like"].false_positives == 1


class TestColumnTolerance:
    @staticmethod
    def _shift(snapshot: IndexSnapshot, delta: int) -> None:
        for symbol_id, symbol in list(snapshot.symbols.items()):
            span = symbol.name_range
            if span.start.character == 0 and span.end.character == 0:
                continue
            shifted = SourceRange.of(
                span.start.line,
                span.start.character + delta,
                span.end.line,
                span.end.character + delta,
            )
            snapshot.symbols[symbol_id] = replace(
                symbol, name_range=shifted, full_range=None
            )

    def test_a_small_shift_still_matches_and_is_counted(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        self._shift(candidate, 1)
        result = compare_snapshots(candidate, oracle)
        assert result.definitions.f1 == pytest.approx(1.0)
        assert result.tolerant_definition_matches > 0

    def test_exact_policy_refuses_a_shift(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        self._shift(candidate, 1)
        result = compare_snapshots(candidate, oracle, ComparisonOptions(policy="exact"))
        assert result.definitions.f1 < 1.0

    def test_a_differing_encoding_is_reported(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.encoding = PositionEncoding.UTF16
        assert compare_snapshots(candidate, oracle).encoding_mismatch


class TestPartialOracle:
    def test_files_the_oracle_never_saw_are_skipped_by_default(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.add_symbol(
            Symbol(
                id="untyped",
                name="untyped",
                kind=SymbolKind.FUNCTION,
                path="scripts/build.js",
                name_range=SourceRange.of(0, 0, 0, 7),
            )
        )
        result = compare_snapshots(candidate, oracle)
        assert result.definitions.false_positives == 0
        assert result.skipped_paths == 1

    def test_those_files_can_be_scored_on_request(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.add_symbol(
            Symbol(
                id="untyped",
                name="untyped",
                kind=SymbolKind.FUNCTION,
                path="scripts/build.js",
                name_range=SourceRange.of(0, 0, 0, 7),
            )
        )
        result = compare_snapshots(
            candidate, oracle, ComparisonOptions(restrict_to_oracle_paths=False)
        )
        assert result.definitions.false_positives == 1


class TestCalibration:
    def test_a_confident_and_correct_index_is_well_calibrated(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.edges = [
            replace(edge, tier=ResolutionTier.ORACLE, confidence=1.0)
            for edge in candidate.edges
        ]
        result = compare_snapshots(candidate, oracle)
        assert result.calibration is not None
        assert result.calibration.expected_error == pytest.approx(0.0, abs=1e-9)

    def test_overconfidence_on_a_wrong_edge_is_detected(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.add_edge(
            Edge(
                src_id=SYM_MAIN,
                dst_id=SYM_USER,
                kind=EdgeKind.CALLS,
                tier=ResolutionTier.IMPORT_MAP,
                site_path="src/app.ts",
                site_range=SourceRange.of(3, 20, 3, 24),
            )
        )
        result = compare_snapshots(candidate, oracle)
        assert result.calibration is not None
        worst = result.calibration.worst_bin()
        assert worst is not None
        assert worst.gap > 0

    def test_calibration_is_absent_when_no_edge_could_be_scored(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.edges = []
        assert compare_snapshots(candidate, oracle).calibration is None


class TestConfidenceIntervals:
    def test_intervals_are_reported_when_several_files_are_compared(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        result = compare_snapshots(candidate, oracle)
        assert result.definition_interval is not None
        assert result.definition_interval.low <= result.definition_interval.point

    def test_the_same_inputs_give_the_same_interval(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        first = compare_snapshots(candidate, oracle).definition_interval
        second = compare_snapshots(candidate, oracle).definition_interval
        assert first is not None and second is not None
        assert (first.low, first.high) == (second.low, second.high)


class TestReporting:
    def test_json_output_is_parseable_and_complete(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        payload = json.loads(to_json(compare_snapshots(candidate, oracle)))
        assert payload["definitions"]["f1"] == 1.0
        assert "references_by_kind" in payload
        assert payload["dangling_edges"] == 0

    def test_markdown_leads_with_the_headline_scores(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        text = to_markdown(compare_snapshots(candidate, oracle))
        assert text.startswith("# Index accuracy: repoatlas tree-sitter vs scip-typescript")
        assert "| definitions |" in text
        assert "| references |" in text

    def test_markdown_names_the_overconfident_rung(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        for _ in range(4):
            candidate.add_edge(
                Edge(
                    src_id=SYM_MAIN,
                    dst_id=SYM_USER,
                    kind=EdgeKind.CALLS,
                    tier=ResolutionTier.IMPORT_MAP,
                    site_path="src/app.ts",
                    site_range=SourceRange.of(3, 20, 3, 24),
                )
            )
        text = to_markdown(compare_snapshots(candidate, oracle))
        assert "overconfident" in text

    def test_markdown_reports_dangling_edges(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.add_edge(
            Edge(
                src_id=SYM_MAIN,
                dst_id="nowhere",
                kind=EdgeKind.CALLS,
                site_path="src/app.ts",
                site_range=SourceRange.of(3, 20, 3, 24),
            )
        )
        text = to_markdown(compare_snapshots(candidate, oracle))
        assert "Dangling edges" in text
        assert "1 (" in text


class TestComparisonScope:
    """The comparison states what it is comparing, rather than assuming."""

    def test_locals_are_out_of_scope_by_default(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        # A compiler-backed oracle records every binding it resolves. A map
        # for an agent has no use for a loop counter, so neither side is
        # penalised for the other's choice.
        oracle.add_symbol(
            Symbol(
                id="local 1",
                name="temp",
                kind=SymbolKind.VARIABLE,
                path="src/app.ts",
                name_range=SourceRange.of(2, 8, 2, 12),
                local=True,
            )
        )
        result = compare_snapshots(candidate, oracle)
        assert result.definitions.false_negatives == 0

    def test_locals_can_be_brought_into_scope(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        from repoatlas.eval.facts import definition_facts

        oracle.add_symbol(
            Symbol(
                id="local 1",
                name="temp",
                kind=SymbolKind.VARIABLE,
                path="src/app.ts",
                name_range=SourceRange.of(2, 8, 2, 12),
                local=True,
            )
        )
        assert len(definition_facts(oracle, include_local=True)) > len(
            definition_facts(oracle)
        )

    def test_a_kind_outside_the_scope_is_neither_right_nor_wrong(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        for snapshot in (candidate, oracle):
            snapshot.add_symbol(
                Symbol(
                    id=f"param:{id(snapshot)}",
                    name="label",
                    kind=SymbolKind.PARAMETER,
                    path="src/app.ts",
                    name_range=SourceRange.of(1, 20, 1, 25),
                )
            )
        result = compare_snapshots(candidate, oracle)
        assert result.definitions.f1 == pytest.approx(1.0)
        assert result.symbol_kind_scope is not None
        assert SymbolKind.PARAMETER not in result.symbol_kind_scope

    def test_comparing_every_kind_can_be_asked_for(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        oracle.add_symbol(
            Symbol(
                id="param:oracle-only",
                name="label",
                kind=SymbolKind.PARAMETER,
                path="src/app.ts",
                name_range=SourceRange.of(1, 20, 1, 25),
            )
        )
        result = compare_snapshots(
            candidate, oracle, ComparisonOptions(symbol_kinds=None)
        )
        assert result.definitions.false_negatives == 1

    def test_an_edge_kind_the_oracle_never_emits_is_not_scored(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        # SCIP records occurrences, not structure, so it has nothing to say
        # about containment; marking ours wrong would punish a claim the
        # oracle does not contradict.
        candidate.add_edge(
            Edge(
                src_id=SYM_USER,
                dst_id=SYM_GREET,
                kind=EdgeKind.CONTAINS,
                site_path="src/user.ts",
                site_range=SourceRange.of(1, 2, 1, 7),
            )
        )
        result = compare_snapshots(candidate, oracle)
        assert result.unscored_edge_kinds == ["contains"]
        assert result.unscored_edges == 1
        assert result.references.false_positives == 0

    def test_those_edges_can_be_scored_on_request(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        candidate.add_edge(
            Edge(
                src_id=SYM_USER,
                dst_id=SYM_GREET,
                kind=EdgeKind.CONTAINS,
                site_path="src/user.ts",
                site_range=SourceRange.of(1, 2, 1, 7),
            )
        )
        result = compare_snapshots(
            candidate, oracle, ComparisonOptions(restrict_to_oracle_edge_kinds=False)
        )
        assert result.references.false_positives == 1
        assert result.unscored_edges == 0

    def test_the_json_report_records_the_scope(
        self, candidate: IndexSnapshot, oracle: IndexSnapshot
    ) -> None:
        payload = json.loads(to_json(compare_snapshots(candidate, oracle)))
        assert "symbol_kind_scope" in payload
        assert "class" in payload["symbol_kind_scope"]
        assert payload["unscored_edges"] == 0
