"""Tier 4, without a shell: reviewing a diff you cannot grep.

Every negative result in this project was measured in the same setting —
a local checkout, a shell, and grep. Five experiments, five ties, and the
consistent explanation was that the agent reads whatever it is given and
then greps, so grep recovers anything the index would have supplied.

This is the setting where it cannot. A reviewer looking at a pull request
through an API has the diff and can fetch a file by path; it has no
`rg` over the tree, because there is no tree. That is not a contrivance
to make the index look good: it is how a review bot, a CI check and a
hosted agent actually see a repository, and the comparison has never been
run there.

The question is the reviewer's own: *this changed — what else uses it?*
The ground truth is the compiler-backed oracle's references, minus the
ones inside the file the diff already shows, because a reviewer can see
those. Tasks come from :mod:`repoatlas.callsites`, so the population is
the same one the shell-equipped benchmark used and the two are readable
side by side.

The diff is synthesised rather than taken from history, and deliberately.
A real commit's diff would have to be scored against an oracle built at
that commit, and the reference lines drift between one commit and the
next; synthesising the hunk from the tree the oracle indexed keeps every
location exact. What is synthetic is the edit. The symbol, its code, the
question and the answer are all real.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agentbench import Arm, RunTrace, _run_command, mcp_config
from .callsites import CallSiteTask, score_sites, select_tasks
from .localize import HistoryError
from .model import IndexSnapshot

__all__ = [
    "REVIEW_ARMS",
    "ReviewBenchResult",
    "ReviewRun",
    "diff_of",
    "review_prompt",
    "run_reviewbench",
]

# No Grep, no Glob, no shell. `Read` stays because an API-backed reviewer
# can fetch a file whose path it knows, and holding that constant is what
# makes the two arms differ by the index alone.
_NO_SHELL = ("Read",)

# What "no checkout" has to mean, spelled out, because `--allowedTools`
# alone does not withhold anything: the first run of this benchmark had
# the read arm reaching for Bash and grepping with it, and scoring 1.000.
_WITHHELD = (
    # Anything that runs a command, searches a tree, or spawns something
    # that can. The list is explicit rather than "everything but Read"
    # because ToolSearch has to stay: it is how the MCP tools are loaded,
    # and denying it would cripple the arm under test instead of the
    # baseline. Twice now a hole here has invalidated a run — Bash first,
    # then Monitor, which takes a shell command of its own.
    "Bash",
    "Grep",
    "Glob",
    "LS",
    "Task",
    "Agent",
    "Monitor",
    "Workflow",
    "Skill",
    "WebSearch",
    "WebFetch",
    "Edit",
    "Write",
    "NotebookEdit",
    "CronCreate",
    "CronDelete",
    "CronList",
    "RemoteTrigger",
    "SendMessage",
    "ScheduleWakeup",
    "PushNotification",
    "DesignSync",
    "EnterWorktree",
    "ExitWorktree",
    "TaskOutput",
    "TaskStop",
    "ListAgents",
    "Artifact",
)

REVIEW_ARMS: dict[str, Arm] = {
    "read": Arm(
        name="read",
        server=None,
        allowed_tools=_NO_SHELL,
        denied_tools=_WITHHELD,
        hint=(
            "You have no shell and no search: there is no checkout. You may read "
            "a file with Read if you can work out its path."
        ),
    ),
    "index": Arm(
        name="index",
        server="repoatlas",
        allowed_tools=(*_NO_SHELL, "mcp__repoatlas__*"),
        denied_tools=_WITHHELD,
        hint=(
            "You have no shell and no search: there is no checkout. An MCP server "
            "called repoatlas is attached, with an index of this repository: symbol "
            "search, outlines, and resolved references. You may also read a file "
            "with Read if you can work out its path."
        ),
    ),
    # The reference point, not a competitor: what the same question costs
    # when the tree is on disk after all. Every other benchmark here ran
    # under these conditions, and it belongs in the table so the two
    # settings can be compared rather than asserted about.
    "grep": Arm(
        name="grep",
        server=None,
        allowed_tools=(
            "Read",
            "Grep",
            "Glob",
            "LS",
            "Bash(rg *)",
            "Bash(grep *)",
            "Bash(ls *)",
            "Bash(find *)",
            "Bash(cat *)",
            "Bash(sed -n *)",
        ),
        hint="Use Grep, Glob and Read over the checkout.",
    ),
}

_ANSWER_RULES = (
    "Do not edit anything. Reply with only a JSON array of strings, each a "
    'repository-relative "path:line" of one place that uses what this hunk '
    "declares, and nothing else. Do not list anything inside the changed file: "
    "the diff already shows it."
)


def diff_of(root: Path, task: CallSiteTask, *, context: int = 4) -> str:
    """A unified diff of a plausible edit to one symbol.

    The hunk names the symbol the way a review does — by showing its code
    — rather than by handing over a location, which is the whole point:
    an agent that is told `path:line` has been given the answer's first
    half.
    """
    try:
        lines = (root / task.path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise HistoryError(f"cannot read {task.path}: {exc}") from None
    index = max(0, task.line - 1)
    if index >= len(lines):
        raise HistoryError(f"{task.path} has no line {task.line}")
    start = max(0, index - context)
    end = min(len(lines), index + context + 1)
    body = lines[start:end]
    header = [
        f"diff --git a/{task.path} b/{task.path}",
        f"--- a/{task.path}",
        f"+++ b/{task.path}",
        f"@@ -{start + 1},{len(body)} +{start + 1},{len(body)} @@",
    ]
    out = []
    for offset, text in enumerate(body, start=start):
        if offset == index:
            out.append(f"-{text}")
            out.append(f"+{text}")
        else:
            out.append(f" {text}")
    return "\n".join([*header, *out])


def review_prompt(task: CallSiteTask, diff: str, hint: str) -> str:
    """The reviewer's question, with the hunk and nothing else given away."""
    return (
        "You are reviewing a change to this repository. The declaration in the "
        "hunk below was modified, so every place that uses it has to be checked.\n\n"
        f"{diff}\n\n"
        "Find every place elsewhere in the repository that uses what this hunk "
        "declares. Uses of a different symbol that happens to share the name do "
        f"not count.\n\n{hint} {_ANSWER_RULES}"
    )


@dataclass(slots=True)
class ReviewRun:
    """One agent, one hunk, one arm."""

    name: str
    path: str = ""
    line: int = 0
    """Where the hunk's declaration is.

    A name alone does not identify a task: two `apply` methods on
    different classes are two questions, and an analysis keyed on the name
    silently keeps one of them.
    """

    arm: str = ""
    ok: bool = False
    reason: str = ""
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    answered: int = 0
    sites: int = 0
    grep_lines: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    mcp_calls: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "line": self.line,
            "arm": self.arm,
            "ok": self.ok,
            "reason": self.reason,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "answered": self.answered,
            "sites": self.sites,
            "grep_lines": self.grep_lines,
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 4),
            "turns": self.turns,
            "mcp_calls": self.mcp_calls,
            "tool_calls": dict(self.tool_calls),
        }


@dataclass(slots=True)
class ReviewBenchResult:
    arms: tuple[str, ...]
    tasks: int = 0
    runs: list[ReviewRun] = field(default_factory=list)
    stopped: str = ""

    def scored(self, arm: str) -> list[ReviewRun]:
        return [run for run in self.runs if run.arm == arm and run.ok]

    def _mean(self, arm: str, name: str) -> float:
        values = [float(getattr(run, name)) for run in self.scored(arm)]
        return statistics.mean(values) if values else 0.0

    def as_dict(self, *, include_runs: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "arms": list(self.arms),
            "tasks": self.tasks,
            "stopped": self.stopped,
            "per_arm": {
                arm: {
                    "runs": len(self.scored(arm)),
                    "precision": round(self._mean(arm, "precision"), 4),
                    "recall": round(self._mean(arm, "recall"), 4),
                    "f1": round(self._mean(arm, "f1"), 4),
                    "answered": round(self._mean(arm, "answered"), 2),
                    "tokens": int(self._mean(arm, "tokens")),
                    "cost_usd": round(self._mean(arm, "cost_usd"), 4),
                    "turns": round(self._mean(arm, "turns"), 2),
                    "mcp_calls": round(self._mean(arm, "mcp_calls"), 2),
                }
                for arm in self.arms
            },
        }
        if include_runs:
            payload["runs"] = [run.as_dict() for run in self.runs]
        return payload

    def as_text(self) -> str:
        rows: list[tuple[str, Callable[[str], str]]] = [
            ("runs", lambda a: f"{len(self.scored(a))}"),
            ("precision", lambda a: f"{self._mean(a, 'precision'):.3f}"),
            ("recall", lambda a: f"{self._mean(a, 'recall'):.3f}"),
            ("F1", lambda a: f"{self._mean(a, 'f1'):.3f}"),
            ("answered", lambda a: f"{self._mean(a, 'answered'):.1f}"),
            ("turns", lambda a: f"{self._mean(a, 'turns'):.1f}"),
            ("tokens", lambda a: f"{int(self._mean(a, 'tokens')):,}"),
            ("cost usd", lambda a: f"{self._mean(a, 'cost_usd'):.3f}"),
            ("mcp calls", lambda a: f"{self._mean(a, 'mcp_calls'):.1f}"),
        ]
        width = max(len(label) for label, _ in rows) + 2
        lines = [" " * width + "".join(f"{arm:>12}" for arm in self.arms)]
        for label, render in rows:
            lines.append(f"{label:<{width}}" + "".join(f"{render(a):>12}" for a in self.arms))
        excluded = [run for run in self.runs if not run.ok]
        if excluded:
            lines.append("")
            lines.append("excluded:")
            for run in excluded[:8]:
                lines.append(
                    f"  {run.name} ({run.path}:{run.line}) {run.arm}: {run.reason[:60]}"
                )
        if self.stopped:
            lines.append(f"stopped: {self.stopped[:80]}")
        return "\n".join(lines) + "\n"


def _answers(text: str) -> list[tuple[str, int | None]]:
    from .agentbench import parse_locations

    return parse_locations(text)


def run_reviewbench(
    root: Path,
    oracle: IndexSnapshot,
    store: Path,
    *,
    arms: tuple[str, ...] = ("read", "index"),
    limit: int = 14,
    claude: str = "claude",
    model: str | None = None,
    max_turns: int = 30,
    timeout: int = 900,
    progress: Any = None,
) -> ReviewBenchResult:
    """Put each hunk to each arm and score the answers against the oracle."""
    for name in arms:
        if name not in REVIEW_ARMS:
            raise HistoryError(
                f"unknown arm {name!r}; choose from {', '.join(REVIEW_ARMS)}"
            )
    # Two uses outside the declaring file, at least: a reviewer already
    # has that file in the diff, so a symbol used only within it asks
    # nothing. Selecting on that beats dropping afterwards, which on the
    # larger application discarded all twenty tasks and ran nothing.
    tasks = select_tasks(oracle, root, limit=limit, min_external=2)
    if not tasks:
        raise HistoryError("no symbol in this oracle has enough use sites to ask about")

    configs: dict[str, Path] = {}
    for name in arms:
        arm = REVIEW_ARMS[name]
        if not arm.mcp:
            continue
        path = Path(f"{store}.review.{name}.json")
        path.write_text(
            json.dumps(mcp_config(root, store, server=arm.server or "")), encoding="utf-8"
        )
        configs[name] = path

    result = ReviewBenchResult(arms=tuple(arms), tasks=len(tasks))
    for task in tasks:
        # A reviewer already sees the changed file, so uses inside it are
        # not what the question is about.
        elsewhere = frozenset(site for site in task.sites if site[0] != task.path)
        if not elsewhere:
            continue
        scoped = CallSiteTask(
            symbol_id=task.symbol_id,
            name=task.name,
            path=task.path,
            line=task.line,
            kind=task.kind,
            sites=elsewhere,
            collisions=task.collisions,
            grep_lines=task.grep_lines,
        )
        diff = diff_of(root, task)
        for name in arms:
            arm = REVIEW_ARMS[name]
            outcome = _run_command(
                root,
                review_prompt(scoped, diff, arm.hint),
                arm,
                claude=claude,
                model=model,
                max_turns=max_turns,
                timeout=timeout,
                config_path=configs.get(name),
            )
            run = _score(scoped, name, outcome)
            result.runs.append(run)
            if progress is not None:
                progress(run)
    return result


def _score(task: CallSiteTask, arm: str, outcome: RunTrace | str) -> ReviewRun:
    if isinstance(outcome, str):
        return ReviewRun(
            name=task.name, path=task.path, line=task.line, arm=arm, ok=False, reason=outcome
        )
    answered = _answers(outcome.result_text)
    precision, recall, f1 = score_sites(answered, task)
    return ReviewRun(
        name=task.name,
        path=task.path,
        line=task.line,
        arm=arm,
        ok=True,
        precision=precision,
        recall=recall,
        f1=f1,
        answered=len(answered),
        sites=len(task.sites),
        grep_lines=task.grep_lines,
        tokens=outcome.tokens,
        cost_usd=outcome.cost_usd,
        turns=outcome.turns,
        mcp_calls=outcome.mcp_calls,
        tool_calls=dict(outcome.tool_calls),
    )
