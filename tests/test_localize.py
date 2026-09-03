"""Tests for scoring the map against a repository's own history.

The benchmark is only useful if its ground truth is what a commit really
touched and its vocabulary is what the message really said, so these tests
build a small repository with real commits and check both, then check that
a message naming a symbol lifts recall the way steering is meant to.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from repoatlas.localize import commit_cases, run_localize, words_of
from repoatlas.store import IndexStore, update_store

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
            "PATH": __import__("os").environ["PATH"],
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
    git(root, "init", "-q")
    write(root, "lib/billing.py", "class Invoice:\n    def total(self):\n        return 1\n")
    write(root, "lib/auth.py", "class Session:\n    def user(self):\n        return None\n")
    write(root, "app/main.py", "from lib.billing import Invoice\nfrom lib.auth import Session\n\n\ndef run():\n    return Invoice().total(), Session().user()\n")
    write(root, "README.md", "# demo\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "Initial layout")
    write(root, "lib/billing.py", "class Invoice:\n    def total(self):\n        return 2\n\n    def tax(self):\n        return 0\n")
    git(root, "commit", "-q", "-am", "Add tax to Invoice totals")
    write(root, "README.md", "# demo\n\nmore\n")
    git(root, "commit", "-q", "-am", "Expand the README")
    return root


class TestVocabulary:
    def test_words_keep_identifiers_and_drop_filler(self) -> None:
        assert words_of("Add tax to Invoice totals") == ("tax", "Invoice", "totals")

    def test_words_are_unique_and_ordered(self) -> None:
        assert words_of("Resolver calls Resolver twice") == ("Resolver", "calls", "twice")


class TestCommitCases:
    def test_only_commits_touching_indexed_files_count(self, repo: Path) -> None:
        indexed = {"lib/billing.py", "lib/auth.py", "app/main.py"}
        cases = commit_cases(repo, indexed, commits=10)
        subjects = [case.subject for case in cases]
        assert "Add tax to Invoice totals" in subjects
        assert "Expand the README" not in subjects

    def test_a_case_records_what_was_touched(self, repo: Path) -> None:
        indexed = {"lib/billing.py", "lib/auth.py", "app/main.py"}
        case = next(c for c in commit_cases(repo, indexed) if c.subject.startswith("Add tax"))
        assert case.touched == ("lib/billing.py",)
        assert "Invoice" in case.mentions

    def test_large_commits_are_not_tasks(self, repo: Path) -> None:
        indexed = {"lib/billing.py", "lib/auth.py", "app/main.py"}
        cases = commit_cases(repo, indexed, commits=10, max_files=2)
        assert all(len(case.touched) <= 2 for case in cases)
        assert not any(case.subject == "Initial layout" for case in cases)


class TestRunLocalize:
    def test_a_message_naming_a_symbol_finds_its_file(self, repo: Path, tmp_path: Path) -> None:
        with IndexStore(tmp_path / "index.db") as store:
            update_store(repo, store, use_git=False)
            result = run_localize(store, repo, commits=10, budget=60)
        case = next(c for c in result.cases if c.subject.startswith("Add tax"))
        assert case.matched_mentions >= 1
        assert case.recall_mentioned == 1.0
        assert case.first_hit_mentioned

    def test_the_summary_is_serialisable(self, repo: Path, tmp_path: Path) -> None:
        with IndexStore(tmp_path / "index.db") as store:
            update_store(repo, store, use_git=False)
            result = run_localize(store, repo, commits=10)
        payload = json.loads(json.dumps(result.as_dict()))
        assert payload["commits"] >= 1
        assert 0.0 <= payload["recall_mentioned"] <= 1.0
        assert "recall, with mention" in result.as_text()


class TestCli:
    def test_reports_and_writes_json(self, repo: Path, tmp_path: Path, capsys) -> None:
        from repoatlas.cli import main

        out = tmp_path / "loc.json"
        assert main(["localize", str(repo), "--commits", "5", "--out", str(out)]) == 0
        assert json.loads(out.read_text(encoding="utf-8"))["commits"] >= 1
        assert "recall" in capsys.readouterr().out

    def test_refuses_a_directory_without_history(self, tmp_path: Path) -> None:
        from repoatlas.cli import main

        with pytest.raises(SystemExit, match="not a git repository"):
            main(["localize", str(tmp_path)])
