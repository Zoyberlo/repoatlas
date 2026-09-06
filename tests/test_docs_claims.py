"""Checking a knowledge base's claims against the index.

What is tested is the judgement, not the parsing: which claims count as
claims at all, when a file name is too vague to check, and when a line
range has gone stale. The tool exists because a wrong row costs more than
a missing one, so its own false alarms would defeat it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoatlas.docs import check_documents, iter_claims
from repoatlas.store import IndexStore, update_store

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

SOURCE = (
    "class Greeter:\n"          # 1
    "    def greet(self):\n"    # 2
    "        return 1\n"        # 3
    "\n"                        # 4
    "\n"                        # 5
    "def main():\n"             # 6
    "    return Greeter()\n"    # 7
    "\n"                        # 8
    "# a trailing note\n"       # 9
    "# and another\n"           # 10
    "# and a third\n"           # 11
    "# and a fourth\n"          # 12
)
"""Seven lines of code and a tail of comments.

The tail is what makes the stale-range case testable. A claim has to span
at least three lines before "nothing is declared here" means anything, so
that a row pointing at one line inside a function body is not called stale
for it.
"""


@pytest.fixture
def store(tmp_path: Path):
    project = tmp_path / "project"
    (project / "app").mkdir(parents=True)
    (project / "app" / "greeter.py").write_bytes(SOURCE.encode("utf-8"))
    (project / "app" / "other.py").write_bytes(b"def other():\n    pass\n")
    with IndexStore(tmp_path / "index.db") as opened:
        update_store(project, opened, use_git=False)
        yield opened


def document(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "INDEX.md"
    path.write_text(body, encoding="utf-8")
    return path


class TestWhatCountsAsAClaim:
    def test_only_backticked_paths_are_claims(self, tmp_path: Path) -> None:
        # Prose mentioning a file is not an assertion about where code is,
        # and treating it as one would bury the real findings.
        doc = document(tmp_path, "See greeter.py for this, and `app/greeter.py` for that.\n")
        claims = list(iter_claims([doc]))
        assert len(claims) == 1
        assert claims[0].path == "app/greeter.py"

    def test_a_line_and_a_range_are_both_read(self, tmp_path: Path) -> None:
        doc = document(tmp_path, "`app/greeter.py:2` and `app/greeter.py:1-3`\n")
        claims = list(iter_claims([doc]))
        assert (claims[0].start, claims[0].end) == (2, 2)
        assert (claims[1].start, claims[1].end) == (1, 3)

    def test_the_document_and_line_are_kept_so_a_finding_is_actionable(
        self, tmp_path: Path
    ) -> None:
        doc = document(tmp_path, "one\ntwo `app/greeter.py`\n")
        claim = next(iter_claims([doc]))
        assert claim.line == 2
        assert "INDEX.md:2" in str(claim)


class TestWhatTheIndexSaysAboutThem:
    def test_a_path_that_exists_holds(self, store: IndexStore, tmp_path: Path) -> None:
        result = check_documents(store, [document(tmp_path, "`app/greeter.py`\n")])
        assert result.ok == 1 and result.problems == 0

    def test_a_bare_file_name_resolves_when_only_one_file_has_it(
        self, store: IndexStore, tmp_path: Path
    ) -> None:
        result = check_documents(store, [document(tmp_path, "`greeter.py`\n")])
        assert result.ok == 1

    def test_a_path_nothing_indexes_is_reported(
        self, store: IndexStore, tmp_path: Path
    ) -> None:
        result = check_documents(store, [document(tmp_path, "`app/gone.py`\n")])
        assert len(result.unindexed) == 1
        assert result.problems == 1

    def test_a_line_past_the_end_of_the_file_is_stale(
        self, store: IndexStore, tmp_path: Path
    ) -> None:
        # The real finding this was built for: a row claiming
        # ChangeLogService.php:5147-5178 in a file that ends at 1709.
        result = check_documents(store, [document(tmp_path, "`app/greeter.py:900-950`\n")])
        assert len(result.stale) == 1
        assert "has 12 line(s)" in result.stale[0][1]

    def test_a_range_with_nothing_declared_in_it_is_stale(
        self, store: IndexStore, tmp_path: Path
    ) -> None:
        result = check_documents(store, [document(tmp_path, "`app/greeter.py:9-12`\n")])
        assert len(result.stale) == 1
        assert "nothing is declared" in result.stale[0][1]

    def test_a_range_that_still_covers_a_declaration_holds(
        self, store: IndexStore, tmp_path: Path
    ) -> None:
        result = check_documents(store, [document(tmp_path, "`app/greeter.py:1-3`\n")])
        assert result.ok == 1 and result.problems == 0


class TestWhatItRefusesToDecide:
    def test_a_name_several_files_share_is_imprecise_not_wrong(
        self, tmp_path: Path
    ) -> None:
        # Two `helper.py` and a row saying `helper.py` is vague prose, not a
        # false claim, and reporting it as a failure would train a reader to
        # ignore the whole report.
        project = tmp_path / "p"
        for where in ("a", "b"):
            (project / where).mkdir(parents=True)
            (project / where / "helper.py").write_bytes(b"def f():\n    pass\n")
        with IndexStore(tmp_path / "i.db") as store:
            update_store(project, store, use_git=False)
            result = check_documents(store, [document(tmp_path, "`helper.py`\n")])
        assert len(result.ambiguous) == 1
        assert result.problems == 0

    def test_a_partial_path_matches_on_a_boundary(self, tmp_path: Path) -> None:
        # `app/greeter.py` must not be satisfied by `myapp/greeter.py`.
        project = tmp_path / "p"
        (project / "myapp").mkdir(parents=True)
        (project / "myapp" / "greeter.py").write_bytes(b"def f():\n    pass\n")
        with IndexStore(tmp_path / "i.db") as store:
            update_store(project, store, use_git=False)
            result = check_documents(store, [document(tmp_path, "`app/greeter.py`\n")])
        assert len(result.unindexed) == 1
