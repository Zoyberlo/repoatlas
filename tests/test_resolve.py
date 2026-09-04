"""Tests for cross-file resolution.

The cascade's job is not to be right every time, which without a compiler
is impossible, but to be right as often as it claims. So these tests check
two things: that each rung fires on the evidence it is meant to, and that a
weaker rung never wins over a stronger one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoatlas.model import EdgeKind, ResolutionTier, SourceRange, Symbol, SymbolKind
from repoatlas.parse.imports import FileImports, ImportBinding, ImportStatement
from repoatlas.resolve.cascade import ResolutionStats, Resolver, SymbolIndex
from repoatlas.resolve.modules import (
    ComposerResolver,
    NodeResolver,
    PythonResolver,
    resolver_for,
)

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

from repoatlas.parse.build import build_snapshot
from repoatlas.parse.extract import Reference, extract_source
from repoatlas.parse.languages import language_for_path

FIXTURE = Path(__file__).parent / "fixtures" / "tsdemo"


def extract(path: str, source: bytes):
    spec = language_for_path(path)
    assert spec is not None
    return extract_source(path, source, spec)


class TestImportExtraction:
    def test_python_named_and_aliased_imports(self) -> None:
        result = extract("a.py", b"from a.b import C as D, E\n")
        statement = result.imports.statements[0]
        assert statement.module == "a.b"
        assert [(b.local, b.source_name) for b in statement.bindings] == [
            ("D", "C"),
            ("E", "E"),
        ]

    def test_python_relative_imports_count_their_dots(self) -> None:
        result = extract("pkg/a.py", b"from . import sib\nfrom ..other import Deep\n")
        first, second = result.imports.statements
        assert (first.module, first.relative_level) == ("", 1)
        assert (second.module, second.relative_level) == ("other", 2)
        assert first.is_relative and second.is_relative

    def test_a_plain_python_import_binds_its_first_segment(self) -> None:
        result = extract("a.py", b"import os.path\nimport numpy as np\n")
        modules = [(s.module, s.bindings[0].local) for s in result.imports.statements]
        assert modules == [("os.path", "os"), ("numpy", "np")]

    def test_typescript_named_default_and_namespace_clauses(self) -> None:
        result = extract(
            "a.ts",
            b'import { User, makeUser as mk } from "./user";\n'
            b'import React from "react";\n'
            b'import * as fs from "node:fs";\n',
        )
        by_module = {s.module: s for s in result.imports.statements}
        assert [(b.local, b.source_name) for b in by_module["./user"].bindings] == [
            ("User", "User"),
            ("mk", "makeUser"),
        ]
        assert by_module["react"].bindings[0].local == "React"
        assert by_module["node:fs"].bindings[0].local == "fs"

    def test_a_side_effect_import_records_the_module_and_no_binding(self) -> None:
        result = extract("a.ts", b'import "./styles";\n')
        statement = result.imports.statements[0]
        assert statement.module == "./styles"
        assert statement.bindings == ()

    def test_php_use_declarations_and_the_file_namespace(self) -> None:
        result = extract(
            "a.php",
            b"<?php\nnamespace App\\Http;\n"
            b"use App\\Models\\User;\nuse App\\Contracts\\Greeter as G;\n",
        )
        assert result.imports.namespace == "App\\Http"
        pairs = [
            (s.module, s.bindings[0].local, s.bindings[0].source_name)
            for s in result.imports.statements
        ]
        assert pairs == [
            ("App\\Models\\User", "User", "User"),
            ("App\\Contracts\\Greeter", "G", "Greeter"),
        ]

    def test_a_file_with_no_imports_yields_nothing(self) -> None:
        assert extract("a.py", b"x = 1\n").imports.statements == []


class TestNodeResolver:
    @pytest.fixture
    def resolver(self) -> NodeResolver:
        return NodeResolver(
            known_files=frozenset(
                {
                    "src/user.ts",
                    "src/app.tsx",
                    "src/deep/index.ts",
                    "lib/helper.js",
                    "src/types.d.ts",
                }
            )
        )

    @pytest.mark.parametrize(
        ("module", "expected"),
        [
            ("./user", "src/user.ts"),
            ("./app", "src/app.tsx"),
            ("./deep", "src/deep/index.ts"),
            ("../lib/helper", "lib/helper.js"),
            ("./user.js", "src/user.ts"),
        ],
    )
    def test_probes_extensions_and_index_files(
        self, resolver: NodeResolver, module: str, expected: str
    ) -> None:
        assert resolver.resolve(module, from_path="src/main.ts") == expected

    def test_a_bare_specifier_is_a_package_not_a_file(self, resolver: NodeResolver) -> None:
        assert resolver.resolve("react", from_path="src/main.ts") is None

    def test_a_relative_path_that_matches_nothing_stays_unresolved(
        self, resolver: NodeResolver
    ) -> None:
        assert resolver.resolve("./missing", from_path="src/main.ts") is None

    def test_follows_a_tsconfig_path_alias(self) -> None:
        resolver = NodeResolver(
            known_files=frozenset({"src/lib/thing.ts"}),
            aliases={"@lib/*": ("src/lib/*",)},
        )
        assert resolver.resolve("@lib/thing", from_path="src/main.ts") == "src/lib/thing.ts"

    def test_follows_base_url(self) -> None:
        resolver = NodeResolver(
            known_files=frozenset({"src/thing.ts"}), base_urls=("src",)
        )
        assert resolver.resolve("thing", from_path="app/main.ts") == "src/thing.ts"

    def test_reads_tsconfig_from_a_project(self, tmp_path: Path) -> None:
        (tmp_path / "tsconfig.json").write_text(
            """{
  // a comment, which tsconfig allows and JSON does not
  "compilerOptions": {
    "baseUrl": ".",
    "paths": { "@app/*": ["src/*"] },
  }
}""",
            encoding="utf-8",
        )
        resolver = resolver_for("typescript", tmp_path, frozenset({"src/thing.ts"}))
        assert resolver is not None
        assert resolver.resolve("@app/thing", from_path="x.ts") == "src/thing.ts"

    def test_an_unparseable_tsconfig_costs_aliases_not_the_run(self, tmp_path: Path) -> None:
        (tmp_path / "tsconfig.json").write_text("{ this is not json", encoding="utf-8")
        resolver = resolver_for("typescript", tmp_path, frozenset({"src/a.ts"}))
        assert resolver is not None
        assert resolver.resolve("./a", from_path="src/b.ts") == "src/a.ts"


class TestPythonResolver:
    @pytest.fixture
    def resolver(self) -> PythonResolver:
        return PythonResolver(
            known_files=frozenset(
                {"pkg/mod.py", "pkg/__init__.py", "src/other/thing.py", "top.py"}
            )
        )

    def test_maps_dots_to_directories(self, resolver: PythonResolver) -> None:
        assert resolver.resolve("pkg.mod", from_path="main.py") == "pkg/mod.py"

    def test_finds_a_package_init(self, resolver: PythonResolver) -> None:
        assert resolver.resolve("pkg", from_path="main.py") == "pkg/__init__.py"

    def test_searches_conventional_source_roots(self, resolver: PythonResolver) -> None:
        assert resolver.resolve("other.thing", from_path="main.py") == "src/other/thing.py"

    def test_resolves_a_relative_import_against_the_importing_file(
        self, resolver: PythonResolver
    ) -> None:
        assert resolver.resolve("mod", from_path="pkg/a.py", relative_level=1) == "pkg/mod.py"

    def test_a_third_party_module_stays_unresolved(self, resolver: PythonResolver) -> None:
        assert resolver.resolve("numpy.linalg", from_path="main.py") is None


class TestComposerResolver:
    def test_maps_a_psr4_prefix_to_a_directory(self, tmp_path: Path) -> None:
        (tmp_path / "composer.json").write_text(
            json.dumps({"autoload": {"psr-4": {"App\\": "app/"}}}), encoding="utf-8"
        )
        resolver = resolver_for("php", tmp_path, frozenset({"app/Models/User.php"}))
        assert resolver is not None
        assert (
            resolver.resolve("App\\Models\\User", from_path="app/Http/C.php")
            == "app/Models/User.php"
        )

    def test_prefers_the_longest_matching_prefix(self) -> None:
        resolver = ComposerResolver(
            known_files=frozenset({"app/User.php", "modules/admin/User.php"}),
            prefixes=(("App\\Admin\\", ("modules/admin",)), ("App\\", ("app",))),
        )
        assert (
            resolver.resolve("App\\Admin\\User", from_path="x.php")
            == "modules/admin/User.php"
        )

    def test_a_vendor_class_stays_unresolved(self) -> None:
        resolver = ComposerResolver(
            known_files=frozenset({"app/User.php"}), prefixes=(("App\\", ("app",)),)
        )
        assert resolver.resolve("Illuminate\\Support\\Str", from_path="x.php") is None


def _symbol(
    symbol_id: str,
    name: str,
    path: str,
    line: int,
    kind: SymbolKind = SymbolKind.FUNCTION,
    container: str | None = None,
    local: bool = False,
) -> Symbol:
    return Symbol(
        id=symbol_id,
        name=name,
        kind=kind,
        path=path,
        name_range=SourceRange.of(line, 0, line, len(name)),
        full_range=SourceRange.of(line, 0, line + 5, 0),
        container_id=container,
        qualified_name=name,
        local=local,
    )


class TestCascade:
    """Each rung fires on its own evidence, and stronger evidence wins."""

    @staticmethod
    def _resolver(symbols: list[Symbol], imports: dict[str, FileImports] | None = None) -> Resolver:
        table = {s.id: s for s in symbols}
        return Resolver(
            index=SymbolIndex(table),
            imports=imports or {},
            resolvers={},
            languages={},
            module_symbols={},
            stats=ResolutionStats(),
        )

    def test_an_import_that_names_a_file_gives_the_strongest_edge(self) -> None:
        target = _symbol("user.ts#User", "User", "user.ts", 0, SymbolKind.CLASS)
        caller = _symbol("app.ts#run", "run", "app.ts", 3)
        decoy = _symbol("other.ts#User", "User", "other.ts", 0, SymbolKind.CLASS)
        imports = {
            "app.ts": FileImports(
                statements=[
                    ImportStatement(
                        module="./user",
                        bindings=(
                            ImportBinding("User", None, SourceRange.of(0, 9, 0, 13)),
                        ),
                        span=SourceRange.of(0, 0, 0, 30),
                    )
                ]
            )
        }
        resolver = self._resolver([target, caller, decoy], imports)
        resolver.resolvers = {"ts": _FixedResolver({"./user": "user.ts"})}
        resolver.languages = {"app.ts": "ts"}
        edges = resolver.resolve_file(
            "app.ts", [Reference("User", "call", SourceRange.of(4, 2, 4, 6), caller.id)]
        )
        assert len(edges) == 1
        assert edges[0].dst_id == target.id, "the imported file wins over the decoy"
        assert edges[0].tier is ResolutionTier.IMPORT_MAP

    def test_a_member_resolves_inside_the_enclosing_type_first(self) -> None:
        # `this.greet()` inside User.shout means User.greet, even though an
        # interface declares a greet of its own.
        klass = _symbol("u.ts#User", "User", "u.ts", 6, SymbolKind.CLASS)
        method = _symbol("u.ts#User.greet", "greet", "u.ts", 13, SymbolKind.METHOD, klass.id)
        shout = _symbol("u.ts#User.shout", "shout", "u.ts", 17, SymbolKind.METHOD, klass.id)
        interface = _symbol("u.ts#Greets", "Greets", "u.ts", 0, SymbolKind.INTERFACE)
        other = _symbol("u.ts#Greets.greet", "greet", "u.ts", 1, SymbolKind.METHOD, interface.id)
        resolver = self._resolver([klass, method, shout, interface, other])
        edges = resolver.resolve_file(
            "u.ts",
            [
                Reference(
                    "greet", "call", SourceRange.of(18, 16, 18, 21), shout.id, receiver="this"
                )
            ],
        )
        assert edges[0].dst_id == method.id

    def test_a_name_defined_in_the_same_file_beats_a_repository_search(self) -> None:
        local_def = _symbol("a.py#helper", "helper", "a.py", 0)
        elsewhere = _symbol("b.py#helper", "helper", "b.py", 0)
        caller = _symbol("a.py#main", "main", "a.py", 10)
        resolver = self._resolver([local_def, elsewhere, caller])
        edges = resolver.resolve_file(
            "a.py", [Reference("helper", "call", SourceRange.of(11, 4, 11, 10), caller.id)]
        )
        assert edges[0].dst_id == local_def.id
        assert edges[0].tier is ResolutionTier.SAME_MODULE

    def test_a_single_definition_anywhere_is_the_unique_name_rung(self) -> None:
        target = _symbol("b.py#only", "only", "b.py", 0)
        caller = _symbol("a.py#main", "main", "a.py", 10)
        resolver = self._resolver([target, caller])
        edges = resolver.resolve_file(
            "a.py", [Reference("only", "call", SourceRange.of(11, 4, 11, 8), caller.id)]
        )
        assert edges[0].tier is ResolutionTier.UNIQUE_NAME

    def test_several_candidates_land_on_the_weakest_rung(self) -> None:
        first = _symbol("b.py#same", "same", "b.py", 0)
        second = _symbol("c.py#same", "same", "c.py", 0)
        caller = _symbol("a.py#main", "main", "a.py", 10)
        resolver = self._resolver([first, second, caller])
        edges = resolver.resolve_file(
            "a.py", [Reference("same", "call", SourceRange.of(11, 4, 11, 8), caller.id)]
        )
        assert edges[0].tier is ResolutionTier.SUFFIX
        assert resolver.stats.ambiguous == 1

    def test_an_unknown_name_produces_no_edge_rather_than_a_guess(self) -> None:
        caller = _symbol("a.py#main", "main", "a.py", 10)
        resolver = self._resolver([caller])
        edges = resolver.resolve_file(
            "a.py", [Reference("nowhere", "call", SourceRange.of(11, 4, 11, 11), caller.id)]
        )
        assert edges == []
        assert resolver.stats.unresolved == 1

    def test_a_local_binding_is_never_a_target(self) -> None:
        # Not even in its own file. An agent cannot navigate to a loop
        # counter, so an edge to one is work that gets filtered anyway, and
        # letting a local win would shadow the real definition.
        public = _symbol("b.py#thing", "thing", "b.py", 0)
        hidden = _symbol("a.py#main.thing", "thing", "a.py", 11, local=True)
        caller = _symbol("a.py#main", "main", "a.py", 10)
        resolver = self._resolver([public, hidden, caller])
        edges = resolver.resolve_file(
            "a.py", [Reference("thing", "call", SourceRange.of(12, 4, 12, 9), caller.id)]
        )
        assert edges[0].dst_id == public.id

    def test_a_call_prefers_a_callable_over_a_field_of_the_same_name(self) -> None:
        field = _symbol("b.py#run", "run", "b.py", 0, SymbolKind.FIELD)
        function = _symbol("c.py#run", "run", "c.py", 0, SymbolKind.FUNCTION)
        caller = _symbol("a.py#main", "main", "a.py", 10)
        resolver = self._resolver([field, function, caller])
        edges = resolver.resolve_file(
            "a.py", [Reference("run", "call", SourceRange.of(11, 4, 11, 7), caller.id)]
        )
        assert edges[0].dst_id == function.id

    def test_an_inheritance_reference_only_matches_a_type(self) -> None:
        function = _symbol("b.py#Base", "Base", "b.py", 0, SymbolKind.FUNCTION)
        klass = _symbol("c.py#Base", "Base", "c.py", 0, SymbolKind.CLASS)
        child = _symbol("a.py#Child", "Child", "a.py", 10, SymbolKind.CLASS)
        resolver = self._resolver([function, klass, child])
        edges = resolver.resolve_file(
            "a.py", [Reference("Base", "class", SourceRange.of(10, 12, 10, 16), child.id)]
        )
        assert edges[0].dst_id == klass.id
        assert edges[0].kind is EdgeKind.INHERITS

    def test_a_new_expression_targets_the_constructor(self) -> None:
        klass = _symbol("u.ts#User", "User", "u.ts", 6, SymbolKind.CLASS)
        ctor = _symbol(
            "u.ts#User.constructor", "constructor", "u.ts", 9, SymbolKind.CONSTRUCTOR, klass.id
        )
        caller = _symbol("u.ts#make", "make", "u.ts", 22)
        resolver = self._resolver([klass, ctor, caller])
        edges = resolver.resolve_file(
            "u.ts", [Reference("User", "construct", SourceRange.of(23, 13, 23, 17), caller.id)]
        )
        assert edges[0].dst_id == ctor.id

    def test_a_class_without_a_constructor_stays_the_target(self) -> None:
        klass = _symbol("u.ts#Plain", "Plain", "u.ts", 0, SymbolKind.CLASS)
        caller = _symbol("u.ts#make", "make", "u.ts", 22)
        resolver = self._resolver([klass, caller])
        edges = resolver.resolve_file(
            "u.ts", [Reference("Plain", "construct", SourceRange.of(23, 13, 23, 18), caller.id)]
        )
        assert edges[0].dst_id == klass.id

    def test_recursion_is_a_call_but_a_type_naming_itself_is_not(self) -> None:
        # A function calling itself is a call, and an oracle records it. A
        # class naming its own type in its body is not a dependency.
        recursive = _symbol("a.py#loop", "loop", "a.py", 0)
        klass = _symbol("a.py#K", "K", "a.py", 5, SymbolKind.CLASS)
        resolver = self._resolver([recursive, klass])
        edges = resolver.resolve_file(
            "a.py",
            [
                Reference("loop", "call", SourceRange.of(1, 4, 1, 8), recursive.id),
                Reference("K", "type", SourceRange.of(6, 4, 6, 5), klass.id),
            ],
        )
        assert [(e.src_id, e.dst_id) for e in edges] == [(recursive.id, recursive.id)]

    def test_stats_report_the_shape_of_the_evidence(self) -> None:
        target = _symbol("b.py#only", "only", "b.py", 0)
        caller = _symbol("a.py#main", "main", "a.py", 10)
        resolver = self._resolver([target, caller])
        resolver.resolve_file(
            "a.py",
            [
                Reference("only", "call", SourceRange.of(11, 4, 11, 8), caller.id),
                Reference("gone", "call", SourceRange.of(12, 4, 12, 8), caller.id),
            ],
        )
        payload = resolver.stats.as_dict()
        assert payload["resolved"] == 1
        assert payload["unresolved"] == 1
        assert payload["resolution_rate"] == 0.5


class _FixedResolver:
    """A module resolver with a fixed table, for testing the cascade alone."""

    def __init__(self, table: dict[str, str]) -> None:
        self.table = table

    def resolve(self, module: str, *, from_path: str, relative_level: int = 0) -> str | None:
        return self.table.get(module)


class TestEndToEnd:
    def test_the_fixture_resolves_most_of_its_references(self) -> None:
        build = build_snapshot(FIXTURE, use_git=False)
        assert build.resolution.resolution_rate > 0.8
        assert build.resolution.by_tier["import_map"] > 0

    def test_resolution_can_be_skipped(self) -> None:
        build = build_snapshot(FIXTURE, use_git=False, resolve=False)
        assert build.resolution.resolved == 0
        assert all(e.kind is EdgeKind.CONTAINS for e in build.snapshot.edges)

    def test_an_import_edge_points_across_files(self) -> None:
        build = build_snapshot(FIXTURE, use_git=False)
        symbols = build.snapshot.symbols
        crossing = [
            e
            for e in build.snapshot.edges
            if e.site_path == "src/app.ts" and symbols[e.dst_id].path == "src/user.ts"
        ]
        assert crossing, "app.ts imports from user.ts"
        assert all(e.tier is not ResolutionTier.FUZZY for e in crossing)

    def test_resolution_is_deterministic(self) -> None:
        first = build_snapshot(FIXTURE, use_git=False).snapshot
        second = build_snapshot(FIXTURE, use_git=False).snapshot

        def key(snapshot):
            return sorted(
                (e.src_id, e.dst_id, e.kind.value, e.tier.label) for e in snapshot.edges
            )

        assert key(first) == key(second)


class TestTypedReceivers:
    """A member reached through a typed receiver goes to that type's member."""

    def project(self, root: Path) -> None:
        (root / "greeter.py").write_text(
            "class Greeter:\n"
            "    def greet(self, name):\n"
            "        return name\n"
            "\n\n"
            "class Other:\n"
            "    def greet(self, name):\n"
            "        return name\n",
            encoding="utf-8",
        )
        (root / "loud.py").write_text(
            "from greeter import Greeter\n\n\n"
            "class LoudGreeter(Greeter):\n"
            "    pass\n",
            encoding="utf-8",
        )
        (root / "app.py").write_text(
            "from greeter import Greeter\n"
            "from loud import LoudGreeter\n\n\n"
            "def run(g: Greeter, loud: LoudGreeter):\n"
            "    return g.greet('a'), loud.greet('b')\n",
            encoding="utf-8",
        )

    def edges_from(self, root: Path) -> dict[int, str]:
        result = build_snapshot(root, use_git=False)
        return {
            e.site_range.start.character: e.dst_id
            for e in result.snapshot.edges
            if e.site_path == "app.py" and e.site_range and e.site_range.start.line == 5
        }

    def test_the_declared_type_decides_between_two_greets(self, tmp_path: Path) -> None:
        # Two classes define `greet`; without the type of `g` the bottom
        # rung would pick one by position. The annotation says which.
        self.project(tmp_path)
        edges = self.edges_from(tmp_path)
        assert edges[13] == "greeter.py#Greeter.greet"

    def test_an_inherited_member_is_found_up_the_chain_across_files(self, tmp_path: Path) -> None:
        # `loud: LoudGreeter` has no `greet` of its own; it inherits one from
        # a class in a third file. The chain has to be learned before any
        # lookup walks it.
        self.project(tmp_path)
        edges = self.edges_from(tmp_path)
        assert edges[30] == "greeter.py#Greeter.greet"

    def test_a_typed_receiver_carries_the_tier_of_its_type(self, tmp_path: Path) -> None:
        self.project(tmp_path)
        result = build_snapshot(tmp_path, use_git=False)
        edge = next(
            e for e in result.snapshot.edges
            if e.site_path == "app.py" and e.site_range and e.site_range.start.character == 13
        )
        assert edge.tier.label == "import_map"


class TestDerivedInheritance:
    def project(self, root: Path) -> None:
        (root / "base.ts").write_text(
            "export interface Greets { greet(): string; }\n"
            "export class User implements Greets { greet(): string { return 'u'; } }\n",
            encoding="utf-8",
        )
        (root / "admin.ts").write_text(
            "import { User } from './base';\n"
            "export class Admin extends User { greet(): string { return 'a'; } }\n",
            encoding="utf-8",
        )

    def test_an_override_is_an_edge_to_what_it_overrides(self, tmp_path: Path) -> None:
        self.project(tmp_path)
        result = build_snapshot(tmp_path, use_git=False)
        pairs = {(e.src_id, e.dst_id) for e in result.snapshot.edges if e.kind.value == "implements"}
        assert ("admin.ts#Admin.greet", "base.ts#User.greet") in pairs
        assert ("base.ts#User.greet", "base.ts#Greets.greet") in pairs

    def test_an_interface_is_implemented_transitively(self, tmp_path: Path) -> None:
        self.project(tmp_path)
        result = build_snapshot(tmp_path, use_git=False)
        pairs = {(e.src_id, e.dst_id) for e in result.snapshot.edges if e.kind.value == "implements"}
        assert ("admin.ts#Admin", "base.ts#Greets") in pairs
        # Two levels up, the override reaches the interface method too.
        assert ("admin.ts#Admin.greet", "base.ts#Greets.greet") in pairs

    def test_derived_edges_carry_no_site_and_are_deterministic(self, tmp_path: Path) -> None:
        from repoatlas.resolve.derived import derive_inheritance, is_derived

        self.project(tmp_path)
        result = build_snapshot(tmp_path, use_git=False)
        derived = [e for e in result.snapshot.edges if is_derived(e)]
        assert derived and all(e.site_path is None for e in derived)
        again = derive_inheritance(result.snapshot.symbols, result.snapshot.edges)
        assert [(e.src_id, e.dst_id) for e in again] == [(e.src_id, e.dst_id) for e in derived]

    def test_the_store_derives_the_same_edges(self, tmp_path: Path) -> None:
        from repoatlas.store import IndexStore, update_store

        project = tmp_path / "p"
        project.mkdir()
        self.project(project)
        with IndexStore(tmp_path / "i.db") as store:
            update_store(project, store, use_git=False)
            stored = {(e.src_id, e.dst_id) for e in store.edges() if e.kind.value == "implements"}
        direct = {
            (e.src_id, e.dst_id)
            for e in build_snapshot(project, use_git=False).snapshot.edges
            if e.kind.value == "implements"
        }
        assert stored == direct


class TestMemberRungs:
    """A member resolves through its receiver, or not at all.

    Every test here is a false positive or a miss a real Laravel
    application produced against scip-php, reduced to the two files that
    reproduce it.
    """

    def test_an_untyped_receiver_does_not_resolve_by_name_across_files(self, tmp_path: Path) -> None:
        # `$order->update([])` was resolving to a controller's `update`, and
        # `$order->id` to a job's `$id`: 0 of 890 such edges were right.
        (tmp_path / "UserController.php").write_text(
            "<?php\nclass UserController { public function update() {} public $id; }\n",
            encoding="utf-8",
        )
        (tmp_path / "run.php").write_text(
            "<?php\nfunction run($ad) { $order->update([]); return $order->id; }\n", encoding="utf-8"
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert [e for e in edges if e.kind.value != "contains" and e.site_path == "run.php"] == []

    def test_a_static_call_on_a_foreign_class_stops_at_that_class(self, tmp_path: Path) -> None:
        # `Auth::user()` inside a controller that has its own `user()`
        # method: the receiver is the facade, which is not ours, and the
        # enclosing class has nothing to do with it.
        (tmp_path / "AuthController.php").write_text(
            "<?php\nuse Illuminate\\Support\\Facades\\Auth;\n"
            "class AuthController { public function user() {} "
            "public function me() { return Auth::user(); } }\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert not [e for e in edges if e.kind.value != "contains" and e.dst_id.endswith("AuthController.user")]

    def test_a_member_of_an_expression_is_not_looked_up_by_name(self, tmp_path: Path) -> None:
        # `Auth::guard('web')->login($user)` in a controller with a
        # `login()` action: the receiver is whatever `guard()` returned.
        (tmp_path / "AuthController.php").write_text(
            "<?php\nclass AuthController { public function login() {} "
            "public function go() { Auth::guard('web')->login(1); } }\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert not [e for e in edges if e.kind.value != "contains" and e.dst_id.endswith("AuthController.login")]

    def test_a_static_call_does_not_make_its_class_a_base(self, tmp_path: Path) -> None:
        # `Client::where()` inside a command once recorded Client as what
        # the command extends, and `parent::__construct()` went there.
        (tmp_path / "Client.php").write_text(
            "<?php\nclass Client { public static function where() {} }\n", encoding="utf-8"
        )
        (tmp_path / "Cmd.php").write_text(
            "<?php\nclass Cmd extends Command { public function __construct() "
            "{ parent::__construct(); Client::where(); } }\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert not [e for e in edges if e.kind.value != "contains" and e.kind.value == "inherits" and e.src_id == "Cmd.php#Cmd"]
        targets = {e.dst_id for e in edges if e.kind.value != "contains" and e.site_path == "Cmd.php"}
        assert targets == {"Client.php#Client", "Client.php#Client.where"}

    def test_a_typed_property_carries_calls_through_this(self, tmp_path: Path) -> None:
        # The most common miss: `$this->service->handle()` where the
        # constructor promoted `private Service $service`.
        (tmp_path / "Svc.php").write_text(
            "<?php\nclass Svc { public function go() {} }\n", encoding="utf-8"
        )
        (tmp_path / "Ctl.php").write_text(
            "<?php\nclass Ctl { private Svc $svc; "
            "public function __construct(private Svc $other) {} "
            "public function h() { $this->svc->go(); $this->other->go(); } }\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        calls = [e for e in edges if e.kind.value != "contains" and e.site_path == "Ctl.php" and e.dst_id == "Svc.php#Svc.go"]
        assert len(calls) == 2

    def test_a_typed_field_carries_calls_through_this_in_typescript(self, tmp_path: Path) -> None:
        (tmp_path / "svc.ts").write_text("export class Svc { go(): void {} }\n", encoding="utf-8")
        (tmp_path / "ctl.ts").write_text(
            "import { Svc } from './svc';\n"
            "export class Ctl { private svc: Svc; constructor(private other: Svc) {} "
            "run() { this.svc.go(); this.other.go(); } }\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        calls = [e for e in edges if e.kind.value != "contains" and e.site_path == "ctl.ts" and e.dst_id == "svc.ts#Svc.go"]
        assert len(calls) == 2
        assert {e.tier.label for e in calls} == {"import_map"}

    def test_an_inherited_method_is_found_through_this(self, tmp_path: Path) -> None:
        (tmp_path / "Base.php").write_text(
            "<?php\nclass Base { protected function helper() {} }\n", encoding="utf-8"
        )
        (tmp_path / "Child.php").write_text(
            "<?php\nclass Child extends Base { public function run() { $this->helper(); } }\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert any(
            e.site_path == "Child.php"
            and e.dst_id == "Base.php#Base.helper"
            and e.tier.label == "same_module"
            for e in edges
        )

    def test_a_trait_method_is_found_through_this(self, tmp_path: Path) -> None:
        (tmp_path / "HasName.php").write_text(
            "<?php\ntrait HasName { public function name() {} }\n", encoding="utf-8"
        )
        (tmp_path / "User.php").write_text(
            "<?php\nclass User { use HasName; public function greet() { return $this->name(); } }\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert any(
            e.site_path == "User.php" and e.dst_id == "HasName.php#HasName.name" for e in edges
        )

    def test_this_in_a_vue_options_object_reaches_the_file_own_methods(self, tmp_path: Path) -> None:
        (tmp_path / "Comp.vue").write_text(
            "<template><div/></template>\n<script>\n"
            "export default { methods: { bar() {}, foo() { this.bar(); } } }\n</script>\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert any(
            e.site_path == "Comp.vue" and e.dst_id.endswith("bar") and e.kind.value == "calls"
            for e in edges
        )

    def test_an_aliased_external_import_does_not_resolve_to_a_namesake(self, tmp_path: Path) -> None:
        # `use Foundation\Console\Kernel as ConsoleKernel;` above
        # `class Kernel`: the import site resolved to the class under it.
        (tmp_path / "Kernel.php").write_text(
            "<?php\nuse Illuminate\\Foundation\\Console\\Kernel as ConsoleKernel;\n"
            "class Kernel extends ConsoleKernel {}\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert [e for e in edges if e.kind.value != "contains" and e.site_path == "Kernel.php" and e.site_range] == []

    def test_this_inside_an_anonymous_class_is_that_class(self, tmp_path: Path) -> None:
        (tmp_path / "Export.php").write_text(
            "<?php\nclass Export { protected $data; }\n", encoding="utf-8"
        )
        (tmp_path / "make.php").write_text(
            "<?php\nfunction make($d) { return new class($d) { protected $data; "
            "public function __construct($d) { $this->data = $d; } }; }\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        targets = {e.dst_id for e in edges if e.kind.value != "contains" and e.site_path == "make.php" and e.site_range}
        assert targets == {"make.php#make.class@anonymous.data"}


class TestReturnTypesAndRecursion:
    def test_a_local_assigned_from_a_call_takes_the_callee_return_type(self, tmp_path: Path) -> None:
        # The phpdemo edge scip-php could not see: `$greeter = $this->build(true)`
        # where `build(): Greeter`.
        (tmp_path / "Greeter.php").write_text(
            "<?php\nclass Greeter { public function greet() {} }\n", encoding="utf-8"
        )
        (tmp_path / "App.php").write_text(
            "<?php\nclass App { public function build(): Greeter { return new Greeter(); } "
            "public function run() { $g = $this->build(); return $g->greet(); } }\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert any(e.site_path == "App.php" and e.dst_id == "Greeter.php#Greeter.greet" for e in edges)

    def test_a_python_class_called_directly_types_the_local(self, tmp_path: Path) -> None:
        (tmp_path / "models.py").write_text(
            "class Admin:\n    def audit(self):\n        pass\n\n\n"
            "def build() -> Admin:\n    return Admin()\n",
            encoding="utf-8",
        )
        (tmp_path / "app.py").write_text(
            "from models import Admin, build\n\n\n"
            "def run():\n    a = Admin()\n    b = build()\n    return a.audit(), b.audit()\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        audits = [e for e in edges if e.site_path == "app.py" and e.dst_id == "models.py#Admin.audit"]
        assert len(audits) == 2

    def test_a_typescript_local_from_a_call_is_typed(self, tmp_path: Path) -> None:
        (tmp_path / "user.ts").write_text(
            "export class User { greet(): string { return 'x'; } }\n"
            "export function makeUser(): User { return new User(); }\n"
            "export async function loadUser(): Promise<User> { return new User(); }\n",
            encoding="utf-8",
        )
        (tmp_path / "app.ts").write_text(
            "import { makeUser, loadUser } from './user';\n"
            "export async function run() { const u = makeUser(); const v = await loadUser(); "
            "return u.greet() + v.greet(); }\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        greets = [e for e in edges if e.site_path == "app.ts" and e.dst_id == "user.ts#User.greet"]
        assert len(greets) == 2

    def test_recursion_is_a_call(self, tmp_path: Path) -> None:
        (tmp_path / "K.php").write_text(
            "<?php\nclass K { public static function walk($n) { return self::walk($n - 1); } "
            "public function again() { return $this->again(); } }\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        calls = {(e.src_id, e.dst_id) for e in edges if e.kind.value == "calls"}
        assert ("K.php#K.walk", "K.php#K.walk") in calls
        assert ("K.php#K.again", "K.php#K.again") in calls

    def test_static_as_a_return_type_is_the_enclosing_class(self, tmp_path: Path) -> None:
        (tmp_path / "F.php").write_text(
            "<?php\nclass F { public function unverified(): static { return $this; } }\n",
            encoding="utf-8",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert any(e.kind.value == "uses_type" and e.dst_id == "F.php#F" for e in edges)

    def test_a_builtin_function_does_not_reach_a_field_of_the_same_name(self, tmp_path: Path) -> None:
        # PHP's `end($list)` was resolving to `public $end` in some class.
        (tmp_path / "P.php").write_text(
            "<?php\nclass P { public $end = 1; }\n", encoding="utf-8"
        )
        (tmp_path / "f.php").write_text(
            "<?php\nfunction last($xs) { return end($xs); }\n", encoding="utf-8"
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert not [e for e in edges if e.site_path == "f.php" and e.kind.value != "contains"]


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


LARAVEL_COMPOSER = (
    '{"require": {"laravel/framework": "^10"}, '
    '"autoload": {"psr-4": {"App\\\\": "app/"}}}'
)


class TestRealProjectShapes:
    """Each test is a shape the real Laravel + Quasar monorepo had and the index missed."""

    def test_composer_is_read_in_the_project_directory_of_a_monorepo(self, tmp_path: Path) -> None:
        # `backend/composer.json` declares `App\` as `backend/app/`. Read at
        # the repository root it was not there, and 91% of the real
        # application's references went unresolved.
        _write(tmp_path, "backend/composer.json", LARAVEL_COMPOSER)
        _write(
            tmp_path,
            "backend/app/Models/Ad.php",
            "<?php\nnamespace App\\Models;\nclass Ad extends Model { public function client() {} }\n",
        )
        _write(
            tmp_path,
            "backend/app/Services/AdService.php",
            "<?php\nnamespace App\\Services;\nuse App\\Models\\Ad;\n"
            "class AdService { public function f(Ad $ad) { return $ad->client(); } }\n",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        targets = {e.dst_id for e in edges if e.site_path == "backend/app/Services/AdService.php"}
        assert "backend/app/Models/Ad.php#Ad" in targets
        assert "backend/app/Models/Ad.php#Ad.client" in targets

    def test_a_pinia_store_is_the_type_of_what_its_hook_returns(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "src/stores/auth.js",
            'export const useAuthStore = defineStore("auth", { actions: { async fetchUser() {} } });\n',
        )
        _write(
            tmp_path,
            "src/page.js",
            'import { useAuthStore } from "./stores/auth";\n'
            "export function run() { const store = useAuthStore(); return store.fetchUser(); }\n",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert any(e.dst_id == "src/stores/auth.js#useAuthStore.fetchUser" for e in edges)

    def test_eloquent_finders_return_the_model(self, tmp_path: Path) -> None:
        # `Ad::find(1)` and `$ad->fresh()` are Ads, though Eloquent supplies
        # both methods and the model declares neither.
        _write(tmp_path, "composer.json", LARAVEL_COMPOSER)
        _write(
            tmp_path,
            "app/Models/Ad.php",
            "<?php\nnamespace App\\Models;\nclass Ad extends Model { public function client() {} }\n",
        )
        _write(
            tmp_path,
            "app/Svc.php",
            "<?php\nnamespace App;\nuse App\\Models\\Ad;\n"
            "class Svc { public function f() { $ad = Ad::find(1); $ad->client(); "
            "$b = $ad->fresh(); return $b->client(); } }\n",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        hits = [e for e in edges if e.dst_id == "app/Models/Ad.php#Ad.client" and e.kind.value == "calls"]
        assert len(hits) == 2
        assert {e.tier.label for e in hits} == {"unique_name"}

    def test_a_docblock_return_types_the_local_when_the_signature_does_not(self, tmp_path: Path) -> None:
        _write(tmp_path, "Greeter.php", "<?php\nclass Greeter { public function greet() {} }\n")
        _write(
            tmp_path,
            "App.php",
            "<?php\nclass App {\n    /** @return \\Greeter|null */\n    public function build() {}\n"
            "    public function run() { $g = $this->build(); return $g->greet(); }\n}\n",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert any(e.dst_id == "Greeter.php#Greeter.greet" for e in edges)

    def test_a_dynamic_import_reaches_the_module(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/pages/Index.vue", "<template><div/></template>\n<script>\nexport default { name: 'Index' }\n</script>\n")
        _write(tmp_path, "src/util.js", "export function helper() {}\n")
        _write(
            tmp_path,
            "src/router.js",
            'const routes = [{ path: "/", component: () => import("./pages/Index.vue") }];\n'
            'const util = require("./util");\nexport default routes;\n',
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        targets = {e.dst_id for e in edges if e.site_path == "src/router.js" and e.kind.value == "imports"}
        assert "src/pages/Index.vue#<module>" in targets
        assert "src/util.js#<module>" in targets

    def test_a_route_action_array_is_a_call_of_the_controller_method(self, tmp_path: Path) -> None:
        _write(tmp_path, "composer.json", LARAVEL_COMPOSER)
        _write(
            tmp_path,
            "app/Http/Controllers/AdController.php",
            "<?php\nnamespace App\\Http\\Controllers;\nclass AdController { public function index() {} }\n",
        )
        _write(
            tmp_path,
            "routes/api.php",
            "<?php\nuse App\\Http\\Controllers\\AdController;\n"
            "Route::get('ads', [AdController::class, 'index']);\n",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert any(
            e.site_path == "routes/api.php"
            and e.dst_id == "app/Http/Controllers/AdController.php#AdController.index"
            and e.kind.value == "calls"
            for e in edges
        )

    def test_a_constructor_assignment_types_the_property(self, tmp_path: Path) -> None:
        # Laravel's dependency injection: an untyped property assigned from
        # a typed constructor parameter.
        _write(tmp_path, "Logger.php", "<?php\nclass Logger { public function log() {} }\n")
        _write(
            tmp_path,
            "Ctl.php",
            "<?php\nclass Ctl { protected $logger;\n"
            "    public function __construct(Logger $logger) { $this->logger = $logger; }\n"
            "    public function h() { $this->logger->log(); } }\n",
        )
        _write(tmp_path, "svc.ts", "export class Svc { go(): void {} }\n")
        _write(
            tmp_path,
            "ctl.ts",
            "import { Svc } from './svc';\n"
            "export class Ctl { private svc; constructor(svc: Svc) { this.svc = svc; } "
            "run() { this.svc.go(); } }\n",
        )
        _write(tmp_path, "client.py", "class Client:\n    def fetch(self):\n        pass\n")
        _write(
            tmp_path,
            "app.py",
            "from client import Client\n\n\nclass App:\n    def __init__(self, client: Client):\n"
            "        self.client = client\n\n    def run(self):\n        return self.client.fetch()\n",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        targets = {e.dst_id for e in edges if e.kind.value == "calls"}
        assert "Logger.php#Logger.log" in targets
        assert "svc.ts#Svc.go" in targets
        assert "client.py#Client.fetch" in targets

    def test_a_var_docblock_types_the_property(self, tmp_path: Path) -> None:
        _write(tmp_path, "Svc.php", "<?php\nclass Svc { public function go() {} }\n")
        _write(
            tmp_path,
            "Ctl.php",
            "<?php\nclass Ctl {\n    /** @var Svc */\n    protected $svc;\n"
            "    public function h() { $this->svc->go(); } }\n",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert any(e.dst_id == "Svc.php#Svc.go" for e in edges)

    def test_a_vue_component_is_the_type_of_this(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "src/stores/ads.js",
            'export const useAdsStore = defineStore("ads", { actions: { fetch() {} } });\n',
        )
        _write(
            tmp_path,
            "src/pages/AddAd.vue",
            "<template><div/></template>\n<script>\n"
            'import { useAdsStore } from "../stores/ads";\n'
            "export default {\n  props: { userId: Number },\n"
            "  data() { return { adsStore: useAdsStore(), busy: false }; },\n"
            "  methods: {\n    save() { this.adsStore.fetch(); this.reset(); },\n    reset() {},\n  },\n"
            "};\n</script>\n",
        )
        result = build_snapshot(tmp_path, use_git=False)
        names = {s.qualified_name: s.kind.value for s in result.snapshot.symbols.values() if not s.synthetic}
        assert names["AddAd"] == "component"
        assert names["AddAd.userId"] == "field"
        assert names["AddAd.adsStore"] == "field"
        assert names["AddAd.save"] == "method"
        targets = {e.dst_id for e in result.snapshot.edges if e.kind.value == "calls"}
        assert "src/pages/AddAd.vue#AddAd.reset" in targets
        assert "src/stores/ads.js#useAdsStore.fetch" in targets

    def test_a_script_setup_component_holds_its_declarations(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "src/pages/Setup.vue",
            "<script setup>\nconst count = ref(0)\nfunction bump() { count.value++ }\n</script>\n"
            "<template><div/></template>\n",
        )
        result = build_snapshot(tmp_path, use_git=False)
        names = {s.qualified_name: s.kind.value for s in result.snapshot.symbols.values() if not s.synthetic}
        assert names["Setup"] == "component"
        assert names["Setup.count"] == "constant"
        # A function declared inside the component is one of its methods.
        assert names["Setup.bump"] == "method"

    def test_object_literal_keys_type_this_only_in_a_component(self, tmp_path: Path) -> None:
        # A TypeScript class returning `{ user: makeUser() }` does not make
        # `this.user` a User; a Vue component's `data()` does.
        _write(tmp_path, "user.ts", "export class User { greet(): string { return ''; } }\n")
        _write(
            tmp_path,
            "svc.ts",
            "import { User } from './user';\nfunction makeUser(): User { return new User(); }\n"
            "export class Svc { build() { return { user: makeUser() }; } run() { return this.user.greet(); } }\n",
        )
        edges = build_snapshot(tmp_path, use_git=False).snapshot.edges
        assert not any(e.site_path == "svc.ts" and e.dst_id == "user.ts#User.greet" for e in edges)


class TestStoreStateAndAssignedTypes:
    def test_pinia_state_keys_are_fields_of_the_store(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "src/stores/ads.js",
            'export const useAdsStore = defineStore("ads", {\n'
            "  state: () => ({ ads: [], editedAd: null }),\n"
            "  actions: { fetchAds() {} },\n});\n",
        )
        _write(
            tmp_path,
            "src/stores/users.ts",
            'export const useUsersStore = defineStore("users", {\n'
            "  state() { return { users: [] }; },\n});\n",
        )
        _write(
            tmp_path,
            "src/page.js",
            'import { useAdsStore } from "./stores/ads";\n'
            "export function run() { const s = useAdsStore(); return s.editedAd; }\n",
        )
        result = build_snapshot(tmp_path, use_git=False)
        names = {s.qualified_name: s.kind.value for s in result.snapshot.symbols.values()}
        assert names["useAdsStore.editedAd"] == "field"
        assert names["useAdsStore.ads"] == "field"
        assert names["useUsersStore.users"] == "field"
        assert any(e.dst_id == "src/stores/ads.js#useAdsStore.editedAd" for e in result.snapshot.edges)

    def test_a_property_typed_by_assignment_has_its_own_shape(self, tmp_path: Path) -> None:
        # scip-php resolves members of declared-typed properties and not
        # of assigned ones; the report must be able to tell them apart.
        from repoatlas.resolve.cascade import receiver_shape

        _write(tmp_path, "Logger.php", "<?php\nclass Logger { public function log() {} }\n")
        _write(
            tmp_path,
            "Ctl.php",
            "<?php\nclass Ctl { protected $logger; private Logger $typed;\n"
            "    public function __construct(Logger $logger) { $this->logger = $logger; }\n"
            "    public function h() { $this->logger->log(); $this->typed->log(); } }\n",
        )
        result = build_snapshot(tmp_path, use_git=False)
        shapes = {r.receiver: receiver_shape(r) for p, r in result.references if r.name == "log"}
        assert shapes["this.logger"] == "property of self (typed by assignment)"
        assert shapes["this.typed"] == "property of self (typed)"
        calls = [e for e in result.snapshot.edges if e.dst_id == "Logger.php#Logger.log" and e.kind.value == "calls"]
        assert len(calls) == 2
