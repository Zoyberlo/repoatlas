"""The graphify arm: installed as documented, invisible to the baseline.

These do not test graphify. They test the two properties the comparison's
honesty rests on: that the arm gets what `graphify install --project` gives
an agent, and that the baseline arm gets none of it.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from repoatlas.agentbench import ARMS, claude_command
from repoatlas.graphify_arm import PROVIDER_VARIABLES, Graphify, GraphifyError


def fake_graphify(tmp_path: Path, *, nodes: int = 3, exit_code: int = 0) -> Path:
    """An executable standing in for graphify: writes a graph, or fails."""
    package = tmp_path / "pkg" / "graphify"
    (package / "always_on").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "always_on" / "claude-md.md").write_text(
        "## graphify\n\nRun `graphify query` first.\n", encoding="utf-8"
    )
    script = tmp_path / "bin" / "graphify"
    script.parent.mkdir()
    body = f"""#!{sys.executable}
import json, os, sys
sys.path.insert(0, {str(tmp_path / 'pkg')!r})
out = os.environ["GRAPHIFY_OUT"]
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "env.json"), "w") as fh:
    json.dump(dict(os.environ), fh)
if {exit_code}:
    sys.exit({exit_code})
graph = {{"nodes": [{{"id": i}} for i in range({nodes})], "links": [{{"source": 0, "target": 1}}]}}
with open(os.path.join(out, "graph.json"), "w") as fh:
    json.dump(graph, fh)
"""
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


posix_only = pytest.mark.skipif(os.name == "nt", reason="uses a shebang executable")


class TestWhatTheArmIsGiven:
    def test_the_hooks_match_graphify_s_own_install(self, tmp_path: Path) -> None:
        g = Graphify(Path("/opt/g/graphify"), tmp_path / "out")
        hooks = g.settings()["hooks"]["PreToolUse"]
        assert [h["matcher"] for h in hooks] == ["Bash|Grep", "Read|Glob"]
        assert "hook-guard search" in hooks[0]["hooks"][0]["command"]
        assert hooks[1]["hooks"][0]["command"].endswith("hook-guard read")
        assert all(h["hooks"][0]["timeout"] == 10 for h in hooks)

    def test_strict_mode_is_only_on_the_read_guard(self, tmp_path: Path) -> None:
        # graphify blocks the first raw read in strict mode and keeps search
        # nudge-only; putting --strict on search would test a mode it lacks.
        hooks = Graphify(Path("/opt/g/graphify"), tmp_path, strict=True).settings()
        search, read = hooks["hooks"]["PreToolUse"]
        assert "--strict" not in search["hooks"][0]["command"]
        assert read["hooks"][0]["command"].endswith("hook-guard read --strict")

    def test_graphify_is_on_path_for_the_arm_only(self, tmp_path: Path) -> None:
        base = {"PATH": "/usr/bin"}
        executable = Path("/opt/g/graphify")
        env = Graphify(executable, tmp_path / "out").environment(base)
        assert env["PATH"].startswith(str(executable.parent))
        assert env["GRAPHIFY_OUT"] == str(tmp_path / "out")
        assert base == {"PATH": "/usr/bin"}, "the baseline's environment must be untouched"

    def test_the_hook_mode_is_pinned_to_the_arm(self, tmp_path: Path) -> None:
        assert Graphify(Path("/g"), tmp_path).environment({})["GRAPHIFY_HOOK_STRICT"] == "0"
        assert Graphify(Path("/g"), tmp_path, strict=True).environment({})["GRAPHIFY_HOOK_STRICT"] == "1"

    @posix_only
    def test_instructions_are_read_from_the_installed_package(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The block is located by importing graphify under the interpreter the
        # executable runs, as a real install would have it importable.
        exe = fake_graphify(tmp_path)
        monkeypatch.setenv("PYTHONPATH", str(tmp_path / "pkg"))
        assert "graphify query" in Graphify(exe, tmp_path / "out").instructions()

    @posix_only
    def test_missing_instructions_are_an_error_not_a_weaker_arm(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        exe = fake_graphify(tmp_path)
        monkeypatch.setenv("PYTHONPATH", str(tmp_path / "pkg"))
        (tmp_path / "pkg" / "graphify" / "always_on" / "claude-md.md").unlink()
        with pytest.raises(GraphifyError):
            Graphify(exe, tmp_path / "out").instructions()


class TestBuildingTheGraph:
    @posix_only
    def test_provider_keys_never_reach_the_build(self, tmp_path: Path, monkeypatch) -> None:
        for name in PROVIDER_VARIABLES:
            monkeypatch.setenv(name, "must-not-leak")
        g = Graphify(fake_graphify(tmp_path), tmp_path / "out")
        g.rebuild(tmp_path)
        seen = json.loads((tmp_path / "out" / "env.json").read_text(encoding="utf-8"))
        assert not set(PROVIDER_VARIABLES) & set(seen)

    @posix_only
    def test_a_previous_commit_s_graph_cannot_survive(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "GRAPH_REPORT.md").write_text("from another commit", encoding="utf-8")
        (out / "cache").mkdir()
        (out / "cache" / "keep").write_text("content-addressed", encoding="utf-8")
        Graphify(fake_graphify(tmp_path), out).rebuild(tmp_path)
        assert not (out / "GRAPH_REPORT.md").exists()
        assert (out / "cache" / "keep").exists(), "the cache is meant to survive"

    @posix_only
    def test_an_empty_graph_stops_the_run(self, tmp_path: Path) -> None:
        # Otherwise the arm is the baseline with a longer prompt, scored as
        # if it were graphify.
        with pytest.raises(GraphifyError, match="empty"):
            Graphify(fake_graphify(tmp_path, nodes=0), tmp_path / "out").rebuild(tmp_path)

    @posix_only
    def test_a_failed_build_stops_the_run(self, tmp_path: Path) -> None:
        with pytest.raises(GraphifyError):
            Graphify(fake_graphify(tmp_path, exit_code=2), tmp_path / "out").rebuild(tmp_path)


class TestTheArmsAgainstTheBaseline:
    def test_the_only_extra_tool_is_graphify(self) -> None:
        grep = set(ARMS["grep"].allowed_tools)
        for name in ("graphify", "graphify-strict"):
            assert set(ARMS[name].allowed_tools) - grep == {"Bash(graphify *)"}

    def test_the_hint_is_the_baseline_s_word_for_word(self) -> None:
        assert ARMS["graphify"].hint == ARMS["grep"].hint
        assert ARMS["graphify-strict"].hint == ARMS["grep"].hint

    def test_the_baseline_command_carries_nothing_of_graphify(self, tmp_path: Path) -> None:
        command = claude_command(
            "claude", "task", ARMS["grep"], mcp_config_path=None, model=None,
            max_turns=30, settings_path=tmp_path / "s.json", append_system="## graphify",
        )
        # Handed both by mistake, the baseline still carries neither.
        assert "--settings" not in command
        assert "--append-system-prompt" not in command

    def test_the_graphify_command_carries_settings_and_instructions(self, tmp_path: Path) -> None:
        command = claude_command(
            "claude", "task", ARMS["graphify"], mcp_config_path=None, model=None,
            max_turns=30, settings_path=tmp_path / "s.json", append_system="## graphify",
        )
        assert command[command.index("--settings") + 1] == str(tmp_path / "s.json")
        assert command[command.index("--append-system-prompt") + 1] == "## graphify"


class TestKnowingWhetherGraphifyWasUsed:
    """A null means nothing without knowing whether the tool was called."""

    def stream(self, command: str) -> list[str]:
        event = {
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": command}}
            ]},
        }
        return [json.dumps(event)]

    def test_a_graphify_query_is_counted_apart(self) -> None:
        from repoatlas.agentbench import parse_stream

        trace = parse_stream(self.stream('graphify query "where are estimates saved"'))
        assert trace.tool_calls["Bash"] == 1
        assert trace.tool_calls["Bash:graphify"] == 1

    def test_by_absolute_path_and_after_cd_too(self) -> None:
        from repoatlas.agentbench import parse_stream

        for command in ("/home/u/.venv/bin/graphify explain Foo", "cd app && graphify path A B"):
            assert parse_stream(self.stream(command)).tool_calls["Bash:graphify"] == 1

    def test_a_search_that_mentions_graphify_is_not_a_call(self) -> None:
        from repoatlas.agentbench import parse_stream

        trace = parse_stream(self.stream("rg graphify src/"))
        assert trace.tool_calls["Bash:graphify"] == 0


class TestEachRunIsItsOwnSession:
    """Strict mode must not be silenced by a query some other run made."""

    def test_the_last_query_stamp_is_cleared(self, tmp_path: Path) -> None:
        stamp = tmp_path / "cache" / "last_query_stamp"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("", encoding="utf-8")
        Graphify(Path("/g"), tmp_path).forget_recent_queries()
        assert not stamp.exists()

    def test_the_graph_and_its_cache_are_untouched(self, tmp_path: Path) -> None:
        (tmp_path / "cache").mkdir()
        (tmp_path / "cache" / "entry").write_text("parsed", encoding="utf-8")
        (tmp_path / "graph.json").write_text("{}", encoding="utf-8")
        Graphify(Path("/g"), tmp_path).forget_recent_queries()
        assert (tmp_path / "graph.json").exists()
        assert (tmp_path / "cache" / "entry").exists()

    def test_no_stamp_is_not_an_error(self, tmp_path: Path) -> None:
        Graphify(Path("/g"), tmp_path).forget_recent_queries()
