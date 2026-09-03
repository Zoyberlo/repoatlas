"""Languages written inside other languages.

A Vue single-file component is HTML with a TypeScript island in it. A Blade
template is HTML with PHP islands. Neither host grammar knows what is inside
those islands, and the inner grammar cannot parse the file around them.

tree-sitter has no injection engine; every editor that supports injections
implements its own. This is that, in about a hundred lines: find the regions
that belong to another language, hand them to that language's parser through
``included_ranges``, and merge what comes back.

The one thing that makes it painless is that ``included_ranges`` takes the
*whole* file buffer and a list of regions to attend to. Every position the
inner parse reports is therefore already a real position in the real file,
so nothing needs translating and no offset arithmetic can go wrong.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..model import SourceRange

if TYPE_CHECKING:  # pragma: no cover - imported only for type checking
    from tree_sitter import Node, Tree

__all__ = ["EmbeddedRegion", "embedded_regions", "parse_embedded"]


@dataclass(frozen=True, slots=True)
class EmbeddedRegion:
    """A stretch of one file written in a different language."""

    language: str
    """RepoAtlas's name for the inner language."""

    node: Node
    """The host node holding the inner source."""

    span: SourceRange

    @property
    def is_empty(self) -> bool:
        return self.node.start_byte >= self.node.end_byte


def _attribute_value(tag: Node, name: str) -> str | None:
    """Read an attribute off a start tag, unquoted.

    Written against the HTML shape both the Vue and Blade grammars inherit:
    an ``attribute`` node holding an ``attribute_name`` and, when it has a
    value, an ``attribute_value`` inside quotes.
    """
    for child in tag.named_children:
        if child.type != "attribute":
            continue
        key = child.child_by_field_name("name")
        if key is None:
            key = next(
                (c for c in child.named_children if c.type == "attribute_name"), None
            )
        if key is None or key.text is None:
            continue
        if key.text.decode("utf-8", errors="replace") != name:
            continue
        for candidate in child.named_children:
            if candidate.type in ("quoted_attribute_value", "attribute_value"):
                inner = next(
                    (c for c in candidate.named_children if c.type == "attribute_value"),
                    candidate,
                )
                if inner.text is not None:
                    return inner.text.decode("utf-8", errors="replace").strip("\"'")
    return None


# What a `lang` attribute on a Vue `<script>` block means. Anything else,
# including no attribute at all, is plain JavaScript, which is what Vue
# itself assumes.
_SCRIPT_LANGUAGES = {
    "ts": "typescript",
    "typescript": "typescript",
    "tsx": "tsx",
    "jsx": "tsx",
    "js": "javascript",
    "javascript": "javascript",
}


def _vue_regions(tree: Tree) -> Iterator[EmbeddedRegion]:
    """The script blocks of a single-file component.

    A component may carry both a `<script setup>` and a plain `<script>`,
    and both hold definitions the rest of the project imports, so both are
    yielded rather than only the first.
    """
    from .extract import to_range

    for node in _walk(tree.root_node):
        if node.type != "script_element":
            continue
        raw = next((c for c in node.named_children if c.type == "raw_text"), None)
        if raw is None:
            continue
        start = next((c for c in node.named_children if c.type == "start_tag"), None)
        declared = _attribute_value(start, "lang") if start is not None else None
        language = _SCRIPT_LANGUAGES.get((declared or "").lower(), "javascript")
        yield EmbeddedRegion(language=language, node=raw, span=to_range(raw))


_REGION_FINDERS: dict[str, Callable[[Tree], Iterator[EmbeddedRegion]]] = {
    "vue": _vue_regions,
}


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.children:
        yield from _walk(child)


def embedded_regions(tree: Tree, host_language: str) -> list[EmbeddedRegion]:
    """Find every region of ``tree`` written in another language."""
    finder = _REGION_FINDERS.get(host_language)
    if finder is None:
        return []
    return [region for region in finder(tree) if not region.is_empty]


def parse_embedded(source: bytes, region: EmbeddedRegion) -> Tree | None:
    """Parse one embedded region, keeping positions absolute.

    The parser is handed the whole file and told which bytes to attend to,
    so every position it reports is a position in the real file. Slicing the
    region out instead would need every span shifted back afterwards, and a
    single mistake there puts a symbol on the wrong line.
    """
    from tree_sitter import Point, Range

    from .languages import LanguageUnavailable, get_parser

    try:
        parser = get_parser(region.language)
    except LanguageUnavailable:
        return None
    parser.included_ranges = [
        Range(
            start_point=Point(*region.node.start_point),
            end_point=Point(*region.node.end_point),
            start_byte=region.node.start_byte,
            end_byte=region.node.end_byte,
        )
    ]
    return parser.parse(source)
