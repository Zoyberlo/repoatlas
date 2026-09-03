"""Every tool answer must be byte-identical when nothing changed.

Once a tool answer is in the conversation it is part of every later
request's prefix, and every provider's prompt cache works on exact prefix
match. A map that renders in a different order on the second call, or a
status line that changes without the index changing, costs the agent the
cache on everything that follows. These tests make that a promise rather
than an accident: same index, same question, same bytes, across calls,
across a reopened store, and across a no-op re-index.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoatlas.server import tools
from repoatlas.store import IndexStore, update_store

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

FIXTURE = Path(__file__).parent / "fixtures" / "tsdemo"
USER_CLASS = "src/user.ts#User"


def every_answer(store: IndexStore) -> dict[str, str]:
    """One representative call to each tool."""
    return {
        "repo_map": tools.repo_map(store, budget=600),
        "repo_map_focus": tools.repo_map(store, focus=("src/app.ts",), budget=600),
        "search": tools.search_symbols(store, "e", limit=5),
        "symbol": tools.get_symbol(store, USER_CLASS),
        "references": tools.find_references(store, USER_CLASS),
        "neighbours": tools.neighbours(store, USER_CLASS, direction="both"),
        "outline": tools.file_outline(store, "src/user.ts"),
        "status": tools.index_status(store),
    }


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.db"
    with IndexStore(path) as store:
        update_store(FIXTURE, store, use_git=False)
    return path


def test_the_same_question_gets_the_same_bytes(store_path: Path) -> None:
    with IndexStore(store_path) as store:
        first = every_answer(store)
        second = every_answer(store)
    assert first == second


def test_reopening_the_store_changes_nothing(store_path: Path) -> None:
    with IndexStore(store_path) as store:
        first = every_answer(store)
    with IndexStore(store_path) as store:
        second = every_answer(store)
    assert first == second


def test_a_no_op_reindex_changes_nothing(store_path: Path) -> None:
    with IndexStore(store_path) as store:
        first = every_answer(store)
        update_store(FIXTURE, store, use_git=False)
        second = every_answer(store)
    assert first == second


def test_status_keeps_its_volatile_line_last(store_path: Path) -> None:
    # The size is the one thing that may legitimately differ between two
    # indexes of the same code. Everything before it must not.
    with IndexStore(store_path) as store:
        lines = tools.index_status(store).rstrip().splitlines()
    assert lines[-1].startswith("size:")
    assert not any(line.startswith("size:") for line in lines[:-1])


def test_answers_carry_no_timestamps_or_paths_of_this_machine(store_path: Path) -> None:
    # Anything tied to when or where the index was built would differ
    # between two agents looking at the same commit.
    with IndexStore(store_path) as store:
        answers = every_answer(store)
    del answers["status"]  # the one place the project root is meant to appear
    for name, text in answers.items():
        assert str(store_path.parent) not in text, name
        assert "2026" not in text, name
