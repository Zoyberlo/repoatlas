"""Where an index's tokens go: the cost of the whole skeleton, by directory.

A budgeted map spends two thousand tokens; the skeleton it chooses from
costs what it costs, and until now nothing said how much or where. This
is the tree Repomix prints as `--token-count-tree`, for the same reason:
a user deciding what to exclude, and this project tuning a budget, both
want to see which directories are heavy. With a ceiling it is also a CI
gate, `repoatlas tokens index.db --max-total 60000`, which exits non-zero
when the skeleton has outgrown what an agent is expected to hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .rank.tokens import TokenEstimator
from .server.tools import file_outline
from .store import IndexStore

__all__ = ["CostNode", "CostTree", "token_tree"]

_UNBOUNDED = 10**9


@dataclass(slots=True)
class CostNode:
    """One directory or file and what its skeleton costs."""

    path: str
    tokens: int
    files: int
    symbols: int
    children: list[CostNode] = field(default_factory=list)
    is_file: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "tokens": self.tokens,
            "files": self.files,
            "symbols": self.symbols,
        }
        if self.children:
            payload["children"] = [child.as_dict() for child in self.children]
        return payload


@dataclass(slots=True)
class CostTree:
    root: CostNode
    depth: int

    @property
    def total(self) -> int:
        return self.root.tokens

    def as_dict(self) -> dict[str, Any]:
        return {"total_tokens": self.total, "depth": self.depth, "tree": self.root.as_dict()}

    def as_text(self, *, top: int = 25) -> str:
        lines = [
            f"skeleton of {self.root.files} files, {self.root.symbols} symbols: "
            f"{self.root.tokens} tokens (estimated)",
            "",
        ]
        self._render(self.root, lines, level=0, top=top)
        return "\n".join(lines) + "\n"

    def _render(self, node: CostNode, lines: list[str], *, level: int, top: int) -> None:
        shown = node.children[:top]
        for child in shown:
            share = child.tokens / self.total if self.total else 0.0
            name = child.path.rsplit("/", 1)[-1] + ("" if child.is_file else "/")
            lines.append(
                f"{'  ' * level}{child.tokens:>8}  {share:>5.1%}  {name}"
                f"  ({child.files} files, {child.symbols} symbols)"
            )
            self._render(child, lines, level=level + 1, top=top)
        left = len(node.children) - len(shown)
        if left > 0:
            rest = sum(child.tokens for child in node.children[top:])
            lines.append(f"{'  ' * level}{rest:>8}  {rest / self.total:>5.1%}  [{left} more]")


def token_tree(
    store: IndexStore, *, depth: int = 2, estimator: TokenEstimator | None = None
) -> CostTree:
    """The skeleton's token cost per directory, ``depth`` levels down.

    Every file's cost is what `file_outline` would print for it, measured
    with the store's own estimator, so the numbers are the ones a map
    budget is spent against.
    """
    estimate = estimator or store.estimator()
    root = CostNode(path="", tokens=0, files=0, symbols=0)
    index: dict[str, CostNode] = {"": root}
    for path in sorted(store.languages()):
        text = file_outline(store, path, budget=_UNBOUNDED, estimator=estimate)
        tokens = estimate(text)
        symbols = sum(1 for line in text.splitlines()[1:] if line.strip()[:1].isdigit())
        parts = path.split("/")
        node = root
        node.tokens += tokens
        node.files += 1
        node.symbols += symbols
        # Directories down to ``depth``, and the file itself where the
        # depth allows, so a file at the root is a row and not a remainder.
        for level in range(1, min(depth, len(parts)) + 1):
            key = "/".join(parts[:level])
            child = index.get(key)
            if child is None:
                child = CostNode(
                    path=key, tokens=0, files=0, symbols=0, is_file=level == len(parts)
                )
                index[key] = child
                node.children.append(child)
            child.tokens += tokens
            child.files += 1
            child.symbols += symbols
            node = child
    _sort(root)
    return CostTree(root=root, depth=depth)


def _sort(node: CostNode) -> None:
    node.children.sort(key=lambda child: (-child.tokens, child.path))
    for child in node.children:
        _sort(child)
