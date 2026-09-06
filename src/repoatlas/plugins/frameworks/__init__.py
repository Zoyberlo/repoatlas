"""One directory per framework, discovered rather than listed.

Adding support for a framework is adding a directory here with a
``conventions.json`` and an ``__init__.py``. Nothing central needs editing,
which is the point: a list of frameworks kept somewhere else is a list that
eventually disagrees with what is on disk.

The unit is a framework rather than a language, because a framework spans
languages and a language does not span frameworks. Laravel's conventions
are written in PHP and in Blade, and splitting them by language would put
one framework's rules in two places and let a change touch one of them.

A directory holds everything that framework needs. Today that is a JSON
file of naming conventions. When a framework needs more than data, and
Laravel's named routes will, the code goes in the same directory and
``plugins()`` returns it alongside.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from ..base import FrameworkPlugin

__all__ = [
    "framework_names",
    "framework_plugins",
    "frameworks_source",
    "manifest_names",
]

_HERE = Path(__file__).parent


def framework_names() -> tuple[str, ...]:
    """Every framework directory, in the order their rules are tried."""
    return tuple(
        sorted(
            name
            for _finder, name, is_package in pkgutil.iter_modules([str(_HERE)])
            if is_package
        )
    )


def framework_plugins() -> tuple[FrameworkPlugin, ...]:
    """What every framework contributes, in directory-name order.

    A package that will not import stops the index rather than being
    skipped. A framework silently absent is a class of edges silently
    missing, and that is the failure this project exists to avoid.
    """
    found: list[FrameworkPlugin] = []
    for name in framework_names():
        module = importlib.import_module(f"{__name__}.{name}")
        contributed = module.plugins()
        found.extend(contributed)
    return tuple(found)


def frameworks_source() -> str:
    """Everything under this directory, as one text, for the toolchain stamp.

    A store's edges depend on these rules, and a no-op re-index skips
    resolution entirely. Without them in the stamp, editing a convention or
    a framework's code would leave every edge it used to produce in place
    and every edge it newly allows missing, with nothing to show that
    anything had changed.

    Paths are included, so adding, removing or renaming a file changes the
    stamp even when nothing inside one did.
    """
    parts = []
    for path in sorted(_HERE.rglob("*")):
        if path.suffix not in (".json", ".py") or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(_HERE).as_posix()
        parts.append(f"{relative}\n{path.read_text(encoding='utf-8')}")
    return "\n".join(parts)


def manifest_names() -> tuple[str, ...]:
    """Every manifest file framework detection reads, deduplicated.

    Asked for by the git-object index, which has no working tree to read
    manifests from and so must write out the few that could matter. Derived
    from the registry rather than listed by hand, because a hand-written
    list is one that goes stale the first time a framework is added and
    then silently disables that framework's conventions.
    """
    names: list[str] = []
    for plugin in framework_plugins():
        framework = getattr(plugin, "framework", None)
        for clause in getattr(framework, "detect", ()):
            name = getattr(clause, "file", None)
            if isinstance(name, str) and name not in names:
                names.append(name)
    return tuple(names)
