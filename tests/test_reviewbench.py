"""The review benchmark: the setting where there is no shell to grep with.

What is tested here is the setup rather than the agent — which tools each
arm may use, what the hunk gives away, and that the ground truth excludes
what the reviewer can already see. The scoring is `callsites.score_sites`
and is tested there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoatlas.callsites import CallSiteTask
from repoatlas.reviewbench import REVIEW_ARMS, diff_of, review_prompt

SOURCE = """<?php

class Invoice
{
    public function total(): int
    {
        return 1;
    }
}
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "Invoice.php").write_bytes(SOURCE.encode("utf-8"))
    return tmp_path


def task(line: int = 5) -> CallSiteTask:
    return CallSiteTask(
        symbol_id="app/Invoice.php#Invoice.total",
        name="total",
        path="app/Invoice.php",
        line=line,
        kind="method",
        sites=frozenset({("app/Invoice.php", 7), ("app/Other.php", 12)}),
        collisions=3,
        grep_lines=40,
    )


class TestTheArms:
    def test_neither_of_the_two_compared_arms_has_a_shell(self) -> None:
        # The whole point: this is the setting where grep is not there.
        for name in ("read", "index"):
            allowed = REVIEW_ARMS[name].allowed_tools
            assert "Grep" not in allowed
            assert "Glob" not in allowed
            assert not any(tool.startswith("Bash") for tool in allowed)

    def test_the_shell_is_withheld_and_not_merely_left_off_the_list(self) -> None:
        # `--allowedTools` pre-approves; it does not withhold. The first
        # run of this benchmark had the read arm calling Bash three times
        # a run, grepping with it, and scoring 1.000.
        from repoatlas.agentbench import claude_command

        for name in ("read", "index"):
            arm = REVIEW_ARMS[name]
            assert "Bash" in arm.denied_tools
            command = claude_command(
                "claude", "p", arm, mcp_config_path=None, model=None, max_turns=5
            )
            assert "--disallowedTools" in command
            denied = command[command.index("--disallowedTools") + 1]
            for tool in ("Bash", "Grep", "Glob"):
                assert tool in denied.split(",")

    def test_the_reference_arm_keeps_its_shell(self) -> None:
        from repoatlas.agentbench import claude_command

        command = claude_command(
            "claude", "p", REVIEW_ARMS["grep"], mcp_config_path=None, model=None, max_turns=5
        )
        assert "--disallowedTools" not in command

    def test_file_reading_is_held_constant_so_only_the_index_differs(self) -> None:
        read, index = REVIEW_ARMS["read"], REVIEW_ARMS["index"]
        assert "Read" in read.allowed_tools and "Read" in index.allowed_tools
        assert set(index.allowed_tools) - set(read.allowed_tools) == {"mcp__repoatlas__*"}
        assert read.server is None and index.server == "repoatlas"

    def test_the_grep_arm_is_a_reference_point_and_keeps_its_shell(self) -> None:
        # Every other benchmark in this project ran with a checkout; this
        # arm is here so the two settings sit in one table.
        assert "Grep" in REVIEW_ARMS["grep"].allowed_tools
        assert REVIEW_ARMS["grep"].server is None

    def test_both_hints_say_there_is_no_checkout(self) -> None:
        for name in ("read", "index"):
            assert "no checkout" in REVIEW_ARMS[name].hint


class TestTheHunk:
    def test_it_shows_the_declaration_as_changed(self, project: Path) -> None:
        diff = diff_of(project, task())
        assert "diff --git a/app/Invoice.php" in diff
        assert "-    public function total(): int" in diff
        assert "+    public function total(): int" in diff

    def test_it_carries_context_around_the_change(self, project: Path) -> None:
        diff = diff_of(project, task())
        assert " class Invoice" in diff

    def test_it_never_hands_over_the_location(self, project: Path) -> None:
        # An agent told `app/Invoice.php:5` has been given the first half
        # of the answer. A review shows code, not coordinates.
        prompt = review_prompt(task(), diff_of(project, task()), REVIEW_ARMS["read"].hint)
        assert "app/Invoice.php:5" not in prompt

    def test_a_line_past_the_end_is_refused_rather_than_silently_empty(
        self, project: Path
    ) -> None:
        from repoatlas.localize import HistoryError

        with pytest.raises(HistoryError, match="no line"):
            diff_of(project, task(line=900))


class TestTheQuestion:
    def test_it_asks_for_uses_elsewhere_and_says_so(self, project: Path) -> None:
        prompt = review_prompt(task(), diff_of(project, task()), REVIEW_ARMS["index"].hint)
        assert "elsewhere in the repository" in prompt
        assert "already shows it" in prompt

    def test_it_warns_about_namesakes(self, project: Path) -> None:
        prompt = review_prompt(task(), diff_of(project, task()), REVIEW_ARMS["read"].hint)
        assert "share the name do not count" in prompt.replace("\n", " ")
