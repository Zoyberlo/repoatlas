"""Resolving a name to the definition it means, and saying how sure we are.

A compiler knows what ``greet`` refers to. Without one, the best available
answer is a ladder of decreasing evidence: the name was imported from a file
this index covers, or it is defined in this very file, or exactly one
definition of it exists in the repository, or several do and only the name
matches.

Each rung carries a confidence, and the harness checks those numbers against
a compiler-backed oracle. That is the point of the whole arrangement: a
resolver that guesses is fine as long as it says so, and the calibration
table is what turns "0.55 feels about right" into a measurement.

The ladder follows the cascade in the Codebase-Memory paper
(arXiv:2603.27277); the confidences are its starting values, to be tuned
against calibration rather than kept out of loyalty.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..model import Edge, EdgeKind, ResolutionTier, Symbol, SymbolKind
from ..parse.extract import Reference
from ..parse.imports import FileImports
from ..plugins.base import FrameworkPlugin
from .modules import ModuleResolver

__all__ = ["CONVENTION_KINDS", "ResolutionStats", "Resolver", "SymbolIndex"]

# Reference kinds the tag queries produce, mapped to the edge they become.
_EDGE_KIND_BY_REFERENCE = {
    "call": EdgeKind.CALLS,
    "class": EdgeKind.INHERITS,
    "type": EdgeKind.USES_TYPE,
    "import": EdgeKind.IMPORTS,
    "member": EdgeKind.REFERENCES,
    "construct": EdgeKind.CALLS,
    "value": EdgeKind.REFERENCES,
    # A template a controller renders, or a partial a layout pulls in.
    # Both are dependencies of the file that names them.
    "view": EdgeKind.IMPORTS,
    "extends": EdgeKind.INHERITS,
    "include": EdgeKind.IMPORTS,
    "component": EdgeKind.IMPORTS,
    "route": EdgeKind.REFERENCES,
}

# Reference kinds whose name is a framework convention rather than an
# identifier. `view('users.index')` names a template; letting it fall
# through the identifier cascade would match any function called
# `index`, and a confident wrong edge is worse than none.
CONVENTION_KINDS = frozenset(
    {"view", "extends", "include", "component", "route"}
)
_CONVENTION_KINDS = CONVENTION_KINDS

# The exception. A component tag in a Vue template *is* an identifier:
# `<MyButton />` is the symbol the script block imported. Blade's
# `<x-alert />` is not. The name itself says which.
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*\Z")

# Kinds that can be reached through a receiver: `this.x`, `self.x`.
_MEMBER_REFERENCE_KINDS = frozenset({"call", "member"})

# What each language calls the method `new` invokes.
_CONSTRUCTOR_NAMES = ("constructor", "__init__", "__construct")

# A member access resolves to a method or field, never to a free function;
# an inheritance clause names a type. Filtering by kind removes a whole
# class of wrong answers before confidence is even considered.
_KINDS_BY_REFERENCE: dict[str, frozenset[SymbolKind]] = {
    "class": frozenset(
        {
            SymbolKind.CLASS,
            SymbolKind.INTERFACE,
            SymbolKind.TRAIT,
            SymbolKind.ENUM,
            SymbolKind.TYPE_ALIAS,
        }
    ),
    "type": frozenset(
        {
            SymbolKind.CLASS,
            SymbolKind.INTERFACE,
            SymbolKind.TRAIT,
            SymbolKind.ENUM,
            SymbolKind.TYPE_ALIAS,
        }
    ),
}


@dataclass(slots=True)
class ResolutionStats:
    """How each reference was settled, for the report.

    A resolver's tier histogram is the shape of its evidence. Most edges
    landing on the bottom rung means the import map is not working, which
    the aggregate precision alone would not show.
    """

    by_tier: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    unresolved: int = 0
    ambiguous: int = 0
    """Names with several equally good candidates, resolved by picking one."""

    external: int = 0
    """Names imported from outside the repository, correctly left alone."""

    @property
    def resolved(self) -> int:
        return sum(self.by_tier.values())

    @property
    def resolution_rate(self) -> float:
        total = self.resolved + self.unresolved + self.external
        return self.resolved / total if total else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "resolved": self.resolved,
            "unresolved": self.unresolved,
            "external": self.external,
            "ambiguous": self.ambiguous,
            "resolution_rate": round(self.resolution_rate, 4),
            "by_tier": dict(sorted(self.by_tier.items())),
        }


class SymbolIndex:
    """Lookup tables over every definition, built once per repository."""

    __slots__ = (
        "_by_name",
        "_by_path_name",
        "_by_qualified",
        "_members",
        "_public_by_name",
        "_symbols",
    )

    def __init__(self, symbols: dict[str, Symbol]) -> None:
        self._symbols = symbols
        self._public_by_name: dict[str, list[Symbol]] = {}
        self._by_name: dict[str, list[Symbol]] = defaultdict(list)
        self._by_path_name: dict[tuple[str, str], list[Symbol]] = defaultdict(list)
        self._by_qualified: dict[str, list[Symbol]] = defaultdict(list)
        self._members: dict[tuple[str, str], list[Symbol]] = defaultdict(list)
        for symbol in symbols.values():
            if symbol.synthetic:
                continue
            self._by_name[symbol.name].append(symbol)
            self._by_path_name[(symbol.path, symbol.name)].append(symbol)
            if symbol.qualified_name:
                self._by_qualified[symbol.qualified_name].append(symbol)
            if symbol.container_id is not None:
                self._members[(symbol.container_id, symbol.name)].append(symbol)

    def by_name(self, name: str) -> list[Symbol]:
        return self._by_name.get(name, [])

    def public_by_name(self, name: str) -> list[Symbol]:
        """Every non-local definition of ``name``, filtered once per name.

        A repository has thousands of methods called `run`, and every call
        to one used to filter the whole list again. Ten thousand references
        times six thousand candidates was forty million checks.
        """
        cached = self._public_by_name.get(name)
        if cached is None:
            cached = [symbol for symbol in self.by_name(name) if not symbol.local]
            self._public_by_name[name] = cached
        return cached

    def in_file(self, path: str, name: str) -> list[Symbol]:
        return self._by_path_name.get((path, name), [])

    def by_qualified(self, qualified: str) -> list[Symbol]:
        return self._by_qualified.get(qualified, [])

    def members(self, container_id: str, name: str) -> list[Symbol]:
        """Definitions named ``name`` directly inside ``container_id``."""
        return self._members.get((container_id, name), [])

    def get(self, symbol_id: str) -> Symbol | None:
        return self._symbols.get(symbol_id)

    def enclosing_type(self, symbol_id: str | None) -> Symbol | None:
        """Walk up from a symbol to the type that contains it.

        A reference written `this.greet()` inside a method means the
        method's own class, and that is knowable without any type
        inference: it is where the reference is written.
        """
        current = self._symbols.get(symbol_id) if symbol_id else None
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            if current.kind.is_type_like:
                return current
            seen.add(current.id)
            current = (
                self._symbols.get(current.container_id)
                if current.container_id
                else None
            )
        return None

    def __len__(self) -> int:
        return len(self._by_name)


def _prefer(candidates: list[Symbol], reference: Reference) -> Symbol | None:
    """Choose among equally-ranked candidates, deterministically.

    Ordering by path and position rather than by dictionary order keeps a
    run reproducible, which matters because an index that resolves the same
    call differently on two machines cannot be regression-tested.
    """
    if not candidates:
        return None
    # A local binding is never a navigation target: an agent cannot go to a
    # loop counter, the comparison excludes them from scope, and resolving
    # to one only produces an edge that will be filtered later. Dropping
    # them here also stops a local shadowing a real definition of the same
    # name in the same-file rung.
    candidates = [symbol for symbol in candidates if not symbol.local]
    if not candidates:
        return None
    allowed = _KINDS_BY_REFERENCE.get(reference.kind)
    filtered = [s for s in candidates if allowed is None or s.kind in allowed]
    pool = filtered or candidates
    if reference.kind == "call":
        callable_only = [s for s in pool if s.kind.is_callable]
        pool = callable_only or pool
    # A concrete type is a likelier target than the interface it
    # implements: an interface member has no body to navigate to.
    if reference.kind in _MEMBER_REFERENCE_KINDS:
        concrete = [s for s in pool if not _declared_in_interface(s)]
        pool = concrete or pool
    return min(pool, key=lambda s: (s.path, s.name_range.start, s.id))


def _declared_in_interface(symbol: Symbol) -> bool:
    """Whether a symbol is an interface member rather than a real body."""
    return bool(symbol.qualified_name) and symbol.kind in (
        SymbolKind.METHOD,
        SymbolKind.FIELD,
    ) and symbol.full_range is not None and symbol.full_range.is_single_line


@dataclass(slots=True)
class Resolver:
    """Turns unresolved references into edges, one file at a time."""

    index: SymbolIndex
    imports: dict[str, FileImports]
    resolvers: dict[str, ModuleResolver]
    languages: dict[str, str]
    """Which language each indexed file is, so the right resolver is used."""

    module_symbols: dict[str, str] = field(default_factory=dict)
    """File path to its synthetic module symbol id, for top-level references."""

    plugins: tuple[FrameworkPlugin, ...] = ()
    """Framework plugins that recognised this repository.

    Consulted before any other rung, because a convention is not a
    guess: `view('users.index')` names one file, and either it is in the
    repository or the reference goes unresolved.
    """

    known_files: frozenset[str] = frozenset()

    stats: ResolutionStats = field(default_factory=ResolutionStats)

    _choices: dict[tuple[str, str], Symbol | None] = field(
        default_factory=dict, repr=False
    )
    """The bottom rung's pick, per name and reference kind.

    Choosing among every `run` in the repository depends on nothing but
    the name and what kind of reference asked, so it is made once. This
    was the whole cost of resolution on a large index: the choice scanned
    thousands of candidates, and was made again for every one of thousands
    of references to the same name.
    """

    def resolve_file(self, path: str, references: list[Reference]) -> list[Edge]:
        """Resolve every reference in one file."""
        return [
            edge
            for edge in (self._resolve(path, reference) for reference in references)
            if edge is not None
        ]

    def _resolve(self, path: str, reference: Reference) -> Edge | None:
        target, tier = self._target(path, reference)
        if target is None:
            return None
        target = self._constructor_of(target, reference)
        source_id = reference.container_id or self.module_symbols.get(path)
        if source_id is None or source_id == target.id:
            return None
        self.stats.by_tier[tier.label] += 1
        return Edge(
            src_id=source_id,
            dst_id=target.id,
            kind=_EDGE_KIND_BY_REFERENCE.get(reference.kind, EdgeKind.REFERENCES),
            tier=tier,
            site_path=path,
            site_range=reference.span,
        )

    def _constructor_of(self, target: Symbol, reference: Reference) -> Symbol:
        """For `new User()`, prefer the constructor over the class itself.

        The name written is the class, but what the expression invokes is
        its constructor, and that is what a reader following the edge wants
        to land on. A class that declares none keeps the class as target.
        """
        if reference.kind != "construct" or not target.kind.is_type_like:
            return target
        for name in _CONSTRUCTOR_NAMES:
            members = [
                symbol
                for symbol in self.index.members(target.id, name)
                if symbol.kind is SymbolKind.CONSTRUCTOR
            ]
            if members:
                return members[0]
        return target

    def _target(self, path: str, reference: Reference) -> tuple[Symbol | None, ResolutionTier]:
        name = reference.name

        # Rung zero: a framework convention. Either the file the
        # convention names is in the repository or it is not, so a hit is
        # evidence rather than inference and ranks with a resolved import.
        if reference.kind in _CONVENTION_KINDS:
            identifier = _as_identifier(reference)
            # An imported name follows its import. A convention is how a
            # framework finds what nothing imported, so consulting it over
            # an explicit import would answer a question nobody asked, and
            # answer it wrongly wherever two files share a name.
            if identifier is None or not self._is_imported(path, identifier):
                resolved = self._by_convention(path, reference)
                if resolved is not None:
                    return resolved, ResolutionTier.IMPORT_MAP
            if identifier is None:
                # Nothing below this rung can read the name, because it is
                # not one an identifier cascade would recognise.
                self.stats.unresolved += 1
                return None, ResolutionTier.FUZZY
            name = identifier

        file_imports = self.imports.get(path)

        # Rung one: the name was imported, and the import names a file this
        # index covers. This is the only rung that is evidence rather than
        # inference, which is why it is worth the module resolvers.
        if file_imports is not None:
            found = file_imports.binding_for(name)
            if found is not None:
                statement, binding = found
                resolved_path = self._resolve_module(path, statement.module, statement.relative_level)
                if resolved_path is not None:
                    candidates = self.index.in_file(resolved_path, binding.source_name)
                    chosen = _prefer(candidates, reference)
                    if chosen is not None:
                        return chosen, ResolutionTier.IMPORT_MAP
                    # A default or namespace import whose target declares no
                    # matching name: the module never named what it exported,
                    # so the file itself is the answer. A Vue single-file
                    # component is exactly this, and so is any module whose
                    # default export is anonymous.
                    if binding.original is None:
                        module_id = self.module_symbols.get(resolved_path)
                        module_symbol = self.index.get(module_id) if module_id else None
                        if module_symbol is not None:
                            return module_symbol, ResolutionTier.IMPORT_MAP
                    # The file is ours but the name is not in it: a re-export,
                    # or a name the extractor missed. Weaker, not absent.
                    fallback = _prefer(self.index.by_name(binding.source_name), reference)
                    if fallback is not None:
                        return fallback, ResolutionTier.IMPORT_SUFFIX
                elif _is_external(statement.module):
                    self.stats.external += 1
                    return None, ResolutionTier.FUZZY

        # Rung two: a member of the type the reference is written in.
        # `this.greet()` inside `User.shout` means `User.greet`, and no
        # type inference is needed to know it: the receiver is the
        # enclosing class. Without this rung the name falls through to a
        # repository-wide search that cannot tell a class's method from
        # the interface method it implements.
        if reference.kind in _MEMBER_REFERENCE_KINDS:
            owner = self.index.enclosing_type(reference.container_id)
            if owner is not None:
                member = _prefer(self.index.members(owner.id, name), reference)
                if member is not None:
                    return member, ResolutionTier.SAME_MODULE

        # Rung three: defined in this very file. No import needed and no
        # ambiguity possible, so it ranks just below a resolved import.
        same_file = _prefer(self.index.in_file(path, name), reference)
        if same_file is not None:
            return same_file, ResolutionTier.SAME_MODULE

        # Rung four: exactly one definition of the name in the repository.
        # Wrong only when the true target was never indexed.
        public = self.index.public_by_name(name)
        if len(public) == 1:
            return public[0], ResolutionTier.UNIQUE_NAME

        # Rung five: several definitions share the name. Picking one is a
        # coin flip weighted by kind, and the confidence says so.
        if public:
            self.stats.ambiguous += 1
            key = (name, reference.kind)
            if key not in self._choices:
                self._choices[key] = _prefer(public, reference)
            chosen = self._choices[key]
            if chosen is not None:
                return chosen, ResolutionTier.SUFFIX

        self.stats.unresolved += 1
        return None, ResolutionTier.FUZZY

    def _is_imported(self, path: str, name: str) -> bool:
        file_imports = self.imports.get(path)
        return file_imports is not None and file_imports.binding_for(name) is not None

    def _by_convention(self, path: str, reference: Reference) -> Symbol | None:
        """Ask each plugin what file this conventional name refers to."""
        for plugin in self.plugins:
            if reference.kind not in plugin.kinds:
                continue
            target_path = plugin.resolve(
                reference.kind,
                reference.name,
                from_path=path,
                language=self.languages.get(path),
                files=self.known_files,
            )
            if target_path is None or target_path == path:
                continue
            module_id = self.module_symbols.get(target_path)
            target = self.index.get(module_id) if module_id else None
            if target is not None:
                return target
        return None

    def _resolve_module(self, path: str, module: str, relative_level: int) -> str | None:
        language = self.languages.get(path)
        resolver = self.resolvers.get(language or "")
        if resolver is None:
            return None
        return resolver.resolve(module, from_path=path, relative_level=relative_level)


def _as_identifier(reference: Reference) -> str | None:
    """The identifier a conventional name also is, if it is one.

    Only component tags qualify, and only in a template whose components
    are imported symbols. Vue writes `<MyButton />` for an import and
    accepts `<my-button />` for the same one, so both readings are tried;
    Blade's `<x-alert />` is neither and returns nothing.
    """
    if reference.kind != "component":
        return None
    name = reference.name
    if _IDENTIFIER.match(name):
        return name
    if name.startswith("x-") or "." in name or "-" not in name:
        return None
    pascal = "".join(part[:1].upper() + part[1:] for part in name.split("-") if part)
    return pascal if _IDENTIFIER.match(pascal) else None


def _is_external(module: str) -> bool:
    """Whether an unresolvable import names a package rather than a mistake.

    A bare specifier such as ``react`` is a dependency; a relative path that
    did not resolve is either a file outside the index or a genuine miss,
    and counting it as external would hide that.
    """
    return bool(module) and not module.startswith(".")
