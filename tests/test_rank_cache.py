"""Tests for keeping the graph and ranking between map calls.

The map used to be rebuilt from nothing on every call, six and a half
seconds on a hundred-thousand-symbol index. The cache keeps what does not
depend on the question, so what these tests check is the two ways that can
go wrong: serving something stale after the index changed, and serving
something different from what a fresh computation would have said.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoatlas.rank import RankCache, rank_symbols
from repoatlas.server import tools
from repoatlas.store import IndexStore, update_store

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

FIXTURE = Path(__file__).parent / "fixtures" / "tsdemo"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for source in (FIXTURE / "src").glob("*.ts"):
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / source.name).write_bytes(source.read_bytes())
    return root


@pytest.fixture
def store(tmp_path: Path, project: Path) -> IndexStore:
    with IndexStore(tmp_path / "index.db") as opened:
        update_store(project, opened, use_git=False)
        yield opened


class TestGeneration:
    def test_a_fresh_index_has_a_generation(self, store: IndexStore) -> None:
        assert store.generation() != "0"

    def test_a_no_op_reindex_leaves_it_alone(self, store: IndexStore, project: Path) -> None:
        before = store.generation()
        update_store(project, store, use_git=False)
        assert store.generation() == before

    def test_a_change_moves_it(self, store: IndexStore, project: Path) -> None:
        before = store.generation()
        (project / "src" / "extra.ts").write_text("export function extra() {}\n", encoding="utf-8")
        update_store(project, store, use_git=False)
        assert store.generation() != before

    def test_a_reset_moves_it_too(self, store: IndexStore) -> None:
        before = store.generation()
        store.reset()
        assert store.generation() != before


class TestStoredRanks:
    def test_resolution_records_the_global_ranking(self, store: IndexStore) -> None:
        stored = store.ranks()
        assert stored
        fresh = rank_symbols(store.snapshot())
        assert {item.symbol.id for item in fresh} == set(stored)
        for item in fresh:
            score, degree = stored[item.symbol.id]
            assert score == pytest.approx(item.score)
            assert degree == item.in_degree

    def test_a_map_from_stored_ranks_matches_a_fresh_one(self, store: IndexStore) -> None:
        # A cold cache reads the stored ranking; a warm one may have computed
        # its own. The map must not depend on which.
        from repoatlas.rank import MapOptions, render_map

        cold = tools.repo_map(store, budget=800, cache=RankCache())
        computed = render_map(rank_symbols(store.snapshot()), MapOptions(budget=800), estimator=store.estimator())
        assert cold.split("\n", 2)[2] == computed.text

    def test_an_older_store_without_ranks_still_maps(self, store: IndexStore) -> None:
        with store.transaction():
            store._connection.execute("DELETE FROM ranks")
        assert store.ranks() == {}
        assert "src/user.ts:" in tools.repo_map(store, budget=800, cache=RankCache())


class TestCacheReuse:
    def test_the_graph_is_built_once_per_generation(self, store: IndexStore) -> None:
        cache = RankCache()
        tools.repo_map(store, budget=500, cache=cache)
        tools.repo_map(store, budget=500, cache=cache)
        tools.repo_map(store, focus=("src/app.ts",), budget=500, cache=cache)
        assert cache.loads == 1
        assert cache.graph(store) is cache.graph(store)

    def test_a_focused_ranking_reuses_the_graph_but_not_the_order(self, store: IndexStore) -> None:
        # The fixture is small enough that any budget shows all of it, so
        # the rendered maps agree; the rankings behind them must not.
        cache = RankCache()
        plain = [item.symbol.id for item in cache.ranking(store)]
        focused = [item.symbol.id for item in cache.ranking(store, focus_paths={"src/app.ts"})]
        assert cache.loads == 1
        assert set(plain) == set(focused)
        assert plain != focused
        # Rank flows toward definitions, so the focused walk lands on what
        # app.ts uses as much as on app.ts itself; what it must do is lift
        # app.ts's own symbols above where the global walk put them.
        assert focused.index("src/app.ts#Admin") < plain.index("src/app.ts#Admin")

    def test_a_changed_index_is_reloaded(self, store: IndexStore, project: Path) -> None:
        cache = RankCache()
        before = tools.repo_map(store, budget=2000, cache=cache)
        (project / "src" / "extra.ts").write_text(
            "export class Brandnew { shout(): string { return 'hi' } }\n", encoding="utf-8"
        )
        update_store(project, store, use_git=False)
        after = tools.repo_map(store, budget=2000, cache=cache)
        assert cache.loads == 2
        assert "Brandnew" in after and "Brandnew" not in before

    def test_cached_and_uncached_answers_are_identical(self, store: IndexStore) -> None:
        cache = RankCache()
        warm = [tools.repo_map(store, budget=700, cache=cache) for _ in range(2)]
        cold = tools.repo_map(store, budget=700)
        assert warm[0] == warm[1] == cold


class TestAdapter:
    def test_the_server_holds_one_cache(self, store: IndexStore) -> None:
        pytest.importorskip("mcp", reason="needs the serve extra")
        import asyncio

        from repoatlas.server.app import build_server

        server = build_server(store)

        async def call_twice() -> None:
            for _ in range(2):
                await server.call_tool("repo_map", {"budget": 400})

        asyncio.run(call_twice())
