"""Readers for compiler-backed indexes used as ground truth.

An oracle is anything that resolves names with a real compiler front end.
SCIP indexers are the first supported family because one format covers
TypeScript, Python, Java, Kotlin and PHP, and because their output is a file
on disk rather than a live server, which makes evaluation reproducible.

A language server is the other obvious oracle and fits the same interface,
but it has to be driven request by request, so it is a later addition.

PHPStan is the second family, and it is here for a reason the SCIP family
cannot cover: on a Laravel application ``scip-php`` shares this index's
blind spots rather than exposing them, and an oracle that agrees with your
blindness measures agreement rather than accuracy.
"""

from __future__ import annotations

from .phpstan import (
    PhpStanError,
    PhpStanRun,
    read_phpstan,
    run_phpstan,
)
from .scip import (
    ParsedSymbol,
    ScipError,
    SymbolRole,
    cross_check,
    parse_symbol,
    read_scip,
    read_scip_binary,
    read_scip_json,
)

__all__ = [
    "ParsedSymbol",
    "PhpStanError",
    "PhpStanRun",
    "ScipError",
    "SymbolRole",
    "cross_check",
    "parse_symbol",
    "read_phpstan",
    "read_scip",
    "read_scip_binary",
    "read_scip_json",
    "run_phpstan",
]
