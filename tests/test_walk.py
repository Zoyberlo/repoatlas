"""Tests for deciding which files to read.

Getting traversal wrong is quietly expensive: walking `node_modules` turns a
two-second index into a two-minute one, and skipping a source directory
produces an index that looks healthy and answers wrongly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from repoatlas.parse.walk import (
    DEFAULT_EXCLUDED_DIRECTORIES,
    WalkStats,
    is_git_repository,
    iter_source_files,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    (tmp_path / "src" / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("module.exports = 1;\n", encoding="utf-8")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lib.php").write_text("<?php\n", encoding="utf-8")
    return tmp_path


def paths_of(root: Path, **kwargs: object) -> list[str]:
    return sorted(f.path for f in iter_source_files(root, **kwargs))  # type: ignore[arg-type]


class TestSelection:
    def test_finds_source_files(self, project: Path) -> None:
        assert paths_of(project, use_git=False) == ["src/app.ts", "src/main.py"]

    def test_skips_files_in_languages_it_cannot_parse(self, project: Path) -> None:
        assert "README.md" not in paths_of(project, use_git=False)

    def test_skips_dependency_directories(self, project: Path) -> None:
        found = paths_of(project, use_git=False)
        assert not any("node_modules" in path for path in found)
        assert not any("vendor" in path for path in found)

    def test_reports_posix_paths_on_every_platform(self, project: Path) -> None:
        assert all("\\" not in path for path in paths_of(project, use_git=False))

    def test_records_the_language_of_each_file(self, project: Path) -> None:
        by_path = {f.path: f.language.name for f in iter_source_files(project, use_git=False)}
        assert by_path["src/main.py"] == "python"
        assert by_path["src/app.ts"] == "typescript"

    def test_skips_a_file_above_the_size_limit(self, project: Path) -> None:
        big = project / "src" / "generated.py"
        big.write_text("x = 1\n" * 50_000, encoding="utf-8")
        stats = WalkStats()
        found = [f.path for f in iter_source_files(project, use_git=False, max_bytes=1000, stats=stats)]
        assert "src/generated.py" not in found
        assert stats.too_large == 1
        assert "src/generated.py" in stats.excluded_paths

    def test_counts_why_files_were_left_out(self, project: Path) -> None:
        stats = WalkStats()
        list(iter_source_files(project, use_git=False, stats=stats))
        assert stats.selected == 2
        assert stats.unsupported_language >= 1
        assert stats.as_dict()["selected"] == 2

    def test_an_empty_directory_yields_nothing(self, tmp_path: Path) -> None:
        assert paths_of(tmp_path, use_git=False) == []

    def test_rejects_a_path_that_is_not_a_directory(self, project: Path) -> None:
        with pytest.raises(NotADirectoryError):
            list(iter_source_files(project / "README.md"))

    def test_the_exclusion_list_covers_the_usual_suspects(self) -> None:
        assert {"node_modules", "vendor", ".git", "__pycache__", "dist"} <= (
            DEFAULT_EXCLUDED_DIRECTORIES
        )


class TestGitAwareness:
    @pytest.fixture
    def repository(self, project: Path) -> Path:
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        (project / ".gitignore").write_text("secrets.py\nbuild/\n", encoding="utf-8")
        (project / "secrets.py").write_text("TOKEN = 'x'\n", encoding="utf-8")
        (project / "build").mkdir()
        (project / "build" / "out.py").write_text("pass\n", encoding="utf-8")
        return project

    def test_detects_a_repository(self, repository: Path, tmp_path: Path) -> None:
        assert is_git_repository(repository)

    def test_honours_gitignore(self, repository: Path) -> None:
        found = paths_of(repository)
        assert "secrets.py" not in found
        assert not any(path.startswith("build/") for path in found)

    def test_still_finds_tracked_and_new_source(self, repository: Path) -> None:
        found = paths_of(repository)
        assert "src/main.py" in found
        assert "src/app.ts" in found

    def test_falls_back_to_walking_when_git_is_declined(self, repository: Path) -> None:
        # Without git, .gitignore is not consulted, so the ignored file
        # reappears. That difference is the reason git is preferred.
        assert "secrets.py" in paths_of(repository, use_git=False)
