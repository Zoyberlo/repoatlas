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
from ..parse.extract import (
    ASSIGNED_TYPE_PREFIX,
    CALL_TYPE_PREFIX,
    EXPRESSION_RECEIVER,
    Reference,
)
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
    # `self::`, `static::`, `parent::`: a class named without being spelled.
    "scope": EdgeKind.REFERENCES,
    # `import("pages/Index.vue")`, `require("./util")`: a module by path.
    "module": EdgeKind.IMPORTS,
}

# What each spelling of a relative scope means, per language. `static` is
# late-bound in PHP, but statically it can only be read as the class in
# which it was written.
_SCOPE_SELF = frozenset({"self", "static"})
_SCOPE_PARENT = frozenset({"parent"})

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

# Receivers that mean the enclosing class, which the member rung already
# handles without any type being declared, and the ones that mean what it
# extends.
_SELF_RECEIVERS = frozenset({"this", "self", "cls", "static", "super", "parent"})
_PARENT_RECEIVERS = frozenset({"super", "parent"})


def receiver_shape(reference: Reference) -> str:
    """What a member was reached through, named as the accuracy report names it.

    ``none`` is not a member access at all. The rest describe the
    receiver: ``self``; ``type`` for a class named outright; ``variable``
    and ``property of self``, each ``(typed)`` when a declaration or a
    `new` says what it is and ``(untyped)`` otherwise; ``expression`` for
    a member of whatever a call or a subscript returned.

    The comparison groups edges by this, because oracles differ in which
    shapes they can resolve at all, and a shape an oracle never resolves
    cannot be scored against it.
    """
    if reference.kind not in _MEMBER_REFERENCE_KINDS:
        return "none"
    receiver = reference.receiver
    if receiver is None:
        return "none" if reference.kind == "call" else "expression"
    if receiver == EXPRESSION_RECEIVER:
        return "expression"
    if receiver in _SELF_RECEIVERS:
        return "self"
    typed = " (typed)" if reference.receiver_type else " (untyped)"
    if reference.receiver_type and reference.receiver_type.startswith(ASSIGNED_TYPE_PREFIX):
        typed = " (typed by assignment)"
    if "." in receiver:
        return "property of self" + typed
    if reference.receiver_type:
        if reference.receiver_type.startswith(CALL_TYPE_PREFIX):
            return "variable (typed by call)"
        return "variable (typed)"
    if receiver[:1].isupper():
        return "type"
    return "variable (untyped)"


# How the member rung reads each shape: what to look the member up in.
_RUNG_BY_SHAPE = {
    "none": "bare",
    "self": "self",
    "type": "static",
    "variable (typed)": "typed",
    "variable (typed by call)": "typed",
    "property of self (typed)": "typed",
    "property of self (typed by assignment)": "typed",
    "variable (typed by assignment)": "typed",
}

# A declared return type, read off a signature line: PHP and TypeScript
# write `): Type`, Python `-> Type`. Generics and namespaces are cut down
# to the bare name, which is what a type reference is resolved from.
_RETURN_TYPE = re.compile(
    r"\)\s*:\s*\??\s*(?:Promise<)?\s*\\?(?P<colon>[A-Za-z_][\w\\.]*)"
    r"|->\s*(?P<arrow>[A-Za-z_][\w.]*)"
)


# `@return \App\Models\Ad|null`, `@return static`, `@return Collection<Ad>`:
# the first type named, without namespace, generics or nullability.
_DOC_RETURN = re.compile(r"@return\s+\\?([\w\\|$]+)")
_NOT_A_TYPE = frozenset({"null", "void", "mixed", "never", "bool", "int", "string", "array", "float"})


def _doc_return_type(documentation: str | None) -> str | None:
    """The return type a docblock declares, for a signature that does not."""
    if not documentation:
        return None
    found = _DOC_RETURN.search(documentation)
    if found is None:
        return None
    for option in found.group(1).split("|"):
        name = option.replace("\\", ".").rsplit(".", 1)[-1].strip("$")
        if name == "this":
            return "static"
        if name and name not in _NOT_A_TYPE:
            return name
    return None


def _declared_return_type(signature: str | None) -> str | None:
    if not signature:
        return None
    found = _RETURN_TYPE.search(signature)
    if found is None:
        return None
    name = found.group("colon") or found.group("arrow") or ""
    return name.replace("\\", ".").rsplit(".", 1)[-1] or None


def _receiver_shape(reference: Reference) -> str:
    return _RUNG_BY_SHAPE.get(receiver_shape(reference), "unknown")

# A member access resolves to a method or field, never to a free function;
# an inheritance clause names a type. Filtering by kind removes a whole
# class of wrong answers before confidence is even considered.
_KINDS_BY_REFERENCE: dict[str, frozenset[SymbolKind]] = {
    "class": frozenset(
        {
            SymbolKind.CLASS,
            SymbolKind.COMPONENT,
            SymbolKind.INTERFACE,
            SymbolKind.TRAIT,
            SymbolKind.ENUM,
            SymbolKind.TYPE_ALIAS,
        }
    ),
    "type": frozenset(
        {
            SymbolKind.CLASS,
            SymbolKind.COMPONENT,
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
        "_containers",
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
        self._containers: set[str] = set()
        for symbol in symbols.values():
            if symbol.synthetic:
                continue
            if symbol.container_id is not None:
                self._containers.add(symbol.container_id)
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

    def has_members(self, symbol_id: str) -> bool:
        """Whether anything is declared inside this symbol.

        A value that owns members is its own type: a Pinia store, an
        object literal, a module object.
        """
        return symbol_id in self._containers

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

    _bases: dict[str, Symbol] = field(default_factory=dict, repr=False)
    """The class each type extends, for `parent::`."""

    _ancestors: dict[str, list[Symbol]] = field(default_factory=dict, repr=False)
    """Everything each type extends, implements or uses, in the order written.

    A member reached through a type is looked for here, breadth first, so
    a method inherited from a base or pulled in by a trait is found where
    it is declared.
    """
    """Each type's resolved base, recorded as its `extends` clause resolves.

    What `parent::` means. A base clause is written before the methods that
    say `parent::`, so within a file the answer is always already known
    when it is needed.
    """
    """The bottom rung's pick, per name and reference kind.

    Choosing among every `run` in the repository depends on nothing but
    the name and what kind of reference asked, so it is made once. This
    was the whole cost of resolution on a large index: the choice scanned
    thousands of candidates, and was made again for every one of thousands
    of references to the same name.
    """

    def learn_bases(self, path: str, references: list[Reference]) -> None:
        """Record what every type in ``references`` extends, before resolving.

        Run over every file first. A receiver typed as `Admin` may call a
        method `Admin` inherits from `User` in another file, and finding it
        means walking the chain, which has to be complete before the walk.
        """
        for reference in references:
            if reference.kind != "class":
                continue
            owner = self.index.enclosing_type(reference.container_id)
            if owner is None:
                continue
            target, _tier = self._target(path, reference)
            if target is not None and target.kind.is_type_like:
                self._record_ancestor(owner, target)

    def _record_ancestor(self, owner: Symbol | None, target: Symbol) -> None:
        """A base clause resolved: remember it for `parent::` and member lookups.

        Only base clauses arrive here. `Client::where()` also names a
        class, but the class it names is not what the enclosing type
        extends, and recording it as such once made every command that
        queried a model inherit from it.
        """
        if owner is None or target.id == owner.id:
            return
        chain = self._ancestors.setdefault(owner.id, [])
        if all(existing.id != target.id for existing in chain):
            chain.append(target)
        if owner.id not in self._bases and target.kind is SymbolKind.CLASS:
            self._bases[owner.id] = target

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
        if source_id is None:
            return None
        if source_id == target.id and reference.kind not in _MEMBER_REFERENCE_KINDS:
            # A class naming itself in its own body is not a dependency.
            # A method calling itself is: recursion is a call, and an
            # oracle records it.
            return None
        if reference.kind == "class" and target.kind.is_type_like:
            self._record_ancestor(self.index.enclosing_type(reference.container_id), target)
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

        if reference.kind == "scope":
            # `self`, `static` and `parent` are not names to look up: they
            # are positions. Never let them reach the identifier cascade,
            # where a function called `parent` would happily answer.
            owner = self.index.enclosing_type(reference.container_id)
            if owner is not None and name in _SCOPE_SELF:
                return owner, ResolutionTier.SAME_MODULE
            if owner is not None and name in _SCOPE_PARENT:
                base = self._bases.get(owner.id)
                if base is not None:
                    return base, ResolutionTier.SAME_MODULE
            self.stats.unresolved += 1
            return None, ResolutionTier.FUZZY

        if reference.kind == "module":
            # A module named by path in an expression. The module resolver
            # either knows the file, aliases included, or it is a package.
            resolved_path = self._resolve_module(path, name, 0)
            module_id = self.module_symbols.get(resolved_path) if resolved_path else None
            module_symbol = self.index.get(module_id) if module_id else None
            if module_symbol is not None:
                return module_symbol, ResolutionTier.IMPORT_MAP
            self.stats.external += 1
            return None, ResolutionTier.FUZZY

        if reference.kind == "type" and name in _SCOPE_SELF:
            # `: static`, `: self`, `new self()`: the enclosing class,
            # written without its name.
            owner = self.index.enclosing_type(reference.container_id)
            if owner is not None:
                return owner, ResolutionTier.SAME_MODULE
            self.stats.unresolved += 1
            return None, ResolutionTier.FUZZY

        # Rung zero: a framework convention. Either the file the
        # convention names is in the repository or it is not, so a hit is
        # evidence rather than inference and ranks with a resolved import.
        if reference.kind in _CONVENTION_KINDS:
            identifier = _as_identifier(reference)
            # An imported name follows its import — but only when the import
            # leads somewhere. A bundler alias this resolver does not know
            # about would otherwise silence the convention as well, and the
            # reference would resolve to nothing at all. That is what a
            # Quasar project's `src/components/X.vue` did before the alias
            # tables were read per project: the import was there, it did not
            # resolve, and the convention was never asked.
            if identifier is None or not self._import_resolves(path, identifier):
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

        if reference.kind == "import":
            # An import site names what it imports and nothing else. The
            # rung above is the only one that can say where that is; the
            # ones below would match the name against whatever this file
            # happens to define, which is how `use Foundation\Kernel as
            # ConsoleKernel` once resolved to the `Kernel` written under it.
            self.stats.external += 1
            return None, ResolutionTier.FUZZY

        # Rung two: a member, reached through its receiver. What the
        # receiver is decides everything: `$this` is the enclosing type, a
        # typed local or a static scope is that type, and a name nobody
        # declared a type for is unknown. Unknown stops here. The rungs
        # below match a bare name against the whole repository, and a
        # real Laravel application measured that at 0 of 890 right for
        # `suffix` and 48 of 812 for `unique_name`: `$order->id` is not some
        # job's `$id`, and `$order->update()` is not a controller's.
        member = self._resolve_member(path, reference)
        if member is not None:
            return member

        # Rung three: defined in this very file. No import needed and no
        # ambiguity possible, so it ranks just below a resolved import.
        same_file = _prefer(self.index.in_file(path, name), reference)
        if same_file is not None:
            return same_file, ResolutionTier.SAME_MODULE

        # Rung four: exactly one definition of the name in the repository.
        # Wrong only when the true target was never indexed.
        public = self.index.public_by_name(name)
        if reference.kind == "call" and reference.receiver is None:
            # A bare call reaches a function, or a class in a language
            # that constructs by calling. Never a method or a field: those
            # need a receiver, and PHP's `end($list)` is not a property
            # called `$end` in some other class.
            public = [s for s in public if s.kind is SymbolKind.FUNCTION or s.kind.is_type_like]
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

    def _resolve_member(
        self, path: str, reference: Reference
    ) -> tuple[Symbol | None, ResolutionTier] | None:
        """Resolve a member through its receiver, or say that nothing can.

        Returns ``None`` only when the reference is not a member access at
        all, a bare call such as `helper()`, so that the name rungs may
        try. For a member the answer is final: the symbol, or an
        unresolved edge with the statistics saying why.
        """
        shape = _receiver_shape(reference)
        if shape == "bare":
            return None
        receiver = reference.receiver or ""
        if shape == "self":
            owner = self.index.enclosing_type(reference.container_id)
            if owner is None:
                # `this.save()` with no class around it: a Vue options
                # object, or a mixin. The file's own methods and fields
                # are what `this` can reach there.
                found = _prefer(self.index.in_file(path, reference.name), reference)
                if found is not None and (
                    found.kind.is_callable or found.kind is SymbolKind.FIELD
                ):
                    return found, ResolutionTier.SAME_MODULE
                return self._unresolved()
            found = self._member_in_hierarchy(
                owner, reference, skip_own=receiver in _PARENT_RECEIVERS
            )
            if found is not None:
                return found, ResolutionTier.SAME_MODULE
            return self._unresolved()
        if shape in ("typed", "static"):
            # `Util::helper()`, `Config.get()`, or `greeter.greet()` where
            # `greeter: Greeter`: the receiver is a type, resolved like any
            # other reference, and the member is looked for in it and in
            # what it extends. The edge takes the tier the type resolved
            # at. When the type is not ours, neither is the member.
            type_name = reference.receiver_type if shape == "typed" else receiver
            if type_name is not None and type_name.startswith(ASSIGNED_TYPE_PREFIX):
                type_name = type_name[len(ASSIGNED_TYPE_PREFIX) :]
            if type_name is not None and type_name.startswith(CALL_TYPE_PREFIX):
                owner, tier = self._type_of_call(path, reference, type_name)
            else:
                owner, tier = self._target(
                    path,
                    Reference(
                        name=type_name or receiver,
                        kind="type",
                        span=reference.span,
                        container_id=reference.container_id,
                    ),
                )
            return self._member_of(owner, tier, reference)
        # An untyped name. An import may still say what it is: a module
        # whose export is the member, or a class imported under a name
        # that does not look like one.
        via_import = self._receiver_via_import(path, receiver)
        if via_import is not None:
            return self._member_of(via_import, ResolutionTier.IMPORT_MAP, reference)
        return self._unresolved()

    def _type_of_call(
        self, path: str, reference: Reference, marker: str
    ) -> tuple[Symbol | None, ResolutionTier]:
        """The type a local was assigned from a call: `$g = $this->build()`.

        The callee is resolved like the call it is; what it returns is
        read off its signature, in the callee's own file, where the type
        name means what that file's imports say. A class called directly,
        as Python constructs, is its own answer.
        """
        # Two splits, not every one: the receiver type may itself be a
        # call marker, `$b = $ad->fresh()` after `$ad = Ad::find(1)`.
        callee, receiver, receiver_type = [*marker[len(CALL_TYPE_PREFIX) :].split("|", 2), "", ""][:3]
        target, tier = self._target(
            path,
            Reference(
                name=callee,
                kind="call",
                span=reference.span,
                container_id=reference.container_id,
                receiver=receiver or None,
                receiver_type=receiver_type or None,
            ),
        )
        if target is None:
            # The callee is not ours. A framework may still say what it
            # returns: `Ad::find(1)` is an Ad and `$ad->fresh()` is one
            # too, though Eloquent supplies both methods, not the model.
            owner = self._receiver_type_of(path, reference, receiver, receiver_type)
            if owner is not None and any(
                plugin.returns_receiver(callee) for plugin in self.plugins
            ):
                return owner, ResolutionTier.UNIQUE_NAME
            return None, ResolutionTier.FUZZY
        if target.kind.is_type_like:
            return target, tier
        if target.kind is SymbolKind.CONSTRUCTOR:
            return self.index.enclosing_type(target.container_id), tier
        declared = _declared_return_type(target.signature) or _doc_return_type(
            target.documentation
        )
        if declared is None:
            if self.index.has_members(target.id):
                # `useAuthStore()` returns the store, whose actions are
                # the members declared inside the constant.
                return target, tier
            return self._unresolved()
        if declared in _SCOPE_SELF:
            return self.index.enclosing_type(target.container_id), tier
        owner, type_tier = self._target(
            target.path,
            Reference(
                name=declared,
                kind="type",
                span=target.name_range,
                container_id=target.container_id,
            ),
        )
        weaker = type_tier if type_tier.default_confidence < tier.default_confidence else tier
        return owner, weaker

    def _receiver_type_of(
        self, path: str, reference: Reference, receiver: str, receiver_type: str
    ) -> Symbol | None:
        """The type a call's receiver has, when it is one of ours."""
        if receiver_type and receiver_type.startswith(CALL_TYPE_PREFIX):
            # The receiver was itself assigned from a call; find out what
            # that returned before asking what this one returns.
            owner, _tier = self._type_of_call(path, reference, receiver_type)
            return owner if owner is not None and owner.kind.is_type_like else None
        if receiver_type.startswith(ASSIGNED_TYPE_PREFIX):
            receiver_type = receiver_type[len(ASSIGNED_TYPE_PREFIX) :]
        type_name = receiver_type or (receiver if receiver[:1].isupper() else "")
        if not type_name:
            return None
        owner, _tier = self._target(
            path,
            Reference(
                name=type_name,
                kind="type",
                span=reference.span,
                container_id=reference.container_id,
            ),
        )
        return owner if owner is not None and owner.kind.is_type_like else None

    def _member_of(
        self, owner: Symbol | None, tier: ResolutionTier, reference: Reference
    ) -> tuple[Symbol | None, ResolutionTier]:
        if owner is None:
            # The lookup that failed has already been counted.
            return None, ResolutionTier.FUZZY
        if owner.kind is SymbolKind.MODULE:
            found = _prefer(self.index.in_file(owner.path, reference.name), reference)
            return (found, tier) if found is not None else self._unresolved()
        if not owner.kind.is_type_like and not self.index.has_members(owner.id):
            return self._unresolved()
        found = self._member_in_hierarchy(owner, reference)
        return (found, tier) if found is not None else self._unresolved()

    def _member_in_hierarchy(
        self, owner: Symbol, reference: Reference, *, skip_own: bool = False
    ) -> Symbol | None:
        """The member as declared on ``owner`` or the nearest ancestor that has it."""
        queue = [owner]
        seen: set[str] = set()
        while queue:
            current = queue.pop(0)
            if current.id in seen:
                continue
            seen.add(current.id)
            if not (skip_own and current.id == owner.id):
                found = _prefer(self.index.members(current.id, reference.name), reference)
                if found is not None:
                    return found
            queue.extend(self._ancestors.get(current.id, ()))
        return None

    def _receiver_via_import(self, path: str, receiver: str) -> Symbol | None:
        """What an imported name stands for, when it is a type or a module."""
        file_imports = self.imports.get(path)
        if file_imports is None:
            return None
        found = file_imports.binding_for(receiver)
        if found is None:
            return None
        statement, binding = found
        resolved_path = self._resolve_module(path, statement.module, statement.relative_level)
        if resolved_path is None:
            return None
        candidates = [
            symbol
            for symbol in self.index.in_file(resolved_path, binding.source_name)
            if symbol.kind.is_type_like or symbol.kind is SymbolKind.MODULE
        ]
        if candidates:
            return min(candidates, key=lambda s: (s.path, s.name_range.start, s.id))
        if binding.original is None:
            module_id = self.module_symbols.get(resolved_path)
            return self.index.get(module_id) if module_id else None
        return None

    def _unresolved(self) -> tuple[None, ResolutionTier]:
        self.stats.unresolved += 1
        return None, ResolutionTier.FUZZY

    def _import_resolves(self, path: str, name: str) -> bool:
        """Whether ``name`` was imported from a file this index covers.

        An import to a package, or through an alias nothing here knows,
        answers no: the name is not spoken for, and a framework convention
        may still say where it lives.
        """
        file_imports = self.imports.get(path)
        if file_imports is None:
            return False
        found = file_imports.binding_for(name)
        if found is None:
            return False
        statement, _binding = found
        return (
            self._resolve_module(path, statement.module, statement.relative_level)
            is not None
        )

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
