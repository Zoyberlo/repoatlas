"""Does the map put the right code in front of the agent? Git history says.

Every localisation benchmark in the literature is Python, and none covers
the stack this index is used on. But every repository carries its own
ground truth: a commit that touched a few symbols was, for its author, a
localisation task, and its message is what the author knew before finding
them. So for each commit the question is whether a map of the tree *as it
stood before that commit*, drawn around the words of the message, names
the code the commit went on to change.

That is the number the ranking weights have been waiting for. Every choice
in `rank/pagerank.py` — the kind prior, the containment direction, the edge
weights, the damping, how hard a focus pulls — was written down as a
judgement. This is what turns one into a measurement.

Two things it took a wrong answer to learn.

**Score symbols, not files.** The first version of this counted how many of
a commit's *files* appeared on the map, and that metric cannot settle
anything about how the budget is spent: a map trades symbols for filenames,
so file recall rises monotonically until the map is a list of paths that
scores best and says least. Symbol recall does not reward that, because a
filename with one line under it contains none of the changed code.

**Index the tree the task saw.** Scoring against today's index made a file
since renamed a miss, and put symbols in the map that did not exist when
the work started. So history is walked in a scratch clone, oldest commit
first, each parent tree indexed incrementally from the last.

The remaining caveat cannot be engineered away: a commit message is a
generous proxy for a task, written afterwards by the person who did the
work. These numbers are a ceiling for what an agent's own words would get.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import Edge, EdgeKind, IndexSnapshot, ResolutionTier, Symbol, SymbolKind
from .rank import MapOptions, RankOptions, rank_symbols, render_map
from .rank.cache import mention_keys
from .rank.tokens import estimate_tokens
from .store import IndexStore, update_store

__all__ = [
    "Commit",
    "CommitCase",
    "LocalizeResult",
    "commit_cases",
    "run_localize",
    "words_of",
]

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+")

# Words a commit subject is full of and a codebase is not. Left as a plain
# list because the only thing that would justify a cleverer filter is a
# measurement, and every one so far says the vocabulary matters less than
# how it is matched.
_STOPWORDS = frozenset(
    ["the", "and", "for", "with", "from", "into", "that", "this", "then", "than", "when", "where", "which", "while", "make", "makes", "made", "add", "adds", "added", "fix", "fixes", "fixed", "remove", "removes", "removed", "use", "uses", "used", "update", "updates", "updated", "change", "changes", "changed", "move", "moved", "let", "lets", "keep", "keeps", "give", "gives", "put", "puts", "spend", "one", "two", "three", "every", "each", "not", "now", "new", "old", "its", "over", "under", "between", "before", "after", "only", "also", "what", "why", "how", "does", "did", "done", "can", "could", "should", "would", "will", "still"]
)


class HistoryError(RuntimeError):
    """The repository could not be read, and the message says how."""


def words_of(subject: str) -> tuple[str, ...]:
    """The identifier-like words of a commit subject, in order, once each."""
    seen: dict[str, None] = {}
    for word in _WORD.findall(subject):
        if word.lower() in _STOPWORDS:
            continue
        seen.setdefault(word, None)
    return tuple(seen)


def _git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    if check and result.returncode != 0:
        raise HistoryError(f"git {args[0]} failed: {result.stderr.strip()[:200]}")
    return result.stdout


@dataclass(frozen=True, slots=True)
class Commit:
    """One commit, with the parent tree its task would have started from."""

    sha: str
    parent: str
    subject: str
    files: tuple[str, ...]

    @property
    def mentions(self) -> tuple[str, ...]:
        return words_of(self.subject)


@dataclass(slots=True)
class CommitCase:
    """What one commit changed, and how well the map named it."""

    sha: str
    subject: str
    symbols: int
    files: int
    matched_mentions: int = 0
    symbol_recall_plain: float = 0.0
    symbol_recall_steered: float = 0.0
    symbol_recall_skeleton: float = 0.0
    """The baseline: the whole repository's skeleton, files in path order, cut at the budget."""

    symbol_recall_grep: float = 0.0
    """What grep gives for the same words and the same budget: hits, busiest file first."""

    symbol_recall_names: float = 0.0
    """The same ranking over a graph built by matching names, as aider's repo map does."""

    file_recall_plain: float = 0.0
    file_recall_steered: float = 0.0
    file_recall_skeleton: float = 0.0
    file_recall_grep: float = 0.0
    file_recall_names: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "symbols": self.symbols,
            "files": self.files,
            "matched_mentions": self.matched_mentions,
            "symbol_recall_plain": round(self.symbol_recall_plain, 3),
            "symbol_recall_steered": round(self.symbol_recall_steered, 3),
            "symbol_recall_skeleton": round(self.symbol_recall_skeleton, 3),
            "symbol_recall_grep": round(self.symbol_recall_grep, 3),
            "symbol_recall_names": round(self.symbol_recall_names, 3),
            "file_recall_plain": round(self.file_recall_plain, 3),
            "file_recall_steered": round(self.file_recall_steered, 3),
            "file_recall_skeleton": round(self.file_recall_skeleton, 3),
            "file_recall_grep": round(self.file_recall_grep, 3),
            "file_recall_names": round(self.file_recall_names, 3),
        }


@dataclass(slots=True)
class LocalizeResult:
    """What the whole walk measured."""

    budget: int
    walked: int = 0
    """Commits considered, including those with nothing to score."""

    cases: list[CommitCase] = field(default_factory=list)

    def _mean(self, name: str) -> float:
        values = [getattr(case, name) for case in self.cases]
        return statistics.mean(values) if values else 0.0

    def as_dict(self, *, include_cases: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "budget": self.budget,
            "walked": self.walked,
            "scored": len(self.cases),
            "symbol_recall_plain": round(self._mean("symbol_recall_plain"), 4),
            "symbol_recall_steered": round(self._mean("symbol_recall_steered"), 4),
            "symbol_recall_skeleton": round(self._mean("symbol_recall_skeleton"), 4),
            "symbol_recall_grep": round(self._mean("symbol_recall_grep"), 4),
            "symbol_recall_names": round(self._mean("symbol_recall_names"), 4),
            "file_recall_plain": round(self._mean("file_recall_plain"), 4),
            "file_recall_steered": round(self._mean("file_recall_steered"), 4),
            "file_recall_skeleton": round(self._mean("file_recall_skeleton"), 4),
            "file_recall_grep": round(self._mean("file_recall_grep"), 4),
            "file_recall_names": round(self._mean("file_recall_names"), 4),
        }
        if include_cases:
            # Subjects and paths are the repository's own content, so they
            # are off by default: a benchmark result is publishable, a
            # client project's commit log is not.
            payload["cases"] = [case.as_dict() for case in self.cases]
        return payload

    def as_text(self) -> str:
        return "\n".join(
            (
                f"commits:  {len(self.cases)} scored of {self.walked} walked "
                f"(budget {self.budget})",
                "",
                f"{'':<22}{'plain':>8}{'steered':>10}{'skeleton':>10}{'grep':>8}{'names':>8}",
                f"{'symbol recall':<22}{self._mean('symbol_recall_plain'):>8.3f}"
                f"{self._mean('symbol_recall_steered'):>10.3f}"
                f"{self._mean('symbol_recall_skeleton'):>10.3f}"
                f"{self._mean('symbol_recall_grep'):>8.3f}"
                f"{self._mean('symbol_recall_names'):>8.3f}",
                f"{'file recall':<22}{self._mean('file_recall_plain'):>8.3f}"
                f"{self._mean('file_recall_steered'):>10.3f}"
                f"{self._mean('file_recall_skeleton'):>10.3f}"
                f"{self._mean('file_recall_grep'):>8.3f}"
                f"{self._mean('file_recall_names'):>8.3f}",
            )
        ) + "\n"


def commit_cases(
    root: Path, *, commits: int = 200, max_files: int = 8
) -> list[Commit]:
    """Recent commits that read as localisation tasks, oldest first.

    A commit with no parent has no tree to have started from, and one
    touching more than ``max_files`` is a refactor rather than a task.
    More history is read than asked for, because most of it is filtered.
    """
    raw = _git(
        root,
        "log",
        "--no-merges",
        f"--max-count={max(commits * 5, commits + 50)}",
        "--format=%x00%H%x01%P%x01%s",
        "--name-only",
    ).splitlines()
    found: list[Commit] = []
    sha = parents = subject = ""
    touched: list[str] = []

    def flush() -> None:
        parent = parents.split()[0] if parents else ""
        if sha and parent and 0 < len(touched) <= max_files:
            found.append(Commit(sha, parent, subject, tuple(sorted(set(touched)))))

    for line in raw:
        if line.startswith("\x00"):
            flush()
            sha, parents, subject = line[1:].split("\x01", 2)
            touched = []
        elif line.strip():
            touched.append(line.strip().replace("\\", "/"))
    flush()
    return list(reversed(found[:commits]))


def _changed_lines(root: Path, commit: Commit, path: str) -> set[int]:
    """One-based lines of ``path`` in the *parent* tree that the commit changed."""
    diff = _git(
        root,
        "diff",
        "--unified=0",
        "--no-color",
        commit.parent,
        commit.sha,
        "--",
        path,
        check=False,
    )
    lines: set[int] = set()
    for line in diff.splitlines():
        match = _HUNK.match(line)
        if match:
            start = int(match.group(1))
            lines.update(range(start, start + int(match.group(2) or 1)))
    return lines


def _touched_symbols(store: IndexStore, root: Path, commit: Commit) -> set[str]:
    """The parent tree's symbols that this commit went on to change."""
    indexed = set(store.languages())
    found: set[str] = set()
    for path in commit.files:
        if path not in indexed:
            continue
        lines = _changed_lines(root, commit, path)
        if not lines:
            continue
        for symbol in store.symbols(path=path):
            if symbol.synthetic or symbol.local:
                continue
            span = symbol.full_range or symbol.name_range
            if any(span.start.line <= number - 1 <= span.end.line for number in lines):
                found.add(symbol.id)
    return found


def _entries(text: str) -> tuple[set[tuple[str, int]], set[str]]:
    """The (path, line) entries a rendered map lists, and the files it names."""
    points: set[tuple[str, int]] = set()
    files: set[str] = set()
    current = ""
    for line in text.splitlines():
        if line and not line.startswith(" ") and line.endswith(":"):
            current = line[:-1]
            files.add(current)
        elif line.strip() and current:
            head = line.split(None, 1)[0] if line.split() else ""
            if head.isdigit():
                points.add((current, int(head)))
    return points, files


def _prepare_clone(source: Path, work: Path) -> Path:
    """A scratch clone, so the walk never touches the repository it reads."""
    if not (source / ".git").exists():
        raise HistoryError(f"not a git repository: {source}")
    if not (work / ".git").exists():
        work.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", "--quiet", "--local", str(source), str(work)],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if result.returncode != 0:
            raise HistoryError(f"could not clone: {result.stderr.strip()[:200]}")
    return work


def _seed_index(
    symbols: Iterable[Any], paths: Iterable[str]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    by_name: dict[str, list[str]] = {}
    by_stem: dict[str, list[str]] = {}
    for symbol in symbols:
        if symbol.synthetic or symbol.local:
            continue
        for key in mention_keys(symbol.name):
            by_name.setdefault(key, []).append(symbol.id)
    for path in paths:
        stem = path.rsplit("/", 1)[-1].split(".", 1)[0]
        for key in mention_keys(stem):
            by_stem.setdefault(key, []).append(path)
    return by_name, by_stem


def walk(
    source: Path,
    *,
    work: Path,
    commits: int = 200,
    budget: int = 2000,
    max_files: int = 8,
    options: RankOptions | None = None,
    map_options: MapOptions | None = None,
) -> Iterator[tuple[Commit, CommitCase]]:
    """Walk history, yielding each commit's case as it is scored.

    Yielding rather than returning a list so a long walk can report
    progress; :func:`run_localize` is the whole-result front door.
    """
    root = _prepare_clone(source, work)
    options = options or RankOptions()
    map_options = map_options or MapOptions(budget=budget)
    store_path = root / ".repoatlas-localize.db"
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(store_path) + suffix)
        if stale.exists():
            stale.unlink()

    selected = commit_cases(root, commits=commits, max_files=max_files)
    if not selected:
        raise HistoryError("no commit in this history touches a handful of files")
    head = _git(root, "rev-parse", "HEAD").strip()
    try:
        with IndexStore(store_path) as store:
            for commit in selected:
                _git(root, "checkout", "--quiet", "--detach", commit.parent)
                update_store(root, store, use_git=True)
                wanted = _touched_symbols(store, root, commit)
                case = CommitCase(
                    sha=commit.sha[:12],
                    subject=commit.subject,
                    symbols=len(wanted),
                    files=len(commit.files),
                )
                if not wanted:
                    # Nothing the index holds; a new file, or a change
                    # only to lines no symbol covers.
                    yield commit, case
                    continue

                snapshot = store.snapshot()
                by_name, by_stem = _seed_index(
                    snapshot.symbols.values(), store.languages()
                )
                seeds: set[str] = set()
                seed_paths: set[str] = set()
                for word in commit.mentions:
                    key = word.strip().lower()
                    seeds.update(by_name.get(key, ()))
                    seed_paths.update(by_stem.get(key, ()))
                case.matched_mentions = sum(
                    1
                    for word in commit.mentions
                    if word.lower() in by_name or word.lower() in by_stem
                )

                wanted_ids = {item for item in wanted if item in snapshot.symbols}
                wanted_files = {snapshot.symbols[item].path for item in wanted_ids}
                locate = _Locator(snapshot)

                for label, focus_paths, focus_symbols in (
                    ("plain", set(), set()),
                    ("steered", seed_paths, seeds),
                ):
                    ranked = rank_symbols(
                        snapshot,
                        focus_paths=focus_paths,
                        focus_symbols=focus_symbols,
                        options=options,
                    )
                    points, files = _entries(render_map(ranked, map_options).text)
                    setattr(
                        case,
                        f"symbol_recall_{label}",
                        len(locate.credit(points) & wanted_ids) / len(wanted_ids),
                    )
                    setattr(
                        case,
                        f"file_recall_{label}",
                        len(wanted_files & files) / len(wanted_files),
                    )
                # What the same ranking finds over a graph built the way
                # aider's repo map builds one: a reference to a name is an
                # edge to every symbol of that name, with no import
                # resolution and no types. Steered the same way, so the
                # only difference is the graph.
                names_snapshot = name_matched(snapshot, store)
                ranked = rank_symbols(
                    names_snapshot,
                    focus_paths=seed_paths,
                    focus_symbols=seeds,
                    options=options,
                )
                points, files = _entries(render_map(ranked, map_options).text)
                case.symbol_recall_names = len(locate.credit(points) & wanted_ids) / len(wanted_ids)
                case.file_recall_names = len(wanted_files & files) / len(wanted_files)

                # The baselines any ranking has to beat. The skeleton of the
                # whole repository, files in path order, cut at the same
                # budget, is what `repomix --compress` hands a model; grep
                # for the same words, busiest file first and cut at the
                # same budget, is what an agent with no index does first.
                # This project once lost to a baseline that naive.
                for label, text in (
                    ("skeleton", skeleton_prefix(snapshot, map_options.budget)),
                    ("grep", grep_prefix(root, snapshot, commit.mentions, map_options.budget)),
                ):
                    points, files = _entries(text)
                    setattr(
                        case,
                        f"symbol_recall_{label}",
                        len(locate.credit(points) & wanted_ids) / len(wanted_ids),
                    )
                    setattr(
                        case,
                        f"file_recall_{label}",
                        len(wanted_files & files) / len(wanted_files),
                    )
                yield commit, case
    finally:
        _git(root, "checkout", "--quiet", "--detach", head, check=False)


class _Locator:
    """Which symbol a `path:line` point lands in: the innermost one around it.

    A map names a symbol by its declaration line; grep lands anywhere in
    its body. Both have found the symbol, and an agent shown either would
    open the same function, so both are scored the same way.
    """

    __slots__ = ("_spans",)

    def __init__(self, snapshot: IndexSnapshot) -> None:
        self._spans: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        for symbol in snapshot.symbols.values():
            if symbol.synthetic or symbol.local:
                continue
            span = symbol.full_range or symbol.name_range
            self._spans[symbol.path].append((span.start.line + 1, span.end.line + 1, symbol.id))

    def credit(self, points: Iterable[tuple[str, int]]) -> set[str]:
        found: set[str] = set()
        for path, line in points:
            best: tuple[int, str] | None = None
            for start, end, symbol_id in self._spans.get(path, ()):
                if start <= line <= end and (best is None or end - start < best[0]):
                    best = (end - start, symbol_id)
            if best is not None:
                found.add(best[1])
        return found


def grep_prefix(root: Path, snapshot: IndexSnapshot, words: Sequence[str], budget: int) -> str:
    """What grep shows for the task's words, busiest file first, cut at the budget.

    Case-insensitive substring hits over the indexed files, rendered in
    the map's own format so the same scorer reads them. Files come in
    order of how many hits they hold, which is where an agent reading
    grep output looks first; within a file, in line order.
    """
    needles = [word.lower() for word in words if word]
    if not needles:
        return ""
    hits: dict[str, list[tuple[int, str]]] = {}
    for path in sorted({symbol.path for symbol in snapshot.symbols.values()}):
        try:
            text = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = [
            (number, line.strip()[:120])
            for number, line in enumerate(text.splitlines(), start=1)
            if any(needle in line.lower() for needle in needles)
        ]
        if found:
            hits[path] = found
    lines: list[str] = []
    spent = 0
    for path in sorted(hits, key=lambda item: (-len(hits[item]), item)):
        block = [f"{path}:", *(f"{number:>5}  {text}" for number, text in hits[path]), ""]
        for line in block:
            cost = estimate_tokens(line + "\n")
            if spent + cost > budget:
                return "\n".join(lines) + "\n"
            spent += cost
            lines.append(line)
    return "\n".join(lines) + "\n"


def name_matched(snapshot: IndexSnapshot, store: IndexStore) -> IndexSnapshot:
    """The same symbols, with edges built by matching names alone.

    This is how aider's repo map connects a repository, and it is the
    only part of this project that another tool already does: a
    reference to `client` becomes an edge to every symbol called
    `client`, however many that is and whatever the imports say. Ranking
    the two graphs the same way, and scoring them the same way, is what
    isolates the contribution of resolving a reference rather than
    matching it.
    """
    by_name: dict[str, list[str]] = defaultdict(list)
    for symbol in snapshot.symbols.values():
        if not symbol.synthetic and not symbol.local:
            by_name[symbol.name].append(symbol.id)
    modules = {
        symbol.path: symbol.id
        for symbol in snapshot.symbols.values()
        if symbol.kind is SymbolKind.MODULE
    }
    copy = IndexSnapshot(producer=snapshot.producer)
    for symbol in snapshot.symbols.values():
        copy.add_symbol(symbol)
    seen: set[tuple[str, str]] = set()
    for path, reference in store.references():
        source = reference.container_id or modules.get(path)
        if source is None:
            continue
        for target in by_name.get(reference.name, ()):
            if target == source or (source, target) in seen:
                continue
            seen.add((source, target))
            copy.add_edge(
                Edge(
                    src_id=source,
                    dst_id=target,
                    kind=EdgeKind.REFERENCES,
                    tier=ResolutionTier.FUZZY,
                )
            )
    # Containment is structure and the ranking wants it either way.
    for edge in snapshot.edges:
        if edge.kind is EdgeKind.CONTAINS:
            copy.add_edge(edge)
    return copy


def skeleton_prefix(snapshot: IndexSnapshot, budget: int) -> str:
    """The repository's skeleton in path order, cut where ``budget`` runs out.

    Rendered in the map's own format so the same scorer reads it: a
    `path:` header, then one indented line per symbol in source order.
    No ranking, no steering, no spread; this is the baseline.
    """
    by_path: dict[str, list[Symbol]] = defaultdict(list)
    for symbol in snapshot.symbols.values():
        if not symbol.synthetic and not symbol.local:
            by_path[symbol.path].append(symbol)
    lines: list[str] = []
    spent = 0
    for path in sorted(by_path):
        symbols = sorted(by_path[path], key=lambda s: s.name_range)
        block = [f"{path}:"]
        by_id = {s.id: s for s in symbols}
        for symbol in symbols:
            depth = 0
            container = symbol.container_id
            while container in by_id:
                depth += 1
                container = by_id[container].container_id
            body = symbol.signature or f"{symbol.kind.value} {symbol.name}"
            block.append(f"{'  ' * depth}{symbol.name_range.start.line + 1:>5}  {body}")
        block.append("")
        for line in block:
            cost = estimate_tokens(line + "\n")
            if spent + cost > budget:
                return "\n".join(lines) + "\n"
            spent += cost
            lines.append(line)
    return "\n".join(lines) + "\n"


def run_localize(
    source: Path,
    *,
    work: Path,
    commits: int = 200,
    budget: int = 2000,
    max_files: int = 8,
    options: RankOptions | None = None,
    map_options: MapOptions | None = None,
    progress: Sequence[Any] | None = None,
) -> LocalizeResult:
    """Score the map against the repository's own history."""
    result = LocalizeResult(budget=budget)
    for _commit, case in walk(
        source,
        work=work,
        commits=commits,
        budget=budget,
        max_files=max_files,
        options=options,
        map_options=map_options,
    ):
        result.walked += 1
        if case.symbols:
            result.cases.append(case)
        if progress is not None:
            progress.append(case)  # type: ignore[attr-defined]
    return result


def to_json(result: LocalizeResult, *, include_cases: bool = False) -> str:
    return json.dumps(result.as_dict(include_cases=include_cases), indent=2) + "\n"
