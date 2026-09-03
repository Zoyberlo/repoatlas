"""Compare a candidate index against an oracle and report where it differs.

This is the entry point for the first evaluation tier: before any retrieval
metric or agent A/B test, an index has to be shown to describe the code
correctly. The output is deliberately blunt about the parts most tools leave
out, namely per-edge-kind recall, dangling edges and confidence calibration.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..model import Edge, IndexSnapshot, SymbolKind
from .facts import (
    NAVIGABLE_KINDS,
    MatchPolicy,
    MatchResult,
    RefFact,
    definition_facts,
    match_facts,
    normalise_path,
    reference_facts,
)
from .metrics import CalibrationReport, Interval, Score, bootstrap_interval, calibrate

__all__ = ["Comparison", "ComparisonOptions", "compare_snapshots"]


@dataclass(frozen=True, slots=True)
class ComparisonOptions:
    """Knobs for a comparison run.

    ``restrict_to_oracle_paths`` defaults to true because oracles are
    routinely partial: ``scip-typescript`` indexes what ``tsconfig.json``
    includes and nothing else. Scoring a candidate against files the oracle
    never looked at would count correct edges as false positives.
    """

    policy: MatchPolicy = "overlap"
    case_fold_paths: bool = False
    collapse_edge_kinds: bool = True
    compare_symbol_kinds: bool = False
    restrict_to_oracle_paths: bool = True

    symbol_kinds: frozenset[SymbolKind] | None = NAVIGABLE_KINDS
    """Which symbol kinds are in scope; ``None`` compares everything.

    Defaults to the symbols an agent would navigate to. Compiler-backed
    oracles also record parameters, locals and a symbol for the file itself,
    which a map has no use for; counting those as misses would measure a
    difference in purpose rather than in accuracy.
    """

    restrict_to_oracle_edge_kinds: bool = True
    """Score only the edge kinds the oracle actually emits.

    SCIP records occurrences, not structure, so it has nothing to say about
    containment. Scoring a containment edge against it would mark every one
    a false positive for being a claim the oracle never contradicts.
    """

    bootstrap_resamples: int = 2000
    bootstrap_seed: int = 20260903
    confidence_level: float = 0.95


@dataclass(slots=True)
class Comparison:
    """Everything learned by comparing one candidate index to one oracle."""

    definitions: Score = field(default_factory=lambda: Score(0, 0, 0))
    references: Score = field(default_factory=lambda: Score(0, 0, 0))
    references_by_kind: dict[str, Score] = field(default_factory=dict)
    definition_interval: Interval | None = None
    reference_interval: Interval | None = None
    calibration: CalibrationReport | None = None
    dangling_edges: list[Edge] = field(default_factory=list)
    unsited_edges: list[Edge] = field(default_factory=list)
    tolerant_definition_matches: int = 0
    tolerant_reference_matches: int = 0
    compared_paths: int = 0
    skipped_paths: int = 0
    candidate_producer: str | None = None
    oracle_producer: str | None = None
    encoding_mismatch: bool = False
    encoding_assumed: bool = False
    definition_result: MatchResult | None = None
    reference_result: MatchResult | None = None

    symbol_kind_scope: frozenset[SymbolKind] | None = None
    """Which symbol kinds were in scope, so the report can say so."""

    unscored_edge_kinds: list[str] = field(default_factory=list)
    """Edge kinds the candidate emits that the oracle never does."""

    unscored_edges: int = 0

    @property
    def dangling_rate(self) -> float:
        """Share of candidate edges pointing at symbols it never defined."""
        total = self.references.predicted + len(self.dangling_edges)
        return len(self.dangling_edges) / total if total else 0.0

    def sample_false_positives(self, limit: int = 10) -> list[str]:
        result = self.reference_result
        if result is None:
            return []
        return [str(fact) for fact in result.false_positives[:limit]]

    def sample_false_negatives(self, limit: int = 10) -> list[str]:
        result = self.reference_result
        if result is None:
            return []
        return [str(fact) for fact in result.false_negatives[:limit]]

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "candidate": self.candidate_producer,
            "oracle": self.oracle_producer,
            "compared_paths": self.compared_paths,
            "skipped_paths": self.skipped_paths,
            "encoding_mismatch": self.encoding_mismatch,
            "encoding_assumed": self.encoding_assumed,
            "definitions": self.definitions.as_dict(),
            "references": self.references.as_dict(),
            "references_by_kind": {
                kind: score.as_dict() for kind, score in sorted(self.references_by_kind.items())
            },
            "dangling_edges": len(self.dangling_edges),
            "dangling_rate": round(self.dangling_rate, 4),
            "unsited_edges": len(self.unsited_edges),
            "unscored_edge_kinds": self.unscored_edge_kinds,
            "unscored_edges": self.unscored_edges,
            "symbol_kind_scope": (
                sorted(kind.value for kind in self.symbol_kind_scope)
                if self.symbol_kind_scope is not None
                else None
            ),
            "tolerant_matches": {
                "definitions": self.tolerant_definition_matches,
                "references": self.tolerant_reference_matches,
            },
        }
        if self.definition_interval is not None:
            payload["definition_f1_interval"] = self.definition_interval.as_dict()
        if self.reference_interval is not None:
            payload["reference_f1_interval"] = self.reference_interval.as_dict()
        if self.calibration is not None:
            payload["calibration"] = self.calibration.as_dict()
        return payload


def _score(result: MatchResult) -> Score:
    return Score(
        true_positives=result.true_positive_count,
        false_positives=len(result.false_positives),
        false_negatives=len(result.false_negatives),
    )


def _interval(
    result: MatchResult, options: ComparisonOptions
) -> Interval | None:
    units: Sequence[tuple[int, int, int]] = list(result.by_path().values())
    if len(units) < 2:
        return None
    return bootstrap_interval(
        units,
        level=options.confidence_level,
        resamples=options.bootstrap_resamples,
        seed=options.bootstrap_seed,
    )


def compare_snapshots(
    candidate: IndexSnapshot,
    oracle: IndexSnapshot,
    options: ComparisonOptions | None = None,
) -> Comparison:
    """Score ``candidate`` against ``oracle`` and explain the difference."""
    options = options or ComparisonOptions()
    report = Comparison(
        candidate_producer=candidate.producer,
        oracle_producer=oracle.producer,
        encoding_mismatch=candidate.encoding is not oracle.encoding,
        encoding_assumed=not (candidate.encoding_declared and oracle.encoding_declared),
    )

    oracle_paths = {
        normalise_path(path, case_fold=options.case_fold_paths) for path in oracle.paths
    }
    candidate_paths = {
        normalise_path(path, case_fold=options.case_fold_paths) for path in candidate.paths
    }
    scope = oracle_paths if options.restrict_to_oracle_paths else None
    report.compared_paths = len(oracle_paths & candidate_paths) if scope is not None else len(
        oracle_paths | candidate_paths
    )
    report.skipped_paths = len(candidate_paths - oracle_paths) if scope is not None else 0

    candidate_defs = definition_facts(
        candidate,
        case_fold=options.case_fold_paths,
        include_kind=options.compare_symbol_kinds,
        paths=scope,
        kinds=options.symbol_kinds,
    )
    oracle_defs = definition_facts(
        oracle,
        case_fold=options.case_fold_paths,
        include_kind=options.compare_symbol_kinds,
        paths=scope,
        kinds=options.symbol_kinds,
    )
    report.symbol_kind_scope = options.symbol_kinds
    definition_result = match_facts(candidate_defs, oracle_defs, policy=options.policy)
    report.definition_result = definition_result
    report.definitions = _score(definition_result)
    report.tolerant_definition_matches = definition_result.tolerant_matches
    report.definition_interval = _interval(definition_result, options)

    candidate_refs, unprojectable = reference_facts(
        candidate,
        case_fold=options.case_fold_paths,
        collapse_kinds=options.collapse_edge_kinds,
        paths=scope,
        require_site=False,
    )
    oracle_refs, _ = reference_facts(
        oracle,
        case_fold=options.case_fold_paths,
        collapse_kinds=options.collapse_edge_kinds,
        paths=scope,
        require_site=False,
    )
    for edge in unprojectable:
        if edge.dst_id not in candidate.symbols:
            report.dangling_edges.append(edge)
        else:
            report.unsited_edges.append(edge)

    if options.restrict_to_oracle_edge_kinds:
        oracle_kinds = {fact.kind for fact in oracle_refs}
        out_of_scope = {fact.kind for fact in candidate_refs} - oracle_kinds
        if out_of_scope:
            report.unscored_edge_kinds = sorted(out_of_scope)
            report.unscored_edges = sum(
                1 for fact in candidate_refs if fact.kind in out_of_scope
            )
            candidate_refs = [
                fact for fact in candidate_refs if fact.kind in oracle_kinds
            ]

    reference_result = match_facts(candidate_refs, oracle_refs, policy=options.policy)
    report.reference_result = reference_result
    report.references = _score(reference_result)
    report.tolerant_reference_matches = reference_result.tolerant_matches
    report.reference_interval = _interval(reference_result, options)

    # Per-kind scores need their own matching pass: a candidate edge labelled
    # `calls` must not be allowed to satisfy an oracle `imports` fact just
    # because both landed in the same collapsed group.
    kinds = {fact.kind for fact in candidate_refs} | {fact.kind for fact in oracle_refs}
    for kind in sorted(kinds):
        kind_result = match_facts(
            [f for f in candidate_refs if f.kind == kind],
            [f for f in oracle_refs if f.kind == kind],
            policy=options.policy,
        )
        report.references_by_kind[kind] = _score(kind_result)

    report.calibration = _calibrate_edges(candidate, reference_result, options)
    return report


def _calibrate_edges(
    candidate: IndexSnapshot,
    result: MatchResult,
    options: ComparisonOptions,
) -> CalibrationReport | None:
    """Pair each candidate edge's stated confidence with whether it was right.

    Edges are looked up by their projected fact, so an edge only counts once
    even when several edges share a site.
    """
    outcomes: list[tuple[float, bool]] = []
    fact_index: dict[tuple[str, int, int, str], bool] = {}
    for fact, _oracle in result.matched:
        if isinstance(fact, RefFact):
            fact_index[_ref_key(fact)] = True
    for fact in result.false_positives:
        if isinstance(fact, RefFact):
            fact_index.setdefault(_ref_key(fact), False)

    for edge in candidate.edges:
        target = candidate.symbols.get(edge.dst_id)
        if target is None:
            continue
        site_path = edge.site_path
        site_range = edge.site_range
        if site_path is None or site_range is None:
            source = candidate.symbols.get(edge.src_id)
            if source is None:
                continue
            site_path, site_range = source.path, source.name_range
        key = (
            normalise_path(site_path, case_fold=options.case_fold_paths),
            site_range.start.line,
            site_range.start.character,
            normalise_path(target.path, case_fold=options.case_fold_paths),
        )
        if key not in fact_index:
            continue
        outcomes.append((edge.score, fact_index[key]))

    if not outcomes:
        return None
    return calibrate(outcomes)


def _ref_key(fact: RefFact) -> tuple[str, int, int, str]:
    return (
        fact.site_path,
        fact.site_span.start.line,
        fact.site_span.start.character,
        fact.target_path,
    )
