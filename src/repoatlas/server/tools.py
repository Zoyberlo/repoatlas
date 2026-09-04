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

import re
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
    "name_path",
    "neighbours",
    "repo_map",
    "resolve_symbol",
    "search_symbols",
]

Detail = Literal["concise", "detailed", "skeleton"]

# Defaults sit well under the 25,000-token cut so a result is never
# truncated by the client, where the agent cannot see what went missing.
DEFAULT_BUDGET = 4000
MAP_BUDGET = 2000
MAP_BUDGET_UNSTEERED = 4000
"""The map budget when nothing steers it.

A map is worth most exactly when the agent has nothing else to go on,
which is the first call of a session, before any file is known. aider
multiplies its map budget by eight in that case. Doubling is what fits
here: Claude Code warns at ten thousand tokens per tool result, and a map
that trips the warning is one the agent learns not to ask for.
"""


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
    # The name is printed as a name path rather than a dotted qualified
    # name because every tool here accepts a name path back. Printing the
    # form that can be pasted is the cheapest way to teach it.
    headline = f"{_location(symbol)}  {symbol.kind.value} {name_path(symbol)}{suffix}"
    if detail == "concise":
        return headline
    signature = symbol.signature or ""
    parts = [headline]
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


_SEPARATORS = re.compile(r"::|[./\\]")
"""What separates one segment of a name path from the next.

Four spellings of the same thing, because an agent will paste whichever
its language uses or whichever an earlier answer printed: `Ad.save` from
an outline, `Ad::save` from PHP, `Ad/save` from habit.
"""

_INDEXED = re.compile(r"^(?P<body>.+?)\[(?P<index>\d+)\]$")
_CANDIDATE_LIMIT = 10


def _split_scope(text: str) -> tuple[str, str]:
    """Separate a leading file or directory from the rest, `app:Ad/save`.

    The colon has to be told apart from PHP's `::`, which is a segment
    separator and not a scope.
    """
    for index, character in enumerate(text):
        if character != ":":
            continue
        if text[index + 1 : index + 2] == ":" or text[index - 1 : index] == ":":
            continue
        return text[:index], text[index + 1 :]
    return "", text


def _segments(text: str) -> list[str]:
    return [part for part in _SEPARATORS.split(text) if part]


def name_path(symbol: Symbol) -> str:
    """The containers a symbol sits in and its own name, `Ad/save`.

    Serena's spelling, and the one printed in the ambiguity messages,
    because slashes read as a path and dots read as an expression.
    """
    return "/".join(_chain(symbol))


def _chain(symbol: Symbol) -> list[str]:
    """A symbol's containers and its own name, outermost first."""
    qualified = symbol.qualified_name or symbol.name
    if qualified == symbol.name:
        return [symbol.name]
    if qualified.endswith(symbol.name):
        prefix = qualified[: -len(symbol.name)]
        if prefix.endswith("."):
            # The name itself may hold a separator; only what precedes it
            # is a chain of containers.
            return [*_segments(prefix[:-1]), symbol.name]
    return _segments(qualified) or [symbol.name]


def _matches(symbol: Symbol, wanted: list[str], *, absolute: bool) -> bool:
    chain = _chain(symbol)
    if absolute:
        return chain == wanted
    return len(wanted) <= len(chain) and chain[len(chain) - len(wanted) :] == wanted


def _within(symbol: Symbol, scope: str) -> bool:
    cleaned = scope.strip("/")
    return symbol.path == cleaned or f"/{symbol.path}/".find(f"/{cleaned}/") >= 0


def _members_of(store: IndexStore, container: Symbol) -> list[Symbol]:
    """What a container declares directly, in source order."""
    return sorted(
        (
            candidate
            for candidate in store.symbols(path=container.path)
            if candidate.container_id == container.id and not candidate.synthetic
        ),
        key=lambda item: item.name_range,
    )


def _no_such_member(
    store: IndexStore, reference: str, wanted: list[str], scope: str
) -> ToolError:
    """`AdController has no store; it has storeData, storeOriginalFile, ...`

    The useful answer to a near miss. An agent that guessed a member name
    wrong wants that container's members, not a search of the whole
    repository, which was what a first version gave it: asking for
    `AdController/store` returned `LeadController/store`, a different
    class, ranked first.
    """
    containers = [
        symbol
        for symbol in store.symbols_named(wanted[-2], path_scope=scope or None, limit=4)
        if symbol.kind.is_type_like
    ]
    lines = [f"{'/'.join(wanted)} does not exist."]
    for container in containers[:2]:
        members = [member.name for member in _members_of(store, container)]
        shown = ", ".join(members[:12]) or "nothing"
        more = f", and {len(members) - 12} more" if len(members) > 12 else ""
        lines.append(f"  {name_path(container)} ({container.path}) declares: {shown}{more}")
    if not containers:
        lines.append(
            f"  nothing named {wanted[-2]!r} contains anything; "
            f"search_symbols({wanted[-1]!r}) lists what does"
        )
    return ToolError("\n".join(lines))


def _ambiguous(
    store: IndexStore, reference: str, found: list[Symbol], *, verb: str = "matches"
) -> ToolError:
    """The candidates, addressable, instead of an instruction to go search.

    Sixty-five percent of the symbols in a real application share a name
    with another, so this is the common case rather than the awkward one.
    Refusing it costs a turn; answering it costs a few lines.
    """
    counts = store.reference_counts([symbol.id for symbol in found[:_CANDIDATE_LIMIT]])
    lines = [f"{reference!r} {verb} {len(found)} symbols; pass one of:"]
    for position, symbol in enumerate(found[:_CANDIDATE_LIMIT], start=1):
        uses = counts.get(symbol.id, 0)
        tail = f"  ({uses} use{'s' if uses != 1 else ''})" if uses else ""
        lines.append(
            f"  [{position}] {_location(symbol)}  {symbol.kind.value} "
            f"{name_path(symbol)}{tail}"
        )
    if len(found) > _CANDIDATE_LIMIT:
        lines.append(f"  ... {len(found) - _CANDIDATE_LIMIT} more")
    lines.append(
        f"Address one by position, {reference}[1], by path, "
        f"{found[0].path}:{name_path(found[0])}, or by adding a container."
    )
    return ToolError("\n".join(lines))


def resolve_symbol(store: IndexStore, reference: str) -> Symbol:
    """Turn whatever the agent is holding into one symbol.

    Four forms resolve, so that nothing printed by an earlier answer has to
    be converted before it can be used again:

    - an id as the index stores it, ``app/Models/Ad.php#Ad.save``;
    - a name path, ``Ad/save``, or the ``Ad.save`` an outline prints, or
      PHP's ``Ad::save``, matched as a suffix of the containers a symbol
      sits in unless it starts with a slash, which anchors it to the top
      level of its file;
    - a location, ``app/Models/Ad.php:42``, which every answer prints, and
      which addresses the innermost thing declared around that line;
    - either of those narrowed by a file or directory,
      ``app/Models:Ad/save``.

    An ambiguous name is answered with its candidates rather than refused,
    each addressable by position. That is the whole point: a measured run
    spent four and a half calls per session turning a name it already knew
    into an id it could pass, because the id was the only thing accepted
    and nothing printed it.
    """
    text = reference.strip()
    if not text:
        raise ToolError("give a symbol: an id, a name path like Ad/save, or path:line")

    # An id is unambiguous and cheap to check, so it goes first and a name
    # that happens to look like one cannot shadow it.
    exact = store.symbol(text)
    if exact is not None:
        return exact

    position: int | None = None
    indexed = _INDEXED.match(text)
    if indexed is not None:
        text = indexed.group("body")
        position = int(indexed.group("index"))
        exact = store.symbol(text)
        if exact is not None and position == 1:
            return exact

    scope, rest = _split_scope(text)
    if scope and rest.isdigit():
        located = store.symbol_at(scope, int(rest))
        if located is None:
            raise ToolError(
                f"nothing is declared around {scope}:{rest}; "
                "file_outline lists what that file defines"
            )
        return located

    absolute = rest.startswith(("/", "\\"))
    wanted = _segments(rest)
    if not wanted:
        raise ToolError(f"{reference!r} names nothing; try a name path like Ad/save")

    found = [
        symbol
        for symbol in store.symbols_named(wanted[-1], path_scope=scope or None)
        if _matches(symbol, wanted, absolute=absolute)
    ]
    if not found and len(wanted) > 1:
        # A named container and a member it does not have. Searching for
        # the member alone would answer with a different container's, which
        # is worse than saying no.
        raise _no_such_member(store, reference, wanted, scope)
    if not found and not absolute:
        # One segment, and nothing is named that exactly. The agent may be
        # holding part of a name, which is what search is for; a single hit
        # needs no narration, since every answer prints where it landed.
        hits = [
            symbol
            for symbol in store.search(wanted[0], limit=_CANDIDATE_LIMIT + 1)
            if not scope or _within(symbol, scope)
        ]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise _ambiguous(store, reference, hits, verb="is part of the name of")
    if not found:
        where = f" under {scope}" if scope else ""
        raise ToolError(
            f"no symbol{where} has the name path {rest!r}; "
            f"search_symbols({wanted[-1]!r}) lists what there is"
        )
    if len(found) == 1:
        return found[0]
    if position is not None:
        if not 1 <= position <= len(found):
            raise _ambiguous(store, reference, found)
        return found[position - 1]
    raise _ambiguous(store, reference, found)


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
    symbol: str,
    *,
    detail: Detail = "detailed",
    include_body: bool = False,
    max_body_lines: int = 80,
    budget: int = DEFAULT_BUDGET,
    estimator: TokenEstimator | None = None,
) -> str:
    """Describe one symbol: where it is, what contains it, what it touches.

    Three levels of detail, because the gap between a signature and a body
    is where most of the tokens go. ``detailed`` is the signature and the
    first line of documentation; ``skeleton`` adds what the symbol
    contains, every nested definition with its line, which for a class is
    the whole shape of it in a dozen lines; ``include_body`` reads the
    source from disk, and is off by default because a body is the most
    expensive thing this index can hand over and the skeleton usually says
    which lines of it to read.

    ``symbol`` is anything :func:`resolve_symbol` understands: an id, a
    name path, or a location.
    """
    found = resolve_symbol(store, symbol)
    lines = [_describe(found, detail="detailed" if detail == "skeleton" else detail)]
    if found.container_id:
        container = store.symbol(found.container_id)
        if container is not None:
            lines.append(f"  in: {container.kind.value} {name_path(container)}")
    if found.full_range is not None and found.full_range != found.name_range:
        span = found.full_range
        lines.append(f"  lines: {span.start.line + 1}-{span.end.line + 1}")

    outgoing = store.out_degree(found.id)
    if outgoing:
        lines.append(f"  uses: {outgoing}")
    # One symbol may reach another by several edges at once, an import and
    # a call among them. The count is of callers, not edges, so five lines
    # are five different places rather than one place repeated, and the
    # store answers it without loading every edge.
    total_callers, sample = store.callers(found.id, limit=5)
    if total_callers:
        lines.append(f"  used by: {total_callers}")
        for source in sample:
            lines.append(f"    {_location(source)}  {name_path(source)}")
        if total_callers > len(sample):
            lines.append(
                f"    ... {total_callers - len(sample)} more; find_references gives all of them"
            )

    if detail == "skeleton":
        lines.extend(_skeleton(store, found, budget, estimator or store.estimator()))

    if include_body:
        body = _read_body(store, found, max_body_lines)
        if body:
            lines.append("")
            lines.append(body)
    return "\n".join(lines) + "\n"


def _skeleton(
    store: IndexStore, symbol: Symbol, budget: int, estimate: TokenEstimator
) -> list[str]:
    """What a symbol contains, as declaration lines, in source order.

    Built from the index rather than by re-parsing: every nested definition
    is already stored with its declaration line and position. A class
    skeleton is its methods; a function's is the definitions nested in it,
    usually none, in which case the body's length is the useful fact.
    """
    inside = {symbol.id}
    members: list[Symbol] = []
    for candidate in sorted(
        store.symbols(path=symbol.path), key=lambda item: item.name_range
    ):
        if candidate.synthetic or candidate.id == symbol.id:
            continue
        if candidate.container_id in inside:
            inside.add(candidate.id)
            members.append(candidate)
    span = symbol.full_range or symbol.name_range
    body_lines = span.end.line - span.start.line + 1
    if not members:
        return [f"  body: {body_lines} line(s); include_body reads them"]
    depth_of = {symbol.id: 0}
    budgeted = _Budget(budget, estimate)
    budgeted.add(f"  contains {len(members)} definition(s) across {body_lines} lines:")
    shown = 0
    for member in members:
        depth = depth_of.get(member.container_id or "", 0) + 1
        depth_of[member.id] = depth
        line = member.name_range.start.line + 1
        text = member.signature or f"{member.kind.value} {member.name}"
        if not budgeted.add(f"  {'  ' * depth}{line:>5}  {text}"):
            break
        shown += 1
    result = budgeted.lines
    if shown < len(members):
        result.append(f"  ... {len(members) - shown} more; file_outline shows the whole file")
    return result


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


def _namesake_note(store: IndexStore, symbol: Symbol) -> str:
    """`; 14 other symbols are also called client`, or nothing.

    Said only when it changes what the reader should do: one namesake is
    a coincidence, several mean a name search cannot answer this question.
    """
    others = store.namesakes(symbol.name)
    if others < 1:
        return ""
    thing = "symbol is" if others == 1 else "symbols are"
    return f"; {others} other {thing} also called {symbol.name}"


def find_references(
    store: IndexStore,
    symbol: str,
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

    The header says how many other symbols answer to the same name, which
    is the one thing a search for that name cannot tell you about itself,
    and the reason this list is not the same as its output.

    ``symbol`` is anything :func:`resolve_symbol` understands: an id, a
    name path, or a location.
    """
    found = resolve_symbol(store, symbol)
    # Containment is structure, not use; the class holding a method is
    # not one of its callers.
    edges = [
        edge
        for edge in store.edges_to(found.id)
        if edge.score >= min_confidence and edge.kind is not EdgeKind.CONTAINS
    ]
    if not edges:
        threshold = f" above confidence {min_confidence}" if min_confidence else ""
        return f"nothing uses {name_path(found)}{threshold}\n"

    edges.sort(key=lambda edge: (edge.site_path or "", edge.site_range or found.name_range))
    offset = _decode_cursor(cursor)
    window = edges[offset : offset + limit]
    budgeted = _Budget(budget, estimator or store.estimator())
    budgeted.add(
        f"{len(edges)} use(s) of {name_path(found)}{_namesake_note(store, found)}:"
    )

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
        origin = name_path(source) if source else "?"
        marker = "" if edge.score >= 0.9 else f"  [{edge.tier.label} {edge.score:.2f}]"
        if not budgeted.add(f"  {line}  {edge.kind.value} from {origin}{marker}"):
            break
        shown += 1
    remaining = len(edges) - offset - shown
    hint = f"pass cursor={offset + shown} for more" if remaining > 0 else ""
    return budgeted.render(max(0, remaining), hint)


def neighbours(
    store: IndexStore,
    symbol: str,
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

    ``symbol`` is anything :func:`resolve_symbol` understands: an id, a
    name path, or a location.
    """
    if depth < 1:
        raise ToolError("depth must be at least 1")
    if depth > 3:
        raise ToolError("depth above 3 returns more than an agent can read; use 1 or 2")
    root = resolve_symbol(store, symbol)
    symbol_id = root.id
    wanted = _edge_kinds(kinds)

    budgeted = _Budget(budget, estimator or store.estimator())
    arrow = {"out": "uses", "in": "used by", "both": "connected to"}[direction]
    budgeted.add(f"{arrow}, from {name_path(root)} ({_location(root)}):")

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
                f"{'  ' * (level + 1)}{edge.kind.value} {name_path(other)}"
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
        subject = name_path(root)
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
    mention: tuple[str, ...] = (),
    budget: int | None = None,
    estimator: TokenEstimator | None = None,
    cache: RankCache | None = None,
) -> str:
    """Sketch what the repository is built around, within a token budget.

    Start here when the task names no file. With ``focus`` set to the files
    being worked on, or ``mention`` set to the names the task talks about,
    the same budget is spent on what those reach instead of on what is
    globally central.

    ``mention`` is the lever a task description gives: "the invoice export"
    names `InvoiceExporter` and `invoice.ts` before any file is open. Each
    word is matched to symbols by name and to files by stem, and the walk
    restarts there. aider does the same with the identifiers in the chat,
    weighting them ten times an ordinary file.

    ``budget`` defaults to more when nothing steers the map, because that is
    when the agent has least else to go on.

    ``cache`` keeps the graph between calls. Without one, every call loads
    and ranks the whole index; a server passes the one it holds.
    """
    cache = cache or RankCache()
    snapshot = cache.snapshot(store)
    if not snapshot.symbols:
        return "the index is empty; run an index first\n"
    focus_paths = {item.replace("\\", "/").lstrip("./") for item in focus}
    unknown = focus_paths - set(store.languages())
    seed_symbols, seed_paths, unmatched = cache.seeds_for(store, mention)
    steered = bool(focus_paths or seed_symbols or seed_paths)
    if budget is None:
        budget = MAP_BUDGET if steered else MAP_BUDGET_UNSTEERED
    ranked = cache.ranking(
        store, focus_paths=focus_paths | seed_paths, focus_symbols=seed_symbols
    )
    rendered = render_map(
        ranked, MapOptions(budget=budget), estimator=estimator or store.estimator()
    )
    header = f"{rendered.included} of {rendered.total} symbols, {rendered.files} files"
    if unknown:
        # Silently ignoring an unrecognised focus would return a global map
        # that looks like an answer to the question actually asked.
        header += f"; focus not in the index: {', '.join(sorted(unknown))}"
    if unmatched:
        header += f"; mentioned but not found: {', '.join(sorted(unmatched))}"
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
