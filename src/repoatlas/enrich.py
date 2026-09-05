"""Fold a type engine's answers into an index that could not infer them.

The cascade resolves a member through its receiver, and on a Laravel
application the receiver usually has no declared type: `$ad->client()`
says nothing a parser can follow, and `$ad->balance_due` names a column
that exists only once a row is fetched. Measured on one application's
backend, the index resolves 9.9% of member sites where PHPStan with
larastan resolves 63.1%.

Most of that gap is not navigable — 86% of what PHPStan resolves points
into `vendor`, and an edge into a file nobody will open is not
navigation. What is left is 583 sites whose target is a declaration in
the repository itself, against the 961 the index already had.

So this reads a dump, keeps only the sites whose target is something the
index holds, and adds those edges. Nothing is overwritten: a site the
cascade already resolved is left exactly as it was, because the point is
to measure what the type engine adds rather than to have it quietly
disagree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import Edge, EdgeKind, ResolutionTier, SourceRange, Symbol
from .oracle.phpstan import _EDGE_KINDS, PhpStanError, _Offsets
from .store import IndexStore

__all__ = ["EnrichmentResult", "enrich_from_phpstan"]


@dataclass(slots=True)
class EnrichmentResult:
    """What folding a dump into an index changed, and what it could not."""

    sites: int = 0
    resolved: int = 0
    already_known: int = 0
    """Sites the cascade had already settled; left untouched."""

    outside_index: int = 0
    """Resolved, but the target is in vendor or otherwise unindexed."""

    unplaceable: int = 0
    """The target is in the repository but no symbol sits where it should."""

    added: int = 0
    magic_added: int = 0
    """Added edges whose target is a member nothing declares, an Eloquent
    column reached through the model that owns it."""

    by_file: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sites": self.sites,
            "resolved": self.resolved,
            "already_known": self.already_known,
            "outside_index": self.outside_index,
            "unplaceable": self.unplaceable,
            "added": self.added,
            "magic_added": self.magic_added,
            "top_files": dict(
                sorted(self.by_file.items(), key=lambda item: -item[1])[:15]
            ),
        }


def enrich_from_phpstan(
    store: IndexStore,
    dump: Path | str,
    *,
    phpstan_root: Path | str,
    prefix: str = "",
    dry_run: bool = False,
) -> EnrichmentResult:
    """Add the edges a type engine resolved and the cascade could not.

    ``phpstan_root`` is the directory PHPStan analysed, which is what its
    absolute paths are relative to; ``prefix`` is where that directory
    sits inside the indexed repository, so a backend analysed on its own
    still lands on ``backend/...`` paths. Both are needed because the
    analysis is often run over a copy, and guessing between the two roots
    silently produces an enrichment that matches nothing.
    """
    root = Path(phpstan_root).resolve()
    cleaned = prefix.strip("/")
    records = _records(Path(dump))

    def relative(raw: str) -> str | None:
        if not raw:
            return None
        try:
            inner = Path(raw).resolve().relative_to(root).as_posix()
        except ValueError:
            return None
        return f"{cleaned}/{inner}" if cleaned else inner

    offsets: dict[str, _Offsets] = {}

    def column(inner: str, offset: int, line: int) -> int:
        """The byte column a dump offset falls on, read off the file.

        Coarser matching was tried first and cost real edges: skipping a
        site because *some* edge already sat on its line threw away the
        second member access on a chained line, which on this application
        is most of them.
        """
        if inner not in offsets:
            try:
                offsets[inner] = _Offsets((root / inner).read_bytes())
            except OSError:
                offsets[inner] = _Offsets(b"")
        found, character = offsets[inner].at(offset)
        return character if found == line else 0

    # Declarations first: they are what a resolved site has to land on, and
    # the dump gives them with the same byte offsets as the sites.
    declarations: dict[str, tuple[str, int]] = {}
    for record in records:
        if record.get("kind") != "def":
            continue
        path = relative(str(record.get("path", "")))
        fqn = str(record.get("fqn", ""))
        if path is None or not fqn:
            continue
        declarations.setdefault(fqn, (path, int(record.get("line", 0))))

    known = {
        (edge.site_path, edge.site_range.start.line, edge.site_range.start.character)
        for edge in store.edges()
        if edge.site_path and edge.site_range
    }
    result = EnrichmentResult()
    edges: list[Edge] = []
    seen: set[tuple[str, int, int, str]] = set()
    for record in records:
        if record.get("kind") == "def":
            continue
        raw = str(record.get("path", ""))
        path = relative(raw)
        if path is None:
            continue
        result.sites += 1
        if not record.get("resolved"):
            continue
        result.resolved += 1
        line = int(record.get("line", 0))
        inner = Path(raw).resolve().relative_to(root).as_posix()
        start = record.get("start")
        character = column(inner, start, line) if isinstance(start, int) else 0
        if (path, line, character) in known:
            result.already_known += 1
            continue
        target = _target(record, declarations)
        if target is None:
            result.outside_index += 1
            continue
        destination = _at(store, *target)
        if destination is None:
            result.unplaceable += 1
            continue
        source = _at(store, path, line)
        if source is None or source.id == destination.id:
            result.unplaceable += 1
            continue
        key = (path, line, character, destination.id)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            Edge(
                src_id=source.id,
                dst_id=destination.id,
                kind=_EDGE_KINDS.get(str(record.get("kind")), EdgeKind.REFERENCES),
                tier=ResolutionTier.TYPE_ENGINE,
                site_path=path,
                site_range=SourceRange.of(line, character, line, character),
            )
        )
        result.added += 1
        if record.get("magic"):
            result.magic_added += 1
        result.by_file[path] = result.by_file.get(path, 0) + 1
    if edges and not dry_run:
        with store.transaction():
            store.add_edges(edges)
        store.bump_generation()
    return result


def _target(
    record: dict[str, Any], declarations: dict[str, tuple[str, int]]
) -> tuple[str, int] | None:
    owner = str(record.get("class", ""))
    if not owner:
        return None
    if record.get("kind") == "new":
        return declarations.get(owner)
    placed = declarations.get(f"{owner}::{record.get('name', '')}")
    if placed is not None:
        return placed
    # An Eloquent column: larastan named the model, and nothing declares
    # the member. The model itself is where an agent needs to go, and it
    # is a better answer than none.
    if record.get("magic"):
        return declarations.get(owner)
    return None


def _at(store: IndexStore, path: str, line: int) -> Symbol | None:
    """The innermost symbol around a zero-based line.

    Both ends of an edge want this. A use site inside a method addresses
    the method, which is the source; a declaration line addresses the
    thing declared, which is the target.
    """
    return store.symbol_at(path, line + 1)


def _records(dump: Path) -> list[dict[str, Any]]:
    try:
        text = dump.read_text(encoding="utf-8")
    except OSError as exc:
        raise PhpStanError(f"cannot read {dump}: {exc}") from None
    records: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PhpStanError(f"{dump}:{number} is not JSON: {exc}") from None
        if isinstance(record, dict):
            records.append(record)
    return records
