"""Laravel's naming conventions, as resolution rules.

Four kinds of string name a file in a Laravel project, and none of them
looks like a reference to any parser:

``view('users.index')``
    Dots are directories under the view root:
    ``resources/views/users/index.blade.php``.

``@extends('layouts.app')`` and ``@include('partials.header')``
    The same rule, written in Blade instead of PHP.

``<x-alert />`` and ``<x-forms.input />``
    An anonymous component under ``resources/views/components``, or a class
    under ``app/View/Components``. Laravel tries both, so this does too.

The rules are conventions, not guesses: either the file the convention
names exists in the repository or it does not, and an edge is only claimed
when it does. That is why a resolved one is worth as much as a resolved
import, and why an unresolved one is silence rather than a weaker guess.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .base import register

__all__ = ["LaravelPlugin"]

# Where a Laravel project keeps its views. Configurable in principle, by a
# `view.paths` entry that is PHP rather than data, so a reader cannot parse
# it. These are the defaults every Laravel project starts with.
_VIEW_ROOTS = ("resources/views",)
_COMPONENT_ROOTS = ("resources/views/components",)
_COMPONENT_CLASS_ROOTS = ("app/View/Components",)
_BLADE_SUFFIXES = (".blade.php", ".blade.phtml", ".php")


@dataclass(slots=True)
class LaravelPlugin:
    """Resolves Laravel's view, include and component names."""

    name: str = "laravel"
    kinds: tuple[str, ...] = ("view", "extends", "include", "component")
    view_roots: tuple[str, ...] = field(default=_VIEW_ROOTS)

    def detect(self, root: Path, files: frozenset[str]) -> bool:
        """Whether this repository requires Laravel.

        Read from `composer.json`, because that is where the project states
        it. A `resources/views` directory proves nothing: plenty of projects
        have one, and a Laravel package may have none.
        """
        manifest = root / "composer.json"
        if not manifest.is_file():
            return False
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return False
        required = {
            *(data.get("require") or {}),
            *(data.get("require-dev") or {}),
        }
        return any(
            package in required
            for package in (
                "laravel/framework",
                "illuminate/view",
                "illuminate/support",
                "laravel/lumen-framework",
            )
        )

    def resolve(
        self, kind: str, name: str, *, from_path: str, files: frozenset[str]
    ) -> str | None:
        """Turn one conventional name into a repository-relative path."""
        cleaned = name.strip()
        if not cleaned:
            return None
        if kind == "component":
            return self._component(cleaned, files)
        if kind in ("view", "extends", "include"):
            return self._view(cleaned, files)
        return None

    def _view(self, name: str, files: frozenset[str]) -> str | None:
        """`users.index` under a view root, with dots as directories.

        A name may already contain slashes when it came from a package
        namespace such as `mail::message`, which no local file answers, so
        anything with a namespace separator is left alone.
        """
        if "::" in name:
            return None
        relative = name.replace(".", "/")
        for root in self.view_roots:
            for suffix in _BLADE_SUFFIXES:
                candidate = f"{root}/{relative}{suffix}"
                if candidate in files:
                    return candidate
        return None

    def _component(self, tag: str, files: frozenset[str]) -> str | None:
        """`<x-forms.input>` as either an anonymous view or a component class.

        Laravel looks for the view first and the class second, and so does
        this. A tag without the `x-` prefix is a plain HTML element or a
        framework component from elsewhere, and is not ours to resolve.
        """
        if not tag.startswith("x-"):
            return None
        relative = tag[2:].replace(".", "/")
        if not relative:
            return None
        for root in _COMPONENT_ROOTS:
            for suffix in _BLADE_SUFFIXES:
                candidate = f"{root}/{relative}{suffix}"
                if candidate in files:
                    return candidate
            # `<x-alert>` may be a directory holding an `index` view.
            for suffix in _BLADE_SUFFIXES:
                candidate = f"{root}/{relative}/index{suffix}"
                if candidate in files:
                    return candidate
        for root in _COMPONENT_CLASS_ROOTS:
            candidate = f"{root}/{_studly(relative)}.php"
            if candidate in files:
                return candidate
        return None


def _studly(path: str) -> str:
    """`forms/input-group` to `Forms/InputGroup`, Laravel's class naming."""
    parts = []
    for segment in path.split("/"):
        words = segment.replace("_", "-").split("-")
        parts.append("".join(word[:1].upper() + word[1:] for word in words if word))
    return "/".join(parts)


register(LaravelPlugin())
