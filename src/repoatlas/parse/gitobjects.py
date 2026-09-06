"""Read a repository's files at one revision, without a working tree.

`git clone` does two separate things: it copies the object database, and it
expands one revision of it into files on disk. The second is the checkout,
and it is the part this module does not need.

That matters because of the one place this index measurably beats grep.
Across nine agent-level comparisons it never won where a shell and a
checkout exist — the agent simply greps. It won once, reviewing a diff with
no checkout: 0.503 to 0.839 against reading alone, paired +0.337 [+0.129,
+0.565], six wins to none. An index substitutes for a checkout. But until
now an index could only be *built* from one, so the result was reproducible
and not deployable: to serve the case where there is no checkout you first
had to have a checkout.

So: `git ls-tree` for what is in the revision, `git cat-file --batch` for
the bytes. Both work against a bare repository, which is what a mirror or a
CI cache actually holds. The output is a store that can be copied to a
machine holding no source at all and served with `serve --no-refresh`.

One difference from a checkout is worth knowing rather than discovering. A
blob is what was committed, so a repository configured with `autocrlf` will
give this module LF where the working tree has CRLF. Byte offsets in the
two indexes then differ by one per preceding line. Line and column numbers
do not, and those are what the tools answer with.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from pathlib import Path

from .languages import language_for_path
from .walk import (
    DEFAULT_EXCLUDED_DIRECTORIES,
    MAX_FILE_BYTES,
    SourceFile,
    Tree,
    WalkStats,
)

__all__ = ["GitObjectError", "RevisionTree", "resolve_revision"]

# Blob modes git uses for ordinary files. A symlink (120000) stores its
# target as its content and a submodule (160000) stores a commit id; both
# would parse as nonsense source, and a checkout-based index never sees
# them as files either.
_REGULAR_MODES = frozenset({"100644", "100755"})

# Which manifests the framework plugins might want to read. Kept as a
# fallback: the real list is asked of the plugin registry, and this is what
# is used if that import is unavailable for any reason.
_FALLBACK_MANIFESTS = ("composer.json", "package.json")


class GitObjectError(RuntimeError):
    """Git could not answer, or answered something unusable."""


def _git(repo: Path, *arguments: str, timeout: int = 120) -> bytes:
    """Run one git command against ``repo`` and return its raw stdout."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitObjectError("git is not on PATH") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitObjectError(f"git {' '.join(arguments)}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitObjectError(f"git {' '.join(arguments)}: {detail or 'failed'}")
    return result.stdout


def resolve_revision(repo: Path, revision: str = "HEAD") -> str:
    """The commit id ``revision`` names, in ``repo``.

    Resolved once and used everywhere after, so that a build cannot read
    half of one commit and half of another because a branch moved under it.
    """
    raw = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}")
    commit = raw.decode("ascii", errors="replace").strip()
    if not commit:
        raise GitObjectError(f"{revision} does not name a commit in {repo}")
    return commit


def _entries(repo: Path, commit: str) -> Iterator[tuple[str, str, int]]:
    """Every blob in the commit's tree: mode-checked path, object id, size.

    `-z` because repositories do contain filenames with newlines in them,
    and `-l` so the size is known before anything is read — a minified
    bundle can be skipped without pulling a megabyte through a pipe.
    """
    raw = _git(repo, "ls-tree", "-r", "-l", "-z", "--full-tree", commit, timeout=300)
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        head, _, name = entry.partition(b"\t")
        if not name:
            continue
        fields = head.split()
        if len(fields) != 4:
            continue
        mode, kind, oid, size = (field.decode("ascii", errors="replace") for field in fields)
        if kind != "blob" or mode not in _REGULAR_MODES:
            continue
        try:
            byte_count = int(size)
        except ValueError:  # `-` for anything whose size git did not report
            continue
        yield name.decode("utf-8", errors="surrogateescape"), oid, byte_count


class _BatchReader:
    """A single long-lived `git cat-file --batch` to read blobs through.

    One process per blob would be correct and unusably slow: a thousand
    files is a thousand process creations, which on Windows is most of a
    minute. `--batch` answers on a pipe, so the cost is one spawn.
    """

    def __init__(self, repo: Path) -> None:
        try:
            self._process = subprocess.Popen(
                ["git", "-C", str(repo), "cat-file", "--batch"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise GitObjectError("git is not on PATH") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitObjectError(f"git cat-file: {exc}") from exc

    def read(self, oid: str) -> bytes:
        process = self._process
        if process.stdin is None or process.stdout is None:  # pragma: no cover
            raise GitObjectError("git cat-file pipes are closed")
        if process.poll() is not None:
            raise GitObjectError("git cat-file exited early")
        try:
            process.stdin.write(f"{oid}\n".encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline()
        except OSError as exc:
            raise GitObjectError(f"git cat-file: {exc}") from exc
        fields = header.decode("ascii", errors="replace").split()
        if len(fields) != 3 or fields[1] != "blob":
            raise GitObjectError(f"git cat-file did not return a blob for {oid}")
        size = int(fields[2])
        # `readline` would stop at the first newline inside the blob, and
        # `read(size)` on a pipe can come back short, so loop until full.
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = process.stdout.read(remaining)
            if not chunk:
                raise GitObjectError(f"git cat-file truncated {oid}")
            chunks.append(chunk)
            remaining -= len(chunk)
        process.stdout.read(1)  # the newline git writes after every object
        return b"".join(chunks)

    def close(self) -> None:
        process = self._process
        if process.poll() is None:
            if process.stdin is not None:
                with suppress(OSError):  # it may already be gone
                    process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()
        if process.stdout is not None:
            process.stdout.close()


class RevisionTree(Tree):
    """One revision of a repository, read from git's objects.

    Works against a bare repository — `git clone --bare` — which is what a
    mirror or a CI cache holds, and never touches a working tree even when
    one happens to exist beside it.
    """

    def __init__(
        self,
        repo: Path | str,
        revision: str = "HEAD",
        *,
        excluded_directories: Iterable[str] = DEFAULT_EXCLUDED_DIRECTORIES,
        max_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.revision = revision
        self.commit = resolve_revision(self.repo, revision)
        self._excluded = frozenset(excluded_directories)
        self._max_bytes = max_bytes
        self._reader: _BatchReader | None = None

    # -- Tree ------------------------------------------------------------

    @property
    def label(self) -> str:
        # Deliberately not a path. Something read this index's
        # `project_root` and tried to open source files from it; a
        # revision has no directory to offer, and saying so is what makes
        # `index_status` report that bodies are unavailable rather than
        # fail one call at a time.
        return ""

    def provenance(self) -> dict[str, str]:
        return {
            "source": "git-objects",
            "revision": self.revision,
            "commit": self.commit,
            "repository": str(self.repo),
        }

    def files(self, stats: WalkStats | None = None) -> Iterator[SourceFile]:
        tracker = stats if stats is not None else WalkStats()
        for path, oid, size in _entries(self.repo, self.commit):
            tracker.considered += 1
            if set(path.split("/")[:-1]) & self._excluded:
                continue
            spec = language_for_path(path)
            if spec is None:
                tracker.unsupported_language += 1
                continue
            if size > self._max_bytes:
                tracker.too_large += 1
                tracker.excluded_paths.append(path)
                continue
            tracker.selected += 1
            yield SourceFile(path=path, absolute=None, language=spec, size=size, blob=oid)

    def read(self, source: SourceFile) -> bytes:
        if source.blob is None:
            raise GitObjectError(f"{source.path} has no blob to read")
        if self._reader is None:
            self._reader = _BatchReader(self.repo)
        return self._reader.read(source.blob)

    def stamp(self, source: SourceFile) -> tuple[int, int] | None:
        # A commit has no modification times. Returning a constant would
        # make every file look unchanged against a store built from a
        # different revision, which is a wrong index rather than a slow
        # one, so change detection is told to hash instead.
        return None

    def manifests(self, paths: Iterable[str]) -> AbstractContextManager[Path]:
        return self._materialise_manifests(frozenset(paths))

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def __enter__(self) -> RevisionTree:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()

    # -- internals -------------------------------------------------------

    @contextmanager
    def _materialise_manifests(self, paths: frozenset[str]) -> Iterator[Path]:
        """Write out the manifests framework detection would read.

        Detection asks whether `composer.json` declares Laravel, which is a
        file read, and a revision has no file to read. Rather than thread a
        reader through the whole plugin protocol, the handful of manifests
        that could possibly matter are written to a temporary directory
        laid out like the repository. Which ones those are is not guessed:
        the plugin registry names the manifest files, and the indexed paths
        name the directories a project could sit in.

        Without this the whole framework layer silently switches off, and
        with it Blade views, Livewire components and auto-imported Vue —
        the conventions the PHP result rests on.
        """
        wanted = sorted(self._manifest_paths(paths))
        blobs = {path: oid for path, oid, _ in _entries(self.repo, self.commit)}
        with tempfile.TemporaryDirectory(prefix="repoatlas-manifests-") as directory:
            base = Path(directory)
            if self._reader is None:
                self._reader = _BatchReader(self.repo)
            for path in wanted:
                oid = blobs.get(path)
                if oid is None:
                    continue
                destination = base / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(self._reader.read(oid))
            yield base

    @staticmethod
    def _manifest_paths(paths: frozenset[str]) -> set[str]:
        """Every manifest location the plugin registry could ask about."""
        try:
            from ..plugins import manifest_names, project_directories
        except ImportError:  # pragma: no cover - plugins are always present
            names: tuple[str, ...] = _FALLBACK_MANIFESTS
            directories = [""]
        else:
            names = tuple(manifest_names())
            directories = project_directories(paths)
        return {
            f"{where}/{name}" if where else name for where in directories for name in names
        }
