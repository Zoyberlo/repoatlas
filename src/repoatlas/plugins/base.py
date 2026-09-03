"""The plugin contract, and finding which plugins apply to a repository.

Two methods. ``detect`` says whether a repository uses this framework, and
``resolve`` turns one conventional name into a file path. That is the whole
surface, because a plugin that could do more would be a place for framework
knowledge to leak into the parts of the index that must stay general.

Detection reads the project's own manifests rather than guessing from
directory names. A repository with a `resources/views` folder is not
necessarily Laravel; one whose `composer.json` requires `laravel/framework`
is.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = ["FrameworkPlugin", "active_plugins", "register", "registered_plugins"]


@runtime_checkable
class FrameworkPlugin(Protocol):
    """Resolves the string-keyed references one framework uses."""

    name: str

    kinds: tuple[str, ...]
    """Reference kinds this plugin can resolve, such as ``view``."""

    def detect(self, root: Path, files: frozenset[str]) -> bool:
        """Whether this repository uses the framework."""
        ...

    def resolve(
        self, kind: str, name: str, *, from_path: str, files: frozenset[str]
    ) -> str | None:
        """The file a conventional name refers to, or ``None``.

        Returning ``None`` is the normal answer for a name whose target is
        not in this repository, and it must stay cheap: every unresolved
        reference in every template asks this question.
        """
        ...


_REGISTRY: list[FrameworkPlugin] = []


def register(plugin: FrameworkPlugin) -> FrameworkPlugin:
    """Add a plugin to the registry, replacing one of the same name."""
    global _REGISTRY
    _REGISTRY = [existing for existing in _REGISTRY if existing.name != plugin.name]
    _REGISTRY.append(plugin)
    return plugin


def registered_plugins() -> tuple[FrameworkPlugin, ...]:
    return tuple(_REGISTRY)


def active_plugins(
    root: Path, files: Iterable[str]
) -> tuple[FrameworkPlugin, ...]:
    """Which registered plugins recognise this repository.

    Detection runs once per index, not per file, because reading a manifest
    for every one of fifty thousand files would cost more than the edges are
    worth.
    """
    known = frozenset(files)
    return tuple(
        plugin for plugin in _REGISTRY if _safe_detect(plugin, root, known)
    )


def _safe_detect(plugin: FrameworkPlugin, root: Path, files: frozenset[str]) -> bool:
    """Detection must never take the index down with it.

    A plugin reads files it does not control, and a malformed manifest is
    common enough that failing the whole run over one would be wrong. A
    plugin that cannot decide is simply not active.
    """
    try:
        return plugin.detect(root, files)
    except Exception:  # pragma: no cover - defensive
        return False
