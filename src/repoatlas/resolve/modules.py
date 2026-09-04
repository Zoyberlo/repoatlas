"""Turning an import path into a file in the repository.

``import { User } from "./user"`` names a module, not a file. Which file it
is depends on the ecosystem: Node probes a list of extensions and index
files and consults ``tsconfig.json`` path aliases, Python maps dots to
directories and looks for ``__init__.py``, Composer maps a namespace prefix
to a directory through PSR-4.

Each resolver answers one question, ``which indexed file does this import
name``, and answers ``None`` rather than guessing when the target is a
third-party package. That distinction is what keeps the top rung of the
cascade honest: an import that resolves to a file gives a 0.95 edge, and one
that does not falls through to weaker evidence instead of inventing a link.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

# Where a project's own configuration may sit. Shared with framework
# detection because it is the same question: a repository is often
# several projects, and each keeps its settings at its own root.
from ..plugins.registry import project_directories

__all__ = [
    "ComposerResolver",
    "ModuleResolver",
    "NodeResolver",
    "PythonResolver",
    "resolver_for",
]


class ModuleResolver(Protocol):
    """Maps an import path to a repository-relative file path."""

    def resolve(self, module: str, *, from_path: str, relative_level: int = 0) -> str | None:
        """Return the file ``module`` names, or ``None`` if it is external."""
        ...


def _parent(path: str, levels: int = 1) -> PurePosixPath:
    parent = PurePosixPath(path).parent
    for _ in range(levels - 1):
        parent = parent.parent
    return parent


def _normalise(path: PurePosixPath) -> str:
    """Collapse ``.`` and ``..`` without touching the filesystem."""
    parts: list[str] = []
    for part in path.parts:
        if part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


@dataclass(slots=True)
class NodeResolver:
    """Node and TypeScript module resolution, as much as matters here.

    Implements the parts that decide whether a path names a file in this
    repository: relative paths with extension probing and index files, and
    ``tsconfig.json`` ``baseUrl`` and ``paths`` aliases. Package resolution
    through ``node_modules`` is deliberately absent, because a dependency is
    not a file this index covers.
    """

    known_files: frozenset[str]
    base_urls: tuple[str, ...] = ()
    """Where a bare specifier is resolved from, one entry per project.

    A repository is often several: a Quasar front end keeps its own
    `jsconfig.json`, and reading only the repository root found none of its
    aliases and left every aliased import unresolved.
    """

    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)

    EXTENSIONS = (
        ".ts",
        ".tsx",
        ".d.ts",
        ".mts",
        ".cts",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        # A bundler resolves `./MyButton` to `MyButton.vue`; Vue's own
        # documentation writes the extension, but both appear in real
        # projects.
        ".vue",
    )
    INDEX_NAMES = ("index.ts", "index.tsx", "index.js", "index.jsx", "index.mjs")

    def _probe(self, base: str) -> str | None:
        """Try a path as written, then with each extension, then as a directory."""
        if not base:
            return None
        if base in self.known_files:
            return base
        # An import may already carry the extension it will be built to.
        for suffix in (".js", ".mjs", ".cjs"):
            if base.endswith(suffix):
                stem = base[: -len(suffix)]
                for extension in (".ts", ".tsx", ".mts", ".cts"):
                    if stem + extension in self.known_files:
                        return stem + extension
        for extension in self.EXTENSIONS:
            if base + extension in self.known_files:
                return base + extension
        for index in self.INDEX_NAMES:
            candidate = f"{base}/{index}"
            if candidate in self.known_files:
                return candidate
        return None

    def resolve(self, module: str, *, from_path: str, relative_level: int = 0) -> str | None:
        if not module:
            return None
        if module.startswith("."):
            base = _normalise(_parent(from_path) / module)
            return self._probe(base)
        for prefix, targets in self.aliases.items():
            rest = _match_alias(prefix, module)
            if rest is None:
                continue
            for target in targets:
                candidate = target.replace("*", rest) if "*" in target else target
                found = self._probe(_normalise(PurePosixPath(candidate)))
                if found is not None:
                    return found
        for base in self.base_urls:
            found = self._probe(
                _normalise(PurePosixPath(base) / module) if base else module
            )
            if found is not None:
                return found
        # A bare specifier with no alias is a package, not a file here.
        return None


def _match_alias(pattern: str, module: str) -> str | None:
    """Match a tsconfig path pattern, returning what ``*`` captured."""
    if "*" not in pattern:
        return "" if pattern == module else None
    head, _, tail = pattern.partition("*")
    if not module.startswith(head) or not module.endswith(tail):
        return None
    captured = module[len(head) : len(module) - len(tail) if tail else None]
    return captured


@dataclass(slots=True)
class PythonResolver:
    """Dotted module paths to files, including relative imports.

    Roots are the directories a module path is counted from: the repository
    itself plus conventional source directories, since a project laid out
    with ``src/`` writes ``import mypkg`` and not ``import src.mypkg``.
    """

    known_files: frozenset[str]
    roots: tuple[str, ...] = ("", "src", "lib")

    def _probe(self, base: str) -> str | None:
        if not base:
            return None
        for candidate in (f"{base}.py", f"{base}/__init__.py", f"{base}.pyi"):
            if candidate in self.known_files:
                return candidate
        return None

    def resolve(self, module: str, *, from_path: str, relative_level: int = 0) -> str | None:
        if relative_level > 0:
            base = _parent(from_path, relative_level)
            tail = module.replace(".", "/") if module else ""
            return self._probe(_normalise(base / tail if tail else base))
        if not module:
            return None
        tail = module.replace(".", "/")
        for root in self.roots:
            found = self._probe(_normalise(PurePosixPath(root) / tail) if root else tail)
            if found is not None:
                return found
        # `from a.b import C` may name a module `a/b.py` or a symbol `C`
        # inside `a.py`; the caller tries the symbol form separately.
        return None


@dataclass(slots=True)
class ComposerResolver:
    """PSR-4 namespace prefixes to directories, read from composer.json.

    Composer resolves a class name by finding the longest matching namespace
    prefix and joining the rest as a path, so the prefixes are searched
    longest first.
    """

    known_files: frozenset[str]
    prefixes: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def resolve(self, module: str, *, from_path: str, relative_level: int = 0) -> str | None:
        name = module.strip("\\")
        if not name:
            return None
        for prefix, directories in self.prefixes:
            if prefix and not name.startswith(prefix):
                continue
            rest = name[len(prefix) :].strip("\\").replace("\\", "/")
            if not rest:
                continue
            for directory in directories:
                candidate = _normalise(PurePosixPath(directory) / f"{rest}.php")
                if candidate in self.known_files:
                    return candidate
        return None


def _compiler_options(config_path: Path, data: object, depth: int = 0) -> dict[str, object]:
    """``compilerOptions`` with those of a local ``extends`` chain folded in.

    A `tsconfig.json` that extends `./tsconfig.base.json` declares its
    aliases in the base, and reading the child alone finds none. Only
    relative parents are followed; a package such as `@vue/tsconfig` holds
    no paths of this repository's. The child wins on every key, and on
    every alias, one by one.
    """
    if not isinstance(data, dict):
        return {}
    options: dict[str, object] = dict(data.get("compilerOptions") or {})
    parent = data.get("extends")
    if not isinstance(parent, str) or not parent.startswith(".") or depth >= 5:
        return options
    parent_path = config_path.parent / parent
    if parent_path.suffix != ".json":
        parent_path = parent_path.with_suffix(".json")
    try:
        parent_data = json.loads(
            _strip_json_comments(parent_path.read_text(encoding="utf-8-sig"))
        )
    except (OSError, json.JSONDecodeError):
        return options
    inherited = _compiler_options(parent_path, parent_data, depth + 1)
    merged = dict(inherited)
    merged.update({key: value for key, value in options.items() if key != "paths"})
    own_paths = options.get("paths")
    if isinstance(own_paths, dict):
        base_paths = inherited.get("paths")
        merged["paths"] = {
            **(base_paths if isinstance(base_paths, dict) else {}),
            **own_paths,
        }
    return merged


def _read_tsconfig(
    root: Path, directories: Iterable[str] = ("",)
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """Read every project's ``baseUrl`` and ``paths`` into one alias table.

    ``tsconfig.json`` is JSON with comments by convention, which the standard
    parser rejects, so a failed parse means "no aliases" rather than an
    error: path aliases are an optimisation for the top rung, not a
    requirement.

    Both names are read, and `jsconfig.json` is not an afterthought: a
    Quasar application has only that one, and it is where `src/*` and
    `components/*` are declared. Targets are rewritten relative to the
    repository, so a front end in `frontend/` contributes
    `frontend/src/*` and one alias table serves the whole walk.
    """
    bases: list[str] = []
    aliases: dict[str, list[str]] = {}
    for where in directories:
        for name in ("tsconfig.json", "jsconfig.json"):
            config_path = (root / where / name) if where else (root / name)
            if not config_path.is_file():
                continue
            try:
                raw = config_path.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            try:
                data = json.loads(_strip_json_comments(raw))
            except json.JSONDecodeError:
                continue
            options = _compiler_options(config_path, data)
            declared = str(options.get("baseUrl") or "").strip("./")
            base = _normalise(PurePosixPath(where) / declared) if where else declared
            if base not in bases:
                bases.append(base)
            declared_paths = options.get("paths")
            if not isinstance(declared_paths, dict):
                declared_paths = {}
            for pattern, targets in declared_paths.items():
                if not isinstance(targets, list):
                    continue
                for target in targets:
                    prefix = base + "/" if base else ""
                    resolved = _normalise(
                        PurePosixPath(prefix + str(target).lstrip("./"))
                    )
                    aliases.setdefault(pattern, []).append(resolved)
            break
    return tuple(bases), {
        pattern: tuple(dict.fromkeys(targets)) for pattern, targets in aliases.items()
    }


def _strip_json_comments(text: str) -> str:
    """Remove `//` and `/* */` comments and trailing commas."""
    out: list[str] = []
    index = 0
    in_string = False
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if text.startswith("//", index):
            index = text.find("\n", index)
            if index == -1:
                break
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        out.append(char)
        index += 1
    cleaned = "".join(out)
    # Trailing commas before a closing brace or bracket.
    result: list[str] = []
    for position, char in enumerate(cleaned):
        if char == ",":
            rest = cleaned[position + 1 :].lstrip()
            if rest[:1] in ("}", "]"):
                continue
        result.append(char)
    return "".join(result)


def _read_composer(root: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Read PSR-4 prefixes from composer.json, longest prefix first."""
    config_path = root / "composer.json"
    if not config_path.is_file():
        return ()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ()
    prefixes: list[tuple[str, tuple[str, ...]]] = []
    for section in ("autoload", "autoload-dev"):
        block = data.get(section) or {}
        for scheme in ("psr-4", "psr-0"):
            for prefix, target in (block.get(scheme) or {}).items():
                targets = target if isinstance(target, list) else [target]
                prefixes.append(
                    (
                        str(prefix),
                        tuple(str(item).strip("/") for item in targets),
                    )
                )
    prefixes.sort(key=lambda pair: len(pair[0]), reverse=True)
    return tuple(prefixes)


def resolver_for(
    language: str, root: Path, known_files: frozenset[str]
) -> ModuleResolver | None:
    """Build the resolver for a language, reading its project configuration."""
    if language in ("typescript", "tsx", "javascript", "vue"):
        bases, aliases = _read_tsconfig(root, project_directories(known_files))
        return NodeResolver(known_files=known_files, base_urls=bases, aliases=aliases)
    if language == "python":
        return PythonResolver(known_files=known_files)
    if language == "php":
        return ComposerResolver(known_files=known_files, prefixes=_read_composer(root))
    return None
