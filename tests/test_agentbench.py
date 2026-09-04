"""The tier-4 harness, minus the model: parsing, scoring, the command line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoatlas.agentbench import (
    ARMS,
    AgentBenchResult,
    AgentRun,
    claude_command,
    mcp_config,
    parse_locations,
    parse_stream,
    score_locations,
    task_prompt,
)
from repoatlas.localize import _Locator
from repoatlas.model import IndexSnapshot, SourceRange, Symbol, SymbolKind


def _event(**fields: object) -> str:
    return json.dumps(fields)


class TestParseStream:
    def test_reads_attachment_tools_usage_and_cost(self) -> None:
        lines = [
            "not json",
            _event(type="system", subtype="init", mcp_servers=[{"name": "repoatlas", "status": "connected"}]),
            _event(type="assistant", message={"content": [{"type": "tool_use", "name": "mcp__repoatlas__repo_map"}]}),
            _event(type="assistant", message={"content": [{"type": "tool_use", "name": "Read"}, {"type": "text", "text": "x"}]}),
            _event(
                type="result",
                subtype="success",
                result='["app/A.php:10"]',
                total_cost_usd=0.0421,
                num_turns=4,
                duration_ms=8120,
                usage={"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 20000, "cache_creation_input_tokens": 500},
            ),
        ]
        trace = parse_stream(lines)
        assert trace.ok
        assert trace.mcp_attached is True
        assert trace.tool_calls == {"mcp__repoatlas__repo_map": 1, "Read": 1}
        assert trace.mcp_calls == 1
        assert trace.tokens == 1200 + 300 + 20000 + 500
        assert (trace.cost_usd, trace.turns, trace.duration_ms) == (0.0421, 4, 8120)
        assert trace.result_text == '["app/A.php:10"]'

    def test_a_failed_server_is_reported_not_scored(self) -> None:
        trace = parse_stream([_event(type="system", subtype="init", mcp_servers=[{"name": "repoatlas", "status": "failed"}])])
        assert trace.mcp_attached is False
        assert not trace.ok
        assert trace.reason == "no result event"

    def test_an_error_result_is_not_ok(self) -> None:
        trace = parse_stream([_event(type="result", subtype="error_max_turns", is_error=True, result="")])
        assert not trace.ok
        assert trace.reason == "error_max_turns"


class TestParseLocations:
    def test_a_json_array_inside_prose(self) -> None:
        text = 'Here you go:\n["app/Models/Ad.php:200", "app/Services/AdService.php", "app/Models/Ad.php:200"]\nDone.'
        assert parse_locations(text) == [("app/Models/Ad.php", 200), ("app/Services/AdService.php", None)]

    def test_lines_when_there_is_no_array(self) -> None:
        text = "app/Models/Ad.php:200\n- `app/Services/AdService.php:12:5`\nnothing here\n"
        assert parse_locations(text) == [("app/Models/Ad.php", 200), ("app/Services/AdService.php", 12)]


class TestScore:
    def test_a_line_inside_the_symbol_counts_and_files_are_scored_too(self) -> None:
        snapshot = IndexSnapshot()
        snapshot.add_symbol(
            Symbol(id="a.php#A", name="A", kind=SymbolKind.CLASS, path="a.php",
                   name_range=SourceRange.of(1, 6, 1, 7), full_range=SourceRange.of(1, 0, 40, 1))
        )
        snapshot.add_symbol(
            Symbol(id="a.php#A.run", name="run", kind=SymbolKind.METHOD, path="a.php", container_id="a.php#A",
                   name_range=SourceRange.of(10, 20, 10, 23), full_range=SourceRange.of(10, 4, 20, 5))
        )
        locator = _Locator(snapshot)
        recall, files, precision = score_locations(
            [("a.php", 15), ("b.php", None)], {"a.php#A.run"}, {"a.php"}, locator
        )
        assert (recall, files, precision) == (1.0, 1.0, 0.5)
        recall, files, precision = score_locations([("a.php", 2)], {"a.php#A.run"}, {"a.php"}, locator)
        assert (recall, files, precision) == (0.0, 1.0, 1.0)


class TestCommandLine:
    def test_the_command_follows_the_tier_four_plan(self, tmp_path: Path) -> None:
        arm = ARMS["repoatlas"]
        command = claude_command("claude", task_prompt("fix invoice totals", arm), arm,
                                 mcp_config_path=tmp_path / "mcp.json", model=None, max_turns=12)
        assert command[:3] == ["claude", "-p", task_prompt("fix invoice totals", arm)]
        for flag in ("--strict-mcp-config", "--no-session-persistence", "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk", "--mcp-config"):
            assert flag in command
        assert command[command.index("--max-turns") + 1] == "12"
        assert "mcp__repoatlas__*" in command[command.index("--allowedTools") + 1]
        grep = claude_command("claude", "x", ARMS["grep"], mcp_config_path=None, model="m", max_turns=3)
        assert "--mcp-config" not in grep and "--model" in grep
        assert "--bare" not in grep

    def test_the_prompt_asks_for_locations_not_edits(self) -> None:
        text = task_prompt("Fix the invoice PDF total", ARMS["grep"])
        assert "Fix the invoice PDF total" in text
        assert "Do not edit" in text and "JSON array" in text

    def test_the_mcp_config_serves_the_store_as_it_is(self, tmp_path: Path) -> None:
        config = mcp_config(tmp_path / "clone", tmp_path / "clone" / "i.db")
        server = config["mcpServers"]["repoatlas"]
        assert server["args"][-4:] == ["serve", str(tmp_path / "clone"), "--store", str(tmp_path / "clone" / "i.db")] or "--no-refresh" in server["args"]
        assert "--no-refresh" in server["args"]


class TestResult:
    def test_means_deltas_and_the_exclusion_ledger(self) -> None:
        result = AgentBenchResult(arms=("grep", "repoatlas"), walked=3, tasks=2)
        result.runs += [
            AgentRun("aaa", "grep", 1, True, symbol_recall=0.2, file_recall=0.5, tokens=30000, turns=6),
            AgentRun("aaa", "repoatlas", 1, True, symbol_recall=0.6, file_recall=1.0, tokens=12000, turns=3, mcp_calls=2),
            AgentRun("bbb", "grep", 1, True, symbol_recall=0.0, file_recall=0.0, tokens=40000, turns=8),
            AgentRun("bbb", "repoatlas", 1, False, reason="repoatlas did not attach"),
        ]
        text = result.as_text()
        assert "1 run(s) excluded" in text and "bbb repoatlas #1: repoatlas did not attach" in text
        payload = result.as_dict()
        assert payload["per_arm"]["grep"]["runs"] == 2
        assert payload["per_arm"]["repoatlas"]["symbol_recall"] == pytest.approx(0.6)
        delta = result.paired_delta("grep", "repoatlas")
        assert delta is not None
        mean, _low, _high, pairs = delta
        assert pairs == 1 and mean == pytest.approx(0.4)

    def test_a_login_failure_is_named(self) -> None:
        trace = parse_stream([_event(type="result", subtype="success", is_error=True,
                                     result="Not logged in · Please run /login", num_turns=1)])
        assert not trace.ok
        assert trace.reason.startswith("Not logged in")
