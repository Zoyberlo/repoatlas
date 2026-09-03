"""Building a whole-repository snapshot from per-file extractions.

At this stage the snapshot carries symbols and containment only. References
are collected and counted but not yet turned into edges, because resolving a
name to a definition is the next stage and the stage where confidence tiers
come from.

That is deliberate, and it is also measurable: definition precision and
recall against an oracle can be scored today, and they are the floor
everything else rests on. An index that misses a definition makes every edge
that should have targeted it dangling.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from ..model import Edge, EdgeKind, IndexSnapshot, ResolutionTier, SymbolKind
from .extract import FileExtraction, Reference, extract_source
from .languages import LanguageUnavailable
from .walk import SourceFile, WalkStats, iter_source_files

__all__ = ["BuildResult", "LanguageStats", "build_snapshot"]


@dataclass(slots=True)
class LanguageStats:
    """Per-language health, which is the tier-one quality measure.

    The error rate is the number to watch. Published rates across GitHub run
    from 0.2% of files in Go to 53% in C; a language that comes out high here
    needs its grammar questioned before any accuracy number from it is
    believed.
    """

    files: int = 0
    symbols: int = 0
    references: int = 0
    files_with_errors: int = 0
    error_nodes: int = 0

    @property
    def error_rate(self) -> float:
        return self.files_with_errors / self.files if self.files else 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "files": self.files,
            "symbols": self.symbols,
            "references": self.references,
            "files_with_errors": self.files_with_errors,
            "error_nodes": self.error_nodes,
            "error_rate": round(self.error_rate, 4),
        }


@dataclass(slots=True)
class BuildResult:
    """A snapshot plus everything needed to explain how it came out."""

    snapshot: IndexSnapshot
    references: list[tuple[str, Reference]] = field(default_factory=list)
    """Unresolved references, paired with the file they came from.

    Kept beside the snapshot rather than inside it because an unresolved
    reference is not yet a claim about the code, and the model only carries
    claims.
    """

    by_language: dict[str, LanguageStats] = field(default_factory=dict)
    walk: WalkStats = field(default_factory=WalkStats)
    failures: list[tuple[str, str]] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def files(self) -> int:
        return sum(stats.files for stats in self.by_language.values())

    @property
    def error_rate(self) -> float:
        total = self.files
        if not total:
            return 0.0
        failed = sum(stats.files_with_errors for stats in self.by_language.values())
        return failed / total

    def throughput(self) -> float:
        """Files per second, for the performance figures in the docs."""
        if self.duration_seconds <= 0:
            return 0.0
        return self.files / self.duration_seconds

    def as_dict(self) -> dict[str, object]:
        return {
            "files": self.files,
            "symbols": len(self.snapshot.symbols),
            "references": len(self.references),
            "error_rate": round(self.error_rate, 4),
            "duration_seconds": round(self.duration_seconds, 3),
            "files_per_second": round(self.throughput(), 1),
            "walk": self.walk.as_dict(),
            "by_language": {
                name: stats.as_dict() for name, stats in sorted(self.by_language.items())
            },
            "failures": len(self.failures),
        }


def _add_extraction(
    result: BuildResult, extraction: FileExtraction
) -> None:
    stats = result.by_language.setdefault(extraction.language, LanguageStats())
    stats.files += 1
    stats.symbols += len(extraction.symbols)
    stats.references += len(extraction.references)
    if extraction.has_errors:
        stats.files_with_errors += 1
        stats.error_nodes += extraction.error_count

    for symbol in extraction.symbols:
        result.snapshot.add_symbol(symbol)

    # Containment is the one edge kind available without cross-file
    # resolution, so it is exact rather than merely confident.
    for symbol in extraction.symbols:
        if symbol.container_id is None:
            continue
        result.snapshot.add_edge(
            Edge(
                src_id=symbol.container_id,
                dst_id=symbol.id,
                kind=EdgeKind.CONTAINS,
                tier=ResolutionTier.ORACLE,
                site_path=symbol.path,
                site_range=symbol.name_range,
            )
        )

    for reference in extraction.references:
        result.references.append((extraction.path, reference))


def build_snapshot(
    root: Path | str,
    *,
    files: Iterable[SourceFile] | None = None,
    use_git: bool = True,
) -> BuildResult:
    """Parse a repository into a snapshot of symbols and containment.

    A file that cannot be parsed is recorded in ``failures`` and skipped;
    one broken file must not cost the whole index. A language whose grammar
    will not load is reported once rather than once per file.
    """
    root_path = Path(root).resolve()
    walk_stats = WalkStats()
    source_files = (
        list(files)
        if files is not None
        else list(iter_source_files(root_path, use_git=use_git, stats=walk_stats))
    )

    result = BuildResult(
        snapshot=IndexSnapshot(
            project_root=str(root_path),
            producer=f"repoatlas {__version__} (tree-sitter)",
        ),
        walk=walk_stats,
    )

    unavailable: set[str] = set()
    started = time.perf_counter()
    for source in source_files:
        if source.language.name in unavailable:
            continue
        try:
            content = source.absolute.read_bytes()
        except OSError as exc:
            result.failures.append((source.path, f"unreadable: {exc}"))
            continue
        try:
            extraction = extract_source(source.path, content, source.language)
        except LanguageUnavailable as exc:
            unavailable.add(source.language.name)
            result.failures.append((source.path, str(exc)))
            continue
        except Exception as exc:  # pragma: no cover - grammar crash guard
            result.failures.append((source.path, f"{type(exc).__name__}: {exc}"))
            continue
        _add_extraction(result, extraction)
    result.duration_seconds = time.perf_counter() - started

    _add_module_symbols(result)
    return result


def _add_module_symbols(result: BuildResult) -> None:
    """Give every parsed file a stand-in symbol for its top level.

    A top-level reference such as an import has no enclosing function or
    class, and its edge still needs a source. These are marked synthetic so
    they never appear in definition counts, where an oracle would rightly
    call them invented.
    """
    from ..model import SourceRange, Symbol

    paths = {symbol.path for symbol in result.snapshot.symbols.values()}
    paths.update(path for path, _ in result.references)
    origin = SourceRange.of(0, 0, 0, 0)
    for path in sorted(paths):
        module_id = f"{path}#<module>"
        if module_id in result.snapshot.symbols:
            continue
        result.snapshot.add_symbol(
            Symbol(
                id=module_id,
                name=Path(path).name,
                kind=SymbolKind.MODULE,
                path=path,
                name_range=origin,
                full_range=origin,
                synthetic=True,
            )
        )
