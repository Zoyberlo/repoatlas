"""Reading the convention registry, and resolving names with it.

This is where a naming convention stops being prose and starts being a
lookup: `frameworks/laravel/conventions.json` says a `view` name goes under
`resources/views` with dots as directories, and this builds the paths that
rule allows and keeps the one that is a real file.

Keeping the rules as data rather than as a class per framework is a bet
about where the work goes. There are four rules here and there will be
forty; each is a few lines of description and none of them needs a branch.
Nothing else in the index changes when one is added.

A framework that needs more than data writes code in its own directory
instead, and this module never learns about it. See `frameworks/`.

The format
==========

A framework's `conventions.json` has a ``name``, a list of ``detect``
clauses and a list of ``rules``.

Detection
---------

``detect`` clauses are tried in order and any one is enough. Each names a
JSON manifest ``file``, the ``fields`` inside it whose *keys* are package
names, and the ``packages`` whose presence means the project uses the
framework. JSON is the only format because `composer.json` and
`package.json` are the only manifests this reads; a Python or Ruby
framework would add a format here, and that is a small tested change rather
than a branch kept warm on the chance somebody needs it.

Detection reads what a project declares, never what its directories are
called. A repository with a `resources/views` folder is not Laravel.

It also reads *where*. A repository is often several projects: this index
was first run on a real one whose Laravel lives in `backend/` and whose
Quasar front end lives in `frontend/`, with a `package.json` at the top
holding a single unrelated dependency. Looking only at the repository root
found neither framework and silently dropped every convention edge in the
project. So a manifest is looked for in the root and in the directories
above the files being indexed, and the directory that holds it becomes the
prefix every candidate path is built inside.

Rules
-----

A ``rule`` claims some reference ``kinds``, optionally narrows itself to the
``languages`` whose files may use it, and says how to read the name:

``separator``
    What the framework writes between path segments: ``.`` for Laravel,
    and empty for a Vue tag, whose name is one segment.
``prefix``
    A prefix the name must carry and that is not part of the path: ``x-``
    for a Blade component, ``livewire:`` for a Livewire one.
``reject`` / ``reject_prefix``
    Substrings, or leading strings, that mean the name is not ours. ``::``
    is a view from a package; ``q-`` is one of Quasar's own components,
    which ship in `node_modules` rather than in the repository.

``languages`` matters when two frameworks claim the same kind, which
Laravel and Vue both do for ``component``. Without it a Blade tag would be
offered to Vue's rule and the answer would depend on the order they happen
to sit in the file.

Each ``candidate`` under a rule is one place the file could be, tried in
order, in one of two shapes:

An exact path
    ``root`` is a directory and ``path`` is the name inside it, written as
    ``{path}`` for the name with separators turned into slashes, or
    ``{studly}`` for the class-name spelling Laravel uses under
    `app/View/Components`. ``suffixes`` are extensions to try.

A search by name
    ``under`` is a directory and ``stem`` is the file's basename. This
    finds the file at any depth below that directory, which is what a
    build step that auto-imports components does: `<UserCard />` is
    `src/components/UserCard.vue` whether or not it sits in a subfolder.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_PROJECT_DEPTH",
    "Candidate",
    "ConventionPlugin",
    "Framework",
    "RegistryError",
    "Rule",
    "load_framework",
    "project_directories",
]

class RegistryError(ValueError):
    """The registry file says something this code cannot act on."""


def _dig(data: Any, dotted: str) -> Any:
    """Follow a dotted path through nested mappings, or return ``None``."""
    current = data
    for segment in dotted.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
    return current


# How far below the repository root a project's manifest is looked for.
# One level covers `backend/` and `frontend/`, two covers `apps/api/`, and
# beyond that a manifest is a dependency's own rather than a project's.
MAX_PROJECT_DEPTH = 2


def project_directories(files: Iterable[str]) -> list[str]:
    """The directories a project manifest could sit in, nearest the root first.

    Derived from the files being indexed rather than from a filesystem
    walk: a directory holding no source is not a project, and walking a
    repository that has `node_modules` in it is how an index becomes slow.
    """
    found: set[str] = {""}
    for path in files:
        parts = path.split("/")[:-1]
        for depth in range(1, min(len(parts), MAX_PROJECT_DEPTH) + 1):
            found.add("/".join(parts[:depth]))
    return sorted(found, key=lambda item: (item.count("/") if item else -1, item))


@dataclass(frozen=True, slots=True)
class Detection:
    """One manifest that would prove a project uses a framework."""

    file: str
    packages: frozenset[str]
    fields: tuple[str, ...] = ()

    def roots(self, root: Path, directories: Iterable[str]) -> list[str]:
        """The directories whose manifest declares one of these packages."""
        return [where for where in directories if self._declares(root, where)]

    def _declares(self, root: Path, where: str) -> bool:
        manifest = root / where / self.file if where else root / self.file
        try:
            text = manifest.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return False
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Projects do commit broken manifests, and one must never take
            # an index down. A file that will not parse declares nothing.
            return False
        for dotted in self.fields:
            found = _dig(data, dotted)
            if isinstance(found, Mapping) and self.packages & {
                str(key).lower() for key in found
            }:
                return True
        return False


@dataclass(frozen=True, slots=True)
class Candidate:
    """One place a conventional name could name a file."""

    suffixes: tuple[str, ...]
    root: str = ""
    """The directory an exact path is built inside."""

    path: str = ""
    """The path within ``root``, or empty when this searches by name."""

    under: str = ""
    """The directory a search by name is confined to."""

    stem: str = ""
    """The basename to search for, when this searches by name."""

    @property
    def searches_by_name(self) -> bool:
        return bool(self.stem)


@dataclass(frozen=True, slots=True)
class Rule:
    """How one framework reads the names of one kind of reference."""

    kinds: frozenset[str]
    candidates: tuple[Candidate, ...]
    languages: frozenset[str] = frozenset()
    separator: str = "."
    prefix: str = ""
    reject: tuple[str, ...] = ()
    reject_prefix: tuple[str, ...] = ()

    def claims(self, kind: str, language: str | None) -> bool:
        if kind not in self.kinds:
            return False
        return not self.languages or language is None or language in self.languages

    def relative(self, name: str) -> str | None:
        """The path segments a name means, or ``None`` if it means none.

        Returning ``None`` is the ordinary answer for a name this rule does
        not own: one of Quasar's own components, or a view from a package
        that lives outside the repository.
        """
        if any(marker in name for marker in self.reject):
            return None
        if any(name.startswith(marker) for marker in self.reject_prefix):
            return None
        if self.prefix:
            if not name.startswith(self.prefix):
                return None
            name = name[len(self.prefix) :]
        if not name:
            return None
        return name.replace(self.separator, "/") if self.separator else name


@dataclass(frozen=True, slots=True)
class Framework:
    """A framework's whole entry in the registry."""

    name: str
    detect: tuple[Detection, ...]
    rules: tuple[Rule, ...]
    summary: str = ""
    returns_receiver: tuple[str, ...] = ()
    """Methods the framework supplies that return the receiver's own type.

    `Ad::find(1)` is an Ad and `$ad->fresh()` is one too, though neither
    method is written in the model: Eloquent provides them. Named here so
    the resolver can type a local assigned from one, for a class that does
    not itself declare the method.
    """

    @property
    def kinds(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for rule in self.rules:
            for kind in rule.kinds:
                seen[kind] = None
        return tuple(seen)


def _strings(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RegistryError(f"{what} is a list of strings, not {value!r}")
    return tuple(value)


def _candidate_from(data: Mapping[str, Any]) -> Candidate:
    suffixes = _strings(data.get("suffixes", [""]), "suffixes")
    under = data.get("under")
    if under is not None:
        stem = data.get("stem")
        if not isinstance(under, str) or not isinstance(stem, str) or not stem:
            raise RegistryError(f"a search by name needs `under` and `stem`: {data!r}")
        return Candidate(suffixes=suffixes, under=under.strip("/"), stem=stem)
    root = data.get("root", "")
    path = data.get("path", "{path}")
    if not isinstance(root, str) or not isinstance(path, str) or not path:
        raise RegistryError(f"an exact candidate needs `root` and `path`: {data!r}")
    return Candidate(suffixes=suffixes, root=root.strip("/"), path=path)


def _rule_from(data: Mapping[str, Any]) -> Rule:
    kinds = data.get("kinds")
    if not isinstance(kinds, list) or not kinds:
        raise RegistryError(f"a rule needs the kinds it claims, got {kinds!r}")
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise RegistryError(f"a rule needs candidates, got {candidates!r}")
    return Rule(
        kinds=frozenset(str(kind) for kind in kinds),
        candidates=tuple(_candidate_from(item) for item in candidates),
        languages=frozenset(_strings(data.get("languages", []), "languages")),
        separator=str(data.get("separator", ".")),
        prefix=str(data.get("prefix", "")),
        reject=_strings(data.get("reject", []), "reject"),
        reject_prefix=_strings(data.get("reject_prefix", []), "reject_prefix"),
    )


def _detection_from(data: Mapping[str, Any]) -> Detection:
    file = data.get("file")
    if not isinstance(file, str) or not file:
        raise RegistryError(f"a detection needs a file, got {file!r}")
    packages = data.get("packages")
    if not isinstance(packages, list) or not packages:
        raise RegistryError(f"a detection needs packages, got {packages!r}")
    return Detection(
        file=file,
        packages=frozenset(str(item).lower() for item in packages),
        fields=_strings(data.get("fields", []), "fields"),
    )


def load_framework(source: str) -> Framework:
    """Read one framework's file, raising if it says something unusable.

    Failing loudly is deliberate. A typo in a rule would otherwise show up
    as an index quietly missing a whole class of edges, which is the sort
    of wrong that no test notices.
    """
    try:
        entry = json.loads(source)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"not readable as JSON: {exc}") from exc
    if not isinstance(entry, Mapping):
        raise RegistryError(f"a convention file holds one framework, not {entry!r}")
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise RegistryError(f"a framework needs a name, got {name!r}")
    return Framework(
        name=name,
        summary=str(entry.get("summary", "")),
        detect=tuple(_detection_from(item) for item in entry.get("detect", ())),
        rules=tuple(_rule_from(item) for item in entry.get("rules", ())),
        returns_receiver=_strings(entry.get("returns_receiver", []), "returns_receiver"),
    )


def _studly(path: str) -> str:
    """`forms/input-group` to `Forms/InputGroup`, the class-name spelling.

    Laravel uses it to find `app/View/Components/Forms/InputGroup.php` from
    `<x-forms.input-group />`; Vue uses it to know that `<user-card />` and
    `<UserCard />` are both `UserCard.vue`.
    """
    segments = []
    for segment in path.split("/"):
        words = segment.replace("_", "-").split("-")
        segments.append("".join(word[:1].upper() + word[1:] for word in words if word))
    return "/".join(segments)


@dataclass(slots=True)
class ConventionPlugin:
    """One framework's entry, doing the work the plugin protocol asks for."""

    framework: Framework
    _indexed: frozenset[str] | None = field(default=None, repr=False)
    _by_basename: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    _roots_for: frozenset[str] | None = field(default=None, repr=False)
    _roots: tuple[str, ...] = field(default=(), repr=False)
    """Where in the repository this framework lives, as path prefixes.

    Found by :meth:`detect` and used by :meth:`resolve`, which the plugin
    protocol always calls in that order with the same file set. A resolve
    against any other set falls back to the repository root, which is what
    a single-project repository has anyway.
    """

    @property
    def name(self) -> str:
        return self.framework.name

    @property
    def kinds(self) -> tuple[str, ...]:
        return self.framework.kinds

    def returns_receiver(self, callee: str) -> bool:
        """Whether the framework's ``callee`` returns the receiver's own type."""
        return callee in self.framework.returns_receiver

    def detect(self, root: Path, files: frozenset[str]) -> bool:
        directories = project_directories(files)
        found: list[str] = []
        for clause in self.framework.detect:
            for where in clause.roots(root, directories):
                if where not in found:
                    found.append(where)
        self._roots_for = files
        self._roots = tuple(found)
        return bool(found)

    def roots_in(self, files: frozenset[str]) -> tuple[str, ...]:
        """Where this framework lives, for the file set detection saw."""
        if self._roots_for is not files:
            return ("",)
        return self._roots or ("",)

    def resolve(
        self,
        kind: str,
        name: str,
        *,
        from_path: str,
        language: str | None = None,
        files: frozenset[str],
    ) -> str | None:
        cleaned = name.strip()
        if not cleaned:
            return None
        roots = _nearest_first(self.roots_in(files), from_path)
        for rule in self.framework.rules:
            if not rule.claims(kind, language):
                continue
            relative = rule.relative(cleaned)
            if relative is None:
                continue
            found = self._first_existing(rule, relative, files, roots)
            if found is not None:
                return found
        return None

    def _first_existing(
        self, rule: Rule, relative: str, files: frozenset[str], roots: tuple[str, ...]
    ) -> str | None:
        studly = _studly(relative)
        for project in roots:
            for candidate in rule.candidates:
                if candidate.searches_by_name:
                    found = self._by_name(candidate, relative, studly, files, project)
                else:
                    found = self._at_path(candidate, relative, studly, files, project)
                if found is not None:
                    return found
        return None

    def _at_path(
        self,
        candidate: Candidate,
        relative: str,
        studly: str,
        files: frozenset[str],
        project: str,
    ) -> str | None:
        tail = candidate.path.format(path=relative, studly=studly)
        stem = f"{candidate.root}/{tail}" if candidate.root else tail
        if project:
            stem = f"{project}/{stem}"
        for suffix in candidate.suffixes:
            full = f"{stem}{suffix}"
            if full in files:
                return full
        return None

    def _by_name(
        self,
        candidate: Candidate,
        relative: str,
        studly: str,
        files: frozenset[str],
        project: str,
    ) -> str | None:
        """Find a file by its basename, anywhere below a directory.

        A build step that auto-imports components does not care how deeply
        they are nested, so neither can this. Matching by scanning every
        path for every reference would be quadratic, so the basenames are
        indexed once and kept until the file set is replaced.
        """
        stem = candidate.stem.format(path=relative, studly=studly)
        prefix = f"{candidate.under}/" if candidate.under else ""
        if project:
            prefix = f"{project}/{prefix}"
        for suffix in candidate.suffixes:
            for path in self._paths_named(f"{stem}{suffix}", files):
                if path.startswith(prefix):
                    return path
        return None

    def _paths_named(self, basename: str, files: frozenset[str]) -> tuple[str, ...]:
        if self._indexed is not files:
            self._indexed = files
            index: dict[str, list[str]] = {}
            for path in files:
                index.setdefault(path.rpartition("/")[2], []).append(path)
            self._by_basename = {
                name: tuple(sorted(paths)) for name, paths in index.items()
            }
        return self._by_basename.get(basename, ())


def _nearest_first(roots: tuple[str, ...], from_path: str) -> tuple[str, ...]:
    """Order the projects so the one holding the reference is tried first.

    A `view()` written in `backend/app/` means a template in `backend`,
    even in a repository where a second Laravel app would answer the same
    name. Without this the answer would depend on directory order.
    """
    if len(roots) < 2:
        return roots
    inside = [where for where in roots if where and from_path.startswith(f"{where}/")]
    if not inside:
        return roots
    rest = [where for where in roots if where not in inside]
    return (*sorted(inside, key=len, reverse=True), *rest)


def plugins_from(frameworks: Iterable[Framework]) -> tuple[ConventionPlugin, ...]:
    """Wrap registry entries as plugins, in the order they were written."""
    return tuple(ConventionPlugin(framework) for framework in frameworks)
