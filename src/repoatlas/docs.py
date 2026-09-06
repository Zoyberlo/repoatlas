"""Check the claims a knowledge base makes about where code lives.

A hand-written or model-written guide is only useful while it is true. It
says the row order is decided in `FinanceReportService.php:1898-1933`, the
file gets refactored, and the line moves — and nothing announces it. The
next agent reads the row, opens those lines, finds something unrelated,
and either wastes the turn or, worse, believes it.

That failure has a measured shape in this project. A wrong answer costs
more than a missing one: an index that confidently resolved
`AdController/store` to `LeadController/store` — a different class — was
worse than one that said it did not know, because the reader stops
looking. A stale documentation row is the same error with a slower fuse.

So this reads the claims out of markdown and asks the index whether they
still hold. The index is the right thing to ask: against compiler-backed
oracles it places definitions at 1.000 on three production repositories,
which is a good deal more certain than the prose it is checking.

What it deliberately does not do is guess. A claim it cannot resolve to
exactly one indexed file is reported as unresolved rather than matched to
the nearest thing, because a checker that invents a match teaches its
reader to ignore it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import IndexStore

__all__ = ["Claim", "DocCheck", "check_documents", "iter_claims"]

# `path/to/File.php`, `File.php:120`, `File.php:120-133`. Only inside
# backticks: prose mentioning a file in passing is not a claim about a
# location, and treating it as one would bury the real findings.
_CLAIM = re.compile(
    r"`([A-Za-z0-9_./+-]+\.(?:php|vue|js|jsx|ts|tsx|mjs|py|blade\.php))"
    r"(?::(\d+)(?:-(\d+))?)?`"
)

_STATUS_ORDER = ("stale", "ambiguous", "unindexed", "ok")


@dataclass(frozen=True, slots=True)
class Claim:
    """One `file` or `file:line` a document asserts."""

    document: str
    line: int
    text: str
    path: str
    start: int | None = None
    end: int | None = None

    @property
    def has_lines(self) -> bool:
        return self.start is not None

    def __str__(self) -> str:
        return f"{self.document}:{self.line}  `{self.text}`"


@dataclass(slots=True)
class DocCheck:
    """What the index had to say about a document's claims."""

    claims: int = 0
    ok: int = 0
    unindexed: list[tuple[Claim, str]] = field(default_factory=list)
    ambiguous: list[tuple[Claim, str]] = field(default_factory=list)
    stale: list[tuple[Claim, str]] = field(default_factory=list)

    @property
    def problems(self) -> int:
        return len(self.unindexed) + len(self.stale)

    def as_dict(self) -> dict[str, Any]:
        return {
            "claims": self.claims,
            "ok": self.ok,
            "unindexed": [[str(c), why] for c, why in self.unindexed],
            "ambiguous": [[str(c), why] for c, why in self.ambiguous],
            "stale": [[str(c), why] for c, why in self.stale],
        }


def iter_claims(documents: Iterable[Path], root: Path | None = None) -> Iterator[Claim]:
    """Every location claim in these markdown files, in reading order."""
    for document in documents:
        try:
            text = document.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        label = str(document.relative_to(root)) if root else str(document)
        for number, line in enumerate(text.splitlines(), start=1):
            for match in _CLAIM.finditer(line):
                path, start, end = match.group(1), match.group(2), match.group(3)
                yield Claim(
                    document=label.replace("\\", "/"),
                    line=number,
                    text=match.group(0).strip("`"),
                    path=path,
                    start=int(start) if start else None,
                    end=int(end) if end else (int(start) if start else None),
                )


def check_documents(
    store: IndexStore, documents: Iterable[Path], *, root: Path | None = None
) -> DocCheck:
    """Ask the index whether each claim still holds."""
    indexed = list(store.languages())
    by_suffix: dict[str, list[str]] = {}
    for path in indexed:
        by_suffix.setdefault(path.rsplit("/", 1)[-1], []).append(path)

    result = DocCheck()
    for claim in iter_claims(documents, root):
        result.claims += 1
        matches = _resolve(claim.path, indexed, by_suffix)
        if not matches:
            result.unindexed.append((claim, "no indexed file matches"))
            continue
        if len(matches) > 1:
            # Not a failure: a bare `Cell.php` in a repository with three
            # of them is imprecise prose, not a false claim.
            result.ambiguous.append(
                (claim, f"{len(matches)} files match: {', '.join(matches[:3])}")
            )
            continue
        path = matches[0]
        if not claim.has_lines:
            result.ok += 1
            continue
        why = _check_lines(store, path, claim)
        if why:
            result.stale.append((claim, why))
        else:
            result.ok += 1
    return result


def _resolve(claimed: str, indexed: list[str], by_suffix: dict[str, list[str]]) -> list[str]:
    """The indexed files a claim could mean, exactly.

    A full path wins outright. A partial one — `FsReport/ImagePlaceholder.php`
    — matches on a path boundary so `MyFsReport/...` does not count. A bare
    file name falls back to the basename index.
    """
    if claimed in indexed:
        return [claimed]
    if "/" in claimed:
        tail = f"/{claimed}"
        return [path for path in indexed if path.endswith(tail)]
    return list(by_suffix.get(claimed, ()))


def _file_length(store: IndexStore, path: str) -> int | None:
    """How many lines the file really has, when it is on this machine."""
    root = store.get_meta("project_root")
    if not root:
        return None
    try:
        return len((Path(root) / path).read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return None


def _check_lines(store: IndexStore, path: str, claim: Claim) -> str:
    """Why a line claim no longer holds, or empty if it does."""
    symbols = [s for s in store.symbols(path=path) if not s.synthetic]
    if not symbols:
        return ""  # a file with nothing indexed cannot contradict a line
    start = claim.start or 0

    # Where the file actually ends. The last indexed symbol is not that: a
    # tail of comments, imports or plain statements carries no symbol, and
    # reading the symbol bound as the file's length reported a claim at
    # line 9 of a twelve-line file as past the end. A false alarm here is
    # expensive — it is what teaches a reader to skip the whole report.
    length = _file_length(store, path)
    if length is not None:
        if start > length:
            return f"{path} has {length} line(s)"
    else:
        last = max(
            (s.full_range.end.line if s.full_range else s.name_range.end.line)
            for s in symbols
        )
        # No working tree to measure against, so only a claim far past the
        # last declaration is worth reporting, and it says what it means.
        if start > last + 50:
            return f"nothing is indexed in {path} past line {last + 1}"
    # A range that names a symbol is the useful case: if nothing is
    # declared inside it any more, the row is pointing at moved code.
    end = claim.end or start
    if end - start >= 2:
        inside = [
            s
            for s in symbols
            if start - 1 <= s.name_range.start.line <= end - 1
        ]
        if not inside:
            covering = store.symbol_at(path, start)
            if covering is None:
                return f"nothing is declared in {path}:{start}-{end}"
    return ""


def render(result: DocCheck) -> str:
    """The report, worst first."""
    lines = [
        f"claims:     {result.claims}",
        f"  hold:     {result.ok}",
        f"  stale:    {len(result.stale)}",
        f"  unindexed:{len(result.unindexed)}",
        f"  imprecise:{len(result.ambiguous)}",
    ]
    for label in _STATUS_ORDER:
        if label == "ok":
            continue
        rows = getattr(result, "unindexed" if label == "unindexed" else label)
        if not rows:
            continue
        lines.append("")
        lines.append(f"{label}:")
        for claim, why in rows[:40]:
            lines.append(f"  {claim} — {why}")
        if len(rows) > 40:
            lines.append(f"  ... {len(rows) - 40} more")
    return "\n".join(lines) + "\n"
