"""Regression tests from an adversarial review of the first two stages.

Each test here reproduces a defect that the fixture-sized suite had not
caught, and states what the failure looked like. They stay together so the
next review can see what the last one found.
"""

from __future__ import annotations

import json
import time

import pytest

from repoatlas.eval.compare import compare_snapshots
from repoatlas.eval.facts import DefFact, match_facts
from repoatlas.eval.metrics import bootstrap_interval
from repoatlas.model import (
    Edge,
    EdgeKind,
    IndexSnapshot,
    ResolutionTier,
    SourceRange,
    Symbol,
    SymbolKind,
)
from repoatlas.oracle.scip import (
    ScipError,
    SymbolRole,
    parse_symbol,
    read_scip,
    read_scip_binary,
    read_scip_json,
)
from repoatlas.spans import ScopeIndex

from .conftest import SYM_GREET, SYM_MAIN, SYM_USER, IndexSpec, OccurrenceSpec


class TestScopeIndex:
    def test_finds_the_innermost_of_nested_ranges(self) -> None:
        outer = SourceRange.of(0, 0, 10, 0)
        inner = SourceRange.of(2, 0, 5, 0)
        index = ScopeIndex([(outer, "outer"), (inner, "inner")])
        assert index.innermost(SourceRange.of(3, 1, 3, 4)) == "inner"
        assert index.innermost(SourceRange.of(7, 0, 7, 1)) == "outer"
        assert index.innermost(SourceRange.of(12, 0, 12, 1)) is None

    def test_skips_a_range_equal_to_the_excluded_one(self) -> None:
        shared = SourceRange.of(1, 0, 1, 20)
        index = ScopeIndex([(SourceRange.of(0, 0, 9, 0), "class"), (shared, "sibling")])
        assert index.innermost(SourceRange.of(1, 12, 1, 14), exclude=shared) == "class"

    def test_stops_early_instead_of_scanning_every_earlier_range(self) -> None:
        # Ten thousand disjoint top-level functions, one lookup per function:
        # a linear scan is quadratic, this must not be.
        spans = [(SourceRange.of(i * 3, 0, i * 3 + 2, 0), i) for i in range(10_000)]
        index = ScopeIndex(spans)
        started = time.perf_counter()
        for i in range(10_000):
            assert index.innermost(SourceRange.of(i * 3 + 1, 0, i * 3 + 1, 1)) == i
        assert time.perf_counter() - started < 1.0


class TestExtractorRegressions:
    @pytest.fixture(autouse=True)
    def _needs_parser(self) -> None:
        pytest.importorskip("tree_sitter_language_pack")

    @staticmethod
    def _extract(path: str, source: bytes):
        from repoatlas.parse.extract import extract_source
        from repoatlas.parse.languages import language_for_path

        spec = language_for_path(path)
        assert spec is not None
        return extract_source(path, source, spec)

    def test_sibling_declarations_sharing_one_node_do_not_nest(self) -> None:
        # `public $a, $b;` and `const X = 1, Y = 2;` are one node each. The
        # second member used to be filed inside the first as `G.a.b`.
        source = b"<?php\nclass G {\n    public $a, $b;\n    const X = 1, Y = 2;\n}\n"
        result = self._extract("g.php", source)
        assert [s.qualified_name for s in result.symbols] == ["G", "G.a", "G.b", "G.X", "G.Y"]

    def test_every_definition_capture_yields_exactly_one_symbol(self) -> None:
        source = b"<?php\nclass G {\n    public $a, $b, $c;\n}\n"
        result = self._extract("g.php", source)
        assert sorted(s.name for s in result.symbols) == ["G", "a", "b", "c"]
        assert all(s.container_id == "g.php#G" for s in result.symbols if s.name != "G")

    def test_a_decorator_call_is_one_reference_not_two(self) -> None:
        source = b"@dec()\ndef f():\n    pass\n"
        result = self._extract("d.py", source)
        assert [r.name for r in result.references] == ["dec"]

    def test_a_file_with_thousands_of_symbols_extracts_in_linear_time(self) -> None:
        # 8 000 definitions took 15 s with a linear container scan against
        # 36 ms to parse. Generated code looks like this.
        source = b"".join(
            b"def f%d():\n    return g%d()\n\n" % (i, i) for i in range(6_000)
        )
        started = time.perf_counter()
        result = self._extract("big.py", source)
        elapsed = time.perf_counter() - started
        assert len(result.symbols) == 6_000
        assert len(result.references) == 6_000
        assert elapsed < 3.0, f"took {elapsed:.2f}s"


class TestScipRegressions:
    def test_a_second_definition_of_one_symbol_does_not_crash(
        self, demo_index: IndexSpec
    ) -> None:
        # scip-python emits a Definition per assignment of a module-level
        # name; scip-typescript one per overload signature. The reader
        # raised a bare ValueError on the second.
        demo_index.documents[1].occurrences.append(
            OccurrenceSpec(SYM_MAIN, [4, 9, 13], SymbolRole.DEFINITION, [4, 0, 6, 1])
        )
        snapshot = read_scip_binary(demo_index.to_binary())
        main = snapshot.symbols[SYM_MAIN]
        assert main.name_range.to_scip() == [1, 9, 13], "the first definition anchors"

    def test_enclosing_lookup_scales_to_a_large_document(
        self, demo_index: IndexSpec
    ) -> None:
        # 4 000 definitions with 20 000 references took 14 s per document.
        package = "scip-typescript npm big 1.0.0 src/`big.ts`/"
        occurrences = []
        for i in range(4_000):
            symbol = f"{package}f{i}()."
            base = i * 5
            occurrences.append(
                OccurrenceSpec(symbol, [base, 9, 12], SymbolRole.DEFINITION, [base, 0, base + 4, 1])
            )
            for k in range(5):
                occurrences.append(OccurrenceSpec(f"{package}f{(i + 1) % 4_000}().", [base + 1 + k % 3, 4, 7]))
        from .conftest import DocumentSpec

        demo_index.documents.append(DocumentSpec(path="src/big.ts", occurrences=occurrences))
        started = time.perf_counter()
        snapshot = read_scip_binary(demo_index.to_binary())
        elapsed = time.perf_counter() - started
        assert sum(1 for e in snapshot.edges if e.site_path == "src/big.ts") == 20_000
        assert elapsed < 5.0, f"took {elapsed:.2f}s"

    def test_reads_a_json_dump_written_by_windows_powershell(
        self, tmp_path, demo_index: IndexSpec
    ) -> None:
        # PowerShell 5.1 redirection writes UTF-16 with a byte-order mark,
        # and that is the shell the documented command often runs in.
        dump = tmp_path / "index.json"
        dump.write_bytes(demo_index.to_json().encode("utf-16"))
        assert SYM_USER in read_scip(dump).symbols
        assert SYM_USER in read_scip_json(dump).symbols

    def test_reads_a_json_dump_with_a_utf8_bom(self, tmp_path, demo_index: IndexSpec) -> None:
        dump = tmp_path / "index.json"
        dump.write_bytes(b"\xef\xbb\xbf" + demo_index.to_json().encode("utf-8"))
        assert SYM_USER in read_scip(dump).symbols

    def test_malformed_json_is_a_scip_error_not_a_traceback(self, tmp_path) -> None:
        dump = tmp_path / "index.json"
        dump.write_text("{not json", encoding="utf-8")
        with pytest.raises(ScipError, match="not valid JSON"):
            read_scip(dump)

    def test_double_space_escapes_a_space_in_the_package_fields(self) -> None:
        parsed = parse_symbol("scip-dotnet nuget My  Package 1.0 Ns/Cls#")
        assert parsed.package_name == "My Package"
        assert parsed.version == "1.0"
        assert parsed.descriptors == ("Ns/", "Cls#")


class TestMatchingRegressions:
    @staticmethod
    def _def(line: int, start: int, end: int) -> DefFact:
        return DefFact("a.py", SourceRange.of(line, start, line, end))

    def test_an_exact_partner_wins_over_an_earlier_overlap(self) -> None:
        # The greedy matcher took the first overlapping oracle fact, counted
        # it as a tolerant match, and left the exact partner as a miss.
        result = match_facts([self._def(0, 0, 6)], [self._def(0, 4, 7), self._def(0, 0, 6)])
        assert result.true_positive_count == 1
        assert result.tolerant_matches == 0
        assert len(result.false_negatives) == 1  # the 4-7 fact, honestly unmatched

    def test_two_shifted_pairs_both_match(self) -> None:
        result = match_facts(
            [self._def(0, 0, 6), self._def(0, 5, 8)],
            [self._def(0, 4, 7), self._def(0, 0, 3)],
        )
        assert result.true_positive_count == 2


class TestCalibrationRegression:
    def test_a_wrong_edge_beside_a_right_one_is_scored_wrong(
        self, demo_index: IndexSpec
    ) -> None:
        # Resolving `u.greet()` both to the method (right) and, at low
        # confidence, to the class (wrong) shared one calibration key, so the
        # fuzzy edge was credited as correct.
        oracle = read_scip_binary(demo_index.to_binary())
        candidate = read_scip_binary(demo_index.to_binary())
        greet_call = next(e for e in candidate.edges if e.dst_id == SYM_GREET)
        candidate.add_edge(
            Edge(
                src_id=greet_call.src_id,
                dst_id=SYM_USER,
                kind=EdgeKind.CALLS,
                tier=ResolutionTier.FUZZY,
                site_path=greet_call.site_path,
                site_range=greet_call.site_range,
            )
        )
        result = compare_snapshots(candidate, oracle)
        assert result.references.false_positives == 1
        assert result.calibration is not None
        fuzzy = next(b for b in result.calibration.bins if b.count and b.low < 0.4)
        assert fuzzy.correct == 0


class TestBootstrapRegression:
    def test_many_files_do_not_take_minutes(self) -> None:
        units = [(8, 2, 1), (5, 5, 5), (10, 0, 0), (0, 3, 4)] * 500
        started = time.perf_counter()
        bootstrap_interval(units, resamples=200)
        assert time.perf_counter() - started < 2.0


class TestCliEncoding:
    def test_non_ascii_paths_survive_a_redirected_stdout(
        self, tmp_path, demo_index: IndexSpec, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from repoatlas.cli import main

        demo_index.documents[0].path = "src/користувач.ts"
        binary = tmp_path / "index.scip"
        binary.write_bytes(demo_index.to_binary())
        assert main(["inspect", str(binary), "--paths"]) == 0
        assert "користувач" in capsys.readouterr().out


def test_snapshot_reports_json_round_trip(demo_index: IndexSpec) -> None:
    """The JSON report must stay loadable after the new fields."""
    from repoatlas.eval.report import to_json

    oracle = read_scip_binary(demo_index.to_binary())
    payload = json.loads(to_json(compare_snapshots(oracle, oracle)))
    assert payload["encoding_assumed"] is True
    assert isinstance(payload["symbol_kind_scope"], list)
    assert SymbolKind.CLASS.value in payload["symbol_kind_scope"]
    assert isinstance(IndexSnapshot().encoding_declared, bool)
    assert Symbol  # keep the import honest for the type checker
