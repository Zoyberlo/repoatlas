"""Among equally direct search matches, what the repository uses comes first.

Search used to break ties by name length, so `beta_thing` outranked
`alpha_thing` because it is a character shorter, whichever of the two the
rest of the code actually called. The global rank the index already stores
is what the graph knows; search now consults it after directness and before
length.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoatlas.store import IndexStore, update_store

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

LIB = (
    "def thing():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def alpha_thing():\n"
    "    return 2\n"
    "\n"
    "\n"
    "def beta_thing():\n"
    "    return 3\n"
)


@pytest.fixture
def store(tmp_path: Path) -> IndexStore:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "lib.py").write_text(LIB, encoding="utf-8")
    for i in range(5):
        (root / f"caller{i}.py").write_text(
            f"from lib import alpha_thing\n\n\ndef use{i}():\n    return alpha_thing()\n",
            encoding="utf-8",
        )
    with IndexStore(tmp_path / "index.db") as opened:
        update_store(root, opened, use_git=False)
        yield opened


def test_an_exact_name_still_leads(store: IndexStore) -> None:
    assert store.search("thing")[0].name == "thing"


def test_the_used_symbol_outranks_the_unused_one(store: IndexStore) -> None:
    # `beta_thing` is the shorter name; length used to decide, and decided
    # wrongly, because nothing calls beta_thing.
    names = [symbol.name for symbol in store.search("thing")]
    assert names.index("alpha_thing") < names.index("beta_thing")


def test_a_store_without_ranks_falls_back_to_length(store: IndexStore) -> None:
    with store.transaction():
        store._connection.execute("DELETE FROM ranks")
    names = [symbol.name for symbol in store.search("thing")]
    assert names[0] == "thing"
    assert names.index("beta_thing") < names.index("alpha_thing")


def test_the_count_agrees_with_the_search(store: IndexStore) -> None:
    assert store.search_count("thing") == len(store.search("thing", limit=100))


def test_a_short_query_takes_the_scan_path_with_the_same_order(store: IndexStore) -> None:
    # Two characters cannot use the trigram index; the scan must rank the
    # same way.
    names = [symbol.name for symbol in store.search("th")]
    assert names[0] == "thing"
    assert names.index("alpha_thing") < names.index("beta_thing")
