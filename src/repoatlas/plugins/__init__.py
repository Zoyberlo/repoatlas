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

Each framework gets a directory under `frameworks/`, holding the naming
conventions it uses and, when data is not enough, the code that reads
something else. Adding support for a framework is adding a directory;
nothing central lists them.
"""

from __future__ import annotations

from .base import FrameworkPlugin, active_plugins, register, registered_plugins
from .frameworks import framework_names, framework_plugins, frameworks_source
from .registry import ConventionPlugin, Framework, load_framework

__all__ = [
    "ConventionPlugin",
    "Framework",
    "FrameworkPlugin",
    "active_plugins",
    "framework_names",
    "framework_plugins",
    "frameworks_source",
    "load_framework",
    "register",
    "registered_plugins",
]

for _plugin in framework_plugins():
    register(_plugin)
del _plugin
