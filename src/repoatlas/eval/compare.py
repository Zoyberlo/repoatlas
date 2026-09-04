"""Compare a candidate index against an oracle and report where it differs.

This is the entry point for the first evaluation tier: before any retrieval
metric or agent A/B test, an index has to be shown to describe the code
correctly. The output is deliberately blunt about the parts most tools leave
out, namely per-edge-kind recall, dangling edges and confidence calibration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ..model import Edge, IndexSnapshot, Symbol, SymbolKind
from .facts import (
    NAVIGABLE_KINDS,
    DefFact,
    MatchPolicy,
    MatchResult,
    RefFact,
    _edge_group,
    definition_facts,
    match_facts,
    normalise_path,
    reference_facts,
)
from .metrics import CalibrationReport, Interval, Score, bootstrap_interval, calibrate

__all__ = ["Comparison", "ComparisonOptions", "ShapeCoverage", "compare_snapshots"]


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

    site_shapes: Mapping[tuple[str, int, int], str] | None = None
    """What each candidate reference site reached its member through.

    Keyed by ``(path, line, character)`` of the reference, valued by
    :func:`repoatlas.resolve.cascade.receiver_shape`. When given, edges
    are scored only for the shapes the oracle resolves somewhere: an
    oracle that never resolves a member through a variable, in thousands
    of tries, cannot judge one, and counting its silence as a false
    positive would measure the oracle rather than the index.
    """


# Below this many candidate edges of a shape, the oracle's silence is not
# evidence of anything; at or above it, resolving fewer than one in a
# hundred is.
_MIN_SHAPE_SAMPLE = 30
_MAX_BLIND_COVERAGE = 0.01


@dataclass(frozen=True, slots=True)
class ShapeCoverage:
    """How far the oracle reaches into one receiver shape."""

    shape: str
    candidate: int
    """Candidate edges of this shape, in the compared files."""

    oracle_seen: int
    """Sites among them where the oracle resolved anything at all."""

    scored: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "shape": self.shape,
            "candidate": self.candidate,
            "oracle_seen": self.oracle_seen,
            "scored": self.scored,
        }


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
    uncovered_target_edges: int = 0
    """Edges into files the oracle never indexed, which it therefore cannot confirm or deny."""

    oracle_local_definitions: int = 0
    """Definitions at positions the oracle files as local symbols: a scope difference, not an error."""

    oracle_local_target_edges: int = 0
    """Edges into positions the oracle files as local symbols, which it excludes from its own facts."""

    shape_coverage: list[ShapeCoverage] = field(default_factory=list)
    """Per receiver shape, how many candidate edges there were and how many sites the oracle reached."""

    unjudged_shape_edges: int = 0
    """Edges of shapes the oracle never resolves, left out of the score."""

    definition_coverage: list[ShapeCoverage] = field(default_factory=list)
    """Per kind of definition site, how many the candidate had and how many the oracle defined anything at."""

    unjudged_shape_definitions: int = 0
    """Definitions of a shape the oracle never records, left out of the score."""
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
            "uncovered_target_edges": self.uncovered_target_edges,
            "oracle_local_definitions": self.oracle_local_definitions,
            "oracle_local_target_edges": self.oracle_local_target_edges,
            "shape_coverage": [coverage.as_dict() for coverage in self.shape_coverage],
            "unjudged_shape_edges": self.unjudged_shape_edges,
            "definition_coverage": [c.as_dict() for c in self.definition_coverage],
            "unjudged_shape_definitions": self.unjudged_shape_definitions,
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


def _definition_shape(candidate: IndexSnapshot, symbol: Symbol) -> str:
    """Whether a definition is a member of a type, or of something looser.

    The methods of an object literal, a Pinia store's actions or a Vue
    component's options, are symbols here because an agent goes to them.
    A compiler-backed index may record nothing at all for them, and the
    shape lets the comparison find that out rather than assume it.
    """
    if symbol.kind not in _MEMBER_KINDS:
        return "declaration"
    container = candidate.symbols.get(symbol.container_id or "")
    if container is None or container.kind.is_type_like:
        return "member of a type"
    return "member of an object literal"


_MEMBER_KINDS = frozenset({SymbolKind.METHOD, SymbolKind.FIELD, SymbolKind.PROPERTY})


def _definitions_within_reach(
    report: Comparison,
    candidate: IndexSnapshot,
    candidate_defs: list[DefFact],
    oracle_defs: list[DefFact],
    options: ComparisonOptions,
) -> list[DefFact]:
    """Leave out the shapes of definition the oracle demonstrably never records."""
    by_position = {
        (
            normalise_path(symbol.path, case_fold=options.case_fold_paths),
            symbol.name_range.start.line,
            symbol.name_range.start.character,
        ): symbol
        for symbol in candidate.symbols.values()
    }
    oracle_sites = {(fact.path, fact.span.start.line, fact.span.start.character) for fact in oracle_defs}
    candidates: dict[str, int] = {}
    seen: dict[str, int] = {}
    labelled: list[tuple[str, DefFact]] = []
    for fact in candidate_defs:
        site = (fact.path, fact.span.start.line, fact.span.start.character)
        symbol = by_position.get(site)
        shape = _definition_shape(candidate, symbol) if symbol is not None else "declaration"
        candidates[shape] = candidates.get(shape, 0) + 1
        if site in oracle_sites:
            seen[shape] = seen.get(shape, 0) + 1
        labelled.append((shape, fact))
    blind: set[str] = set()
    if len(candidates) > 1:
        for shape, count in candidates.items():
            reached = seen.get(shape, 0)
            scored = count < _MIN_SHAPE_SAMPLE or reached / count >= _MAX_BLIND_COVERAGE
            report.definition_coverage.append(
                ShapeCoverage(shape=shape, candidate=count, oracle_seen=reached, scored=scored)
            )
            if not scored:
                blind.add(shape)
        report.definition_coverage.sort(key=lambda coverage: -coverage.candidate)
    kept = [fact for shape, fact in labelled if shape not in blind]
    report.unjudged_shape_definitions = len(candidate_defs) - len(kept)
    return kept


def _within_oracle_reach(
    report: Comparison,
    candidate_refs: list[RefFact],
    oracle_refs: list[RefFact],
    options: ComparisonOptions,
) -> list[RefFact]:
    """Leave out the receiver shapes the oracle demonstrably cannot resolve.

    A shape is judged by how often the oracle resolved *anything* at the
    candidate's sites of that shape. `scip-php` 0.0.2 resolves `$this->x`
    at six sites in ten and `$var->x` at none in four thousand; the first
    is a fair judge of that shape and the second is blind to it.
    """
    assert options.site_shapes is not None
    shapes = {
        (normalise_path(path, case_fold=options.case_fold_paths), line, character): shape
        for (path, line, character), shape in options.site_shapes.items()
    }
    oracle_sites = {
        (fact.site_path, fact.site_span.start.line, fact.site_span.start.character)
        for fact in oracle_refs
    }
    candidates: dict[str, int] = {}
    seen: dict[str, int] = {}
    labelled: list[tuple[str, RefFact]] = []
    for fact in candidate_refs:
        site = (fact.site_path, fact.site_span.start.line, fact.site_span.start.character)
        shape = shapes.get(site, "none")
        candidates[shape] = candidates.get(shape, 0) + 1
        if site in oracle_sites:
            seen[shape] = seen.get(shape, 0) + 1
        labelled.append((shape, fact))
    blind: set[str] = set()
    for shape, count in candidates.items():
        reached = seen.get(shape, 0)
        scored = count < _MIN_SHAPE_SAMPLE or reached / count >= _MAX_BLIND_COVERAGE
        report.shape_coverage.append(
            ShapeCoverage(shape=shape, candidate=count, oracle_seen=reached, scored=scored)
        )
        if not scored:
            blind.add(shape)
    report.shape_coverage.sort(key=lambda coverage: -coverage.candidate)
    kept = [fact for shape, fact in labelled if shape not in blind]
    report.unjudged_shape_edges = len(candidate_refs) - len(kept)
    return kept


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
    # A position the oracle files as a local symbol is one it has looked at
    # and declined to make navigable: a method of an object literal, say,
    # which scip-typescript calls `local`. This index makes such members
    # symbols on purpose, so a store's actions can be found. Neither
    # producer is wrong, and the disagreement is stated, not scored.
    oracle_local_positions = {
        (
            normalise_path(symbol.path, case_fold=options.case_fold_paths),
            symbol.name_range.start.line,
            symbol.name_range.start.character,
        )
        for symbol in oracle.symbols.values()
        if symbol.local
    }
    if oracle_local_positions:
        judgeable_defs = [
            fact
            for fact in candidate_defs
            if (fact.path, fact.span.start.line, fact.span.start.character)
            not in oracle_local_positions
        ]
        report.oracle_local_definitions = len(candidate_defs) - len(judgeable_defs)
        candidate_defs = judgeable_defs
    candidate_defs = _definitions_within_reach(report, candidate, candidate_defs, oracle_defs, options)
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
        target_kinds=options.symbol_kinds,
    )
    oracle_refs, _ = reference_facts(
        oracle,
        case_fold=options.case_fold_paths,
        collapse_kinds=options.collapse_edge_kinds,
        paths=scope,
        require_site=False,
        target_kinds=options.symbol_kinds,
    )
    for edge in unprojectable:
        if edge.dst_id not in candidate.symbols:
            report.dangling_edges.append(edge)
        else:
            report.unsited_edges.append(edge)
    if scope is not None:
        # An edge into a file the oracle never indexed is one the oracle
        # could not have produced whether or not it is right: a
        # TypeScript indexer does not open `.vue` files. Scoring it as
        # false would measure the oracle's reach, not the index's accuracy.
        judgeable = [fact for fact in candidate_refs if fact.target_path in scope]
        report.uncovered_target_edges = len(candidate_refs) - len(judgeable)
        candidate_refs = judgeable
    if oracle_local_positions:
        judgeable = [
            fact
            for fact in candidate_refs
            if (fact.target_path, fact.target_span.start.line, fact.target_span.start.character)
            not in oracle_local_positions
        ]
        report.oracle_local_target_edges = len(candidate_refs) - len(judgeable)
        candidate_refs = judgeable
    if options.site_shapes:
        candidate_refs = _within_oracle_reach(report, candidate_refs, oracle_refs, options)

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

    Edges are looked up by their projected fact. Two edges making the same
    claim at the same confidence would count twice, which is the correct
    weight for a producer that asserts the same thing twice.
    """
    outcomes: list[tuple[float, bool]] = []
    fact_index: dict[_RefKey, bool] = {}
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
        key: _RefKey = (
            normalise_path(site_path, case_fold=options.case_fold_paths),
            site_range.start.line,
            site_range.start.character,
            normalise_path(target.path, case_fold=options.case_fold_paths),
            target.name_range.start.line,
            target.name_range.start.character,
            _edge_group(edge.kind, options.collapse_edge_kinds),
        )
        if key not in fact_index:
            continue
        outcomes.append((edge.score, fact_index[key]))

    if not outcomes:
        return None
    return calibrate(outcomes)


_RefKey = tuple[str, int, int, str, int, int, str]


def _ref_key(fact: RefFact) -> _RefKey:
    """Identify a reference claim fully: site, target position and kind.

    Site and target file alone were not enough. A candidate that resolved
    one call both to the right method and, at lower confidence, to its
    class shared a key between a true and a false positive, and the wrong
    edge was credited as correct: exactly the overconfidence calibration
    exists to expose.
    """
    return (
        fact.site_path,
        fact.site_span.start.line,
        fact.site_span.start.character,
        fact.target_path,
        fact.target_span.start.line,
        fact.target_span.start.character,
        fact.kind,
    )
