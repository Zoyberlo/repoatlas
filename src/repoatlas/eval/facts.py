"""Projection of an index into location-keyed facts, for oracle comparison.

Comparing two indexes by symbol identifier does not work: a tree-sitter
extractor invents its own ids while ``scip-typescript`` emits SCIP symbol
strings carrying package and version. The only thing both agree on is
*where in the file* something is.

So both sides are projected into facts anchored at source locations:

:class:`DefFact`
    "there is a definition whose identifier sits here"

:class:`RefFact`
    "the reference at this site resolves to the definition over there"

Matching is by range overlap rather than exact equality, which absorbs the
one systematic difference between producers: SCIP indexers disagree about
whether a character offset counts UTF-8 bytes or UTF-16 code units, and that
only shifts columns on lines containing non-ASCII text. Every match that
needed tolerance is counted so the report can show how often it happened.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Literal, Union

from ..model import Edge, EdgeKind, IndexSnapshot, SourceRange, Symbol, SymbolKind

__all__ = [
    "NAVIGABLE_KINDS",
    "DefFact",
    "Fact",
    "FactSet",
    "MatchPolicy",
    "MatchResult",
    "RefFact",
    "definition_facts",
    "match_facts",
    "normalise_path",
    "reference_facts",
]

MatchPolicy = Literal["exact", "overlap", "line"]

Fact = Union["DefFact", "RefFact"]
"""Either kind of fact. Matching is generic over the two."""


def normalise_path(path: str, *, case_fold: bool = False) -> str:
    """Normalise a repository-relative path for comparison.

    Producers differ on separators and on leading ``./``. Case folding is
    opt-in because git is case-sensitive even where the filesystem is not,
    so folding by default would silently merge distinct files.
    """
    cleaned = path.replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")
    return cleaned.casefold() if case_fold else cleaned


@dataclass(frozen=True, slots=True)
class DefFact:
    """A definition, located by the span of its identifier."""

    path: str
    span: SourceRange
    kind: str = ""

    @property
    def line(self) -> int:
        return self.span.start.line

    def __str__(self) -> str:
        return f"{self.path}:{self.span.start.line + 1}:{self.span.start.character}"


@dataclass(frozen=True, slots=True)
class RefFact:
    """A resolved reference: a use site paired with the definition it reaches."""

    site_path: str
    site_span: SourceRange
    target_path: str
    target_span: SourceRange
    kind: str = ""

    @property
    def line(self) -> int:
        return self.site_span.start.line

    def __str__(self) -> str:
        return (
            f"{self.site_path}:{self.site_span.start.line + 1}"
            f" -> {self.target_path}:{self.target_span.start.line + 1}"
            f" [{self.kind}]"
        )


NAVIGABLE_KINDS = frozenset(
    {
        SymbolKind.CLASS,
        SymbolKind.INTERFACE,
        SymbolKind.TRAIT,
        SymbolKind.ENUM,
        SymbolKind.TYPE_ALIAS,
        SymbolKind.FUNCTION,
        SymbolKind.METHOD,
        SymbolKind.CONSTRUCTOR,
        SymbolKind.PROPERTY,
        SymbolKind.FIELD,
        SymbolKind.CONSTANT,
        SymbolKind.VARIABLE,
        SymbolKind.MACRO,
    }
)
"""Symbol kinds an agent would navigate to, and the default comparison scope.

Producers legitimately disagree about scope. A compiler-backed indexer
records every binding it resolves, so ``scip-typescript`` emits a definition
for each function parameter and for the file itself; a map for an agent has
no use for either, and indexing them would swell the map without helping
anyone find code. Scoring the two against each other unfiltered measures
that disagreement rather than accuracy, so the comparison states its scope
instead of pretending there is only one.
"""


def definition_facts(
    snapshot: IndexSnapshot,
    *,
    case_fold: bool = False,
    include_kind: bool = False,
    paths: set[str] | None = None,
    kinds: frozenset[SymbolKind] | None = None,
    include_local: bool = False,
) -> list[DefFact]:
    """Project every symbol into a definition fact.

    ``include_kind`` makes the comparison stricter by requiring the two
    producers to agree on what kind of thing was defined. It is off by
    default because kind vocabularies differ more than locations do.

    ``kinds`` limits which symbols are projected at all; see
    :data:`NAVIGABLE_KINDS`. ``include_local`` brings in bindings that
    nothing outside their own scope can name, which are excluded by default
    for the same reason: a map of a repository is not a list of its loop
    counters.
    """
    facts: list[DefFact] = []
    for symbol in snapshot.symbols.values():
        if symbol.synthetic:
            continue
        if symbol.local and not include_local:
            continue
        if kinds is not None and symbol.kind not in kinds:
            continue
        path = normalise_path(symbol.path, case_fold=case_fold)
        if paths is not None and path not in paths:
            continue
        facts.append(
            DefFact(
                path=path,
                span=symbol.name_range,
                kind=symbol.kind.value if include_kind else "",
            )
        )
    return facts


def _edge_group(kind: EdgeKind, collapse: bool) -> str:
    """Map an edge kind to the label used for comparison.

    Producers disagree on how finely to slice reference-like edges: SCIP
    reports a bare occurrence where a tree-sitter extractor may have decided
    the same token is a call. Collapsing them keeps the headline number
    honest; the per-kind breakdown still shows the detail.
    """
    if not collapse:
        return str(kind.value)
    # Imports join the reference group deliberately. SCIP defines an Import
    # role, but scip-typescript 0.4.0 never sets it: an import specifier is
    # an occurrence with role 0, indistinguishable from any other use. Kept
    # apart, every import edge a resolver produced would score as one false
    # positive plus one false negative against such an oracle.
    if kind in (
        EdgeKind.CALLS,
        EdgeKind.REFERENCES,
        EdgeKind.USES_TYPE,
        EdgeKind.IMPORTS,
    ):
        return "reference-like"
    if kind in (EdgeKind.INHERITS, EdgeKind.IMPLEMENTS):
        return "inheritance-like"
    return str(kind.value)


def reference_facts(
    snapshot: IndexSnapshot,
    *,
    case_fold: bool = False,
    collapse_kinds: bool = True,
    kinds: set[EdgeKind] | None = None,
    paths: set[str] | None = None,
    require_site: bool = True,
) -> tuple[list[RefFact], list[Edge]]:
    """Project edges into reference facts.

    Returns the facts alongside the edges that could not be projected. An
    edge is unprojectable when its target is not a symbol in the same
    snapshot (a dangling edge) or when it carries no evidence site. Both are
    reported rather than silently dropped, because a producer that emits
    many dangling edges is making claims it cannot support.
    """
    facts: list[RefFact] = []
    unprojectable: list[Edge] = []
    for edge in snapshot.edges:
        if kinds is not None and edge.kind not in kinds:
            continue
        target: Symbol | None = snapshot.symbols.get(edge.dst_id)
        if target is None:
            unprojectable.append(edge)
            continue
        if edge.site_path is not None and edge.site_range is not None:
            site_path = normalise_path(edge.site_path, case_fold=case_fold)
            site_span = edge.site_range
        else:
            source = snapshot.symbols.get(edge.src_id)
            if source is None or require_site:
                unprojectable.append(edge)
                continue
            # Relationship edges carry no occurrence; anchor them on the
            # declaring symbol so inheritance can still be scored.
            site_path = normalise_path(source.path, case_fold=case_fold)
            site_span = source.name_range
        if paths is not None and site_path not in paths:
            continue
        facts.append(
            RefFact(
                site_path=site_path,
                site_span=site_span,
                target_path=normalise_path(target.path, case_fold=case_fold),
                target_span=target.name_range,
                kind=_edge_group(edge.kind, collapse_kinds),
            )
        )
    return facts, unprojectable


@dataclass(slots=True)
class FactSet:
    """Facts indexed by ``(path, line)`` so overlap matching stays linear."""

    definitions: dict[tuple[str, int], list[DefFact]] = field(default_factory=dict)
    references: dict[tuple[str, int], list[RefFact]] = field(default_factory=dict)

    @classmethod
    def build(
        cls, defs: Iterable[DefFact] = (), refs: Iterable[RefFact] = ()
    ) -> FactSet:
        out = cls()
        for definition in defs:
            out.definitions.setdefault(
                (definition.path, definition.line), []
            ).append(definition)
        for reference in refs:
            out.references.setdefault(
                (reference.site_path, reference.line), []
            ).append(reference)
        return out

    def all_definitions(self) -> Iterator[DefFact]:
        for bucket in self.definitions.values():
            yield from bucket

    def all_references(self) -> Iterator[RefFact]:
        for bucket in self.references.values():
            yield from bucket

    @property
    def paths(self) -> set[str]:
        return {path for path, _ in self.definitions} | {
            path for path, _ in self.references
        }


@dataclass(slots=True)
class MatchResult:
    """The outcome of comparing one predicted fact list against an oracle."""

    matched: list[tuple[Fact, Fact]] = field(default_factory=list)
    false_positives: list[Fact] = field(default_factory=list)
    false_negatives: list[Fact] = field(default_factory=list)
    tolerant_matches: int = 0

    @property
    def true_positive_count(self) -> int:
        return len(self.matched)

    def by_path(self) -> dict[str, tuple[int, int, int]]:
        """Per-file ``(tp, fp, fn)`` counts, the unit used for bootstrapping."""
        counts: dict[str, list[int]] = {}

        def bump(path: str, slot: int) -> None:
            counts.setdefault(path, [0, 0, 0])[slot] += 1

        for predicted, _oracle in self.matched:
            bump(_fact_path(predicted), 0)
        for predicted in self.false_positives:
            bump(_fact_path(predicted), 1)
        for oracle in self.false_negatives:
            bump(_fact_path(oracle), 2)
        return {path: (c[0], c[1], c[2]) for path, c in counts.items()}


def _fact_path(fact: Fact) -> str:
    if isinstance(fact, DefFact):
        return fact.path
    return fact.site_path


def _def_compatible(left: DefFact, right: DefFact, policy: MatchPolicy) -> bool:
    if left.kind and right.kind and left.kind != right.kind:
        return False
    if policy == "line":
        return True
    if policy == "exact":
        return left.span == right.span
    return left.span.overlaps(right.span)


def _ref_compatible(left: RefFact, right: RefFact, policy: MatchPolicy) -> bool:
    if left.kind and right.kind and left.kind != right.kind:
        return False
    if left.target_path != right.target_path:
        return False
    if policy == "line":
        return left.target_span.start.line == right.target_span.start.line
    if policy == "exact":
        return left.site_span == right.site_span and left.target_span == right.target_span
    return left.site_span.overlaps(right.site_span) and left.target_span.overlaps(
        right.target_span
    )


def _compatible(left: Fact, right: Fact, policy: MatchPolicy) -> bool:
    """Dispatch to the comparison for whichever fact type this is."""
    if isinstance(left, DefFact) and isinstance(right, DefFact):
        return _def_compatible(left, right, policy)
    if isinstance(left, RefFact) and isinstance(right, RefFact):
        return _ref_compatible(left, right, policy)
    return False


def match_facts(
    predicted: Iterable[Fact],
    oracle: Iterable[Fact],
    *,
    policy: MatchPolicy = "overlap",
) -> MatchResult:
    """Pair predicted facts with oracle facts, one to one, maximally.

    Facts only compete when they share a file and a line, so matching runs
    per line bucket, and buckets hold a handful of facts. Within a bucket
    the pairing is a maximum bipartite matching found by augmenting paths:
    exact pairs are placed first and the policy's looser pairs are only used
    to add matches, never to displace an exact one for a worse one.

    A greedy first-overlap pairing was wrong on the very lines tolerance is
    for. With a UTF-16 column shift two adjacent identifiers both overlap
    each other's oracle facts, and greedy paired the first candidate with
    its neighbour's fact, leaving one match on the table and counting a
    tolerant match where an exact one existed.
    """
    predicted_list = list(predicted)
    oracle_list = list(oracle)
    result = MatchResult()
    if not predicted_list and not oracle_list:
        return result

    oracle_buckets: dict[tuple[str, int], list[int]] = {}
    for index, fact in enumerate(oracle_list):
        oracle_buckets.setdefault((_fact_path(fact), fact.line), []).append(index)
    predicted_buckets: dict[tuple[str, int], list[int]] = {}
    for index, fact in enumerate(predicted_list):
        predicted_buckets.setdefault((_fact_path(fact), fact.line), []).append(index)

    # oracle index -> predicted index, over every bucket.
    owner: dict[int, int] = {}
    partner: dict[int, int] = {}

    def augment(candidate: int, allowed: list[int], attempt: MatchPolicy, seen: set[int]) -> bool:
        for oracle_index in allowed:
            if oracle_index in seen:
                continue
            if not _compatible(predicted_list[candidate], oracle_list[oracle_index], attempt):
                continue
            seen.add(oracle_index)
            current = owner.get(oracle_index)
            if current is None or augment(current, allowed, attempt, seen):
                owner[oracle_index] = candidate
                partner[candidate] = oracle_index
                return True
        return False

    passes: tuple[MatchPolicy, ...] = ("exact",) if policy == "exact" else ("exact", policy)
    for key, candidates in predicted_buckets.items():
        allowed = oracle_buckets.get(key, [])
        if not allowed:
            continue
        for attempt in passes:
            for candidate in candidates:
                if candidate not in partner:
                    augment(candidate, allowed, attempt, set())

    for index, candidate_fact in enumerate(predicted_list):
        oracle_index = partner.get(index)
        if oracle_index is None:
            result.false_positives.append(candidate_fact)
            continue
        oracle_fact = oracle_list[oracle_index]
        result.matched.append((candidate_fact, oracle_fact))
        if policy != "exact" and not _compatible(candidate_fact, oracle_fact, "exact"):
            # The pair only matched because the policy allowed slack, which
            # usually means the two producers count characters differently.
            result.tolerant_matches += 1
    for index, oracle_fact in enumerate(oracle_list):
        if index not in owner:
            result.false_negatives.append(oracle_fact)
    return result
