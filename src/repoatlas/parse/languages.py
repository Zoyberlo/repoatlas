"""Which grammar handles which file, and how to load it.

``tree-sitter-language-pack`` ships compiled grammars but no queries, so
RepoAtlas carries its own tag queries in ``queries/``. That is the better
arrangement anyway: the capture names are then this project's contract
rather than whatever each upstream grammar happened to settle on.

Grammar loading is deliberately forgiving. Some grammars in the pack are
fetched on first use rather than bundled, which fails on an offline machine
or a locked-down CI runner. A language that cannot be loaded is reported as
unavailable and its files are skipped, instead of taking the whole index
down with it.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imported only for type checking
    from tree_sitter import Language, Parser

__all__ = [
    "SUPPORTED",
    "LanguageSpec",
    "LanguageUnavailable",
    "available_languages",
    "get_parser",
    "language_for_path",
    "query_source",
]

_QUERY_DIR = Path(__file__).parent / "queries"


class LanguageUnavailable(RuntimeError):
    """Raised when a grammar cannot be loaded on this machine."""


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    """One language RepoAtlas can extract from."""

    name: str
    """RepoAtlas's name for the language, and the stem of its query file."""

    grammar: str
    """The name ``tree_sitter_language_pack`` knows the grammar by."""

    extensions: tuple[str, ...]
    """Lowercase file suffixes, including the dot."""

    filenames: tuple[str, ...] = ()
    """Exact filenames, for languages identified by name rather than suffix."""

    query_name: str = ""
    """Which query file to use, when a language shares one with another.

    TSX differs from TypeScript only in how it parses angle brackets, and
    the node types the tag query names are identical, so both read
    ``typescript.scm``. JavaScript needs its own, because a query naming
    ``interface_declaration`` fails to compile against a grammar that has no
    such node.
    """

    embeds: tuple[str, ...] = field(default=())
    """Languages that may appear inside this one, such as script in Vue."""

    @property
    def query_path(self) -> Path:
        return _QUERY_DIR / f"{self.query_name or self.name}.scm"


# Ordered by how much of the target stack they cover. Each entry needs a
# matching queries/<name>.scm, which `test_languages.py` enforces.
SUPPORTED: tuple[LanguageSpec, ...] = (
    LanguageSpec("python", "python", (".py", ".pyi")),
    LanguageSpec("typescript", "typescript", (".ts", ".mts", ".cts")),
    LanguageSpec("tsx", "tsx", (".tsx",), query_name="typescript"),
    LanguageSpec("javascript", "javascript", (".js", ".mjs", ".cjs", ".jsx")),
    LanguageSpec("php", "php", (".php", ".phtml")),
)

_BY_EXTENSION: dict[str, LanguageSpec] = {}
_BY_FILENAME: dict[str, LanguageSpec] = {}
_BY_NAME: dict[str, LanguageSpec] = {}
for _spec in SUPPORTED:
    _BY_NAME[_spec.name] = _spec
    for _extension in _spec.extensions:
        _BY_EXTENSION[_extension] = _spec
    for _filename in _spec.filenames:
        _BY_FILENAME[_filename] = _spec


def language_for_path(path: str | Path) -> LanguageSpec | None:
    """Pick the language for a file, or ``None`` if it is not one we handle."""
    name = Path(path).name
    exact = _BY_FILENAME.get(name)
    if exact is not None:
        return exact
    # `.d.ts` is TypeScript, and `Path.suffix` gives `.ts` for it already.
    return _BY_EXTENSION.get(Path(name).suffix.lower())


def language_by_name(name: str) -> LanguageSpec:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise LanguageUnavailable(f"unknown language: {name!r}") from None


@functools.cache
def query_source(name: str) -> str:
    """Read the tag query for a language.

    Cached because a full index runs this once per language, not per file.
    """
    spec = language_by_name(name)
    try:
        return spec.query_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise LanguageUnavailable(
            f"no tag query for {name!r}; expected {spec.query_path}"
        ) from None


@functools.cache
def get_language(name: str) -> Language:
    """Load a grammar, raising :class:`LanguageUnavailable` if it cannot be."""
    spec = language_by_name(name)
    try:
        from tree_sitter_language_pack import get_language as _load
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise LanguageUnavailable(
            "tree-sitter support is not installed; install repoatlas[parse]"
        ) from exc
    try:
        return _load(spec.grammar)
    except Exception as exc:
        # The pack fetches some grammars on first use, so this is a network
        # failure as often as it is a missing grammar.
        raise LanguageUnavailable(
            f"could not load the {spec.grammar!r} grammar: {exc}"
        ) from exc


def get_parser(name: str) -> Parser:
    """Build a parser for a language.

    A fresh parser each time, rather than a cached one: parsers carry
    mutable state such as ``included_ranges``, and sharing one across an
    embedded-language pass would leak that state between files.
    """
    from tree_sitter import Parser

    return Parser(get_language(name))


def available_languages() -> dict[str, bool]:
    """Report which languages can actually be used on this machine."""
    status: dict[str, bool] = {}
    for spec in SUPPORTED:
        try:
            get_language(spec.name)
            query_source(spec.name)
        except LanguageUnavailable:
            status[spec.name] = False
        else:
            status[spec.name] = True
    return status
