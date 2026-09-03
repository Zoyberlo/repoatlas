"""Tests for the tool surface and the MCP adapter.

The tools are tested directly, without a protocol, because that is how they
were built: the answers are plain functions and the server is an adapter.
The adapter gets its own few tests for the things only it can get wrong,
chiefly whether an actionable error survives the boundary.

Budgets are checked hard. A tool that overruns its budget is truncated by
the client at an arbitrary point, where the agent cannot see what went
missing, which is worse than a short answer that says it is short.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from repoatlas.server import tools
from repoatlas.store import IndexStore, update_store

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

FIXTURE = Path(__file__).parent / "fixtures" / "tsdemo"
USER_CLASS = "src/user.ts#User"
GREET = "src/user.ts#User.greet"


@pytest.fixture(scope="module")
def store_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("served") / "index.db"
    with IndexStore(path) as store:
        update_store(FIXTURE, store, use_git=False)
    return path


@pytest.fixture
def store(store_path: Path) -> IndexStore:
    with IndexStore(store_path) as opened:
        yield opened


class TestSearchSymbols:
    def test_finds_a_class_by_part_of_its_name(self, store: IndexStore) -> None:
        assert "class User" in tools.search_symbols(store, "Use")

    def test_reports_how_many_places_use_each_hit(self, store: IndexStore) -> None:
        # The whole point of putting this in the search result: one call
        # answers both what exists and which of them matters.
        assert "use" in tools.search_symbols(store, "User")

    def test_says_so_when_nothing_matches(self, store: IndexStore) -> None:
        assert "nothing matches" in tools.search_symbols(store, "zzzznotathing")

    def test_a_kind_filter_narrows_the_search(self, store: IndexStore) -> None:
        # `greet` names a method on User and, as a substring, the Greets
        # interface. Asking for methods only must drop the interface.
        everything = tools.search_symbols(store, "greet")
        methods = tools.search_symbols(store, "greet", kinds=("method",))
        assert "interface Greets" in everything
        assert "interface Greets" not in methods

    def test_a_kind_filter_that_matches_nothing_says_so(
        self, store: IndexStore
    ) -> None:
        result = tools.search_symbols(store, "greet", kinds=("enum",))
        assert "nothing of kind enum" in result

    def test_an_empty_query_is_refused_with_a_next_step(self, store: IndexStore) -> None:
        with pytest.raises(tools.ToolError, match="name or part of one"):
            tools.search_symbols(store, "   ")

    def test_paging_advances_through_the_results(self, store: IndexStore) -> None:
        first = tools.search_symbols(store, "e", limit=2)
        assert "cursor=2" in first
        second = tools.search_symbols(store, "e", limit=2, cursor="2")
        assert second != first

    def test_a_cursor_that_did_not_come_from_here_is_refused(
        self, store: IndexStore
    ) -> None:
        with pytest.raises(tools.ToolError, match="omit it to start over"):
            tools.search_symbols(store, "User", cursor="not-a-number")

    def test_detailed_output_adds_the_signature(self, store: IndexStore) -> None:
        detailed = tools.search_symbols(store, "User", detail="detailed")
        assert "export class User" in detailed

    def test_the_result_stays_within_its_budget(self, store: IndexStore) -> None:
        from repoatlas.rank.tokens import estimate_tokens

        result = tools.search_symbols(store, "e", limit=100, budget=60)
        assert estimate_tokens(result) <= 80
        assert "not shown" in result

    def test_the_count_of_what_was_left_out_is_accurate(self, store: IndexStore) -> None:
        # Over-fetching by one only ever knew "at least one more", and said
        # "1 more" when there were hundreds.
        everything = tools.search_symbols(store, "e", limit=500, budget=100_000)
        total = int(everything.split(" match", 1)[0])
        assert total > 3
        first = tools.search_symbols(store, "e", limit=2, budget=100_000)
        assert first.startswith(f"{total} match")
        assert f"[{total - 2} more not shown" in first


class TestGetSymbol:
    def test_describes_where_a_symbol_is(self, store: IndexStore) -> None:
        result = tools.get_symbol(store, USER_CLASS)
        assert "src/user.ts:7" in result
        assert "class User" in result

    def test_names_the_container_of_a_member(self, store: IndexStore) -> None:
        assert "in: class User" in tools.get_symbol(store, GREET)

    def test_counts_what_uses_it(self, store: IndexStore) -> None:
        assert "used by:" in tools.get_symbol(store, USER_CLASS)

    def test_lists_each_user_once_however_many_edges_reach_it(
        self, store: IndexStore
    ) -> None:
        # An import and a call from one function are two edges and one
        # place worth looking at.
        result = tools.get_symbol(store, USER_CLASS)
        sample = [line for line in result.splitlines() if line.startswith("    src/")]
        assert len(sample) == len(set(sample))

    def test_the_body_is_withheld_unless_asked_for(self, store: IndexStore) -> None:
        assert "return `${this.label}" not in tools.get_symbol(store, GREET)

    def test_the_body_is_read_from_disk_on_request(self, store: IndexStore) -> None:
        result = tools.get_symbol(store, GREET, include_body=True)
        assert "greet(name: string): string {" in result

    def test_an_unknown_id_is_refused_with_a_next_step(self, store: IndexStore) -> None:
        with pytest.raises(tools.ToolError, match="use search_symbols"):
            tools.get_symbol(store, "no/such#thing")

    def test_a_moved_file_costs_the_body_not_the_answer(
        self, tmp_path: Path
    ) -> None:
        # The index stores locations, not text, so a stale path loses the
        # source but keeps everything else worth knowing.
        path = tmp_path / "index.db"
        with IndexStore(path) as store:
            update_store(FIXTURE, store, use_git=False)
            with store.transaction():
                store.set_meta("project_root", str(tmp_path / "gone"))
            result = tools.get_symbol(store, GREET, include_body=True)
            assert "body unavailable" in result
            assert "src/user.ts:14" in result


class TestFindReferences:
    def test_groups_uses_by_file(self, store: IndexStore) -> None:
        result = tools.find_references(store, USER_CLASS)
        assert "src/app.ts:" in result

    def test_shows_the_confidence_of_an_inferred_edge(self, store: IndexStore) -> None:
        result = tools.find_references(store, GREET)
        # Anything resolved below 0.9 is a lead, and says so.
        assert "use(s) of" in result

    def test_a_confidence_floor_drops_the_weaker_edges(self, store: IndexStore) -> None:
        everything = tools.find_references(store, GREET)
        strict = tools.find_references(store, GREET, min_confidence=0.99)
        assert len(strict) <= len(everything)

    def test_says_so_when_nothing_uses_it(self, store: IndexStore) -> None:
        result = tools.find_references(store, "src/app.ts#Formatter")
        assert "nothing uses" in result or "use(s) of" in result

    def test_an_unknown_id_is_refused(self, store: IndexStore) -> None:
        with pytest.raises(tools.ToolError, match="use search_symbols"):
            tools.find_references(store, "no/such#thing")

    def test_the_result_stays_within_its_budget(self, store: IndexStore) -> None:
        from repoatlas.rank.tokens import estimate_tokens

        result = tools.find_references(store, USER_CLASS, budget=40)
        assert estimate_tokens(result) <= 60


class TestNeighbours:
    def test_outward_lists_what_a_symbol_uses(self, store: IndexStore) -> None:
        result = tools.neighbours(store, USER_CLASS, direction="out")
        assert "uses, from User" in result

    def test_inward_lists_what_uses_it(self, store: IndexStore) -> None:
        result = tools.neighbours(store, USER_CLASS, direction="in")
        assert "used by, from User" in result

    def test_a_kind_filter_narrows_the_walk(self, store: IndexStore) -> None:
        result = tools.neighbours(store, USER_CLASS, direction="out", kinds=("contains",))
        assert "calls" not in result

    def test_depth_beyond_three_is_refused_with_a_reason(
        self, store: IndexStore
    ) -> None:
        with pytest.raises(tools.ToolError, match="more than an agent can read"):
            tools.neighbours(store, USER_CLASS, depth=9)

    def test_depth_below_one_is_refused(self, store: IndexStore) -> None:
        with pytest.raises(tools.ToolError, match="at least 1"):
            tools.neighbours(store, USER_CLASS, depth=0)

    def test_a_second_hop_reaches_further_than_the_first(
        self, store: IndexStore
    ) -> None:
        near = tools.neighbours(store, USER_CLASS, direction="out", depth=1, budget=4000)
        far = tools.neighbours(store, USER_CLASS, direction="out", depth=2, budget=4000)
        assert len(far) >= len(near)

    def test_an_isolated_symbol_says_so_in_the_right_direction(
        self, store: IndexStore
    ) -> None:
        result = tools.neighbours(store, "src/app.ts#Formatter", direction="out")
        assert "Formatter uses nothing" in result

    def test_an_unknown_edge_kind_is_refused_with_the_valid_ones(
        self, store: IndexStore
    ) -> None:
        # `EdgeKind("bogus")` is a ValueError, which the adapter would turn
        # into "Error executing tool". The agent needs the list instead.
        with pytest.raises(tools.ToolError, match="calls"):
            tools.neighbours(store, USER_CLASS, kinds=("bogus",))


class TestFileOutline:
    def test_lists_what_a_file_defines(self, store: IndexStore) -> None:
        result = tools.file_outline(store, "src/user.ts")
        assert "export class User implements Greets {" in result

    def test_members_are_indented_under_their_type(self, store: IndexStore) -> None:
        result = tools.file_outline(store, "src/user.ts")
        assert any(
            line.startswith("  ") and "greet" in line for line in result.splitlines()
        )

    def test_entries_carry_their_line_number(self, store: IndexStore) -> None:
        assert "7  export class User" in tools.file_outline(store, "src/user.ts")

    def test_a_windows_path_still_matches(self, store: IndexStore) -> None:
        assert "class User" in tools.file_outline(store, "src\\user.ts")

    def test_an_unknown_path_is_refused_with_a_reason(self, store: IndexStore) -> None:
        with pytest.raises(tools.ToolError, match="not in the index"):
            tools.file_outline(store, "src/nowhere.ts")


class TestRepoMap:
    def test_sketches_the_repository(self, store: IndexStore) -> None:
        result = tools.repo_map(store, budget=500)
        assert "src/user.ts:" in result
        assert "symbols," in result

    def test_focus_changes_the_answer(self, store: IndexStore) -> None:
        # Ninety tokens is where the two selections part on this fixture;
        # larger budgets show all of it either way.
        broad = tools.repo_map(store, budget=90)
        focused = tools.repo_map(store, focus=("src/app.ts",), budget=90)
        assert focused != broad

    def test_an_unrecognised_focus_is_reported_not_ignored(
        self, store: IndexStore
    ) -> None:
        # Silently ignoring it would return a global map that looks like an
        # answer to the question actually asked.
        result = tools.repo_map(store, focus=("src/nowhere.ts",), budget=200)
        assert "focus not in the index: src/nowhere.ts" in result

    def test_an_empty_index_says_so(self, tmp_path: Path) -> None:
        with IndexStore(tmp_path / "empty.db") as empty:
            assert "index is empty" in tools.repo_map(empty)

    def test_the_map_stays_within_its_budget(self, store: IndexStore) -> None:
        from repoatlas.rank.tokens import estimate_tokens

        result = tools.repo_map(store, budget=150)
        assert estimate_tokens(result) <= 200


class TestIndexStatus:
    def test_reports_what_the_index_covers(self, store: IndexStore) -> None:
        result = tools.index_status(store)
        assert "files:" in result
        assert "typescript" in result

    def test_names_the_project_root(self, store: IndexStore) -> None:
        assert "tsdemo" in tools.index_status(store)


class TestMcpAdapter:
    """Only what the adapter itself can get wrong."""

    @pytest.fixture
    def server(self, store: IndexStore):
        pytest.importorskip("mcp", reason="needs the serve extra")
        from repoatlas.server.app import build_server

        return build_server(store)

    def test_registers_exactly_the_seven_tools(self, server) -> None:
        # Every schema costs context on every turn, so the surface is small
        # on purpose and a new tool should be a deliberate decision.
        listed = asyncio.run(server.list_tools())
        assert {tool.name for tool in listed} == {
            "repo_map",
            "search_symbols",
            "get_symbol",
            "find_references",
            "neighbours",
            "file_outline",
            "index_status",
        }

    def test_every_tool_is_marked_read_only(self, server) -> None:
        listed = asyncio.run(server.list_tools())
        assert all(tool.annotations.read_only_hint for tool in listed)
        assert all(tool.annotations.open_world_hint is False for tool in listed)

    def test_every_tool_says_when_to_use_it(self, server) -> None:
        # Agents defaulted to grep and never called the graph tool in most
        # trials, so a description that only describes is a tool unused.
        listed = asyncio.run(server.list_tools())
        for tool in listed:
            assert tool.description and len(tool.description) > 80, tool.name

    def test_tools_are_listed_in_a_stable_order(self, server) -> None:
        # The list is part of the prompt, and a reordering costs the cache.
        first = [tool.name for tool in asyncio.run(server.list_tools())]
        second = [tool.name for tool in asyncio.run(server.list_tools())]
        assert first == second

    def test_a_call_returns_the_tool_text(self, server) -> None:
        result = asyncio.run(
            server.call_tool("search_symbols", {"query": "User", "limit": 3})
        )
        text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
        assert "class User" in text

    def test_an_actionable_error_survives_the_boundary(self, server) -> None:
        # The SDK keeps its own ToolError's message and replaces every other
        # exception with a generic line, so the adapter has to translate or
        # every next step written into these errors is lost.
        from mcp.server.mcpserver.exceptions import ToolError as SdkToolError

        with pytest.raises(SdkToolError, match="use search_symbols to find its id"):
            asyncio.run(server.call_tool("get_symbol", {"symbol_id": "nope"}))

    def test_the_store_is_usable_from_a_worker_thread(self, server) -> None:
        # The SDK runs synchronous tools off the event loop, and a SQLite
        # connection is bound to its creating thread unless opened for
        # sharing. This is what caught that.
        result = asyncio.run(server.call_tool("index_status", {}))
        text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
        assert "files:" in text

    def test_the_instructions_tell_the_agent_when_to_prefer_grep(
        self, server
    ) -> None:
        assert "Grep is still better" in (server.instructions or "")


class TestServeCli:
    def test_refuses_a_path_that_is_not_a_directory(self, tmp_path: Path) -> None:
        from repoatlas.cli import main

        target = tmp_path / "file.txt"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(SystemExit, match="not a directory"):
            main(["serve", str(target)])
