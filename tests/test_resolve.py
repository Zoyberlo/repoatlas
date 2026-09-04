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
            "u.ts", [Reference("greet", "call", SourceRange.of(18, 16, 18, 21), shout.id)]
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

    def test_an_edge_never_points_a_symbol_at_itself(self) -> None:
        recursive = _symbol("a.py#loop", "loop", "a.py", 0)
        resolver = self._resolver([recursive])
        edges = resolver.resolve_file(
            "a.py", [Reference("loop", "call", SourceRange.of(1, 4, 1, 8), recursive.id)]
        )
        assert edges == []

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
