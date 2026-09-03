"""Import statements: which names a file borrows, and where from.

This is the input to the top rung of the resolution cascade. Knowing that a
file writes ``import { User } from "./user"`` and that ``./user`` is
``src/user.ts`` turns a use of ``User`` from a guess into a fact, which is
the difference between a 0.95 edge and a 0.55 one.

Imports are extracted with their own query rather than the tag query,
because an import binds names *and* names a module at once and the tag
query's one-name-per-match shape cannot carry both. The queries mark where
imports are; this module reads the node's fields for the rest, which keeps
the per-language part declarative without contorting it.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..model import SourceRange
from .languages import LanguageSpec, get_language, imports_query_source

if TYPE_CHECKING:  # pragma: no cover - imported only for type checking
    from tree_sitter import Node, Query, Tree

__all__ = ["FileImports", "ImportBinding", "ImportStatement", "extract_imports"]

# Distinguishes "the query captured no module" from "the module is the empty
# string", which a Python `from . import x` legitimately is.
_NO_MODULE: tuple[str, int] = ("", -1)


@dataclass(frozen=True, slots=True)
class ImportBinding:
    """One name an import brings into the file's scope."""

    local: str
    """The name as this file uses it: the alias when there is one."""

    original: str | None
    """The name in the source module, when it differs from ``local``.

    ``None`` for a default or namespace binding, where the source module
    never named the thing that got bound.
    """

    span: SourceRange

    @property
    def source_name(self) -> str:
        """What to look for in the target module."""
        return self.original or self.local


@dataclass(frozen=True, slots=True)
class ImportStatement:
    """A module this file depends on, and what it took from it."""

    module: str
    """The path as written: ``./user``, ``a.b``, ``App\\Models\\User``."""

    bindings: tuple[ImportBinding, ...]
    span: SourceRange
    relative_level: int = 0
    """Leading dots on a Python relative import; 0 for an absolute one.

    Kept separate from ``module`` because ``from . import x`` and
    ``from x import y`` differ only in this, and the resolver needs to know
    which directory to start from.
    """

    @property
    def is_relative(self) -> bool:
        """Whether the module path is resolved against this file's location."""
        return self.relative_level > 0 or self.module.startswith(".")


@dataclass(slots=True)
class FileImports:
    """Everything one file's imports say."""

    statements: list[ImportStatement] = field(default_factory=list)
    namespace: str | None = None
    """The file's own namespace, where the language has one.

    PHP resolves an unqualified name against the current namespace first,
    so a same-namespace lookup needs this.
    """

    def binding_for(self, name: str) -> tuple[ImportStatement, ImportBinding] | None:
        """Find the import that bound ``name``, if any."""
        for statement in self.statements:
            for binding in statement.bindings:
                if binding.local == name:
                    return statement, binding
        return None


@functools.cache
def _compiled_imports_query(language: str) -> Query:
    from tree_sitter import Query

    return Query(get_language(language), imports_query_source(language))


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _to_range(node: Node) -> SourceRange:
    start_row, start_col = node.start_point
    end_row, end_col = node.end_point
    return SourceRange.of(start_row, start_col, end_row, end_col)


def _binding_from_node(node: Node) -> ImportBinding | None:
    """Read a binding site, whatever shape the grammar gave it.

    Every supported grammar spells the pair the same way: a ``name`` field
    for what the module calls it and an ``alias`` field for what this file
    calls it. A node with neither is itself the bound name, which is how a
    default import and a namespace import arrive.
    """
    alias = node.child_by_field_name("alias")
    name = node.child_by_field_name("name")
    if alias is not None:
        if name is None:
            # PHP's `use App\Contracts\Greeter as G` has no `name` field: the
            # original is the qualified path sitting beside the alias, and
            # reading the clause's whole text instead would yield the alias
            # clause verbatim, "Greeter as G".
            name = next(
                (child for child in node.named_children if child != alias), None
            )
        original = _last_segment(_text(name)) if name is not None else ""
        return ImportBinding(
            local=_text(alias), original=original or None, span=_to_range(alias)
        )
    if name is not None:
        text = _text(name)
        return ImportBinding(local=_last_segment(text), original=None, span=_to_range(name))
    text = _text(node)
    if not text:
        return None
    return ImportBinding(local=_last_segment(text), original=None, span=_to_range(node))


def _last_segment(text: str, alias: Node | None = None) -> str:
    """The bound name from a dotted or namespaced path.

    ``import os.path`` binds ``os``, but ``use App\\Models\\User`` binds
    ``User``: the first segment for a dotted module, the last for a
    namespaced class. Splitting on both separators and preferring the last
    segment is right for every case except the Python one, which the caller
    handles by passing the whole dotted name through untouched.
    """
    cleaned = text.strip().strip("\\")
    if "\\" in cleaned:
        return cleaned.rsplit("\\", 1)[-1]
    return cleaned


def _python_module(node: Node) -> tuple[str, int]:
    """Read a Python module path, counting leading dots for a relative one."""
    text = _text(node)
    if node.type != "relative_import":
        return text, 0
    level = 0
    for char in text:
        if char == ".":
            level += 1
        else:
            break
    return text[level:], level


def extract_imports(tree: Tree, spec: LanguageSpec) -> FileImports:
    """Read every import in one parsed file."""
    from tree_sitter import QueryCursor

    cursor = QueryCursor(_compiled_imports_query(spec.name))
    result = FileImports()
    # A module may bind several names, and the query yields one match per
    # statement, so statements are keyed by their own span to merge.
    by_span: dict[SourceRange, list[ImportBinding]] = {}
    modules: dict[SourceRange, tuple[str, int]] = {}
    order: list[SourceRange] = []

    for _pattern, captures in cursor.matches(tree.root_node):
        if "namespace" in captures:
            result.namespace = _text(captures["namespace"][0]).strip("\\")
            continue

        module_nodes = captures.get("module") or []
        binding_nodes = captures.get("binding") or []
        if not module_nodes and not binding_nodes:
            continue
        if "self_module" in captures and not module_nodes:
            # `import os.path`: the bound name is the module path.
            for node in binding_nodes:
                target = node.child_by_field_name("name") or node
                module_text = _text(target)
                span = _to_range(node)
                binding = _binding_from_node(node)
                if binding is None:
                    continue
                if node.type != "aliased_import":
                    # `import a.b` binds `a`, not `b`.
                    binding = ImportBinding(
                        local=module_text.split(".")[0],
                        original=None,
                        span=binding.span,
                    )
                if span not in by_span:
                    order.append(span)
                    by_span[span] = []
                    modules[span] = (module_text, 0)
                by_span[span].append(binding)
            continue

        anchor = module_nodes[0] if module_nodes else binding_nodes[0]
        span = _to_range(_enclosing_statement(anchor))
        if span not in by_span:
            order.append(span)
            by_span[span] = []
            modules[span] = (
                _python_module(module_nodes[0]) if module_nodes else _NO_MODULE
            )
        for node in binding_nodes:
            binding = _binding_from_node(node)
            if binding is None:
                continue
            if modules[span] is _NO_MODULE:
                # PHP writes no separate module path: the qualified name in
                # `use App\Models\User` is both the module and the binding.
                # A Python relative import, by contrast, captures a module
                # that is legitimately empty, which is why the two cases are
                # told apart by a sentinel rather than by emptiness.
                modules[span] = (_text(node).split(" as ")[0].strip().strip("\\"), 0)
            by_span[span].append(binding)

    for span in order:
        module, level = modules.get(span, _NO_MODULE)
        if not module and not by_span[span] and not level:
            continue
        result.statements.append(
            ImportStatement(
                module=module,
                bindings=tuple(by_span[span]),
                span=span,
                relative_level=level,
            )
        )
    return result


def _enclosing_statement(node: Node) -> Node:
    """Walk up to the statement a capture sits in.

    Bindings and the module path are captured at different depths, and both
    must key the same statement for the merge above to work.
    """
    current: Node | None = node
    while current is not None:
        if current.type in _STATEMENT_TYPES:
            return current
        current = current.parent
    return node


_STATEMENT_TYPES = frozenset(
    {
        "import_statement",
        "import_from_statement",
        "namespace_use_declaration",
        "namespace_use_clause",
        "call_expression",
    }
)
