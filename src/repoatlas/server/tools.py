"""What the index can answer, as plain functions over a store.

Kept separate from the MCP plumbing on purpose. These are the answers; the
server is an adapter. Testing them needs no protocol and no SDK, and a
different front end could serve the same seven.

Three constraints shape every one of them, and all three come from
measurements rather than taste.

Output is text, not JSON. The same content costs thirty to sixty percent
more tokens as JSON, and an agent reads an outline without being told what
the fields mean.

Every answer is bounded. Claude Code truncates a tool result at 25,000
tokens and warns at 10,000, so a tool that returns everything gets cut
somewhere arbitrary. These cut deliberately instead, and say what was left
out and how to ask for less.

Nothing here waits to be discovered. Agents given a graph tool never
called it in fifty-eight percent of trials, so each answer carries the
context that would otherwise need a second call: a search result says how
many things use each hit, so one call tells the agent both what exists and
what matters.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..model import Edge, EdgeKind, Symbol
from ..rank import MapOptions, render_map
from ..rank.cache import RankCache
from ..rank.tokens import TokenEstimator
from ..store import IndexStore

__all__ = [
    "Detail",
    "ToolError",
    "file_outline",
    "find_references",
    "get_symbol",
    "index_status",
    "neighbours",
    "repo_map",
    "search_symbols",
]

Detail = Literal["concise", "detailed"]

# Defaults sit well under the 25,000-token cut so a result is never
# truncated by the client, where the agent cannot see what went missing.
DEFAULT_BUDGET = 4000
MAP_BUDGET = 2000


class ToolError(ValueError):
    """A problem the agent can act on, phrased so it can.

    Never a traceback. "No symbol with that id; try search_symbols" is a
    next step; a stack trace is a dead end.
    """


@dataclass(slots=True)
class _Budget:
    """Tracks how much of an answer has been spent.

    The running total is a sum of per-line estimates rather than one
    estimate of the joined text. That keeps each add constant-time instead
    of re-estimating everything so far, which was quadratic and measured at
    forty-eight microseconds per line by three thousand lines. Per-line
    rounding can only over-count, by less than a token a line, and erring
    under the budget is the safe direction.
    """

    limit: int
    lines: list[str]
    spent: int
    estimate: TokenEstimator

    def __init__(self, limit: int, estimate: TokenEstimator) -> None:
        self.limit = limit
        self.lines = []
        self.spent = 0
        self.estimate = estimate

    def add(self, line: str) -> bool:
        """Append a line, or report that the budget is spent."""
        cost = self.estimate(line + "\n")
        if self.spent + cost > self.limit:
            return False
        self.lines.append(line)
        self.spent += cost
        return True

    def render(self, truncated: int = 0, hint: str = "") -> str:
        text = "\n".join(self.lines)
        if truncated:
            note = f"\n\n[{truncated} more not shown"
            text += note + (f"; {hint}]" if hint else "]")
        return text + "\n" if text and not text.endswith("\n") else text


def _location(symbol: Symbol) -> str:
    """Where a symbol is, in the form an agent can hand to a file reader."""
    return f"{symbol.path}:{symbol.name_range.start.line + 1}"


def _describe(symbol: Symbol, *, detail: Detail = "concise", suffix: str = "") -> str:
    if detail == "concise":
        return f"{_location(symbol)}  {symbol.kind.value} {symbol.qualified_name or symbol.name}{suffix}"
    signature = symbol.signature or ""
    parts = [
        f"{_location(symbol)}  {symbol.kind.value} {symbol.qualified_name or symbol.name}{suffix}",
    ]
    if signature:
        parts.append(f"    {signature}")
    if symbol.documentation:
        first = symbol.documentation.strip().splitlines()[0][:160]
        parts.append(f"    {first}")
    return "\n".join(parts)


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        offset = int(cursor)
    except ValueError:
        raise ToolError(
            f"cursor {cursor!r} is not one this index issued; omit it to start over"
        ) from None
    if offset < 0:
        raise ToolError("cursor must not be negative")
    return offset


def search_symbols(
    store: IndexStore,
    query: str,
    *,
    kinds: tuple[str, ...] = (),
    limit: int = 20,
    cursor: str | None = None,
    detail: Detail = "concise",
    budget: int = DEFAULT_BUDGET,
    estimator: TokenEstimator | None = None,
) -> str:
    """Find definitions whose name contains ``query``.

    Each hit carries how many other symbols refer to it, so one call
    answers both "what exists" and "which of these matters", instead of
    leaving the second question for a follow-up the agent will not make.
    """
    if not query.strip():
        raise ToolError("give a name or part of one to search for")
    offset = _decode_cursor(cursor)
    # Over-fetch by the offset so paging does not need a second index.
    hits = store.search(query, limit=offset + limit, kinds=kinds)
    window = hits[offset : offset + limit]
    if not window:
        if offset:
            raise ToolError(f"no more results past {offset}; omit the cursor to restart")
        kind_note = f" of kind {', '.join(kinds)}" if kinds else ""
        return f"nothing{kind_note} matches {query!r}\n"

    # A count rather than a peek. Fetching one past the window only ever
    # knew "at least one more", and told the agent "1 more" when there
    # were five hundred.
    total = store.search_count(query, kinds=kinds)
    references = store.reference_counts([item.id for item in window])
    budgeted = _Budget(budget, estimator or store.estimator())
    budgeted.add(f"{total} match(es) for {query!r}:")
    shown = 0
    for symbol in window:
        count = references.get(symbol.id, 0)
        suffix = f"  ({count} use{'s' if count != 1 else ''})" if count else ""
        if not budgeted.add(_describe(symbol, detail=detail, suffix=suffix)):
            break
        shown += 1
    remaining = total - offset - shown
    hint = f"pass cursor={offset + shown} for more" if remaining > 0 else ""
    return budgeted.render(max(0, remaining), hint)


def get_symbol(
    store: IndexStore,
    symbol_id: str,
    *,
    detail: Detail = "detailed",
    include_body: bool = False,
    max_body_lines: int = 80,
) -> str:
    """Describe one symbol: where it is, what contains it, what it touches.

    ``include_body`` reads the source from disk. It is off by default
    because a signature and a location are usually what the next step needs,
    and a body is the most expensive thing this index can hand over.
    """
    symbol = store.symbol(symbol_id)
    if symbol is None:
        raise ToolError(
            f"no symbol with id {symbol_id!r}; use search_symbols to find its id"
        )
    lines = [_describe(symbol, detail=detail)]
    if symbol.container_id:
        container = store.symbol(symbol.container_id)
        if container is not None:
            lines.append(f"  in: {container.kind.value} {container.qualified_name or container.name}")
    if symbol.full_range is not None and symbol.full_range != symbol.name_range:
        span = symbol.full_range
        lines.append(f"  lines: {span.start.line + 1}-{span.end.line + 1}")

    outgoing = store.out_degree(symbol.id)
    if outgoing:
        lines.append(f"  uses: {outgoing}")
    # One symbol may reach another by several edges at once, an import and
    # a call among them. The count is of callers, not edges, so five lines
    # are five different places rather than one place repeated, and the
    # store answers it without loading every edge.
    total_callers, sample = store.callers(symbol.id, limit=5)
    if total_callers:
        lines.append(f"  used by: {total_callers}")
        for source in sample:
            lines.append(f"    {_location(source)}  {source.qualified_name or source.name}")
        if total_callers > len(sample):
            lines.append(
                f"    ... {total_callers - len(sample)} more; find_references gives all of them"
            )

    if include_body:
        body = _read_body(store, symbol, max_body_lines)
        if body:
            lines.append("")
            lines.append(body)
    return "\n".join(lines) + "\n"


def _read_body(store: IndexStore, symbol: Symbol, max_lines: int) -> str:
    """Read a symbol's source, if the file is still where the index says.

    The index stores locations, not text, so this is the one place that
    touches the working tree. A file that moved since indexing yields a
    note rather than an error: the location is still useful.
    """
    root = store.get_meta("project_root")
    if not root:
        return "[body unavailable: the index does not record its project root]"
    span = symbol.full_range or symbol.name_range
    target = Path(root) / symbol.path
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"[body unavailable: cannot read {symbol.path}; the index may be stale]"
    lines = text.splitlines()
    start = span.start.line
    end = min(span.end.line + 1, len(lines), start + max_lines)
    if start >= len(lines):
        return f"[body unavailable: {symbol.path} is shorter than the index expects]"
    body = "\n".join(lines[start:end])
    if span.end.line + 1 > end:
        body += f"\n... {span.end.line + 1 - end} more line(s)"
    return body


def find_references(
    store: IndexStore,
    symbol_id: str,
    *,
    min_confidence: float = 0.0,
    limit: int = 50,
    cursor: str | None = None,
    budget: int = DEFAULT_BUDGET,
    estimator: TokenEstimator | None = None,
) -> str:
    """List where a symbol is used, grouped by file.

    Every reference carries the confidence with which it was resolved.
    Without a compiler some of them are inference, and an agent weighing
    whether to open a file deserves to know which.
    """
    symbol = store.symbol(symbol_id)
    if symbol is None:
        raise ToolError(
            f"no symbol with id {symbol_id!r}; use search_symbols to find its id"
        )
    edges = [edge for edge in store.edges_to(symbol_id) if edge.score >= min_confidence]
    if not edges:
        threshold = f" above confidence {min_confidence}" if min_confidence else ""
        return f"nothing uses {symbol.qualified_name or symbol.name}{threshold}\n"

    edges.sort(key=lambda edge: (edge.site_path or "", edge.site_range or symbol.name_range))
    offset = _decode_cursor(cursor)
    window = edges[offset : offset + limit]
    budgeted = _Budget(budget, estimator or store.estimator())
    budgeted.add(f"{len(edges)} use(s) of {symbol.qualified_name or symbol.name}:")

    shown = 0
    current_file = ""
    sources = store.symbols_by_ids(edge.src_id for edge in window)
    for edge in window:
        path = edge.site_path or "?"
        if path != current_file:
            if not budgeted.add(f"{path}:"):
                break
            current_file = path
        line = (edge.site_range.start.line + 1) if edge.site_range else 0
        source = sources.get(edge.src_id)
        origin = source.qualified_name or source.name if source else "?"
        marker = "" if edge.score >= 0.9 else f"  [{edge.tier.label} {edge.score:.2f}]"
        if not budgeted.add(f"  {line}  {edge.kind.value} from {origin}{marker}"):
            break
        shown += 1
    remaining = len(edges) - offset - shown
    hint = f"pass cursor={offset + shown} for more" if remaining > 0 else ""
    return budgeted.render(max(0, remaining), hint)


def neighbours(
    store: IndexStore,
    symbol_id: str,
    *,
    direction: Literal["out", "in", "both"] = "out",
    depth: int = 1,
    kinds: tuple[str, ...] = (),
    min_confidence: float = 0.0,
    budget: int = DEFAULT_BUDGET,
    estimator: TokenEstimator | None = None,
) -> str:
    """Walk the graph from one symbol, rendered as a tree.

    One hop by default. Two hops of a well-connected symbol is already more
    than an agent can read, and the measured result is that flattening a
    second hop *lowers* localisation accuracy unless it is summarised
    first. Use this for the question grep cannot answer: what breaks if
    this changes, and what does this depend on.
    """
    if depth < 1:
        raise ToolError("depth must be at least 1")
    if depth > 3:
        raise ToolError("depth above 3 returns more than an agent can read; use 1 or 2")
    root = store.symbol(symbol_id)
    if root is None:
        raise ToolError(
            f"no symbol with id {symbol_id!r}; use search_symbols to find its id"
        )
    wanted = _edge_kinds(kinds)

    budgeted = _Budget(budget, estimator or store.estimator())
    arrow = {"out": "uses", "in": "used by", "both": "connected to"}[direction]
    budgeted.add(f"{arrow}, from {root.qualified_name or root.name} ({_location(root)}):")

    seen = {symbol_id}
    queue: deque[tuple[str, int]] = deque([(symbol_id, 0)])
    truncated = 0
    while queue:
        current, level = queue.popleft()
        if level >= depth:
            continue
        steps = [
            (edge, other_id)
            for edge, other_id in _step(store, current, direction)
            if (wanted is None or edge.kind in wanted)
            and edge.score >= min_confidence
            and other_id not in seen
        ]
        others = store.symbols_by_ids(other_id for _, other_id in steps)
        for edge, other_id in steps:
            if other_id in seen:
                continue
            other = others.get(other_id)
            if other is None:
                continue
            seen.add(other_id)
            marker = "" if edge.score >= 0.9 else f"  [{edge.tier.label} {edge.score:.2f}]"
            entry = (
                f"{'  ' * (level + 1)}{edge.kind.value} {other.qualified_name or other.name}"
                f"  {_location(other)}{marker}"
            )
            if not budgeted.add(entry):
                truncated += 1
                continue
            queue.append((other_id, level + 1))
    if len(seen) == 1:
        # `arrow` reads correctly in the header but not in a negation:
        # "nothing uses X" answers the opposite question from the one
        # asked when the direction is `out`.
        subject = root.qualified_name or root.name
        return {
            "out": f"{subject} uses nothing the index resolved\n",
            "in": f"nothing uses {subject}\n",
            "both": f"{subject} is connected to nothing the index resolved\n",
        }[direction]
    return budgeted.render(truncated, "narrow with kinds or min_confidence")


def _edge_kinds(kinds: tuple[str, ...]) -> set[EdgeKind] | None:
    """Parse a kind filter, refusing an unknown one with the valid list.

    `EdgeKind("bogus")` raises `ValueError`, which the adapter would turn
    into "Error executing tool", a dead end. Naming the valid kinds is a
    next step.
    """
    if not kinds:
        return None
    valid = {kind.value: kind for kind in EdgeKind}
    unknown = [kind for kind in kinds if kind not in valid]
    if unknown:
        raise ToolError(
            f"unknown edge kind(s) {', '.join(unknown)}; "
            f"kinds are {', '.join(sorted(valid))}"
        )
    return {valid[kind] for kind in kinds}


def _step(
    store: IndexStore, symbol_id: str, direction: str
) -> list[tuple[Edge, str]]:
    """One hop from a symbol, in whichever directions were asked for."""
    steps: list[tuple[Edge, str]] = []
    if direction in ("out", "both"):
        steps.extend((edge, edge.dst_id) for edge in store.edges_from(symbol_id))
    if direction in ("in", "both"):
        steps.extend((edge, edge.src_id) for edge in store.edges_to(symbol_id))
    return steps


def file_outline(
    store: IndexStore,
    path: str,
    *,
    budget: int = DEFAULT_BUDGET,
    estimator: TokenEstimator | None = None,
) -> str:
    """List what one file defines, in source order and nested.

    The cheapest way to decide whether a file is worth opening. A file of
    two thousand lines outlines to twenty, and the outline says which
    twenty lines to read.
    """
    cleaned = path.replace("\\", "/").lstrip("./")
    symbols = [s for s in store.symbols(path=cleaned) if not s.synthetic and not s.local]
    if not symbols:
        known = store.languages()
        if cleaned not in known:
            raise ToolError(
                f"{cleaned!r} is not in the index; check the path, or it may be a "
                f"language this index does not parse"
            )
        return f"{cleaned} defines nothing the index records\n"

    symbols.sort(key=lambda symbol: symbol.name_range)
    by_id = {symbol.id: symbol for symbol in symbols}
    budgeted = _Budget(budget, estimator or store.estimator())
    budgeted.add(f"{cleaned}:")
    shown = 0
    for symbol in symbols:
        depth = 0
        container = symbol.container_id
        seen: set[str] = set()
        while container and container in by_id and container not in seen:
            seen.add(container)
            depth += 1
            container = by_id[container].container_id
        line = symbol.name_range.start.line + 1
        body = symbol.signature or f"{symbol.kind.value} {symbol.name}"
        if not budgeted.add(f"{'  ' * depth}{line:>5}  {body}"):
            break
        shown += 1
    remaining = len(symbols) - shown
    return budgeted.render(remaining, "raise the budget to see the rest")


def repo_map(
    store: IndexStore,
    *,
    focus: tuple[str, ...] = (),
    budget: int = MAP_BUDGET,
    estimator: TokenEstimator | None = None,
    cache: RankCache | None = None,
) -> str:
    """Sketch what the repository is built around, within a token budget.

    Start here when the task names no file. With ``focus`` set to the files
    being worked on, the same budget is spent on what those files reach
    instead of on what is globally central.

    ``cache`` keeps the graph between calls. Without one, every call loads
    and ranks the whole index; a server passes the one it holds.
    """
    cache = cache or RankCache()
    snapshot = cache.snapshot(store)
    if not snapshot.symbols:
        return "the index is empty; run an index first\n"
    focus_paths = {item.replace("\\", "/").lstrip("./") for item in focus}
    unknown = focus_paths - set(store.languages())
    ranked = cache.ranking(store, focus_paths=focus_paths)
    rendered = render_map(
        ranked, MapOptions(budget=budget), estimator=estimator or store.estimator()
    )
    header = f"{rendered.included} of {rendered.total} symbols, {rendered.files} files"
    if unknown:
        # Silently ignoring an unrecognised focus would return a global map
        # that looks like an answer to the question actually asked.
        header += f"; focus not in the index: {', '.join(sorted(unknown))}"
    return f"{header}\n\n{rendered.text}"


def index_status(store: IndexStore) -> str:
    """Say what the index covers and how far it can be trusted.

    Worth calling first. An index built before the last twenty commits
    answers confidently about code that no longer exists, and nothing else
    in this tool set would reveal that.
    """
    counts = store.counts()
    lines = [
        f"root:      {store.get_meta('project_root') or 'unknown'}",
        f"producer:  {store.get_meta('producer') or 'unknown'}",
        f"files:     {counts['files']}",
        f"symbols:   {counts['symbols']}",
        f"edges:     {counts['edges']}",
    ]
    constant = store.chars_per_token()
    if constant:
        model = store.calibrated_model() or "an unnamed model"
        lines.append(f"tokens:    calibrated for {model}, {constant:.2f} chars per token")
    else:
        lines.append(
            "tokens:    estimated, not calibrated; run `repoatlas calibrate` "
            "with the model that will read the answers"
        )
    languages: dict[str, int] = {}
    for language in store.languages().values():
        languages[language] = languages.get(language, 0) + 1
    if languages:
        summary = ", ".join(
            f"{name} {count}" for name, count in sorted(languages.items())
        )
        lines.append(f"languages: {summary}")
    # Size last, because it is the one line that changes on every re-index
    # and a prefix that stays byte-identical is what the agent's prompt
    # cache needs.
    lines.append(f"size:      {store.size_bytes() / 1024:.0f} KiB")
    return "\n".join(lines) + "\n"
