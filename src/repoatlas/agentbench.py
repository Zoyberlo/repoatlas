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
    _touched_symbols,
    commit_cases,
)
from .store import IndexStore, update_store

__all__ = [
    "ARMS",
    "AgentBenchResult",
    "AgentRun",
    "Arm",
    "RunTrace",
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
    mcp: bool
    allowed_tools: tuple[str, ...]
    hint: str


ARMS: dict[str, Arm] = {
    "grep": Arm(
        name="grep",
        mcp=False,
        allowed_tools=("Read", "Grep", "Glob", "LS"),
        hint="Use Grep, Glob and Read to find them.",
    ),
    "repoatlas": Arm(
        name="repoatlas",
        mcp=True,
        allowed_tools=("Read", "Grep", "Glob", "LS", "mcp__repoatlas__*"),
        hint=(
            "An MCP server called repoatlas is attached, with an index of this "
            "repository. Start with its repo_map tool, passing the task's words as "
            "mention, then search_symbols, find_references and file_outline; use "
            "Read only to confirm what the index says."
        ),
    ),
}

_ANSWER_RULES = (
    "Do not edit anything. Reply with only a JSON array of strings, most "
    "likely first, at most 15 entries, each a repository-relative path with "
    "the line of the function, method or class that would change, as "
    '"path:line", or the path alone when you cannot say where in the file.'
)


def task_prompt(subject: str, arm: Arm) -> str:
    """The task as the agent sees it: the commit subject, posed as a change to locate."""
    return (
        "You are localising a change in this repository. Find the files and, where "
        "you can, the functions, methods or classes that would have to change for "
        f"this task:\n\n{subject.strip()}\n\n{arm.hint} {_ANSWER_RULES}"
    )


def claude_command(
    claude: str,
    prompt: str,
    arm: Arm,
    *,
    mcp_config_path: Path | None,
    model: str | None,
    max_turns: int,
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
    ]
    if model:
        command += ["--model", model]
    if arm.mcp and mcp_config_path is not None:
        command += ["--mcp-config", str(mcp_config_path)]
    return command


def mcp_config(root: Path, store: Path) -> dict[str, Any]:
    """The MCP configuration that serves the scratch clone's index as it is.

    `--no-refresh` matters: the store holds the parent commit's index, and
    the server must not re-index the working tree on start.
    """
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
                    and server.get("name") == "repoatlas"
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
) -> tuple[float, float, float]:
    """Symbol recall, file recall and file precision of an answer."""
    if not wanted_ids:
        return 0.0, 0.0, 0.0
    points = [(path, line) for path, line in locations if line is not None]
    credited = locator.credit(points) & wanted_ids
    named_files = {path for path, _line in locations}
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
    named: int = 0
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
            "named": self.named,
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
            "excluded": len(self.excluded()),
            "per_arm": {
                arm: {
                    "runs": len(self.scored(arm)),
                    "symbol_recall": round(self._mean(arm, "symbol_recall"), 4),
                    "file_recall": round(self._mean(arm, "file_recall"), 4),
                    "file_precision": round(self._mean(arm, "file_precision"), 4),
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
            "",
            f"{'':<16}" + "".join(f"{arm:>12}" for arm in self.arms),
        ]
        for label, name, form in (
            ("runs", None, "d"),
            ("symbol recall", "symbol_recall", ".3f"),
            ("file recall", "file_recall", ".3f"),
            ("file precision", "file_precision", ".3f"),
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
                config_path.write_text(json.dumps(mcp_config(root, store_path)), encoding="utf-8")
                for arm_name in chosen:
                    arm = ARMS[arm_name]
                    for repeat in range(1, repeats + 1):
                        run = _run_once(
                            root,
                            commit,
                            arm,
                            repeat,
                            claude=claude,
                            model=model,
                            max_turns=max_turns,
                            timeout=timeout,
                            config_path=config_path if arm.mcp else None,
                            wanted_ids=wanted_ids,
                            wanted_files=wanted_files,
                            locator=locator,
                        )
                        result.runs.append(run)
                        if progress is not None:
                            progress(run)
    finally:
        _git(root, "checkout", "--quiet", "--detach", head, check=False)
    return result


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
    wanted_ids: set[str],
    wanted_files: set[str],
    locator: _Locator,
) -> AgentRun:
    command = claude_command(
        claude,
        task_prompt(commit.subject, arm),
        arm,
        mcp_config_path=config_path,
        model=model,
        max_turns=max_turns,
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
        return AgentRun(commit.sha[:12], arm.name, repeat, ok=False, reason="timeout")
    except OSError as exc:
        return AgentRun(commit.sha[:12], arm.name, repeat, ok=False, reason=f"could not start: {exc}")
    trace = parse_stream(completed.stdout.splitlines())
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if arm.mcp and trace.mcp_attached is False:
        return AgentRun(
            commit.sha[:12], arm.name, repeat, ok=False, reason="repoatlas did not attach"
        )
    if not trace.ok:
        reason = trace.reason or f"exit {completed.returncode}"
        tail = completed.stderr.strip().splitlines()[-1:] if completed.stderr else []
        return AgentRun(
            commit.sha[:12], arm.name, repeat, ok=False, reason=" ".join([reason, *tail])[:200]
        )
    locations = parse_locations(trace.result_text)
    symbol_recall, file_recall, precision = score_locations(
        locations, wanted_ids, wanted_files, locator
    )
    return AgentRun(
        sha=commit.sha[:12],
        arm=arm.name,
        repeat=repeat,
        ok=True,
        symbol_recall=symbol_recall,
        file_recall=file_recall,
        file_precision=precision,
        named=len(locations),
        tokens=trace.tokens,
        cost_usd=trace.cost_usd,
        turns=trace.turns,
        duration_ms=trace.duration_ms or elapsed_ms,
        mcp_calls=trace.mcp_calls,
        tool_calls=dict(trace.tool_calls),
    )


def iter_arms(names: str) -> Iterator[str]:
    for name in names.split(","):
        cleaned = name.strip()
        if cleaned:
            yield cleaned
