"""Deciding which files in a repository to read.

Getting this wrong is expensive in both directions. Walking `node_modules`
or `vendor` turns a two-second index into a two-minute one; skipping a
source directory silently produces an index that looks fine and answers
wrongly.

So the default is to ask git, which already knows what is tracked and what
`.gitignore` excludes, and to fall back to a conservative walk only when the
directory is not a repository.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from .languages import LanguageSpec, language_for_path

__all__ = [
    "DEFAULT_EXCLUDED_DIRECTORIES",
    "MAX_FILE_BYTES",
    "SourceFile",
    "Tree",
    "WalkStats",
    "WorkingTree",
    "is_git_repository",
    "iter_source_files",
]

# Directories that are never source, only build output or dependencies.
# Consulted for the non-git walk; inside a repository, .gitignore already
# covers these and this set is a second line of defence for the cases where
# a project commits its dependencies.
DEFAULT_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "vendor",
        "bower_components",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        "target",
        "out",
        ".next",
        ".nuxt",
        ".gradle",
        ".idea",
        ".vscode",
        "coverage",
        "htmlcov",
        ".repoatlas",
    }
)

# Files above this size are generated or minified far more often than they
# are hand-written. scip-typescript uses the same cutoff.
MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class SourceFile:
    """One file worth parsing."""

    path: str
    """Repository-relative, POSIX separators, as git reports it."""

    absolute: Path | None
    """Where the file is on disk, or ``None`` when there is no checkout.

    An index can be built straight from git's object database, in which
    case the file has a blob but no location: see
    :mod:`repoatlas.parse.gitobjects`. Read a file's bytes through the
    :class:`Tree` that produced it rather than from here.
    """

    language: LanguageSpec
    size: int

    blob: str | None = None
    """The git object id, when the file came from a revision rather than disk."""


@dataclass(slots=True)
class WalkStats:
    """Why files were left out, so a thin index can be explained."""

    considered: int = 0
    selected: int = 0
    unsupported_language: int = 0
    too_large: int = 0
    unreadable: int = 0
    excluded_paths: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "considered": self.considered,
            "selected": self.selected,
            "unsupported_language": self.unsupported_language,
            "too_large": self.too_large,
            "unreadable": self.unreadable,
        }


def is_git_repository(root: Path) -> bool:
    if (root / ".git").exists():
        return True
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _git_files(root: Path) -> list[str] | None:
    """List tracked and untracked-but-not-ignored files, or ``None`` on failure.

    `-z` because a repository may well contain a filename with a newline in
    it, and splitting on newlines would quietly produce two wrong paths.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.decode("utf-8", errors="surrogateescape")
    return [entry for entry in raw.split("\0") if entry]


def _walked_files(root: Path, excluded: frozenset[str]) -> Iterator[str]:
    """Fall back to a filesystem walk, pruning known-uninteresting directories."""
    import os

    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in excluded and not name.startswith(".")
        ]
        base = Path(directory)
        for filename in filenames:
            yield (base / filename).relative_to(root).as_posix()


def iter_source_files(
    root: Path | str,
    *,
    use_git: bool = True,
    excluded_directories: Iterable[str] = DEFAULT_EXCLUDED_DIRECTORIES,
    max_bytes: int = MAX_FILE_BYTES,
    stats: WalkStats | None = None,
) -> Iterator[SourceFile]:
    """Yield the files in ``root`` that RepoAtlas knows how to parse.

    Paths are repository-relative with POSIX separators regardless of host,
    because they end up in symbol ids and in comparisons against oracles
    produced on other machines.
    """
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise NotADirectoryError(f"not a directory: {root_path}")
    excluded = frozenset(excluded_directories)
    tracker = stats if stats is not None else WalkStats()

    listed = _git_files(root_path) if use_git and is_git_repository(root_path) else None
    candidates: Iterable[str] = (
        _walked_files(root_path, excluded) if listed is None else listed
    )

    for relative in candidates:
        tracker.considered += 1
        parts = set(Path(relative).parts[:-1])
        if parts & excluded:
            continue
        spec = language_for_path(relative)
        if spec is None:
            tracker.unsupported_language += 1
            continue
        absolute = root_path / relative
        try:
            size = absolute.stat().st_size
        except OSError:
            # git lists files that a concurrent checkout may have removed.
            tracker.unreadable += 1
            continue
        if size > max_bytes:
            tracker.too_large += 1
            tracker.excluded_paths.append(relative)
            continue
        tracker.selected += 1
        yield SourceFile(
            path=relative, absolute=absolute, language=spec, size=size
        )


class Tree(ABC):
    """Where an index's files and their bytes come from.

    There are two: a checked-out working tree, and a revision read straight
    out of git's object database. The second exists because the one setting
    where this index measurably beats grep is reviewing a diff with no
    checkout, and until an index could be built without one, that result
    could be reproduced but not deployed.

    Everything downstream — change detection, extraction, framework
    detection — goes through this rather than touching the filesystem, so
    the two paths cannot drift into producing different indexes.
    """

    @property
    @abstractmethod
    def label(self) -> str:
        """What to record as the index's origin."""

    @abstractmethod
    def files(self, stats: WalkStats | None = None) -> Iterator[SourceFile]:
        """The files worth parsing."""

    @abstractmethod
    def read(self, source: SourceFile) -> bytes:
        """One file's contents."""

    @abstractmethod
    def stamp(self, source: SourceFile) -> tuple[int, int] | None:
        """Size and mtime, or ``None`` when there is no cheap stamp.

        ``None`` means change detection must hash the contents. A revision
        has no modification times, and inventing one would be worse than
        admitting it: a constant would make every file look unchanged.
        """

    def provenance(self) -> dict[str, str]:
        """Extra meta recording where this index came from.

        Written into the store beside the producer stamp, so an index that
        arrived on a machine with no source can still say which commit it
        describes.
        """
        return {}

    @abstractmethod
    def manifests(self, paths: Iterable[str]) -> AbstractContextManager[Path]:
        """A directory the framework plugins can read manifests from.

        Framework detection asks whether `composer.json` declares Laravel,
        which means reading a real file. A working tree simply hands over
        its root; a revision materialises the handful of manifests that
        could matter into a temporary directory. Without this a
        no-checkout index would quietly lose every framework convention —
        Blade views, Livewire components, auto-imported Vue — which is
        exactly the part the PHP result depends on.
        """


@dataclass(frozen=True)
class WorkingTree(Tree):
    """The ordinary case: files on disk, in a directory."""

    root: Path
    use_git: bool = True
    excluded_directories: frozenset[str] = DEFAULT_EXCLUDED_DIRECTORIES
    max_bytes: int = MAX_FILE_BYTES

    @property
    def label(self) -> str:
        return str(self.root)

    def files(self, stats: WalkStats | None = None) -> Iterator[SourceFile]:
        return iter_source_files(
            self.root,
            use_git=self.use_git,
            excluded_directories=self.excluded_directories,
            max_bytes=self.max_bytes,
            stats=stats,
        )

    def read(self, source: SourceFile) -> bytes:
        if source.absolute is None:  # pragma: no cover - not reachable from here
            raise ValueError(f"{source.path} has no location on disk")
        return source.absolute.read_bytes()

    def stamp(self, source: SourceFile) -> tuple[int, int] | None:
        if source.absolute is None:  # pragma: no cover - not reachable from here
            return None
        stat = source.absolute.stat()
        return stat.st_size, stat.st_mtime_ns

    def manifests(self, paths: Iterable[str]) -> AbstractContextManager[Path]:
        return nullcontext(self.root)
