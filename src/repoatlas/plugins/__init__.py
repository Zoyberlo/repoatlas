"""Framework conventions: the edges no parser can see.

`view('users.index')` is a string to every parser that has ever read it. To
Laravel it is `resources/views/users/index.blade.php`, and that link is
often the most important one in the file: it is how a controller reaches
the thing a user actually looks at.

No general index can know this, because the knowledge is not in the
language. It is in a framework's conventions, and each framework has its
own. So the parser records what it saw, a reference of kind ``view`` whose
name is ``users.index``, and a plugin that recognises the project turns
that into a path.

A plugin is deliberately small: recognise a repository, and answer what
file a conventional name refers to. Everything else, extraction, ranking,
storage, is unchanged by adding one.
"""

from __future__ import annotations

from .base import FrameworkPlugin, active_plugins, register
from .laravel import LaravelPlugin

__all__ = ["FrameworkPlugin", "LaravelPlugin", "active_plugins", "register"]
