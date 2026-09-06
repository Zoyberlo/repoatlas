"""Tier 4: does the index help an agent, or only an evaluation?

Every number this project has is about the index: whether its edges are
right, whether its map names what a commit went on to change. None is
about an agent. An agent with grep and a file reader may find the same
code, and the one measurement that settles it is to ask one: the same
task, the same repository at the same commit, once with the MCP server
attached and once without, and see what each found and what it cost.

The tasks are the ones `repoatlas localize` uses: recent commits that
touched a few files, each posed as "what would have to change for this",
scored against the symbols the commit did change. The agent is asked to
name locations, not to edit, so a run costs a few turns rather than a
session, and the answer is scored the way a map is: a `path:line` credits
the innermost symbol around it.

This is a harness around Claude Code's headless mode; nothing here calls
a model directly. Runs where the server did not attach are excluded and
counted, not scored as losses.
"""

from __future__ import annotations

import json
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .localize import (
    Commit,
    HistoryError,
    _git,
    _Locator,
    _prepare_clone,
    _seed_index,
    _touched_symbols,
    commit_cases,
    skeleton_prefix,
)
from .rank import MapOptions, RankOptions, rank_symbols, render_map
from .store import IndexStore, update_store

__all__ = [
    "ARMS",
    "FATAL_REASONS",
    "AgentBenchResult",
    "AgentRun",
    "Arm",
    "RunTrace",
    "_run_command",
    "claude_command",
    "mcp_config",
    "parse_locations",
    "parse_stream",
    "run_agentbench",
    "score_locations",
    "task_prompt",
]


@dataclass(frozen=True, slots=True)
class Arm:
    """One way of equipping the agent."""

    name: str
    server: str | None
    """The MCP server to attach, or ``None`` for the arm that has only a shell."""

    allowed_tools: tuple[str, ...]
    denied_tools: tuple[str, ...] = ()
    """Tools this arm must not have, whatever it asks for.

    `--allowedTools` pre-approves; it does not withhold. Under
    `--permission-mode dontAsk` an arm listing only `Read` still reached
    for `Bash` three times a run and grepped with it, which turned a
    benchmark about having no shell into a benchmark about having one.
    Withholding takes `--disallowedTools`, and it is checked rather than
    trusted: every run records which tools it called.
    """

    hooks: bool = False
    """Whether to attach the PostToolUse hook that answers a search.

    The tool-shaped arms measured whether an agent *given* the index does
    better, and the answer was no — because it never called it: 0 times out
    of 8 for Serena, 0.8 a run for the map. This arm asks a different
    question. The agent greps exactly as the grep arm does, and the index
    speaks only afterwards, and only when it has something a text search
    could not have said. If this does not move either, the index has
    nothing to offer an agent that already has a shell, and that is worth
    knowing rather than suspecting.
    """

    hint: str = ""
    context: str = ""
    """What to put in the prompt before the task: ``map``, ``skeleton``, or nothing.

    The tool-shaped arms answer "does an agent given this index do better".
    These answer a narrower question the tool-shaped ones cannot, because
    an agent that never calls a tool tells you nothing about the tool: put
    the same number of tokens in front of it either way, ranked by the
    graph or not ranked at all, and the difference is the graph.
    """

    @property
    def mcp(self) -> bool:
        return self.server is not None


# What an agent without an index uses, and what it reaches for anyway: the
# first pilot denied Bash and watched the grep arm spend half its turns
# asking for `rg` and `ls`. Read-only shell commands are allowed to both
# arms; nothing that writes.
_READ_ONLY_TOOLS = (
    "Read",
    "Grep",
    "Glob",
    "LS",
    "Bash(rg *)",
    "Bash(grep *)",
    "Bash(ls *)",
    "Bash(find *)",
    "Bash(cat *)",
    "Bash(head *)",
    "Bash(tail *)",
    "Bash(sed -n *)",
    "Bash(wc *)",
    "Bash(git log *)",
    "Bash(git grep *)",
    "Bash(git show *)",
    "Bash(git ls-files *)",
)

ARMS: dict[str, Arm] = {
    "grep": Arm(
        name="grep",
        server=None,
        allowed_tools=_READ_ONLY_TOOLS,
        hint="Use Grep, Glob and Read to find them.",
    ),
    "repoatlas": Arm(
        name="repoatlas",
        server="repoatlas",
        allowed_tools=(*_READ_ONLY_TOOLS, "mcp__repoatlas__*"),
        hint=(
            "An MCP server called repoatlas is attached, with a tree-sitter index "
            "of this repository: a ranked map, symbol search, outlines, and "
            "resolved references. Its tools can answer this without reading whole "
            "files."
        ),
    ),
    # Not a tool the agent can decline. Same allowed tools as `grep`, same
    # prompt, same hint — the only difference is that a PostToolUse hook
    # runs after each search and appends what the index knows about the
    # name searched for, when that is something grep could not have said.
    "hook": Arm(
        name="hook",
        server=None,
        allowed_tools=_READ_ONLY_TOOLS,
        hooks=True,
        hint="Use Grep, Glob and Read to find them.",
    ),
    # The ablation. Same tools as grep, same task, and the same number of
    # tokens of context in front of it either way: `map` is what the ranked
    # graph draws, `skeleton` is the same repository listed in path order
    # with no ranking at all, which is what is left if the graph goes.
    # Offline the two score 0.309 and 0.028 symbol recall; whether an agent
    # converts that is the whole question, and the tool-shaped arms cannot
    # answer it, because an agent that never calls a tool has measured
    # nothing about the tool.
    "map": Arm(
        name="map",
        server=None,
        allowed_tools=_READ_ONLY_TOOLS,
        hint=(
            "A ranked map of this repository, drawn around the words of the task, "
            "is above. Use Grep, Glob and Read to confirm and extend it."
        ),
        context="map",
    ),
    "skeleton": Arm(
        name="skeleton",
        server=None,
        allowed_tools=_READ_ONLY_TOOLS,
        hint=(
            "An outline of this repository is above. Use Grep, Glob and Read to "
            "confirm and extend it."
        ),
        context="skeleton",
    ),
    # Serena is the closest thing anyone ships to what this project does, and
    # the usual recommendation for Claude Code, so it gets an arm rather than
    # an argument. Only its read-only tools: it can also edit and run shells.
    "serena": Arm(
        name="serena",
        server="serena",
        allowed_tools=(
            *_READ_ONLY_TOOLS,
            "mcp__serena__find_symbol",
            "mcp__serena__find_referencing_symbols",
            "mcp__serena__find_declaration",
            "mcp__serena__find_implementations",
            "mcp__serena__get_symbols_overview",
            "mcp__serena__search_for_pattern",
            "mcp__serena__list_dir",
            "mcp__serena__find_file",
            "mcp__serena__read_file",
            "mcp__serena__activate_project",
            "mcp__serena__initial_instructions",
        ),
        hint=(
            "An MCP server called serena is attached, backed by a language server "
            "for this repository: symbol search, declarations, implementations and "
            "referencing symbols. Its tools can answer this without reading whole "
            "files."
        ),
    ),
}

# Reasons no further run can succeed either. A spend limit does not
# recover in the minutes a walk takes, and sixty tasks of it produce sixty
# identical exclusions and an hour of indexing for nothing, which is what
# the first attempt at a sixty-commit run did.
FATAL_REASONS = ("spend limit", "usage limit", "not logged in", "rate limit")


def _is_fatal(reason: str) -> bool:
    lowered = reason.lower()
    return any(marker in lowered for marker in FATAL_REASONS)


_ANSWER_RULES = (
    "Do not edit anything. Reply with only a JSON array of strings, most "
    "likely first, at most 15 entries, each a repository-relative path with "
    "the line of the function, method or class that would change, as "
    '"path:line", or the path alone when you cannot say where in the file.'
)


def task_prompt(subject: str, arm: Arm, context: str = "") -> str:
    """The task as the agent sees it: the commit subject, posed as a change to locate.

    An arm with a context prefix gets it first, before the task, because
    that is where a map would be if the agent had asked for one, and
    because a wall of paths after the question reads as an afterthought.
    """
    preamble = f"{context.rstrip()}\n\n" if context.strip() else ""
    return (
        f"{preamble}You are localising a change in this repository. Find the files "
        "and, where you can, the functions, methods or classes that would have to "
        f"change for this task:\n\n{subject.strip()}\n\n{arm.hint} {_ANSWER_RULES}"
    )


def claude_command(
    claude: str,
    prompt: str,
    arm: Arm,
    *,
    mcp_config_path: Path | None,
    model: str | None,
    max_turns: int,
    settings_path: Path | None = None,
) -> list[str]:
    """The headless Claude Code invocation for one run.

    Not `--bare`, though the tier-4 plan said so: bare mode skips keychain
    reads, which logs an OAuth account out of every run. Reproducibility
    comes from `--strict-mcp-config`, so no server but the one under test
    attaches to either arm, and from not persisting the sessions.
    """
    command = [
        claude,
        "-p",
        prompt,
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "dontAsk",
        "--max-turns",
        str(max_turns),
        "--allowedTools",
        ",".join(arm.allowed_tools),
        *(
            ["--disallowedTools", ",".join(arm.denied_tools)]
            if arm.denied_tools
            else []
        ),
    ]
    if model:
        command += ["--model", model]
    if arm.mcp and mcp_config_path is not None:
        command += ["--mcp-config", str(mcp_config_path)]
    if arm.hooks and settings_path is not None:
        command += ["--settings", str(settings_path)]
    return command


def mcp_config(
    root: Path, store: Path, *, server: str = "repoatlas", serena: str | None = None
) -> dict[str, Any]:
    """The MCP configuration for one arm's server, over the same tree.

    For this index, `--no-refresh` matters: the store holds the parent
    commit's index and the server must not re-index the working tree on
    start. Serena is given the same root and left to build whatever it
    builds, which is part of what is being compared.
    """
    if server == "serena":
        executable = serena or shutil.which("serena")
        if not executable:
            raise HistoryError(
                "no serena executable found; install it with "
                "`uv tool install git+https://github.com/oraios/serena` or pass --serena"
            )
        return {
            "mcpServers": {
                "serena": {
                    "command": str(executable),
                    "args": [
                        "start-mcp-server",
                        "--context",
                        "ide-assistant",
                        "--project",
                        str(root),
                        "--transport",
                        "stdio",
                        "--enable-web-dashboard",
                        "false",
                        "--enable-gui-log-window",
                        "false",
                        "--log-level",
                        "ERROR",
                    ],
                }
            }
        }
    executable = shutil.which("repoatlas")
    if executable:
        command, args = executable, []
    else:
        command, args = sys.executable, ["-m", "repoatlas"]
    return {
        "mcpServers": {
            "repoatlas": {
                "command": command,
                "args": [*args, "serve", str(root), "--store", str(store), "--no-refresh"],
            }
        }
    }


def hook_settings(store: Path) -> dict[str, Any]:
    """A settings file declaring the PostToolUse hook, for one arm.

    Passed with `--settings`, which applies on top of nothing else here:
    the runs are already isolated by `--strict-mcp-config` and no session
    persistence, so this is the only hook the agent has.

    The store is named rather than discovered. The benchmark's index does
    not live where a project's would, and a hook that quietly answered from
    some other index would produce a number about the wrong thing.
    """
    executable = shutil.which("repoatlas")
    if executable:
        command = f'"{executable}" hook --store "{store}"'
    else:
        command = f'"{sys.executable}" -m repoatlas hook --store "{store}"'
    return {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Grep",
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        }
    }


@dataclass(slots=True)
class RunTrace:
    """What one headless run reported, read off its stream-json events."""

    ok: bool = False
    reason: str = ""
    mcp_attached: bool | None = None
    result_text: str = ""
    tool_calls: Counter[str] = field(default_factory=Counter)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    duration_ms: int = 0
    denials: int = 0
    """Tool calls the permission mode refused: turns spent asking for what was not allowed."""

    @property
    def mcp_calls(self) -> int:
        return sum(count for name, count in self.tool_calls.items() if name.startswith("mcp__"))

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens + self.output_tokens


def parse_stream(lines: Iterable[str]) -> RunTrace:
    """Read a `--output-format stream-json` transcript.

    Tolerant by design: fields are read with defaults, unknown events are
    skipped, and a malformed line is ignored rather than failing the run.
    What matters is whether the server attached, which tools were called,
    the final text, and what it cost.
    """
    trace = RunTrace()
    for raw in lines:
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            servers = event.get("mcp_servers") or []
            if servers:
                trace.mcp_attached = any(
                    isinstance(server, dict)
                    and str(server.get("status", "")).lower() in ("connected", "ok", "ready")
                    for server in servers
                )
            else:
                trace.mcp_attached = False
        elif kind == "assistant":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    trace.tool_calls[str(block.get("name", "?"))] += 1
        elif kind == "result":
            trace.ok = event.get("subtype", "success") == "success" and not event.get("is_error")
            # A failed run says why in its result text ("Not logged in"),
            # its subtype ("error_max_turns"), or nothing at all.
            trace.reason = (
                ""
                if trace.ok
                else str(event.get("result") or event.get("subtype") or "error")[:120]
            )
            trace.result_text = str(event.get("result") or "")
            usage = event.get("usage") or {}
            trace.input_tokens = int(usage.get("input_tokens") or 0)
            trace.output_tokens = int(usage.get("output_tokens") or 0)
            trace.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
            trace.cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
            trace.cost_usd = float(event.get("total_cost_usd") or event.get("cost_usd") or 0.0)
            trace.turns = int(event.get("num_turns") or 0)
            trace.duration_ms = int(event.get("duration_ms") or 0)
            denials = event.get("permission_denials")
            trace.denials = len(denials) if isinstance(denials, list) else 0
    if not trace.result_text and not trace.reason:
        trace.reason = "no result event"
    return trace


_LOCATION = re.compile(r"^(?P<path>[^:\s]+?)(?::(?P<line>\d+))?(?::\d+)?$")


def parse_locations(text: str) -> list[tuple[str, int | None]]:
    """The `path:line` entries of the agent's answer, in the order it gave them.

    A JSON array is looked for first, anywhere in the text, since a model
    told to reply with only JSON often adds a sentence. Failing that,
    every line that looks like a location counts.
    """
    candidates: list[str] = []
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            candidates = [str(item) for item in parsed if isinstance(item, (str, int))]
    if not candidates:
        candidates = [
            line.strip().lstrip("-*0123456789.) ").strip("`\"',") for line in text.splitlines()
        ]
    found: list[tuple[str, int | None]] = []
    seen: set[tuple[str, int | None]] = set()
    for item in candidates:
        match = _LOCATION.match(item.strip().replace("\\", "/").lstrip("./"))
        if not match or ("/" not in match.group("path") and "." not in match.group("path")):
            continue
        path = match.group("path")
        line = int(match.group("line")) if match.group("line") else None
        if (path, line) not in seen:
            seen.add((path, line))
            found.append((path, line))
    return found


def score_locations(
    locations: Sequence[tuple[str, int | None]],
    wanted_ids: set[str],
    wanted_files: set[str],
    locator: _Locator,
    *,
    top: int | None = None,
) -> tuple[float, float, float]:
    """Symbol recall, file recall and file precision of an answer.

    ``top`` scores only the first that many entries: an answer padded to
    the allowed length is a different thing from a confident one, and
    recall at five says which it was.
    """
    if not wanted_ids:
        return 0.0, 0.0, 0.0
    chosen = list(locations[:top]) if top else list(locations)
    points = [(path, line) for path, line in chosen if line is not None]
    credited = locator.credit(points) & wanted_ids
    named_files = {path for path, _line in chosen}
    symbol_recall = len(credited) / len(wanted_ids)
    file_recall = len(named_files & wanted_files) / len(wanted_files) if wanted_files else 0.0
    precision = len(named_files & wanted_files) / len(named_files) if named_files else 0.0
    return symbol_recall, file_recall, precision


@dataclass(slots=True)
class AgentRun:
    """One agent, one task, one arm."""

    sha: str
    arm: str
    repeat: int
    ok: bool
    reason: str = ""
    symbol_recall: float = 0.0
    file_recall: float = 0.0
    file_precision: float = 0.0
    symbol_recall_top5: float = 0.0
    file_recall_top5: float = 0.0
    named: int = 0
    with_lines: int = 0
    denials: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    duration_ms: int = 0
    mcp_calls: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "arm": self.arm,
            "repeat": self.repeat,
            "ok": self.ok,
            "reason": self.reason,
            "symbol_recall": round(self.symbol_recall, 3),
            "file_recall": round(self.file_recall, 3),
            "file_precision": round(self.file_precision, 3),
            "symbol_recall_top5": round(self.symbol_recall_top5, 3),
            "file_recall_top5": round(self.file_recall_top5, 3),
            "named": self.named,
            "with_lines": self.with_lines,
            "denials": self.denials,
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 4),
            "turns": self.turns,
            "duration_ms": self.duration_ms,
            "mcp_calls": self.mcp_calls,
            "tool_calls": dict(sorted(self.tool_calls.items())),
        }


@dataclass(slots=True)
class AgentBenchResult:
    arms: tuple[str, ...]
    runs: list[AgentRun] = field(default_factory=list)
    walked: int = 0
    tasks: int = 0
    skipped: int = 0
    """Runs a resumed walk did not pay for again, because they were scored."""

    stopped: str = ""
    """Why the walk ended early, when it did."""

    def scored(self, arm: str) -> list[AgentRun]:
        return [run for run in self.runs if run.arm == arm and run.ok]

    def excluded(self) -> list[AgentRun]:
        return [run for run in self.runs if not run.ok]

    def _mean(self, arm: str, name: str) -> float:
        values = [float(getattr(run, name)) for run in self.scored(arm)]
        return statistics.mean(values) if values else 0.0

    def paired_delta(
        self, first: str, second: str, name: str = "symbol_recall", *, draws: int = 2000
    ) -> tuple[float, float, float, int] | None:
        """Mean per-task difference ``second - first`` with a bootstrap interval.

        Paired on the task: each task contributes the mean over its
        repeats in each arm, and only tasks scored in both arms count.
        """
        import random

        per_task: dict[str, dict[str, list[float]]] = {}
        for run in self.runs:
            if run.ok and run.arm in (first, second):
                per_task.setdefault(run.sha, {}).setdefault(run.arm, []).append(
                    float(getattr(run, name))
                )
        deltas = [
            statistics.mean(arms[second]) - statistics.mean(arms[first])
            for arms in per_task.values()
            if first in arms and second in arms
        ]
        if not deltas:
            return None
        mean = statistics.mean(deltas)
        generator = random.Random(20260905)
        resampled = sorted(
            statistics.mean(generator.choices(deltas, k=len(deltas))) for _ in range(draws)
        )
        low = resampled[int(0.025 * draws)]
        high = resampled[min(draws - 1, int(0.975 * draws))]
        return mean, low, high, len(deltas)

    def as_dict(self, *, include_runs: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "arms": list(self.arms),
            "walked": self.walked,
            "tasks": self.tasks,
            "skipped": self.skipped,
            "stopped": self.stopped,
            "excluded": len(self.excluded()),
            "per_arm": {
                arm: {
                    "runs": len(self.scored(arm)),
                    "symbol_recall": round(self._mean(arm, "symbol_recall"), 4),
                    "file_recall": round(self._mean(arm, "file_recall"), 4),
                    "file_precision": round(self._mean(arm, "file_precision"), 4),
                    "symbol_recall_top5": round(self._mean(arm, "symbol_recall_top5"), 4),
                    "file_recall_top5": round(self._mean(arm, "file_recall_top5"), 4),
                    "named": round(self._mean(arm, "named"), 1),
                    "with_lines": round(self._mean(arm, "with_lines"), 1),
                    "denials": round(self._mean(arm, "denials"), 1),
                    "tokens": round(self._mean(arm, "tokens")),
                    "cost_usd": round(self._mean(arm, "cost_usd"), 4),
                    "turns": round(self._mean(arm, "turns"), 1),
                    "duration_ms": round(self._mean(arm, "duration_ms")),
                    "mcp_calls": round(self._mean(arm, "mcp_calls"), 2),
                }
                for arm in self.arms
            },
        }
        if len(self.arms) == 2:
            first, second = self.arms
            for name in ("symbol_recall", "file_recall", "tokens"):
                delta = self.paired_delta(first, second, name)
                if delta is not None:
                    mean, low, high, pairs = delta
                    payload[f"delta_{name}"] = {
                        "second_minus_first": round(mean, 4),
                        "low": round(low, 4),
                        "high": round(high, 4),
                        "pairs": pairs,
                    }
        if include_runs:
            payload["runs"] = [run.as_dict() for run in self.runs]
        return payload

    def as_text(self) -> str:
        lines = [
            f"tasks:    {self.tasks} scored of {self.walked} walked; "
            f"{len(self.excluded())} run(s) excluded",
            *([f"stopped:  {self.stopped}"] if self.stopped else []),
            "",
            f"{'':<16}" + "".join(f"{arm:>12}" for arm in self.arms),
        ]
        for label, name, form in (
            ("runs", None, "d"),
            ("symbol recall", "symbol_recall", ".3f"),
            ("file recall", "file_recall", ".3f"),
            ("file precision", "file_precision", ".3f"),
            ("symbol recall@5", "symbol_recall_top5", ".3f"),
            ("file recall@5", "file_recall_top5", ".3f"),
            ("named", "named", ".1f"),
            ("with lines", "with_lines", ".1f"),
            ("denials", "denials", ".1f"),
            ("tokens", "tokens", ",.0f"),
            ("cost usd", "cost_usd", ".3f"),
            ("turns", "turns", ".1f"),
            ("seconds", "duration_ms", ".0f"),
            ("mcp calls", "mcp_calls", ".1f"),
        ):
            cells = []
            for arm in self.arms:
                if name is None:
                    cells.append(f"{len(self.scored(arm)):>12d}")
                else:
                    value = self._mean(arm, name)
                    if name == "duration_ms":
                        value /= 1000
                    cells.append(f"{value:>12{form}}")
            lines.append(f"{label:<16}" + "".join(cells))
        if len(self.arms) == 2:
            first, second = self.arms
            for name in ("symbol_recall", "file_recall", "tokens"):
                delta = self.paired_delta(first, second, name)
                if delta is not None:
                    mean, low, high, pairs = delta
                    lines.append(
                        f"\n{second} minus {first}, {name}: {mean:+.3f} "
                        f"[{low:+.3f}, {high:+.3f}] over {pairs} paired task(s)"
                    )
        excluded = self.excluded()
        if excluded:
            lines.append("")
            lines.append("excluded:")
            for run in excluded:
                lines.append(f"  {run.sha} {run.arm} #{run.repeat}: {run.reason}")
        return "\n".join(lines) + "\n"


def run_agentbench(
    source: Path,
    *,
    work: Path,
    commits: int = 20,
    arms: Sequence[str] = ("grep", "repoatlas"),
    repeats: int = 1,
    claude: str = "claude",
    model: str | None = None,
    max_turns: int = 30,
    max_files: int = 8,
    timeout: int = 900,
    serena: str | None = None,
    budget: int = 2000,
    done: set[tuple[str, str, int]] | None = None,
    progress: Any = None,
) -> AgentBenchResult:
    """Run every task through every arm and score the answers.

    The scratch clone is checked out at each task's parent commit and
    indexed there, so the server the agent sees describes exactly the
    tree it is working in; the repository being read is never touched.
    """
    chosen = tuple(arms)
    for name in chosen:
        if name not in ARMS:
            raise HistoryError(f"unknown arm {name!r}; choose from {', '.join(ARMS)}")
    if shutil.which(claude) is None and not Path(claude).exists():
        raise HistoryError(f"no Claude Code executable at {claude!r}")
    root = _prepare_clone(source, work)
    store_path = root / ".repoatlas-agentbench.db"
    config_path = work.parent / f"{work.name}-mcp.json"
    result = AgentBenchResult(arms=chosen)
    selected = commit_cases(root, commits=commits, max_files=max_files)
    if not selected:
        raise HistoryError("no commit in this history touches a handful of files")
    head = _git(root, "rev-parse", "HEAD").strip()
    try:
        with IndexStore(store_path) as store:
            for commit in selected:
                result.walked += 1
                _git(root, "checkout", "--quiet", "--detach", commit.parent)
                update_store(root, store, use_git=True)
                wanted = _touched_symbols(store, root, commit)
                if not wanted:
                    continue
                snapshot = store.snapshot()
                wanted_ids = {item for item in wanted if item in snapshot.symbols}
                if not wanted_ids:
                    continue
                result.tasks += 1
                wanted_files = {snapshot.symbols[item].path for item in wanted_ids}
                locator = _Locator(snapshot)
                contexts = _contexts(store, snapshot, commit, chosen, budget)
                for arm_name in chosen:
                    arm = ARMS[arm_name]
                    arm_config = config_path.with_name(f"{config_path.name}.{arm_name}.json")
                    settings_file = config_path.with_name(
                        f"{config_path.name}.{arm_name}.settings.json"
                    )
                    if arm.hooks:
                        settings_file.write_text(
                            json.dumps(hook_settings(store_path)), encoding="utf-8"
                        )
                    if arm.mcp:
                        arm_config.write_text(
                            json.dumps(
                                mcp_config(
                                    root, store_path, server=arm.server or "", serena=serena
                                )
                            ),
                            encoding="utf-8",
                        )
                    for repeat in range(1, repeats + 1):
                        if done and (commit.sha[:12], arm_name, repeat) in done:
                            # Resuming after a limit: this one is already
                            # scored, and re-running it would pay twice for
                            # an answer that is already on disk.
                            result.skipped += 1
                            continue
                        run = _run_once(
                            root,
                            commit,
                            arm,
                            repeat,
                            claude=claude,
                            model=model,
                            max_turns=max_turns,
                            timeout=timeout,
                            config_path=arm_config if arm.mcp else None,
                            settings_path=settings_file if arm.hooks else None,
                            wanted_ids=wanted_ids,
                            wanted_files=wanted_files,
                            locator=locator,
                            context=contexts.get(arm.context, ""),
                        )
                        result.runs.append(run)
                        if progress is not None:
                            progress(run)
                        if not run.ok and _is_fatal(run.reason):
                            # Nothing after this can succeed; stop with
                            # what was scored rather than walk on.
                            result.stopped = run.reason
                            return result
    finally:
        _git(root, "checkout", "--quiet", "--detach", head, check=False)
    return result


def _contexts(
    store: IndexStore,
    snapshot: Any,
    commit: Commit,
    chosen: Sequence[str],
    budget: int,
) -> dict[str, str]:
    """Render whatever the chosen arms want in front of the task.

    Both are drawn at the same budget from the same index at the same
    commit, so the only difference between them is the ranking, which is
    the thing being measured. Rendering is skipped entirely when no arm
    asked for it, because ranking a hundred thousand symbols is seconds
    and most runs do not need it.
    """
    wanted = {ARMS[name].context for name in chosen} - {""}
    if not wanted:
        return {}
    contexts: dict[str, str] = {}
    if "skeleton" in wanted:
        contexts["skeleton"] = skeleton_prefix(snapshot, budget)
    if "map" in wanted:
        by_name, by_stem = _seed_index(snapshot.symbols.values(), store.languages())
        seeds: set[str] = set()
        seed_paths: set[str] = set()
        for word in commit.mentions:
            key = word.strip().lower()
            seeds.update(by_name.get(key, ()))
            seed_paths.update(by_stem.get(key, ()))
        ranked = rank_symbols(
            snapshot,
            focus_paths=seed_paths,
            focus_symbols=seeds,
            options=RankOptions(),
        )
        contexts["map"] = render_map(ranked, MapOptions(budget=budget)).text
    return contexts


def _run_command(
    root: Path,
    prompt: str,
    arm: Arm,
    *,
    claude: str,
    model: str | None,
    max_turns: int,
    timeout: int,
    config_path: Path | None,
    settings_path: Path | None = None,
) -> RunTrace | str:
    """One headless run in ``root``: its trace, or why there is none.

    Shared by both task classes, so that "where would this change" and
    "who uses this" are asked of the same agent through the same command
    and differ only in the question.
    """
    command = claude_command(
        claude,
        prompt,
        arm,
        mcp_config_path=config_path if arm.mcp else None,
        model=model,
        max_turns=max_turns,
        settings_path=settings_path if arm.hooks else None,
    )
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError as exc:
        return f"could not start: {exc}"
    trace = parse_stream(completed.stdout.splitlines())
    if not trace.duration_ms:
        trace.duration_ms = int((time.monotonic() - started) * 1000)
    if arm.mcp and trace.mcp_attached is False:
        return f"{arm.server} did not attach"
    if not trace.ok:
        reason = trace.reason or f"exit {completed.returncode}"
        tail = completed.stderr.strip().splitlines()[-1:] if completed.stderr else []
        return " ".join([reason, *tail])[:200]
    return trace


def _run_once(
    root: Path,
    commit: Commit,
    arm: Arm,
    repeat: int,
    *,
    claude: str,
    model: str | None,
    max_turns: int,
    timeout: int,
    config_path: Path | None,
    settings_path: Path | None,
    wanted_ids: set[str],
    wanted_files: set[str],
    locator: _Locator,
    context: str = "",
) -> AgentRun:
    outcome = _run_command(
        root,
        task_prompt(commit.subject, arm, context),
        arm,
        claude=claude,
        model=model,
        max_turns=max_turns,
        timeout=timeout,
        config_path=config_path,
        settings_path=settings_path,
    )
    if isinstance(outcome, str):
        return AgentRun(commit.sha[:12], arm.name, repeat, ok=False, reason=outcome)
    trace = outcome
    locations = parse_locations(trace.result_text)
    symbol_recall, file_recall, precision = score_locations(
        locations, wanted_ids, wanted_files, locator
    )
    symbol_top5, file_top5, _ = score_locations(
        locations, wanted_ids, wanted_files, locator, top=5
    )
    return AgentRun(
        sha=commit.sha[:12],
        arm=arm.name,
        repeat=repeat,
        ok=True,
        symbol_recall=symbol_recall,
        file_recall=file_recall,
        file_precision=precision,
        symbol_recall_top5=symbol_top5,
        file_recall_top5=file_top5,
        named=len(locations),
        with_lines=sum(1 for _path, line in locations if line is not None),
        denials=trace.denials,
        tokens=trace.tokens,
        cost_usd=trace.cost_usd,
        turns=trace.turns,
        duration_ms=trace.duration_ms,
        mcp_calls=trace.mcp_calls,
        tool_calls=dict(trace.tool_calls),
    )


def iter_arms(names: str) -> Iterator[str]:
    for name in names.split(","):
        cleaned = name.strip()
        if cleaned:
            yield cleaned
