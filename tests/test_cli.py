"""Tests for the command line interface and the oracle cross-check.

The cross-check is the guard on this project's riskiest assumption: that its
hand-written SCIP field numbers match the real schema. These tests prove the
check itself catches a disagreement, so that a clean run against a real
indexer means something.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoatlas.cli import main
from repoatlas.oracle.scip import cross_check

from .conftest import SYM_USER, IndexSpec


@pytest.fixture
def index_files(tmp_path: Path, demo_index: IndexSpec) -> tuple[Path, Path]:
    binary = tmp_path / "index.scip"
    dump = tmp_path / "index.json"
    binary.write_bytes(demo_index.to_binary())
    dump.write_text(demo_index.to_json(), encoding="utf-8")
    return binary, dump


class TestCrossCheck:
    def test_agreeing_readings_report_no_problems(
        self, index_files: tuple[Path, Path]
    ) -> None:
        assert cross_check(*index_files) == []

    def test_detects_a_symbol_present_in_only_one_reading(
        self, tmp_path: Path, demo_index: IndexSpec
    ) -> None:
        binary = tmp_path / "index.scip"
        binary.write_bytes(demo_index.to_binary())
        payload = json.loads(demo_index.to_json())
        payload["documents"][0]["occurrences"].pop()
        dump = tmp_path / "index.json"
        dump.write_text(json.dumps(payload), encoding="utf-8")
        problems = cross_check(binary, dump)
        assert any("only in binary" in problem for problem in problems)

    def test_detects_a_symbol_at_a_different_location(
        self, tmp_path: Path, demo_index: IndexSpec
    ) -> None:
        binary = tmp_path / "index.scip"
        binary.write_bytes(demo_index.to_binary())
        payload = json.loads(demo_index.to_json())
        for occurrence in payload["documents"][0]["occurrences"]:
            if occurrence["symbol"] == SYM_USER:
                occurrence["range"] = [3, 0, 4]
        dump = tmp_path / "index.json"
        dump.write_text(json.dumps(payload), encoding="utf-8")
        problems = cross_check(binary, dump)
        assert any(SYM_USER in problem for problem in problems)

    def test_detects_differing_edge_sets(
        self, tmp_path: Path, demo_index: IndexSpec
    ) -> None:
        binary = tmp_path / "index.scip"
        binary.write_bytes(demo_index.to_binary())
        payload = json.loads(demo_index.to_json())
        payload["documents"][1]["occurrences"] = [
            occurrence
            for occurrence in payload["documents"][1]["occurrences"]
            if occurrence.get("symbolRoles") == 1
        ]
        dump = tmp_path / "index.json"
        dump.write_text(json.dumps(payload), encoding="utf-8")
        assert any("edge sets differ" in problem for problem in cross_check(binary, dump))


class TestInspect:
    def test_prints_a_summary(
        self, index_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["inspect", str(index_files[0])]) == 0
        out = capsys.readouterr().out
        assert "scip-typescript" in out
        assert "symbols:" in out
        assert "edges.imports:" in out

    def test_lists_files_on_request(
        self, index_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["inspect", str(index_files[0]), "--paths"])
        out = capsys.readouterr().out
        assert "src/app.ts" in out
        assert "src/user.ts" in out

    def test_reads_the_json_form_too(
        self, index_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["inspect", str(index_files[1])]) == 0
        assert "symbols:" in capsys.readouterr().out

    def test_reports_a_missing_file_clearly(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="no such file"):
            main(["inspect", str(tmp_path / "absent.scip")])

    def test_reports_a_corrupt_file_clearly(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.scip"
        broken.write_bytes(b"\x0a\xff\xff\xff")
        with pytest.raises(SystemExit, match="cannot read"):
            main(["inspect", str(broken)])


class TestVerifyOracle:
    def test_succeeds_when_the_readers_agree(
        self, index_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["verify-oracle", str(index_files[0]), str(index_files[1])]) == 0
        assert "corroborated" in capsys.readouterr().out

    def test_fails_when_the_readers_disagree(
        self, tmp_path: Path, demo_index: IndexSpec, capsys: pytest.CaptureFixture[str]
    ) -> None:
        binary = tmp_path / "index.scip"
        binary.write_bytes(demo_index.to_binary())
        payload = json.loads(demo_index.to_json())
        payload["documents"][0]["occurrences"].pop()
        dump = tmp_path / "index.json"
        dump.write_text(json.dumps(payload), encoding="utf-8")
        assert main(["verify-oracle", str(binary), str(dump)]) == 1
        assert "disagreement" in capsys.readouterr().err


class TestCompare:
    def test_an_index_scores_perfectly_against_itself(
        self, index_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        binary = str(index_files[0])
        assert main(["compare", binary, binary]) == 0
        out = capsys.readouterr().out
        assert "# Index accuracy" in out
        assert "| definitions | 1.000 | 1.000 | 1.000 |" in out

    def test_json_output_parses(
        self, index_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        binary = str(index_files[0])
        main(["compare", binary, binary, "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["references"]["f1"] == 1.0

    def test_writes_to_a_file_when_asked(
        self, index_files: tuple[Path, Path], tmp_path: Path
    ) -> None:
        target = tmp_path / "reports" / "accuracy.md"
        binary = str(index_files[0])
        assert main(["compare", binary, binary, "--out", str(target)]) == 0
        assert target.exists()
        assert "# Index accuracy" in target.read_text(encoding="utf-8")

    def test_the_f1_gate_passes_a_good_index(
        self, index_files: tuple[Path, Path]
    ) -> None:
        binary = str(index_files[0])
        assert main(["compare", binary, binary, "--min-f1", "0.9"]) == 0

    def test_the_f1_gate_fails_a_bad_index(
        self,
        tmp_path: Path,
        demo_index: IndexSpec,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        oracle = tmp_path / "oracle.scip"
        oracle.write_bytes(demo_index.to_binary())
        # A candidate that found the definitions but no references at all.
        for document in demo_index.documents:
            document.occurrences = [
                occurrence for occurrence in document.occurrences if occurrence.roles & 1
            ]
        candidate = tmp_path / "candidate.scip"
        candidate.write_bytes(demo_index.to_binary())
        assert main(["compare", str(candidate), str(oracle), "--min-f1", "0.9"]) == 1
        assert "below the required" in capsys.readouterr().err

    def test_accepts_the_matching_policy_flag(
        self, index_files: tuple[Path, Path]
    ) -> None:
        binary = str(index_files[0])
        assert main(["compare", binary, binary, "--policy", "exact"]) == 0

    def test_accepts_the_kind_splitting_flag(
        self, index_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        binary = str(index_files[0])
        main(["compare", binary, binary, "--split-kinds", "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        assert "references" in payload["references_by_kind"]


class TestParser:
    def test_requires_a_command(self) -> None:
        with pytest.raises(SystemExit):
            main([])

    def test_reports_its_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            main(["--version"])
        assert exit_info.value.code == 0
        assert "repoatlas" in capsys.readouterr().out


tree_sitter = pytest.importorskip("tree_sitter", reason="needs the parse extra")
pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "tsdemo"


class TestIndex:
    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text(
            "class A:\n    def m(self): pass\n", encoding="utf-8"
        )
        (tmp_path / "src" / "b.ts").write_text(
            "export function f(): void {}\n", encoding="utf-8"
        )
        return tmp_path

    def test_reports_what_it_parsed(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["index", str(project), "--no-git"]) == 0
        out = capsys.readouterr().out
        assert "files:      2" in out
        assert "python" in out
        assert "typescript" in out

    def test_json_output_carries_the_per_language_breakdown(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["index", str(project), "--no-git", "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["files"] == 2
        assert payload["by_language"]["python"]["files"] == 1
        assert payload["by_language"]["python"]["error_rate"] == 0.0

    def test_the_error_gate_passes_clean_sources(self, project: Path) -> None:
        assert main(["index", str(project), "--no-git", "--max-error-rate", "0.0"]) == 0

    def test_the_error_gate_fails_broken_sources(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (project / "src" / "broken.py").write_text("class Broken(:\n", encoding="utf-8")
        assert main(["index", str(project), "--no-git", "--max-error-rate", "0.0"]) == 1
        assert "exceeds the allowed" in capsys.readouterr().err

    def test_rejects_a_file_where_a_directory_is_wanted(self, project: Path) -> None:
        with pytest.raises(SystemExit, match="not a directory"):
            main(["index", str(project / "src" / "a.py")])


class TestCompareAgainstADirectory:
    def test_parses_a_repository_and_scores_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The whole loop in one command: parse the fixture project, score it
        # against the real scip-typescript index committed beside it.
        exit_code = main(
            ["compare", str(FIXTURE_REPO), str(FIXTURE_REPO / "index.scip")]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "repoatlas" in out
        assert "scip-typescript" in out
        assert "| definitions | 1.000 | 1.000 | 1.000 |" in out
