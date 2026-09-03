"""Tests for ranking and for fitting a map to a token budget.

Ranking is a judgement, not a fact, so these tests assert the properties
that judgement has to have rather than exact scores: a symbol everything
uses outranks one nothing uses, focus moves the answer toward the focused
files, and the same index ranks the same way twice.

The budget tests are stricter, because fitting a budget is a fact. A map
that claims to be two thousand tokens and is three thousand has failed at
the one job the number exists for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoatlas.model import (
    Edge,
    EdgeKind,
    IndexSnapshot,
    ResolutionTier,
    SourceRange,
    Symbol,
    SymbolKind,
)
from repoatlas.rank import (
    MapOptions,
    RankOptions,
    SymbolGraph,
    estimate_tokens,
    make_estimator,
    rank_symbols,
    render_map,
)

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

from repoatlas.parse.build import build_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "tsdemo"


def symbol(
    symbol_id: str,
    name: str = "",
    *,
    path: str = "a.py",
    line: int = 0,
    kind: SymbolKind = SymbolKind.FUNCTION,
    container: str | None = None,
    local: bool = False,
    synthetic: bool = False,
    signature: str | None = None,
) -> Symbol:
    name = name or symbol_id.rsplit("#", 1)[-1]
    return Symbol(
        id=symbol_id,
        name=name,
        kind=kind,
        path=path,
        name_range=SourceRange.of(line, 0, line, len(name)),
        full_range=SourceRange.of(line, 0, line + 2, 0),
        container_id=container,
        qualified_name=name,
        signature=signature or f"def {name}():",
        local=local,
        synthetic=synthetic,
    )


def snapshot_of(symbols: list[Symbol], edges: list[Edge]) -> IndexSnapshot:
    return IndexSnapshot(symbols={s.id: s for s in symbols}, edges=edges)


def call(src: str, dst: str, kind: EdgeKind = EdgeKind.CALLS) -> Edge:
    return Edge(src_id=src, dst_id=dst, kind=kind, tier=ResolutionTier.IMPORT_MAP)


class TestGraph:
    def test_edges_point_toward_the_definition(self) -> None:
        graph = SymbolGraph.build(
            snapshot_of([symbol("a"), symbol("b", line=5)], [call("a", "b")])
        )
        assert graph.out_edges["a"].keys() == {"b"}
        assert graph.in_degree["b"] == 1

    def test_containment_points_the_other_way(self) -> None:
        # A member lends rank to the type that holds it, not the reverse.
        klass = symbol("C", kind=SymbolKind.CLASS)
        method = symbol("C.m", "m", kind=SymbolKind.METHOD, container="C", line=2)
        graph = SymbolGraph.build(
            snapshot_of([klass, method], [call("C", "C.m", EdgeKind.CONTAINS)])
        )
        assert graph.out_edges.get("C.m", {}).keys() == {"C"}
        assert "C.m" not in graph.out_edges.get("C", {})

    def test_local_and_synthetic_symbols_are_left_out(self) -> None:
        graph = SymbolGraph.build(
            snapshot_of(
                [
                    symbol("real"),
                    symbol("hidden", local=True, line=3),
                    symbol("mod", synthetic=True, line=6),
                ],
                [],
            )
        )
        assert graph.nodes == ["real"]

    def test_a_self_edge_is_dropped(self) -> None:
        graph = SymbolGraph.build(snapshot_of([symbol("a")], [call("a", "a")]))
        assert graph.edge_count == 0

    def test_a_low_confidence_edge_still_carries_some_weight(self) -> None:
        weak = Edge("a", "b", EdgeKind.CALLS, ResolutionTier.FUZZY)
        strong = Edge("a", "b", EdgeKind.CALLS, ResolutionTier.IMPORT_MAP)
        pair = [symbol("a"), symbol("b", line=4)]
        weak_graph = SymbolGraph.build(snapshot_of(pair, [weak]))
        strong_graph = SymbolGraph.build(snapshot_of(pair, [strong]))
        assert 0 < weak_graph.out_edges["a"]["b"] < strong_graph.out_edges["a"]["b"]


class TestRanking:
    def test_a_widely_used_symbol_outranks_an_unused_one(self) -> None:
        symbols = [symbol("core", line=0), symbol("orphan", line=20)]
        edges = []
        for index in range(5):
            caller = symbol(f"c{index}", line=index + 1)
            symbols.append(caller)
            edges.append(call(caller.id, "core"))
        ranked = {item.symbol.id: item.score for item in rank_symbols(snapshot_of(symbols, edges))}
        assert ranked["core"] > ranked["orphan"]

    def test_being_used_by_something_important_counts_for_more(self) -> None:
        # `hub` is called by five things; `quiet` by one. What each of them
        # calls in turn should inherit that difference.
        symbols = [symbol("hub"), symbol("quiet", line=30), symbol("viaHub", line=40),
                   symbol("viaQuiet", line=50)]
        edges = [call("hub", "viaHub"), call("quiet", "viaQuiet")]
        for index in range(5):
            caller = symbol(f"c{index}", line=index + 1)
            symbols.append(caller)
            edges.append(call(caller.id, "hub"))
        symbols.append(symbol("one", line=60))
        edges.append(call("one", "quiet"))
        ranked = {item.symbol.id: item.score for item in rank_symbols(snapshot_of(symbols, edges))}
        assert ranked["viaHub"] > ranked["viaQuiet"]

    def test_focus_lifts_the_files_being_worked_on(self) -> None:
        symbols = [
            symbol("here", path="focused.py"),
            symbol("there", path="other.py", line=5),
        ]
        snapshot = snapshot_of(symbols, [])
        unfocused = {i.symbol.id: i.score for i in rank_symbols(snapshot)}
        focused = {
            i.symbol.id: i.score
            for i in rank_symbols(snapshot, focus_paths={"focused.py"})
        }
        assert unfocused["here"] == pytest.approx(unfocused["there"])
        assert focused["here"] > focused["there"]

    def test_focus_can_name_a_single_symbol(self) -> None:
        snapshot = snapshot_of([symbol("a"), symbol("b", line=5)], [])
        focused = {
            i.symbol.id: i.score for i in rank_symbols(snapshot, focus_symbols={"b"})
        }
        assert focused["b"] > focused["a"]

    def test_a_private_name_is_ranked_below_a_public_one(self) -> None:
        snapshot = snapshot_of([symbol("pub", "helper"), symbol("priv", "_helper", line=5)], [])
        ranked = {i.symbol.id: i.score for i in rank_symbols(snapshot)}
        assert ranked["pub"] > ranked["priv"]

    def test_a_hash_field_is_private(self) -> None:
        public = symbol("a#x", "x", kind=SymbolKind.FIELD, line=1, signature="x: string;")
        hidden = symbol("a#y", "#y", kind=SymbolKind.FIELD, line=5, signature="#y: string;")
        ranked = {i.symbol.id: i.score for i in rank_symbols(snapshot_of([public, hidden], []))}
        assert ranked["a#y"] < ranked["a#x"]

    def test_a_dunder_is_public_api(self) -> None:
        # `__init__` is the most public thing a class has; the underscore
        # rule must not hide it behind `_helper`.
        init = symbol("a#__init__", "__init__", kind=SymbolKind.METHOD, line=1)
        helper = symbol("a#_helper", "_helper", kind=SymbolKind.METHOD, line=5)
        ranked = {i.symbol.id: i.score for i in rank_symbols(snapshot_of([init, helper], []))}
        assert ranked["a#__init__"] > ranked["a#_helper"]

    def test_a_private_keyword_counts_as_much_as_an_underscore(self) -> None:
        # TypeScript, PHP and Java say it in a word, not in the name.
        public = symbol("pub", "label", kind=SymbolKind.FIELD, signature="label: string;")
        private = symbol(
            "priv", "secret", kind=SymbolKind.FIELD, line=5, signature="private secret: string;"
        )
        ranked = {i.symbol.id: i.score for i in rank_symbols(snapshot_of([public, private], []))}
        assert ranked["pub"] > ranked["priv"]

    def test_a_class_outranks_a_field_all_else_equal(self) -> None:
        snapshot = snapshot_of(
            [symbol("C", kind=SymbolKind.CLASS), symbol("f", kind=SymbolKind.FIELD, line=5)],
            [],
        )
        ranked = {i.symbol.id: i.score for i in rank_symbols(snapshot)}
        assert ranked["C"] > ranked["f"]

    def test_the_kind_prior_can_be_turned_off(self) -> None:
        snapshot = snapshot_of(
            [symbol("C", kind=SymbolKind.CLASS), symbol("f", kind=SymbolKind.FIELD, line=5)],
            [],
        )
        ranked = {
            i.symbol.id: i.score
            for i in rank_symbols(snapshot, options=RankOptions(use_kind_prior=False))
        }
        assert ranked["C"] == pytest.approx(ranked["f"])

    def test_scores_sum_to_one(self) -> None:
        symbols = [symbol(f"s{i}", line=i) for i in range(6)]
        edges = [call("s0", "s1"), call("s1", "s2"), call("s2", "s0")]
        ranked = rank_symbols(snapshot_of(symbols, edges))
        assert sum(item.score for item in ranked) == pytest.approx(1.0, abs=1e-6)

    def test_a_dangling_node_does_not_leak_rank(self) -> None:
        # `sink` has no outgoing edge. Without redistributing its mass the
        # total would fall short of one on every iteration.
        symbols = [symbol("a"), symbol("sink", line=5)]
        ranked = rank_symbols(snapshot_of(symbols, [call("a", "sink")]))
        assert sum(item.score for item in ranked) == pytest.approx(1.0, abs=1e-6)

    def test_ranking_is_deterministic(self) -> None:
        snapshot = build_snapshot(FIXTURE, use_git=False).snapshot
        first = [(i.symbol.id, round(i.score, 9)) for i in rank_symbols(snapshot)]
        second = [(i.symbol.id, round(i.score, 9)) for i in rank_symbols(snapshot)]
        assert first == second

    def test_an_empty_snapshot_ranks_to_nothing(self) -> None:
        assert rank_symbols(IndexSnapshot()) == []

    def test_a_graph_with_no_edges_still_ranks(self) -> None:
        ranked = rank_symbols(snapshot_of([symbol("a"), symbol("b", line=3)], []))
        assert len(ranked) == 2
        assert all(item.score > 0 for item in ranked)

    def test_in_degree_is_reported_alongside_the_score(self) -> None:
        symbols = [symbol("core")] + [symbol(f"c{i}", line=i + 1) for i in range(3)]
        edges = [call(f"c{i}", "core") for i in range(3)]
        ranked = {i.symbol.id: i for i in rank_symbols(snapshot_of(symbols, edges))}
        assert ranked["core"].in_degree == 3


class TestTwoWalks:
    """The array path is an accelerator, not a second algorithm."""

    def _ranking(self, snapshot, **kwargs):
        return [(i.symbol.id, i.score) for i in rank_symbols(snapshot, **kwargs)]

    def test_arrays_and_dictionaries_agree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from repoatlas.rank import pagerank

        pytest.importorskip("numpy", reason="the array path needs numpy")
        snapshot = build_snapshot(FIXTURE, use_git=False).snapshot
        with_arrays = self._ranking(snapshot, focus_paths={"src/app.ts"})
        monkeypatch.setattr(pagerank, "_numpy", None)
        pure = self._ranking(snapshot, focus_paths={"src/app.ts"})
        assert [i for i, _ in with_arrays] == [i for i, _ in pure]
        for (_, a), (_, b) in zip(with_arrays, pure, strict=True):
            assert a == pytest.approx(b, abs=1e-9)

    def test_the_pure_walk_stands_on_its_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from repoatlas.rank import pagerank

        monkeypatch.setattr(pagerank, "_numpy", None)
        a = symbol("a#f")
        b = symbol("a#g", line=3)
        ranked = rank_symbols(snapshot_of([a, b], [call("a#f", "a#g")]))
        assert ranked[0].symbol.id == "a#g"

    def test_the_arrays_are_built_once_per_graph(self) -> None:
        from repoatlas.rank import pagerank
        from repoatlas.rank.pagerank import SymbolGraph

        pytest.importorskip("numpy", reason="the array path needs numpy")
        snapshot = build_snapshot(FIXTURE, use_git=False).snapshot
        graph = SymbolGraph.build(snapshot)
        assert graph._arrays is None
        rank_symbols(snapshot, graph=graph)
        first = graph._arrays
        rank_symbols(snapshot, graph=graph, focus_paths={"src/app.ts"})
        assert graph._arrays is first
        assert pagerank._numpy is not None


class TestTokenEstimate:
    def test_an_empty_string_costs_nothing(self) -> None:
        assert estimate_tokens("") == 0

    def test_longer_text_costs_more(self) -> None:
        assert estimate_tokens("a" * 100) > estimate_tokens("a" * 10)

    def test_a_calibrated_estimator_scales_the_other_way(self) -> None:
        text = "def function_name(argument):\n    return argument\n" * 20
        generous = make_estimator(6.0)
        tight = make_estimator(2.0)
        assert generous(text) < estimate_tokens(text) < tight(text)

    def test_a_calibration_constant_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            make_estimator(0)


class TestMapBudget:
    @pytest.fixture(scope="module")
    @staticmethod
    def ranked():
        snapshot = build_snapshot(Path(__file__).parent.parent, use_git=True).snapshot
        return rank_symbols(snapshot)

    @pytest.mark.parametrize("budget", [200, 500, 1000, 2000])
    def test_the_map_stays_within_its_budget(self, ranked, budget: int) -> None:
        result = render_map(ranked, MapOptions(budget=budget))
        assert result.tokens <= budget, result.text[:400]

    @pytest.mark.parametrize("budget", [500, 1000, 2000])
    def test_the_map_uses_most_of_its_budget(self, ranked, budget: int) -> None:
        # A map that stops at a tenth of the budget has wasted the context
        # it was given, which is the failure the binary search exists to
        # avoid.
        result = render_map(ranked, MapOptions(budget=budget))
        assert result.tokens >= budget * 0.5, result.as_dict()

    def test_a_bigger_budget_includes_more(self, ranked) -> None:
        small = render_map(ranked, MapOptions(budget=300))
        large = render_map(ranked, MapOptions(budget=1500))
        assert large.included > small.included
        assert large.tokens > small.tokens

    def test_everything_fits_when_the_budget_is_generous(self) -> None:
        snapshot = build_snapshot(FIXTURE, use_git=False).snapshot
        result = render_map(rank_symbols(snapshot), MapOptions(budget=100_000))
        assert result.included == result.total
        assert result.coverage == 1.0

    def test_a_file_cap_still_spends_the_budget(self, ranked) -> None:
        # Capping files after the fit threw away symbols the search had
        # already paid for, leaving the map far under budget.
        capped = render_map(ranked, MapOptions(budget=1000, max_files=3))
        assert capped.files <= 3
        assert capped.tokens <= 1000
        assert capped.tokens >= 1000 * 0.5, capped.as_dict()

    def test_an_empty_ranking_renders_an_empty_map(self) -> None:
        result = render_map([], MapOptions(budget=1000))
        assert result.text == ""
        assert result.as_dict()["tokens"] == 0


class TestMapRendering:
    @pytest.fixture
    def rendered(self):
        snapshot = build_snapshot(FIXTURE, use_git=False).snapshot
        return render_map(rank_symbols(snapshot), MapOptions(budget=100_000))

    def test_files_are_headed_by_their_path(self, rendered) -> None:
        assert "src/user.ts:" in rendered.text

    def test_entries_show_the_declaration_line(self, rendered) -> None:
        assert "export class User implements Greets {" in rendered.text

    def test_entries_carry_their_line_number(self, rendered) -> None:
        # The number is what makes an entry an address an agent can open,
        # and the gap between two numbers says how much was left out.
        assert "    7  export class User implements Greets {" in rendered.text

    def test_no_decoration_survives(self, rendered) -> None:
        # Elision marks and bars carried no information once only
        # declaration lines are shown, and they rendered badly.
        assert "⋮" not in rendered.text
        assert "│" not in rendered.text

    def test_members_are_indented_under_their_type(self, rendered) -> None:
        assert "   14    greet(name: string): string {" in rendered.text

    def test_entries_are_in_source_order_within_a_file(self, rendered) -> None:
        block = rendered.text.split("src/user.ts:")[1]
        assert block.index("Greets {") < block.index("class User")

    def test_a_shown_member_always_shows_its_type(self) -> None:
        # A method without its class is a line of code with no address, so
        # the container is pulled in even when its own rank missed the cut.
        klass = symbol("C", "Container", kind=SymbolKind.CLASS, signature="class Container:")
        method = symbol(
            "C.m", "method", kind=SymbolKind.METHOD, container="C", line=4,
            signature="def method(self):",
        )
        callers = [symbol(f"u{i}", line=10 + i, path="other.py") for i in range(6)]
        edges = [call(c.id, "C.m") for c in callers]
        ranked = rank_symbols(snapshot_of([klass, method, *callers], edges))
        result = render_map(ranked, MapOptions(budget=60))
        if "def method" in result.text:
            assert "class Container:" in result.text

    def test_scores_can_be_shown_for_debugging(self) -> None:
        snapshot = build_snapshot(FIXTURE, use_git=False).snapshot
        result = render_map(
            rank_symbols(snapshot), MapOptions(budget=100_000, show_scores=True)
        )
        assert "[0." in result.text

    def test_kinds_can_be_shown(self) -> None:
        snapshot = build_snapshot(FIXTURE, use_git=False).snapshot
        result = render_map(
            rank_symbols(snapshot), MapOptions(budget=100_000, show_kinds=True)
        )
        assert "class export class User" in result.text

    def test_the_file_count_can_be_capped(self) -> None:
        snapshot = build_snapshot(FIXTURE, use_git=False).snapshot
        result = render_map(
            rank_symbols(snapshot), MapOptions(budget=100_000, max_files=1)
        )
        assert result.files == 1

    def test_a_symbol_without_a_signature_still_renders(self) -> None:
        bare = Symbol(
            id="x",
            name="Thing",
            kind=SymbolKind.CLASS,
            path="a.py",
            name_range=SourceRange.of(0, 0, 0, 5),
        )
        result = render_map(rank_symbols(snapshot_of([bare], [])), MapOptions(budget=500))
        assert "class Thing" in result.text


class TestMapCli:
    def test_renders_a_map_of_a_directory(self, capsys) -> None:
        from repoatlas.cli import main

        assert main(["map", str(FIXTURE), "--budget", "500", "--no-git"]) == 0
        out = capsys.readouterr()
        assert "src/user.ts:" in out.out
        assert "tokens" in out.err

    def test_focus_narrows_the_map(self, capsys) -> None:
        from repoatlas.cli import main

        main(["map", str(FIXTURE), "--budget", "150", "--no-git"])
        broad = capsys.readouterr().out
        main(
            [
                "map",
                str(FIXTURE),
                "--budget",
                "150",
                "--no-git",
                "--focus",
                "src/app.ts",
            ]
        )
        focused = capsys.readouterr().out
        assert focused != broad
        assert "src/app.ts:" in focused

    def test_a_windows_style_focus_path_still_matches(self, capsys) -> None:
        from repoatlas.cli import main

        main(
            [
                "map",
                str(FIXTURE),
                "--budget",
                "150",
                "--no-git",
                "--focus",
                ".\\src\\app.ts",
            ]
        )
        assert "src/app.ts:" in capsys.readouterr().out

    def test_json_output_carries_the_map_and_its_cost(self, capsys) -> None:
        from repoatlas.cli import main

        main(["map", str(FIXTURE), "--budget", "500", "--no-git", "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["tokens"] <= 500
        assert "src/user.ts:" in payload["text"]

    def test_writes_to_a_file_when_asked(self, tmp_path: Path, capsys) -> None:
        from repoatlas.cli import main

        target = tmp_path / "maps" / "repo.md"
        main(["map", str(FIXTURE), "--no-git", "--out", str(target)])
        assert "src/user.ts:" in target.read_text(encoding="utf-8")
        assert "wrote" in capsys.readouterr().out

    def test_reads_a_stored_index(self, tmp_path: Path, capsys) -> None:
        from repoatlas.cli import main

        database = tmp_path / "i.db"
        main(["index", str(FIXTURE), "--no-git", "--store", str(database)])
        capsys.readouterr()
        assert main(["map", str(database), "--budget", "500"]) == 0
        assert "src/user.ts:" in capsys.readouterr().out

    def test_a_missing_source_says_so(self, tmp_path: Path) -> None:
        from repoatlas.cli import main

        with pytest.raises(SystemExit, match="no such repository or index"):
            main(["map", str(tmp_path / "absent.db")])

    def test_a_mention_steers_the_map(self, capsys) -> None:
        from repoatlas.cli import main

        assert main(["map", str(FIXTURE), "--no-git", "--mention", "Formatter", "--budget", "2000"]) == 0
        steered = capsys.readouterr().out
        assert main(["map", str(FIXTURE), "--no-git", "--budget", "2000"]) == 0
        plain = capsys.readouterr().out
        assert steered.split("\n")[0] == "src/app.ts:"
        assert steered != plain

    def test_a_calibrated_token_constant_changes_what_fits(self, capsys) -> None:
        from repoatlas.cli import main

        root = str(Path(__file__).parent.parent)
        main(["map", root, "--budget", "400", "--chars-per-token", "2.0", "--format", "json"])
        tight = json.loads(capsys.readouterr().out)
        main(["map", root, "--budget", "400", "--chars-per-token", "6.0", "--format", "json"])
        generous = json.loads(capsys.readouterr().out)
        assert generous["included"] > tight["included"]
