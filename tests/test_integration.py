"""End-to-end: extract a real project and score it against a real oracle.

Every other SCIP test in this suite builds its fixture by hand, which proves
the reader is self-consistent without proving it agrees with the format as
an actual indexer writes it. This module closes that gap using an
``index.scip`` committed straight from ``scip-typescript`` 0.4.0.

It is also the regression test for the whole point of the project. If a
change to a tag query starts missing definitions, or the extractor begins
inventing them, the numbers here move and CI says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoatlas.eval.compare import ComparisonOptions, compare_snapshots
from repoatlas.eval.facts import NAVIGABLE_KINDS, definition_facts, match_facts
from repoatlas.eval.report import to_markdown
from repoatlas.model import EdgeKind, SymbolKind
from repoatlas.oracle.scip import read_scip

pytest.importorskip("tree_sitter", reason="needs the parse extra")
pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

from repoatlas.parse.build import build_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "tsdemo"


@pytest.fixture(scope="module")
def oracle():
    return read_scip(FIXTURE / "index.scip")


@pytest.fixture(scope="module")
def build():
    # `use_git=False` so the test does not depend on git being installed or
    # on the fixture being checked in; git would list the same two files.
    return build_snapshot(FIXTURE, use_git=False)


class TestRealOracle:
    def test_reads_a_genuine_scip_typescript_index(self, oracle) -> None:
        assert oracle.producer == "scip-typescript 0.4.0"
        assert len(oracle.symbols) > 10
        assert oracle.edges

    def test_paths_normalise_to_posix_whatever_host_wrote_the_index(
        self, oracle
    ) -> None:
        # The committed index was written on Windows and carries
        # backslashes; one regenerated on Linux would not. Either way the
        # comparison sees the same two POSIX paths.
        from repoatlas.eval.facts import normalise_path

        assert {normalise_path(p) for p in oracle.paths} == {"src/app.ts", "src/user.ts"}

    def test_finds_the_expected_declarations(self, oracle) -> None:
        names = {s.name for s in oracle.symbols.values() if not s.synthetic}
        assert {"User", "Admin", "Greets", "makeUser", "run", "greet"} <= names

    def test_records_inheritance_as_relationships(self, oracle) -> None:
        assert any(e.kind is EdgeKind.IMPLEMENTS for e in oracle.edges)

    def test_marks_function_locals_as_local(self, oracle) -> None:
        locals_found = [s for s in oracle.symbols.values() if s.local]
        assert locals_found, "scip-typescript emits `local N` symbols"
        assert all(s.id.startswith("local ") for s in locals_found)


class TestExtraction:
    def test_parses_the_fixture_without_errors(self, build) -> None:
        assert build.files == 2
        assert build.error_rate == 0.0

    def test_finds_the_expected_declarations(self, build) -> None:
        names = {s.name for s in build.snapshot.symbols.values() if not s.synthetic}
        assert {"User", "Admin", "Greets", "makeUser", "run", "greet"} <= names

    def test_marks_a_binding_inside_a_function_as_local(self, build) -> None:
        by_name = {
            s.name: s for s in build.snapshot.symbols.values() if not s.synthetic
        }
        # `const user` lives in the body of `run`.
        assert by_name["user"].local
        # `DEFAULT_NAME` is exported from the module.
        assert not by_name["DEFAULT_NAME"].local

    def test_emits_containment_for_class_members(self, build) -> None:
        contains = [e for e in build.snapshot.edges if e.kind is EdgeKind.CONTAINS]
        assert contains
        by_id = build.snapshot.symbols
        for edge in contains:
            assert by_id[edge.src_id].full_range is not None
            assert by_id[edge.dst_id].container_id == edge.src_id

    def test_collects_references_for_the_resolver_to_use(self, build) -> None:
        kinds = {reference.kind for _path, reference in build.references}
        assert {"call", "import", "class"} <= kinds


class TestAccuracyAgainstOracle:
    """The headline claim, checked on every CI run."""

    def test_finds_every_navigable_definition_the_oracle_has(
        self, build, oracle
    ) -> None:
        result = compare_snapshots(build.snapshot, oracle)
        assert result.definitions.recall == 1.0, (
            "missed: " + ", ".join(str(f) for f in (result.definition_result or ()).false_negatives)
            if result.definition_result
            else "missed definitions"
        )

    def test_invents_no_definitions(self, build, oracle) -> None:
        result = compare_snapshots(build.snapshot, oracle)
        assert result.definitions.precision == 1.0

    def test_reports_no_dangling_edges(self, build, oracle) -> None:
        result = compare_snapshots(build.snapshot, oracle)
        assert result.dangling_edges == []

    def test_containment_is_out_of_scope_rather_than_wrong(
        self, build, oracle
    ) -> None:
        # SCIP records occurrences, not structure, so it never emits a
        # containment edge. Scoring ours against it would call every one a
        # false positive for a claim the oracle simply does not make.
        result = compare_snapshots(build.snapshot, oracle)
        assert "contains" in result.unscored_edge_kinds
        assert result.unscored_edges > 0
        assert result.references.false_positives == 0

    def test_reference_recall_is_zero_until_the_resolver_exists(
        self, build, oracle
    ) -> None:
        # Deliberate, and the number to watch: the extractor collects
        # references but resolves none, so it claims no reference edges.
        # This asserts the honest zero rather than hiding it.
        result = compare_snapshots(build.snapshot, oracle)
        assert result.references.recall == 0.0
        assert result.references.false_negatives > 0

    def test_scoring_locals_too_shows_the_scope_difference(
        self, build, oracle
    ) -> None:
        # With locals and parameters included the score drops, because the
        # oracle indexes every binding a compiler resolves and the extractor
        # deliberately does not. Asserting this keeps the default scope an
        # explicit choice rather than a number that flatters the extractor.
        strict = match_facts(
            definition_facts(build.snapshot, include_local=True),
            definition_facts(oracle, include_local=True),
        )
        default = match_facts(
            definition_facts(build.snapshot, kinds=NAVIGABLE_KINDS),
            definition_facts(oracle, kinds=NAVIGABLE_KINDS),
        )
        assert len(strict.false_negatives) > len(default.false_negatives)

    def test_the_report_states_its_scope(self, build, oracle) -> None:
        text = to_markdown(compare_snapshots(build.snapshot, oracle))
        assert "Symbol kinds in scope:" in text
        assert "Not scored, because the oracle emits no such edge" in text

    def test_the_exact_policy_agrees_on_every_column(
        self, build, oracle
    ) -> None:
        # Both producers count columns in UTF-8 here, so tolerance should not
        # be doing any work. If this ever fails, the two disagree about
        # offsets and the overlap policy is hiding it.
        result = compare_snapshots(
            build.snapshot, oracle, ComparisonOptions(policy="exact")
        )
        assert result.definitions.f1 == 1.0
        assert result.tolerant_definition_matches == 0


def _family(kind: SymbolKind) -> str:
    """Group kinds the way SCIP's descriptor grammar can actually tell apart.

    A SCIP symbol ends in `#` for every named type alike, so a class, an
    interface and a type alias are one thing as far as the symbol string
    goes. Comparing exact kinds across that boundary measures the format's
    resolution, not either producer's accuracy; comparing families measures
    something real.
    """
    if kind.is_type_like:
        return "type"
    if kind.is_callable:
        return "callable"
    if kind in (SymbolKind.FIELD, SymbolKind.PROPERTY, SymbolKind.CONSTANT, SymbolKind.VARIABLE):
        return "value"
    return kind.value


class TestKindAgreement:
    """Whether the two producers agree about what each symbol is."""

    @staticmethod
    def _pairs(build, oracle):
        """Pair symbols by exact identifier position.

        The column matters: a method and its first parameter start on the
        same line, and pairing by line alone compares one against the other.
        """
        candidate = {
            (
                s.path.replace("\\", "/"),
                s.name_range.start.line,
                s.name_range.start.character,
            ): s
            for s in build.snapshot.symbols.values()
            if not s.synthetic and not s.local
        }
        for symbol in oracle.symbols.values():
            if symbol.synthetic or symbol.local or symbol.kind is SymbolKind.UNKNOWN:
                continue
            key = (
                symbol.path.replace("\\", "/"),
                symbol.name_range.start.line,
                symbol.name_range.start.character,
            )
            mine = candidate.get(key)
            if mine is not None:
                yield symbol, mine

    def test_every_symbol_agrees_at_family_level(self, build, oracle) -> None:
        mismatches = [
            f"{theirs.name}: oracle {theirs.kind.value}, extractor {mine.kind.value}"
            for theirs, mine in self._pairs(build, oracle)
            if _family(theirs.kind) != _family(mine.kind)
        ]
        assert not mismatches, "; ".join(mismatches)

    def test_a_free_function_is_not_reported_as_a_method(
        self, build, oracle
    ) -> None:
        # `makeUser` and `run` are top-level. The descriptor `makeUser().`
        # looks method-shaped, and only the absence of a `#` ahead of it says
        # otherwise.
        by_name = {s.name: s for s in oracle.symbols.values()}
        assert by_name["makeUser"].kind is SymbolKind.FUNCTION
        assert by_name["run"].kind is SymbolKind.FUNCTION

    def test_a_class_member_is_reported_as_a_method(self, build, oracle) -> None:
        by_id = {s.id: s for s in oracle.symbols.values()}
        greet = next(s for i, s in by_id.items() if i.endswith("User#greet()."))
        assert greet.kind is SymbolKind.METHOD

    def test_a_constructor_is_recognised(self, build, oracle) -> None:
        constructors = [
            s for s in oracle.symbols.values() if s.kind is SymbolKind.CONSTRUCTOR
        ]
        assert constructors, "the fixture declares one"
