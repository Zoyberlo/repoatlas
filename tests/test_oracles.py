"""Score the extractor against a real indexer, in every language claimed.

`test_integration.py` does this for TypeScript in detail. This does it for
all three committed oracles at once, and its job is narrower: hold each
language to the numbers it reached, so a tag query that starts missing
definitions cannot pass CI quietly.

The floors are the measured values rounded down, not aspirations. When a
change improves a language, the floor moves up with it in the same commit;
that is what stops "we improved Python" from being a claim nobody checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from repoatlas.eval.compare import ComparisonOptions, compare_snapshots
from repoatlas.oracle.scip import read_scip

pytest.importorskip("tree_sitter", reason="needs the parse extra")
pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

from repoatlas.parse.build import build_snapshot

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class Oracle:
    """One committed index, and the floors it holds the extractor to."""

    name: str
    producer: str
    paths: frozenset[str]
    definition_precision: float
    definition_recall: float
    reference_precision: float
    reference_recall: float

    @property
    def directory(self) -> Path:
        return FIXTURES / self.name


ORACLES = (
    Oracle(
        name="tsdemo",
        producer="scip-typescript 0.4.0",
        paths=frozenset({"src/app.ts", "src/user.ts"}),
        definition_precision=1.0,
        definition_recall=1.0,
        reference_precision=0.95,
        reference_recall=0.95,
    ),
    Oracle(
        name="pydemo",
        producer="scip-python 0.6.6",
        paths=frozenset({"src/app.py", "src/models.py", "src/__init__.py"}),
        definition_precision=1.0,
        definition_recall=1.0,
        reference_precision=1.0,
        reference_recall=0.90,
    ),
    Oracle(
        name="phpdemo",
        producer="scip-php 0.0.1",
        paths=frozenset({"src/App.php", "src/Greeter.php", "src/LoudGreeter.php"}),
        definition_precision=1.0,
        definition_recall=1.0,
        reference_precision=0.90,
        reference_recall=0.95,
    ),
)

IDS = [oracle.name for oracle in ORACLES]


@pytest.fixture(scope="module", params=ORACLES, ids=IDS)
def scored(request):
    oracle: Oracle = request.param
    truth = read_scip(oracle.directory / "index.scip")
    # `use_git=False` so the test needs no git, and so the fixture's own
    # generated files cannot creep into the walk.
    candidate = build_snapshot(oracle.directory, use_git=False).snapshot
    comparison = compare_snapshots(candidate, truth, ComparisonOptions())
    return oracle, truth, comparison


def test_the_oracle_is_a_real_index(scored) -> None:
    oracle, truth, _ = scored
    assert truth.producer == oracle.producer
    assert truth.symbols and truth.edges


def test_paths_normalise_to_the_files_the_fixture_holds(scored) -> None:
    from repoatlas.eval.facts import normalise_path

    oracle, truth, _ = scored
    seen = {normalise_path(path) for path in truth.paths}
    assert oracle.paths <= seen, sorted(seen)


def test_definitions_hold_their_floor(scored) -> None:
    oracle, _, comparison = scored
    score = comparison.definitions
    assert score.precision >= oracle.definition_precision, score.as_dict()
    assert score.recall >= oracle.definition_recall, score.as_dict()


def test_references_hold_their_floor(scored) -> None:
    oracle, _, comparison = scored
    score = comparison.references
    assert score.precision >= oracle.reference_precision, score.as_dict()
    assert score.recall >= oracle.reference_recall, score.as_dict()


def test_no_edge_points_at_a_symbol_the_index_never_defined(scored) -> None:
    # A dangling edge is the one failure an agent cannot recover from: it
    # is handed a location that does not exist.
    _, _, comparison = scored
    assert comparison.dangling_edges == []


def test_every_language_is_covered_by_an_oracle(scored) -> None:
    # Guards the gap this module closed: PHP and Python shipped for a week
    # with every published number measured on TypeScript alone.
    covered = {"typescript", "python", "php"}
    languages = set()
    for oracle in ORACLES:
        result = build_snapshot(oracle.directory, use_git=False)
        languages.update(result.by_language)
    assert covered <= languages
