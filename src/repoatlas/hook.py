"""Answer the question grep just asked, when grep answered it wrongly.

Nine agent-level comparisons said the same thing: where a shell and a
checkout exist, the index does not beat `grep`. The tool-call records say
why, and it is not that the index is worse. Offline, `find_references`
scores 1.000 F1 against `rg -w`'s 0.847 for half the tokens. The agent
simply never calls it — given Serena it called it 0 times out of 8, given a
ranked map 0.8 times a run. It reads its context and it greps, as it always
has.

Descriptions do not fix that; the seven tools already say when to use them.
So this stops offering and starts answering. It runs after a search, sees
what the search printed, and adds what the index knows that the search could
not have said.

What it adds is one thing only, because one thing is what the 0.153 gap is
made of: **which definition each hit belongs to**. `rg -w save` returns
every line containing the token. The index knows that those lines are calls
to four different methods in four different classes, and that eight of the
forty are calls to the one being asked about. An agent that greps a common
method name and reads the first file it sees is making exactly the error
this closes.

Two rules keep it from becoming noise:

- It speaks only when it has something a text search could not produce: a
  name that resolves to more than one symbol, or a resolved site the search
  did not print. When grep already answered, this costs nothing at all.
- Everything it says is a stored edge with a confidence tier behind it. It
  never infers from *absence* — a line grep printed that the index has no
  edge for is not reported as noise, because "I have no edge here" is not
  evidence that the line is meaningless.

It never fails loudly. A hook that wedges a session is worse than a hook
that misses a case, so every error path stays silent and exits zero.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["HookInput", "advise", "main", "run_hook"]

# A search worth commenting on is a search for a name. A regex is not a
# symbol, and answering "which symbol did you mean" for `\bfoo.*bar` would
# be noise dressed as help.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")

# `path:line:` or `path:line` as ripgrep and the Grep tool print them.
# Windows drive letters would eat the first colon, so a path is only taken
# up to the last colon that a line number follows.
_HIT = re.compile(r"^(.+?):(\d+)(?::|$)", re.MULTILINE)

# Bare paths, for a search that printed file names and no line numbers.
_PATH = re.compile(r"^[^\s:]+\.[A-Za-z]{1,10}$", re.MULTILINE)

# How many sites to list before saying how many are left. A hook that
# prints forty lines has replaced one cost with another.
MAX_SITES = 12
MAX_TARGETS = 6

# What `--verbose` raises them to, and what it drops: the silence rule.
# Not a setting anyone should run day to day — it exists so a benchmark
# can ask how much accuracy is available at any price, before asking what
# the price should be. If speaking always scores worse than speaking
# rarely, that is an answer rather than a bug.
LOUD_SITES = 40
LOUD_TARGETS = 12

SEARCH_TOOLS = frozenset({"Grep"})


@dataclass(frozen=True)
class HookInput:
    """What a PostToolUse hook is handed, reduced to what matters here."""

    tool_name: str
    pattern: str
    response: str
    cwd: Path

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> HookInput | None:
        tool = str(payload.get("tool_name") or "")
        if tool not in SEARCH_TOOLS:
            return None
        arguments = payload.get("tool_input")
        if not isinstance(arguments, dict):
            return None
        pattern = str(arguments.get("pattern") or "").strip()
        if not _IDENTIFIER.match(pattern):
            return None
        response = payload.get("tool_response")
        if not isinstance(response, str):
            response = json.dumps(response) if response is not None else ""
        return cls(
            tool_name=tool,
            pattern=pattern,
            response=response,
            cwd=Path(str(payload.get("cwd") or ".")),
        )


def store_path(cwd: Path, named: Path | None = None) -> Path | None:
    """Where this project's index lives, if it has one.

    Never builds and never refreshes: a hook runs inside the agent's turn,
    and an index that takes two seconds to freshen would cost more than
    anything it could say. A stale answer is a real hazard, so the message
    says which commit or tree the store was built from and lets the reader
    judge.
    """
    override = str(named) if named else os.environ.get("REPOATLAS_STORE")
    if override:
        # Named explicitly, so do not fall back to searching: a benchmark
        # arm that silently answered from a different index than the one it
        # was pointed at would be measuring nothing.
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    roots = [cwd]
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if project:
        roots.insert(0, Path(project))
    for root in roots:
        candidate = root / ".repoatlas" / "index.db"
        if candidate.is_file():
            return candidate
    return None


def printed_hits(response: str) -> tuple[set[tuple[str, int]], set[str]]:
    """The (file, line) pairs and the bare files a search printed.

    The Grep tool has several output modes and only some carry line
    numbers, so both are collected and the comparison falls back to file
    granularity when lines are absent.
    """
    sites: set[tuple[str, int]] = set()
    files: set[str] = set()
    for match in _HIT.finditer(response):
        path = match.group(1).strip().replace("\\", "/")
        files.add(path)
        try:
            sites.add((path, int(match.group(2))))
        except ValueError:  # pragma: no cover - the regex guarantees digits
            continue
    for match in _PATH.finditer(response):
        files.add(match.group(0).strip().replace("\\", "/"))
    return sites, files


def _uses(edges: list[Any]) -> list[Any]:
    """The edges that are uses, not the declaration itself.

    `edges_to` includes the `contains` edge that ties a symbol to the file
    it is declared in, whose site is the declaration. Listing that as a use
    would put the definition's own line in a list of call sites — an
    answer that is wrong in the direction that costs a turn.
    """
    return [
        edge
        for edge in edges
        if not edge.kind.is_structural and edge.site_path and edge.site_range
    ]


def _tail(path: str) -> str:
    """Compare paths by their tail, since a search may print absolute ones."""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def advise(store: Any, hook: HookInput, *, verbose: bool = False) -> str:
    """What the index can add to this search, or an empty string.

    Empty is the common case and the intended one. ``verbose`` removes
    that: it answers every search the index knows anything about, with
    higher limits, for measuring the accuracy ceiling.
    """
    max_targets = LOUD_TARGETS if verbose else MAX_TARGETS
    max_sites = LOUD_SITES if verbose else MAX_SITES
    symbols = [s for s in store.symbols_named(hook.pattern, limit=max_targets + 1) if not s.synthetic]
    if not symbols:
        return ""

    sites, files = printed_hits(hook.response)
    by_file: dict[str, set[int]] = {}
    for path, line in sites:
        by_file.setdefault(_tail(path), set()).add(line)
    printed_files = {_tail(path) for path in files}

    groups: list[tuple[Any, list[Any]]] = []
    unprinted = 0
    for symbol in symbols[:max_targets]:
        edges = _uses(store.edges_to(symbol.id))
        groups.append((symbol, edges))
        for edge in edges:
            name = _tail(edge.site_path)
            lines = by_file.get(name)
            if lines is None:
                # The search never printed this file at all.
                unprinted += 1 if name not in printed_files else 0
            elif edge.site_range.start.line + 1 not in lines:
                unprinted += 1

    ambiguous = len(symbols) > 1
    if not ambiguous and unprinted == 0 and not verbose:
        # The search already said everything the index could. Saying it
        # again is the cost with none of the benefit.
        return ""

    total = sum(len(edges) for _, edges in groups)
    if total == 0 and not ambiguous and not verbose:
        return ""

    lines_out: list[str] = []
    more = " (more not listed)" if len(symbols) > max_targets else ""
    lines_out.append(
        f"repoatlas: `{hook.pattern}` names {len(symbols[:max_targets])} "
        f"indexed symbol(s){more}. Resolved uses, by which one they reach:"
    )
    budget = max_sites
    for symbol, edges in sorted(groups, key=lambda pair: -len(pair[1])):
        where = f"{symbol.path}:{symbol.name_range.start.line + 1}"
        label = symbol.qualified_name or symbol.name
        lines_out.append(f"  {label}  defined {where}  — {len(edges)} use(s)")
        for edge in edges[:budget]:
            lines_out.append(
                f"      {edge.site_path}:{edge.site_range.start.line + 1}"
                f"  ({edge.tier.label} {edge.confidence or 0:.2f})"
            )
        budget -= min(len(edges), budget)
        if budget <= 0:
            break
    hidden = total - min(total, max_sites)
    if hidden > 0:
        lines_out.append(f"  ... {hidden} further use(s) not listed")
    if ambiguous:
        lines_out.append(
            "  A text search cannot tell these apart: every hit above shares "
            "the name but not the definition."
        )
    lines_out.append(f"  {_provenance(store)}")
    return "\n".join(lines_out)


def _provenance(store: Any) -> str:
    """One line saying what the index describes, so staleness is visible."""
    commit = store.get_meta("commit")
    if commit:
        return f"(index built from {store.get_meta('revision') or '?'} at {commit[:12]})"
    root = store.get_meta("project_root") or "this working tree"
    return f"(index of {root}; re-index if it is behind)"


def run_hook(raw: str, named: Path | None = None, *, verbose: bool = False) -> str:
    """The whole hook, from stdin text to stdout text. Never raises."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    hook = HookInput.parse(payload)
    if hook is None:
        return ""
    try:
        from .store import IndexStore
    except ImportError:  # pragma: no cover - depends on the extra
        return ""
    path = store_path(hook.cwd, named)
    if path is None:
        return ""
    try:
        with IndexStore(path) as store:
            message = advise(store, hook, verbose=verbose)
    except Exception:  # a hook must never break the session it runs in
        return ""
    if not message:
        return ""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": message,
            }
        }
    )


def main(store: Path | None = None, *, verbose: bool = False) -> int:
    """Entry point for `repoatlas hook`."""
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return 0
    output = run_hook(raw, store, verbose=verbose)
    if output:
        sys.stdout.write(output)
    return 0
