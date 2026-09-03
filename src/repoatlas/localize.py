"""Does the map put the right files in front of the agent? Git history says.

Every localisation benchmark in the literature is Python, and none covers
the stack this index is used on. But every repository carries its own
ground truth: a commit that touched three files was, for its author, a
localisation task, and its message is what the author knew before finding
them. So for each recent commit the question is whether a map drawn
around the words of that message, at the usual budget, lists the files the
commit touched.

That is the number the ranking weights have been waiting for. Every
choice in `pagerank.py`, the kind prior, the containment direction, the
edge weights, the spread exponent, is recorded as a judgement; this is
what turns one of them into a measurement, on the user's own repository.

Two honest caveats. The index is of the current tree, so a file a commit
created that has since been renamed or removed counts as a miss. And a
commit message written after the fact says more than a task description
written before it, so the recall here is a ceiling for what an agent
would see, not a floor.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .rank.cache import RankCache
from .server import tools
from .store.database import IndexStore

__all__ = ["CommitCase", "LocalizeResult", "commit_cases", "run_localize"]

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOPWORDS = frozenset(
    ["the", "and", "for", "with", "from", "into", "that", "this", "then", "than", "when", "where", "which", "while", "make", "makes", "made", "add", "adds", "added", "fix", "fixes", "fixed", "remove", "removes", "removed", "use", "uses", "used", "update", "updates", "updated", "change", "changes", "changed", "move", "moved", "let", "lets", "keep", "keeps", "give", "gives", "put", "puts", "spend", "one", "two", "three", "every", "each", "not", "now", "new", "old", "its", "into", "over", "under", "between", "before", "after", "only", "also", "what", "where", "why", "how", "does", "did", "done", "can", "could", "should", "would", "will", "still"]
)


@dataclass(slots=True)
class CommitCase:
    """One commit as a localisation task."""

    sha: str
    subject: str
    touched: tuple[str, ...]
    """The indexed files the commit changed."""

    mentions: tuple[str, ...]
    """Identifier-like words from the subject, the task's vocabulary."""

    recall_plain: float = 0.0
    recall_mentioned: float = 0.0
    first_hit_plain: bool = False
    first_hit_mentioned: bool = False
    matched_mentions: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "subject": self.subject,
            "touched": list(self.touched),
            "mentions": list(self.mentions),
            "matched_mentions": self.matched_mentions,
            "recall_plain": round(self.recall_plain, 3),
            "recall_mentioned": round(self.recall_mentioned, 3),
            "first_hit_plain": self.first_hit_plain,
            "first_hit_mentioned": self.first_hit_mentioned,
        }


@dataclass(slots=True)
class LocalizeResult:
    budget: int
    cases: list[CommitCase] = field(default_factory=list)

    def _mean(self, values: Iterable[float]) -> float:
        items = list(values)
        return sum(items) / len(items) if items else 0.0

    @property
    def recall_plain(self) -> float:
        return self._mean(case.recall_plain for case in self.cases)

    @property
    def recall_mentioned(self) -> float:
        return self._mean(case.recall_mentioned for case in self.cases)

    @property
    def first_hit_plain(self) -> float:
        return self._mean(float(case.first_hit_plain) for case in self.cases)

    @property
    def first_hit_mentioned(self) -> float:
        return self._mean(float(case.first_hit_mentioned) for case in self.cases)

    def as_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget,
            "commits": len(self.cases),
            "recall_plain": round(self.recall_plain, 3),
            "recall_mentioned": round(self.recall_mentioned, 3),
            "first_hit_plain": round(self.first_hit_plain, 3),
            "first_hit_mentioned": round(self.first_hit_mentioned, 3),
            "cases": [case.as_dict() for case in self.cases],
        }

    def as_text(self) -> str:
        lines = [
            f"commits:              {len(self.cases)} (budget {self.budget})",
            f"recall, plain map:    {self.recall_plain:.3f}",
            f"recall, with mention: {self.recall_mentioned:.3f}",
            f"first file hit, plain:   {self.first_hit_plain:.3f}",
            f"first file hit, mention: {self.first_hit_mentioned:.3f}",
        ]
        return "\n".join(lines) + "\n"


def _git_lines(root: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return result.stdout.splitlines()


def words_of(subject: str) -> tuple[str, ...]:
    """The identifier-like words of a commit subject, in order, once each."""
    seen: dict[str, None] = {}
    for word in _WORD.findall(subject):
        lowered = word.lower()
        if lowered in _STOPWORDS:
            continue
        seen.setdefault(word, None)
    return tuple(seen)


def commit_cases(
    root: Path,
    indexed: set[str],
    *,
    commits: int = 50,
    max_files: int = 8,
) -> list[CommitCase]:
    """The recent commits that read as localisation tasks.

    A commit touching no indexed file says nothing about the map, and one
    touching more than ``max_files`` is a refactor rather than a task; both
    are skipped, and more history is read until ``commits`` remain.
    """
    raw = _git_lines(
        root,
        "log",
        "--no-merges",
        f"--max-count={commits * 4}",
        "--format=%x00%h%x01%s",
        "--name-only",
    )
    cases: list[CommitCase] = []
    sha = subject = ""
    touched: list[str] = []

    def flush() -> None:
        files = tuple(sorted(path for path in touched if path in indexed))
        if sha and 0 < len(files) <= max_files:
            cases.append(
                CommitCase(sha=sha, subject=subject, touched=files, mentions=words_of(subject))
            )

    for line in raw:
        if line.startswith("\x00"):
            flush()
            sha, _, subject = line[1:].partition("\x01")
            touched = []
        elif line.strip():
            touched.append(line.strip().replace("\\", "/"))
    flush()
    return cases[:commits]


def _files_in_map(text: str) -> list[str]:
    """The files a rendered map lists, in the order it lists them."""
    files: list[str] = []
    for line in text.splitlines():
        if line and not line.startswith(" ") and line.endswith(":"):
            files.append(line[:-1])
    return files


def run_localize(
    store: IndexStore,
    root: Path,
    *,
    commits: int = 50,
    budget: int = 2000,
    cache: RankCache | None = None,
) -> LocalizeResult:
    """Score the map against the repository's own history."""
    cache = cache or RankCache()
    indexed = set(store.languages())
    result = LocalizeResult(budget=budget)
    plain_files = _files_in_map(tools.repo_map(store, budget=budget, cache=cache))
    plain_set = set(plain_files)
    for case in commit_cases(root, indexed, commits=commits):
        touched = set(case.touched)
        case.recall_plain = len(touched & plain_set) / len(touched)
        case.first_hit_plain = bool(plain_files) and plain_files[0] in touched
        seeds, paths, unmatched = cache.seeds_for(store, case.mentions)
        case.matched_mentions = len(case.mentions) - len(unmatched)
        steered = tools.repo_map(store, mention=case.mentions, budget=budget, cache=cache)
        steered_files = _files_in_map(steered)
        case.recall_mentioned = len(touched & set(steered_files)) / len(touched)
        case.first_hit_mentioned = bool(steered_files) and steered_files[0] in touched
        result.cases.append(case)
    return result
