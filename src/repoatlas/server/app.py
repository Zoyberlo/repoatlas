"""The MCP server: an adapter over :mod:`repoatlas.server.tools`.

Everything here is protocol. The answers live next door, so they can be
tested without a transport and served by something else later.

Two things about the tool surface are deliberate.

There are seven tools, not thirty. Every schema is loaded into the model's
context on every turn, and a large surface can spend a third of the window
before any work happens; one team cut seventeen tools to eleven and saw
usage improve. Seven is what this index can actually answer.

Each description says *when* to reach for the tool, not only what it
returns. Agents given a graph tool never called it in fifty-eight percent
of trials, defaulting to grep, so a description that only describes is a
tool that goes unused.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from .. import __version__
from ..store import IndexStore, update_store
from . import tools

__all__ = ["build_server", "serve"]

_INSTRUCTIONS = """\
RepoAtlas indexes this repository's symbols and the edges between them.

Reach for it when a plain text search would be slow or ambiguous:

- `repo_map` when the task names no file and you need to know what the
  project is built around. Pass `focus` once you know which files matter.
- `search_symbols` instead of grepping for a definition. It matches
  substrings and tells you how many places use each hit.
- `find_references` to answer "what breaks if I change this", which grep
  answers with every string that merely looks similar.
- `neighbours` to follow the graph one hop: what a symbol depends on, or
  what depends on it.
- `file_outline` before opening a long file, to decide which lines to read.

Grep is still better for text that is not a symbol: a log message, a
configuration value, a comment. Use both.

Every location is `path:line`, ready to pass to a file reader. Edges
resolved without a compiler carry a confidence, shown when it is below
0.9; treat those as leads rather than facts."""


def build_server(store: IndexStore, *, name: str = "repoatlas") -> Any:
    """Register the tool surface against an open store.

    Returns the SDK's server object. Typed loosely on purpose: importing
    the SDK for a type would make the whole package depend on it, and the
    index is useful without ever being served.
    """
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError as SdkToolError
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise RuntimeError(
            "serving needs the extra: pip install 'repoatlas[serve]'"
        ) from exc

    server = MCPServer(
        name=name,
        version=__version__,
        instructions=_INSTRUCTIONS,
    )
    # Every tool here only reads. Saying so lets a client skip the
    # confirmation it would otherwise ask for before each call.
    read_only = ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    )

    def _answer(produce: Callable[[], str]) -> str:
        """Run a tool, translating its refusals into ones the agent sees.

        The SDK keeps the message of its own ToolError and replaces every
        other exception with "Error executing tool <name>". Since the tools
        deliberately know nothing about the SDK, the translation belongs
        here, and without it every actionable message would be swallowed at
        the boundary.
        """
        try:
            return produce()
        except tools.ToolError as exc:
            raise SdkToolError(str(exc)) from None

    @server.tool(annotations=read_only)
    def repo_map(focus: list[str] | None = None, budget: int = 2000) -> str:
        """Sketch what this repository is built around, within a token budget.

        Start here when the task names no file. Once you know which files
        matter, pass them as `focus` and the same budget is spent on what
        those files reach rather than on the project as a whole.

        focus: repository-relative paths to rank around, e.g. ["src/app.ts"].
        budget: target size in tokens; the map is trimmed to fit.
        """
        return _answer(
            lambda: tools.repo_map(store, focus=tuple(focus or ()), budget=budget)
        )

    @server.tool(annotations=read_only)
    def search_symbols(
        query: str,
        kinds: list[str] | None = None,
        limit: int = 20,
        cursor: str | None = None,
        detail: Literal["concise", "detailed"] = "concise",
    ) -> str:
        """Find definitions whose name contains `query`.

        Prefer this to grepping for a definition: it matches substrings, so
        "Resolver" finds "ModuleResolver", and each hit says how many places
        use it, which grep cannot tell you.

        query: part of a symbol name, case-insensitive.
        kinds: restrict to e.g. ["class", "function", "method"].
        cursor: from a previous result, to page through more.
        """
        return _answer(
            lambda: tools.search_symbols(
                store,
                query,
                kinds=tuple(kinds or ()),
                limit=limit,
                cursor=cursor,
                detail=detail,
            )
        )

    @server.tool(annotations=read_only)
    def get_symbol(
        symbol_id: str,
        detail: Literal["concise", "detailed"] = "detailed",
        include_body: bool = False,
    ) -> str:
        """Describe one symbol: location, container, and what uses it.

        Use the id from `search_symbols`. Leave `include_body` off unless
        you actually need the source; a signature and a location are usually
        enough to decide the next step, and the body is the most expensive
        thing this index can return.
        """
        return _answer(
            lambda: tools.get_symbol(
                store, symbol_id, detail=detail, include_body=include_body
            )
        )

    @server.tool(annotations=read_only)
    def find_references(
        symbol_id: str,
        min_confidence: float = 0.0,
        limit: int = 50,
        cursor: str | None = None,
    ) -> str:
        """List every place a symbol is used, grouped by file.

        This is the "what breaks if I change this" question. Grep answers it
        with every string that merely looks similar; this answers with
        resolved references, each carrying how confidently it was resolved.

        min_confidence: raise to 0.9 to see only edges resolved from an
        import or from the same file, dropping the inferred ones.
        """
        return _answer(
            lambda: tools.find_references(
                store,
                symbol_id,
                min_confidence=min_confidence,
                limit=limit,
                cursor=cursor,
            )
        )

    @server.tool(annotations=read_only)
    def neighbours(
        symbol_id: str,
        direction: Literal["out", "in", "both"] = "out",
        depth: int = 1,
        kinds: list[str] | None = None,
        min_confidence: float = 0.0,
    ) -> str:
        """Walk the graph from one symbol, one hop by default.

        `out` is what this symbol depends on; `in` is what depends on it.
        Keep `depth` at 1 or 2: flattening a third hop returns more than is
        readable and has been measured to *lower* accuracy rather than
        raise it.

        kinds: restrict to e.g. ["calls", "imports", "inherits"].
        """
        return _answer(
            lambda: tools.neighbours(
                store,
                symbol_id,
                direction=direction,
                depth=depth,
                kinds=tuple(kinds or ()),
                min_confidence=min_confidence,
            )
        )

    @server.tool(annotations=read_only)
    def file_outline(path: str) -> str:
        """List what one file defines, in source order and nested.

        The cheapest way to decide whether a file is worth opening, and
        which lines of it to read. A two-thousand-line file outlines to
        about twenty.
        """
        return _answer(lambda: tools.file_outline(store, path))

    @server.tool(annotations=read_only)
    def index_status() -> str:
        """Report what the index covers and how large it is.

        Worth one call at the start. An index built before recent commits
        will answer confidently about code that no longer exists, and
        nothing else here would reveal that.
        """
        return _answer(lambda: tools.index_status(store))

    # Referenced so linters see them as used; the decorator has already
    # registered each one with the server.
    del (
        repo_map,
        search_symbols,
        get_symbol,
        find_references,
        neighbours,
        file_outline,
        index_status,
    )
    return server


def serve(
    root: Path | str,
    store_path: Path | str,
    *,
    refresh: bool = True,
    use_git: bool = True,
) -> None:
    """Index ``root`` if asked, then serve the store over stdio.

    Refreshing on startup is the default because a stale index is the one
    failure an agent cannot detect from the answers: every reply looks
    well-formed and describes code that has moved.
    """
    store = IndexStore(store_path)
    try:
        if refresh:
            update_store(root, store, use_git=use_git)
        server = build_server(store)
        server.run("stdio")
    finally:
        store.close()
