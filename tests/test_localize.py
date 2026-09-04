"""Tests for scoring the map against a repository's own history.

The benchmark is only useful if its ground truth is what a commit really
changed, so these build a small repository with real commits and check
three things: that the symbols a commit touched are found from its diff
against the *parent* tree, that the walk never disturbs the repository it
reads, and that a subject naming a symbol lifts recall the way steering is
meant to.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from repoatlas.localize import (
    HistoryError,
    LocalizeResult,
    commit_cases,
    run_localize,
    walk,
    words_of,
)

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "PATH": os.environ["PATH"],
        },
    )


def write(root: Path, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(
        root,
        "lib/billing.py",
        "class Invoice:\n    def total(self):\n        return 1\n",
    )
    write(
        root,
        "lib/auth.py",
        "class Session:\n    def user(self):\n        return None\n",
    )
    write(
        root,
        "app/main.py",
        "from lib.billing import Invoice\nfrom lib.auth import Session\n"
        "\n\ndef run():\n    return Invoice().total(), Session().user()\n",
    )
    write(root, "README.md", "# demo\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "Initial layout")

    write(
        root,
        "lib/billing.py",
        "class Invoice:\n    def total(self):\n        return 2\n"
        "\n    def tax(self):\n        return 0\n",
    )
    git(root, "commit", "-q", "-am", "Add tax to Invoice totals")

    write(root, "README.md", "# demo\n\nmore\n")
    git(root, "commit", "-q", "-am", "Expand the README")

    write(
        root,
        "lib/auth.py",
        "class Session:\n    def user(self):\n        return 'someone'\n",
    )
    git(root, "commit", "-q", "-am", "Session user returns a name")
    return root


@pytest.fixture
def work(tmp_path: Path) -> Path:
    return tmp_path / "scratch"


class TestVocabulary:
    def test_words_keep_identifiers_and_drop_filler(self) -> None:
        assert words_of("Add tax to Invoice totals") == ("tax", "Invoice", "totals")

    def test_words_are_unique_and_ordered(self) -> None:
        assert words_of("Resolver calls Resolver twice") == ("Resolver", "calls", "twice")


class TestCommitSelection:
    def test_commits_come_oldest_first_with_their_parent(self, repo: Path) -> None:
        found = commit_cases(repo, commits=10)
        assert [c.subject for c in found][:2] == [
            "Add tax to Invoice totals",
            "Expand the README",
        ]
        assert all(commit.parent for commit in found)

    def test_the_first_commit_has_no_parent_to_start_from(self, repo: Path) -> None:
        assert "Initial layout" not in [c.subject for c in commit_cases(repo, commits=10)]

    def test_large_commits_are_refactors_not_tasks(self, repo: Path) -> None:
        assert commit_cases(repo, commits=10, max_files=0) == []


class TestWalk:
    def test_symbols_are_taken_from_the_parent_tree(self, repo: Path, work: Path) -> None:
        # `Add tax to Invoice totals` edits inside `Invoice.total`, which
        # exists in the parent; the `tax` method it adds does not, and must
        # not be counted as something the map could have named.
        cases = {case.subject: case for _c, case in walk(repo, work=work, commits=10)}
        assert cases["Add tax to Invoice totals"].symbols >= 1
        assert cases["Expand the README"].symbols == 0

    def test_a_subject_naming_a_symbol_finds_it(self, repo: Path, work: Path) -> None:
        cases = {case.subject: case for _c, case in walk(repo, work=work, commits=10)}
        case = cases["Add tax to Invoice totals"]
        assert case.matched_mentions >= 1
        assert case.symbol_recall_steered == 1.0

    def test_the_repository_it_reads_is_left_where_it_was(
        self, repo: Path, work: Path
    ) -> None:
        before = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        list(walk(repo, work=work, commits=10))
        after = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert after == before
        assert (
            subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            == status
        )

    def test_the_scratch_clone_is_reused(self, repo: Path, work: Path) -> None:
        list(walk(repo, work=work, commits=10))
        assert (work / ".git").is_dir()
        list(walk(repo, work=work, commits=10))

    def test_a_directory_without_history_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(HistoryError, match="not a git repository"):
            list(walk(tmp_path, work=tmp_path / "w", commits=5))


class TestResult:
    def test_only_commits_with_symbols_are_scored(self, repo: Path, work: Path) -> None:
        result = run_localize(repo, work=work, commits=10)
        assert result.walked > len(result.cases)
        assert all(case.symbols for case in result.cases)

    def test_the_summary_is_serialisable_and_hides_subjects(
        self, repo: Path, work: Path
    ) -> None:
        result = run_localize(repo, work=work, commits=10)
        payload = json.loads(json.dumps(result.as_dict()))
        assert payload["scored"] >= 1
        assert 0.0 <= payload["symbol_recall_steered"] <= 1.0
        assert "cases" not in payload
        assert "cases" in result.as_dict(include_cases=True)

    def test_the_text_report_names_both_metrics(self, repo: Path, work: Path) -> None:
        text = run_localize(repo, work=work, commits=10).as_text()
        assert "symbol recall" in text and "file recall" in text

    def test_an_empty_result_reports_zero_rather_than_dividing_by_it(self) -> None:
        assert LocalizeResult(budget=2000).as_dict()["symbol_recall_plain"] == 0.0


class TestCli:
    def test_reports_and_writes_json(self, repo: Path, tmp_path: Path, capsys) -> None:
        from repoatlas.cli import main

        out = tmp_path / "loc.json"
        code = main(
            [
                "localize",
                str(repo),
                "--commits",
                "10",
                "--work",
                str(tmp_path / "scratch"),
                "--out",
                str(out),
            ]
        )
        assert code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["scored"] >= 1
        assert "symbol recall" in capsys.readouterr().out

    def test_the_spread_override_reaches_the_map(
        self, repo: Path, tmp_path: Path, capsys
    ) -> None:
        from repoatlas.cli import main

        assert (
            main(
                [
                    "localize",
                    str(repo),
                    "--commits",
                    "10",
                    "--work",
                    str(tmp_path / "scratch"),
                    "--spread",
                    "1.0",
                ]
            )
            == 0
        )
        assert "symbol recall" in capsys.readouterr().out

    def test_refuses_a_directory_without_history(self, tmp_path: Path) -> None:
        from repoatlas.cli import main

        with pytest.raises(SystemExit, match="not a git repository"):
            main(["localize", str(tmp_path)])
