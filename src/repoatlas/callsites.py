"""The other question: not "where do I change this" but "who calls this".

The localisation harness in `agentbench` measures the first. An
independent five-arm ablation across three models found that on
localisation an agent barely reaches for a semantic tool at all, 0 to 6
percent of the time, and that a language server costs it a few percent
more tokens for the same answer; on reference completeness the same
agents reach for it half the time unprompted and their F1 rises from
0.71 to 0.78. It also found what predicts the difference: how often the
target's name is used for something else in the same repository.

This module poses the second question, with ground truth that is nobody's
opinion: a compiler-backed SCIP index of the same tree says where the
call sites are. The agent is asked to find them; the answer is scored
against the oracle's own occurrence list, so an index that invents a
caller is punished exactly as an agent that misses one is.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import IndexSnapshot, Symbol, SymbolKind

__all__ = [
    "CallSiteTask",
    "collision_rate",
    "score_sites",
    "select_tasks",
    "site_prompt",
]

# Kinds worth asking about: things that are called or read by name.
_ASKABLE = frozenset(
    {SymbolKind.METHOD, SymbolKind.FUNCTION, SymbolKind.FIELD, SymbolKind.PROPERTY}
)


@dataclass(frozen=True, slots=True)
class CallSiteTask:
    """One symbol, and every place the oracle says it is used."""

    symbol_id: str
    name: str
    path: str
    line: int
    kind: str
    sites: frozenset[tuple[str, int]]
    collisions: int
    """How many other symbols in the repository share this name: grep's difficulty."""

    grep_lines: int
    """How many lines a whole-word search for the name would return: grep's noise."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "name": self.name,
            "path": self.path,
            "line": self.line,
            "kind": self.kind,
            "sites": len(self.sites),
            "collisions": self.collisions,
            "grep_lines": self.grep_lines,
        }


def site_prompt(task: CallSiteTask, hint: str) -> str:
    """The task as the agent sees it: name every use of one symbol."""
    return (
        f"In this repository, find every place that uses the {task.kind} "
        f"`{task.name}` declared at {task.path}:{task.line}. Uses of a different "
        f"symbol that happens to have the same name do not count.\n\n{hint} "
        "Do not edit anything. Reply with only a JSON array of strings, each a "
        'repository-relative "path:line" of one use, and nothing else. Include '
        "every use you find; an incomplete list is worse than a long one."
    )


def collision_rate(snapshot: IndexSnapshot) -> dict[str, int]:
    """How many symbols share each name: what makes a name hard to grep for."""
    counts: dict[str, int] = {}
    for symbol in snapshot.symbols.values():
        if symbol.synthetic or symbol.local:
            continue
        counts[symbol.name] = counts.get(symbol.name, 0) + 1
    return counts


def select_tasks(
    oracle: IndexSnapshot,
    root: Path,
    *,
    limit: int = 20,
    min_sites: int = 3,
    max_sites: int = 40,
    min_collisions: int = 1,
) -> list[CallSiteTask]:
    """Symbols worth asking about, hardest for grep first.

    A symbol with one call site tests nothing; one with hundreds is a
    reading exercise. Between those, the ones ordered first are the ones
    whose name is used elsewhere for something else, because that is
    where a name search stops being an answer.
    """
    from .eval.facts import reference_facts

    facts, _ = reference_facts(oracle, require_site=True, collapse_kinds=True)
    by_target: dict[tuple[str, int], set[tuple[str, int]]] = {}
    for fact in facts:
        key = (fact.target_path, fact.target_span.start.line)
        by_target.setdefault(key, set()).add(
            (fact.site_path, fact.site_span.start.line + 1)
        )

    collisions = collision_rate(oracle)
    texts: dict[str, str] = {}
    for path in oracle.paths:
        try:
            texts[path] = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    tasks: list[CallSiteTask] = []
    for symbol in oracle.symbols.values():
        if symbol.synthetic or symbol.local or symbol.kind not in _ASKABLE:
            continue
        sites = by_target.get((symbol.path, symbol.name_range.start.line))
        if not sites:
            continue
        # A declaration is not a use of itself.
        sites = {
            site for site in sites if site != (symbol.path, symbol.name_range.start.line + 1)
        }
        if not (min_sites <= len(sites) <= max_sites):
            continue
        shared = collisions.get(symbol.name, 1)
        if shared < min_collisions:
            continue
        pattern = re.compile(rf"\b{re.escape(symbol.name)}\b")
        noise = sum(len(pattern.findall(text)) for text in texts.values())
        tasks.append(
            CallSiteTask(
                symbol_id=symbol.id,
                name=symbol.name,
                path=symbol.path,
                line=symbol.name_range.start.line + 1,
                kind=symbol.kind.value,
                sites=frozenset(sites),
                collisions=shared,
                grep_lines=noise,
            )
        )
    # Hardest for grep first, then deterministically by name.
    tasks.sort(key=lambda task: (-task.grep_lines, -task.collisions, task.symbol_id))
    return tasks[:limit]


def score_sites(
    answered: Sequence[tuple[str, int | None]], task: CallSiteTask, *, slack: int = 1
) -> tuple[float, float, float]:
    """Precision, recall and F1 of an answer against the oracle's sites.

    ``slack`` allows a line either side: an agent pointing at the line
    above a wrapped call has found it, and the oracle's line is the
    occurrence's, not the statement's.
    """
    if not task.sites:
        return 0.0, 0.0, 0.0
    # A list of locations is a set: naming one twice is naming it once,
    # and should neither help nor hurt.
    given: list[tuple[str, int]] = []
    for path, line in answered:
        if line is not None and (path, line) not in given:
            given.append((path, line))
    if not given:
        return 0.0, 0.0, 0.0
    wanted = set(task.sites)
    matched_sites: set[tuple[str, int]] = set()
    used: set[int] = set()
    correct = 0
    # Exact lines first, then the ones a line either side rescues. Without
    # that order an answer repeating one line twice would take two
    # adjacent sites, and padding would look like finding.
    for distance in range(slack + 1):
        for index, (path, line) in enumerate(given):
            if index in used:
                continue
            hit = next(
                (
                    site
                    for site in sorted(wanted - matched_sites)
                    if site[0] == path and abs(site[1] - line) == distance
                ),
                None,
            )
            if hit is not None:
                matched_sites.add(hit)
                used.add(index)
                correct += 1
    precision = correct / len(given)
    recall = len(matched_sites) / len(wanted)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


@dataclass(slots=True)
class SiteRun:
    """One agent, one symbol, one arm."""

    symbol_id: str
    name: str
    arm: str
    repeat: int
    ok: bool
    reason: str = ""
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    answered: int = 0
    sites: int = 0
    collisions: int = 0
    grep_lines: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    duration_ms: int = 0
    mcp_calls: int = 0
    denials: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "name": self.name,
            "arm": self.arm,
            "repeat": self.repeat,
            "ok": self.ok,
            "reason": self.reason,
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "f1": round(self.f1, 3),
            "answered": self.answered,
            "sites": self.sites,
            "collisions": self.collisions,
            "grep_lines": self.grep_lines,
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 4),
            "turns": self.turns,
            "duration_ms": self.duration_ms,
            "mcp_calls": self.mcp_calls,
            "denials": self.denials,
            "tool_calls": dict(sorted(self.tool_calls.items())),
        }


@dataclass(slots=True)
class SiteBenchResult:
    arms: tuple[str, ...]
    runs: list[SiteRun] = field(default_factory=list)
    tasks: int = 0
    stopped: str = ""

    def scored(self, arm: str) -> list[SiteRun]:
        return [run for run in self.runs if run.arm == arm and run.ok]

    def excluded(self) -> list[SiteRun]:
        return [run for run in self.runs if not run.ok]

    def _mean(self, arm: str, name: str) -> float:
        values = [float(getattr(run, name)) for run in self.scored(arm)]
        return statistics.mean(values) if values else 0.0

    def paired(self, name: str) -> list[float]:
        """Per-symbol difference, second arm minus first, over symbols both answered."""
        if len(self.arms) != 2:
            return []
        first, second = self.arms
        by_symbol: dict[str, dict[str, list[float]]] = {}
        for run in self.runs:
            if run.ok:
                by_symbol.setdefault(run.symbol_id, {}).setdefault(run.arm, []).append(
                    float(getattr(run, name))
                )
        return [
            statistics.mean(arms[second]) - statistics.mean(arms[first])
            for arms in by_symbol.values()
            if first in arms and second in arms
        ]

    def as_dict(self, *, include_runs: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "arms": list(self.arms),
            "tasks": self.tasks,
            "stopped": self.stopped,
            "excluded": len(self.excluded()),
            "per_arm": {
                arm: {
                    "runs": len(self.scored(arm)),
                    "precision": round(self._mean(arm, "precision"), 4),
                    "recall": round(self._mean(arm, "recall"), 4),
                    "f1": round(self._mean(arm, "f1"), 4),
                    "answered": round(self._mean(arm, "answered"), 1),
                    "tokens": round(self._mean(arm, "tokens")),
                    "cost_usd": round(self._mean(arm, "cost_usd"), 4),
                    "turns": round(self._mean(arm, "turns"), 1),
                    "mcp_calls": round(self._mean(arm, "mcp_calls"), 2),
                }
                for arm in self.arms
            },
        }
        for name in ("f1", "recall", "precision", "tokens"):
            deltas = self.paired(name)
            if deltas:
                payload[f"delta_{name}"] = {
                    "second_minus_first": round(statistics.mean(deltas), 4),
                    "wins": sum(1 for value in deltas if value > 0),
                    "losses": sum(1 for value in deltas if value < 0),
                    "pairs": len(deltas),
                }
        if include_runs:
            payload["runs"] = [run.as_dict() for run in self.runs]
        return payload

    def as_text(self) -> str:
        lines = [
            f"symbols:  {self.tasks} asked; {len(self.excluded())} run(s) excluded",
            *([f"stopped:  {self.stopped}"] if self.stopped else []),
            "",
            f"{'':<16}" + "".join(f"{arm:>12}" for arm in self.arms),
        ]
        for label, name, form in (
            ("runs", None, "d"),
            ("precision", "precision", ".3f"),
            ("recall", "recall", ".3f"),
            ("F1", "f1", ".3f"),
            ("answered", "answered", ".1f"),
            ("tokens", "tokens", ",.0f"),
            ("cost usd", "cost_usd", ".3f"),
            ("turns", "turns", ".1f"),
            ("mcp calls", "mcp_calls", ".1f"),
        ):
            cells = []
            for arm in self.arms:
                if name is None:
                    cells.append(f"{len(self.scored(arm)):>12d}")
                else:
                    cells.append(f"{self._mean(arm, name):>12{form}}")
            lines.append(f"{label:<16}" + "".join(cells))
        for name in ("f1", "recall", "tokens"):
            deltas = self.paired(name)
            if deltas:
                wins = sum(1 for value in deltas if value > 0)
                losses = sum(1 for value in deltas if value < 0)
                lines.append(
                    f"\n{self.arms[1]} minus {self.arms[0]}, {name}: "
                    f"{statistics.mean(deltas):+.3f} over {len(deltas)} symbol(s), "
                    f"W{wins}/L{losses}"
                )
        for run in self.excluded():
            lines.append(f"  excluded {run.name} {run.arm}: {run.reason}")
        return "\n".join(lines) + "\n"


def run_sitebench(
    root: Path,
    oracle_path: Path,
    *,
    store: Path,
    arms: Sequence[str] = ("grep", "repoatlas"),
    limit: int = 20,
    repeats: int = 1,
    claude: str = "claude",
    model: str | None = None,
    max_turns: int = 30,
    timeout: int = 900,
    serena: str | None = None,
    progress: Any = None,
) -> SiteBenchResult:
    """Ask both arms where a symbol is used, and score against the oracle.

    ``root`` is a working tree, ``oracle_path`` a SCIP index of it, and
    ``store`` an index of the same tree for the server to serve. Nothing
    is checked out or modified.
    """
    import json
    import shutil

    from .agentbench import ARMS, _is_fatal, _run_command, mcp_config
    from .localize import HistoryError
    from .oracle.scip import read_scip

    chosen = tuple(arms)
    for name in chosen:
        if name not in ARMS:
            raise HistoryError(f"unknown arm {name!r}; choose from {', '.join(ARMS)}")
    if shutil.which(claude) is None and not Path(claude).exists():
        raise HistoryError(f"no Claude Code executable at {claude!r}")
    oracle = read_scip(oracle_path)
    tasks = select_tasks(oracle, root, limit=limit)
    if not tasks:
        raise HistoryError("no symbol in this oracle has enough call sites to ask about")
    configs: dict[str, Path] = {}
    for arm_name in chosen:
        arm = ARMS[arm_name]
        if not arm.mcp:
            continue
        path = Path(f"{store}.{arm_name}.mcp.json")
        path.write_text(
            json.dumps(mcp_config(root, store, server=arm.server or "", serena=serena)),
            encoding="utf-8",
        )
        configs[arm_name] = path
    result = SiteBenchResult(arms=chosen, tasks=len(tasks))
    for task in tasks:
        for arm_name in chosen:
            arm = ARMS[arm_name]
            for repeat in range(1, repeats + 1):
                trace = _run_command(
                    root,
                    site_prompt(task, arm.hint),
                    arm,
                    claude=claude,
                    model=model,
                    max_turns=max_turns,
                    timeout=timeout,
                    config_path=configs.get(arm_name),
                )
                if isinstance(trace, str):
                    run = SiteRun(task.symbol_id, task.name, arm_name, repeat, False, trace)
                else:
                    from .agentbench import parse_locations

                    answered = parse_locations(trace.result_text)
                    precision, recall, f1 = score_sites(answered, task)
                    run = SiteRun(
                        symbol_id=task.symbol_id,
                        name=task.name,
                        arm=arm_name,
                        repeat=repeat,
                        ok=True,
                        precision=precision,
                        recall=recall,
                        f1=f1,
                        answered=len(answered),
                        sites=len(task.sites),
                        collisions=task.collisions,
                        grep_lines=task.grep_lines,
                        tokens=trace.tokens,
                        cost_usd=trace.cost_usd,
                        turns=trace.turns,
                        duration_ms=trace.duration_ms,
                        mcp_calls=trace.mcp_calls,
                        denials=trace.denials,
                        tool_calls=dict(trace.tool_calls),
                    )
                result.runs.append(run)
                if progress is not None:
                    progress(run)
                if not run.ok and _is_fatal(run.reason):
                    result.stopped = run.reason
                    return result
    return result


def _sites_of(oracle: IndexSnapshot, symbol: Symbol) -> Iterable[tuple[str, int]]:  # pragma: no cover
    from .eval.facts import reference_facts

    facts, _ = reference_facts(oracle, require_site=True)
    for fact in facts:
        if (fact.target_path, fact.target_span.start.line) == (
            symbol.path,
            symbol.name_range.start.line,
        ):
            yield fact.site_path, fact.site_span.start.line + 1
