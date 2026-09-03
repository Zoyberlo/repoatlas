"""The MCP server: the index, exposed to an agent."""

from __future__ import annotations

from .tools import (
    Detail,
    ToolError,
    file_outline,
    find_references,
    get_symbol,
    index_status,
    neighbours,
    repo_map,
    search_symbols,
)

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
