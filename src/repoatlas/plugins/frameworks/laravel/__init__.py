"""Laravel: Blade views, layouts, partials, components and Livewire tags.

Everything here is naming conventions, so everything is in
`conventions.json`. Named routes are not, and when they arrive they will be
a module beside this one whose plugin joins the tuple below: `route('users.show')`
resolves through a table read from `routes/*.php`, not through a path
template.
"""

from __future__ import annotations

from ...base import FrameworkPlugin
from ..support import convention_plugins


def plugins() -> tuple[FrameworkPlugin, ...]:
    """What Laravel contributes to the index."""
    return convention_plugins(__file__)
