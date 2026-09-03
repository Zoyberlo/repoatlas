"""What a framework package needs to declare itself.

Every framework directory holds a ``conventions.json`` and an
``__init__.py`` whose ``plugins()`` says what that framework contributes.
For a framework whose whole story is naming conventions, that is one line;
for one that also needs code, the code goes in the same directory and joins
the tuple.
"""

from __future__ import annotations

from pathlib import Path

from ..registry import ConventionPlugin, load_framework

__all__ = ["convention_plugins", "conventions_file"]


def conventions_file(module_file: str) -> Path:
    """The ``conventions.json`` beside a framework's ``__init__.py``."""
    return Path(module_file).with_name("conventions.json")


def convention_plugins(module_file: str) -> tuple[ConventionPlugin, ...]:
    """Read the conventions beside a framework package, as a plugin.

    A framework with no ``conventions.json`` contributes none, which is the
    right answer for one whose rules are all code.
    """
    path = conventions_file(module_file)
    if not path.is_file():
        return ()
    return (ConventionPlugin(load_framework(path.read_text(encoding="utf-8"))),)
