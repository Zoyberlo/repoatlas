"""Tests for the benchmark, which is what makes "large" a number.

The benchmark is not asserted on for speed here; a laptop under load would
make that flaky. What is asserted is that it measures the real pipeline:
the synthetic repository parses, its imports resolve across files, every
phase is timed, and the JSON carries what a later run needs to compare
against.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoatlas.bench import generate_synthetic, run_benchmark

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")


class TestSyntheticRepository:
    def test_generates_the_requested_count_across_languages(self, tmp_path: Path) -> None:
        written = generate_synthetic(tmp_path, 30)
        assert written == 30
        assert len(list((tmp_path / "synth").glob("mod_*.py"))) == 10
        assert len(list((tmp_path / "src").glob("mod_*.ts"))) == 10
        assert len(list((tmp_path / "app" / "Synthetic").glob("Service*.php"))) == 10

    def test_every_file_parses_and_imports_resolve(self, tmp_path: Path) -> None:
        from repoatlas.parse.build import build_snapshot

        generate_synthetic(tmp_path, 30)
        result = build_snapshot(tmp_path, use_git=False)
        assert result.failures == []
        assert result.error_rate == 0.0
        # Every module imports two others by a fixed rule; those must land
        # on the import-map rung, or the benchmark is timing a resolver
        # that has nothing to do.
        assert result.resolution.by_tier.get("import_map", 0) >= 30

    def test_an_unknown_language_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="no synthetic generator"):
            generate_synthetic(tmp_path, 3, languages=("cobol",))


class TestRunBenchmark:
    def test_measures_every_phase(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        generate_synthetic(root, 24)
        result = run_benchmark(root, tmp_path / "bench.db", use_git=False)
        # The Python third brings a package `__init__.py` along.
        assert result.files == 25
        assert result.symbols > 24
        assert result.edges > 0
        assert result.store_bytes > 0
        assert result.index_cold_seconds > 0
        assert result.index_noop_seconds >= 0
        assert result.index_touch_seconds > 0
        for name in ("repo_map cold", "repo_map warm", "search_symbols", "get_symbol", "index_status"):
            assert name in result.tool_seconds, name

    def test_the_touched_file_is_restored(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        generate_synthetic(root, 6)
        before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
        run_benchmark(root, tmp_path / "bench.db", use_git=False)
        after = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
        assert before == after

    def test_the_json_carries_the_environment(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        generate_synthetic(root, 6)
        payload = run_benchmark(root, tmp_path / "bench.db", use_git=False).as_dict()
        assert set(payload["environment"]) >= {"repoatlas", "python", "platform", "numpy"}
        json.dumps(payload)


class TestBenchCli:
    def test_generates_and_reports(self, tmp_path: Path, capsys) -> None:
        from repoatlas.cli import main

        out = tmp_path / "result.json"
        code = main(["bench", str(tmp_path / "repo"), "--synthetic", "12", "--out", str(out)])
        assert code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["files"] == 13
        assert "index cold" in capsys.readouterr().out

    def test_a_missing_directory_is_refused(self, tmp_path: Path) -> None:
        from repoatlas.cli import main

        with pytest.raises(SystemExit, match="not a directory"):
            main(["bench", str(tmp_path / "nope")])
