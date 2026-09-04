"""Edges that follow from other edges, once inheritance is resolved.

Two facts every compiler-backed index records and no tag query can see:
that a method *overrides* the one its base class declares, and that a class
*implements* an interface its base class implemented. Neither is written
anywhere in the overriding class. Both follow from the `extends` and
`implements` clauses once those have resolved, by walking up the chain and
comparing member names.

They matter to an agent for the same reason inheritance does. "What does
this override" is the question behind half of every change to a subclass,
and a map that shows `Admin` but not that its `greet` shadows `User.greet`
is hiding the thing most likely to break.

Derived edges carry no site, because there is no token to point at, and
they carry the confidence of the weakest link in the chain they were
derived through. They are recomputed whole whenever inheritance edges
change: the set is small, and patching it would mean tracking which
derivations each edge participated in.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping

from ..model import Edge, EdgeKind, ResolutionTier, Symbol

__all__ = ["derive_inheritance", "is_derived"]

_INHERITANCE = frozenset({EdgeKind.INHERITS, EdgeKind.IMPLEMENTS})

# Deeper than this is a hierarchy nobody wrote by hand, or a cycle.
_MAX_DEPTH = 12


def is_derived(edge: Edge) -> bool:
    """Whether an edge was derived here rather than resolved from a site."""
    return edge.kind is EdgeKind.IMPLEMENTS and edge.site_path is None


def derive_inheritance(
    symbols: Mapping[str, Symbol], edges: Iterable[Edge]
) -> list[Edge]:
    """Override and transitive-implements edges implied by ``edges``.

    Only resolved inheritance edges with a site are read; edges this
    function produced before are ignored, so it can be run on a snapshot
    that already carries its own output.
    """
    bases: dict[str, list[tuple[str, ResolutionTier]]] = defaultdict(list)
    for edge in edges:
        if edge.kind not in _INHERITANCE or is_derived(edge):
            continue
        if edge.src_id in symbols and edge.dst_id in symbols:
            bases[edge.src_id].append((edge.dst_id, edge.tier))

    members: dict[str, dict[str, Symbol]] = defaultdict(dict)
    for symbol in symbols.values():
        if symbol.container_id and symbol.kind.is_callable and not symbol.synthetic:
            members[symbol.container_id].setdefault(symbol.name, symbol)

    derived: list[Edge] = []
    seen: set[tuple[str, str]] = set()

    def emit(src: str, dst: str, tier: ResolutionTier) -> None:
        if src != dst and (src, dst) not in seen:
            seen.add((src, dst))
            derived.append(Edge(src_id=src, dst_id=dst, kind=EdgeKind.IMPLEMENTS, tier=tier))

    for class_id in sorted(bases):
        # Walk every ancestor, breadth first, remembering the weakest tier
        # on the way. Direct bases are already edges; only ancestors beyond
        # them, and overriding members at every level, are new.
        frontier = [(base, tier, 1) for base, tier in bases[class_id]]
        visited = {class_id}
        own = members.get(class_id, {})
        while frontier:
            ancestor, tier, depth = frontier.pop(0)
            if ancestor in visited or depth > _MAX_DEPTH:
                continue
            visited.add(ancestor)
            if depth > 1:
                emit(class_id, ancestor, tier)
            for name, method in own.items():
                shadowed = members.get(ancestor, {}).get(name)
                if shadowed is not None:
                    emit(method.id, shadowed.id, tier)
            for further, further_tier in bases.get(ancestor, ()):
                weaker = further_tier if further_tier.default_confidence < tier.default_confidence else tier
                frontier.append((further, weaker, depth + 1))

    derived.sort(key=lambda edge: (edge.src_id, edge.dst_id))
    return derived
