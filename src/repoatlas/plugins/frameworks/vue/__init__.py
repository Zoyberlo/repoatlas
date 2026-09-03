"""Vue, and the build steps that auto-import its components.

Quasar and its neighbours make `<UserCard />` mean a file nobody imported,
which is the only thing here a parser cannot work out for itself. Anything
a script block does import is resolved by the import map instead.
"""

from __future__ import annotations

from ...base import FrameworkPlugin
from ..support import convention_plugins


def plugins() -> tuple[FrameworkPlugin, ...]:
    """What Vue contributes to the index."""
    return convention_plugins(__file__)
