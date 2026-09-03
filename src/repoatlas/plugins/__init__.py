"""Framework conventions: the edges no parser can see.

`view('users.index')` is a string to every parser that has ever read it. To
Laravel it is `resources/views/users/index.blade.php`, and that link is
often the most important one in the file: it is how a controller reaches
the thing a user actually looks at. Django writes the same idea as
`render(request, 'polls/index.html')`, and means a file under some app's
`templates` directory.

No general index can know this, because the knowledge is not in the
language. It is in a framework's conventions, and each framework has its
own. So the parser records what it saw, a reference of kind ``view`` whose
name is ``users.index``, and a rule that recognises the project turns that
into a path.

The rules live in `conventions/`, one file per framework, beside the tag
queries that are already one per language. There are two today and there
will be more; each is a few lines of description and none needs a branch.
Adding a framework is adding a file.
"""

from __future__ import annotations

from .base import FrameworkPlugin, active_plugins, register, registered_plugins
from .registry import (
    ConventionPlugin,
    Framework,
    load_framework,
    load_registry,
    registry_files,
    registry_source,
)

__all__ = [
    "ConventionPlugin",
    "Framework",
    "FrameworkPlugin",
    "active_plugins",
    "load_framework",
    "load_registry",
    "register",
    "registered_plugins",
    "registry_files",
    "registry_source",
]


def _register_registry() -> None:
    """Put every framework in the registry into the plugin registry."""
    for framework in load_registry():
        register(ConventionPlugin(framework))


_register_registry()
