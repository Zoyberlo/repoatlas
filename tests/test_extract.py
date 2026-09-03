"""Tests for the tree-sitter extractor.

Each language gets the same shape of fixture: a module-level binding, a
class with a field and a method, a free function, an import and a call. That
makes the per-language assertions directly comparable, which matters because
the point of a shared symbol vocabulary is that a method is a method whether
the grammar spells it `function_definition` or `method_declaration`.
"""

from __future__ import annotations

import pytest

from repoatlas.model import SymbolKind
from repoatlas.parse.extract import extract_source
from repoatlas.parse.languages import (
    SUPPORTED,
    LanguageUnavailable,
    available_languages,
    language_for_path,
)

pytest.importorskip("tree_sitter", reason="needs the parse extra")
pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")


PYTHON_SOURCE = b'''\
import os
from a.b import Helper as H

LIMIT = 10


class Greeter(Base):
    field = 0

    def __init__(self):
        self.value = 1

    def greet(self, name):
        return os.path.join(name)


def main():
    Greeter().greet("x")
'''

TYPESCRIPT_SOURCE = b"""\
import { Helper } from "./helper";

export const LIMIT = 10;

interface Greets {
  greet(name: string): string;
}

type Alias = string;

enum Color {
  Red,
}

export class Greeter extends Base implements Greets {
  field: number = 0;

  constructor() {
    super();
  }

  greet(name: string): string {
    return helper(name);
  }
}

export function helper(name: string): string {
  return name;
}

const arrow = (x: number) => x + 1;
"""

PHP_SOURCE = b"""\
<?php
namespace App;

use App\\Contracts\\Helper as H;

interface Greets {
    public function greet(string $name): string;
}

trait HasName {
    public $name;
}

enum Status: string {
    case Active = 'a';
}

class Greeter extends Base implements Greets {
    use HasName;

    public const LIMIT = 10;

    private string $field = '';

    public function __construct() {}

    public function greet(string $name): string {
        return helper($name);
    }
}

function helper($name) {
    return $name;
}
"""


def extract(path: str, source: bytes):
    spec = language_for_path(path)
    assert spec is not None, f"no language for {path}"
    return extract_source(path, source, spec)


class TestLanguageRegistry:
    def test_every_supported_language_has_a_query_file(self) -> None:
        for spec in SUPPORTED:
            assert spec.query_path.exists(), f"missing query for {spec.name}"

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("a/b.py", "python"),
            ("a/b.pyi", "python"),
            ("a/b.ts", "typescript"),
            ("a/b.d.ts", "typescript"),
            ("a/b.tsx", "tsx"),
            ("a/b.mjs", "javascript"),
            ("a/b.jsx", "javascript"),
            ("a/b.php", "php"),
            ("a/b.phtml", "php"),
        ],
    )
    def test_maps_extensions_to_languages(self, path: str, expected: str) -> None:
        spec = language_for_path(path)
        assert spec is not None
        assert spec.name == expected

    def test_is_case_insensitive_about_extensions(self) -> None:
        spec = language_for_path("A/B.PY")
        assert spec is not None and spec.name == "python"

    @pytest.mark.parametrize("path", ["a/b.rs", "README.md", "noextension", "a.blade.php.txt"])
    def test_returns_none_for_unhandled_files(self, path: str) -> None:
        assert language_for_path(path) is None

    def test_tsx_shares_the_typescript_query(self) -> None:
        tsx = language_for_path("a.tsx")
        ts = language_for_path("a.ts")
        assert tsx is not None and ts is not None
        assert tsx.query_path == ts.query_path

    def test_reports_which_languages_load_here(self) -> None:
        status = available_languages()
        assert set(status) == {spec.name for spec in SUPPORTED}
        assert all(status.values()), f"unavailable: {status}"

    def test_rejects_an_unknown_language_by_name(self) -> None:
        from repoatlas.parse.languages import query_source

        with pytest.raises(LanguageUnavailable, match="unknown language"):
            query_source("cobol")


class TestPython:
    @pytest.fixture
    def result(self):
        return extract("src/demo.py", PYTHON_SOURCE)

    def test_parses_without_errors(self, result) -> None:
        assert result.error_count == 0
        assert not result.has_errors

    def test_finds_every_definition(self, result) -> None:
        assert [s.qualified_name for s in result.symbols] == [
            "LIMIT",
            "Greeter",
            "Greeter.field",
            "Greeter.__init__",
            "Greeter.greet",
            "main",
        ]

    def test_calls_a_nested_function_a_method(self, result) -> None:
        kinds = {s.qualified_name: s.kind for s in result.symbols}
        assert kinds["Greeter.greet"] is SymbolKind.METHOD
        assert kinds["main"] is SymbolKind.FUNCTION

    def test_recognises_the_constructor(self, result) -> None:
        kinds = {s.qualified_name: s.kind for s in result.symbols}
        assert kinds["Greeter.__init__"] is SymbolKind.CONSTRUCTOR

    def test_nests_members_inside_their_class(self, result) -> None:
        by_name = {s.qualified_name: s for s in result.symbols}
        assert by_name["Greeter.greet"].container_id == by_name["Greeter"].id
        assert by_name["Greeter"].container_id is None

    def test_anchors_a_symbol_on_its_identifier(self, result) -> None:
        greeter = next(s for s in result.symbols if s.name == "Greeter")
        assert greeter.name_range.start.line == 6
        assert greeter.full_range is not None
        # The body runs past the identifier line to the last method.
        assert greeter.full_range.end.line > greeter.name_range.end.line

    def test_records_the_base_class_as_a_reference(self, result) -> None:
        bases = [r for r in result.references if r.kind == "class"]
        assert [r.name for r in bases] == ["Base"]

    def test_records_imports(self, result) -> None:
        imports = {r.name for r in result.references if r.kind == "import"}
        assert imports == {"os", "H"}

    def test_attributes_a_call_to_its_enclosing_function(self, result) -> None:
        by_name = {s.qualified_name: s for s in result.symbols}
        join = next(r for r in result.references if r.name == "join")
        assert join.container_id == by_name["Greeter.greet"].id

    def test_does_not_treat_a_definition_as_a_reference_to_itself(self, result) -> None:
        names = [r.name for r in result.references]
        assert "main" not in names
        assert names.count("Greeter") == 1  # the call in main, not the class


class TestTypeScript:
    @pytest.fixture
    def result(self):
        return extract("src/demo.ts", TYPESCRIPT_SOURCE)

    def test_parses_without_errors(self, result) -> None:
        assert result.error_count == 0

    def test_finds_each_declaration_kind(self, result) -> None:
        kinds = {s.qualified_name: s.kind for s in result.symbols}
        assert kinds["Greets"] is SymbolKind.INTERFACE
        assert kinds["Alias"] is SymbolKind.TYPE_ALIAS
        assert kinds["Color"] is SymbolKind.ENUM
        assert kinds["Greeter"] is SymbolKind.CLASS
        assert kinds["Greeter.field"] is SymbolKind.FIELD
        assert kinds["Greeter.greet"] is SymbolKind.METHOD
        assert kinds["helper"] is SymbolKind.FUNCTION

    def test_a_function_bound_to_a_name_is_a_function_not_a_constant(
        self, result
    ) -> None:
        kinds = {s.qualified_name: s.kind for s in result.symbols}
        assert kinds["arrow"] is SymbolKind.FUNCTION
        assert kinds["LIMIT"] is SymbolKind.CONSTANT

    def test_treats_a_class_constructor_as_one(self, result) -> None:
        kinds = {s.qualified_name: s.kind for s in result.symbols}
        assert kinds["Greeter.constructor"] is SymbolKind.CONSTRUCTOR

    def test_records_extends_and_implements(self, result) -> None:
        classes = {r.name for r in result.references if r.kind == "class"}
        assert classes == {"Base", "Greets"}

    def test_records_the_import(self, result) -> None:
        imports = {r.name for r in result.references if r.kind == "import"}
        assert imports == {"Helper"}

    def test_nests_an_interface_method(self, result) -> None:
        by_name = {s.qualified_name: s for s in result.symbols}
        assert by_name["Greets.greet"].container_id == by_name["Greets"].id


class TestTsx:
    def test_parses_jsx_and_finds_the_component(self) -> None:
        source = b"""\
import React from "react";

export function Badge({ label }: { label: string }) {
  return <span className="badge">{label}</span>;
}
"""
        result = extract("src/Badge.tsx", source)
        assert result.error_count == 0
        assert [s.name for s in result.symbols] == ["Badge"]
        assert result.language == "tsx"


class TestJavaScript:
    def test_extracts_without_the_typescript_only_nodes(self) -> None:
        source = b"""\
import { helper } from "./helper";

export class Greeter extends Base {
  greet(name) {
    return helper(name);
  }
}

const arrow = (x) => x + 1;
"""
        result = extract("src/demo.js", source)
        assert result.error_count == 0
        kinds = {s.qualified_name: s.kind for s in result.symbols}
        assert kinds["Greeter"] is SymbolKind.CLASS
        assert kinds["Greeter.greet"] is SymbolKind.METHOD
        assert kinds["arrow"] is SymbolKind.FUNCTION
        assert {r.name for r in result.references if r.kind == "class"} == {"Base"}


class TestPhp:
    @pytest.fixture
    def result(self):
        return extract("src/Demo.php", PHP_SOURCE)

    def test_parses_without_errors(self, result) -> None:
        assert result.error_count == 0

    def test_finds_each_declaration_kind(self, result) -> None:
        kinds = {s.qualified_name: s.kind for s in result.symbols}
        assert kinds["Greets"] is SymbolKind.INTERFACE
        assert kinds["HasName"] is SymbolKind.TRAIT
        assert kinds["Status"] is SymbolKind.ENUM
        assert kinds["Greeter"] is SymbolKind.CLASS
        assert kinds["Greeter.LIMIT"] is SymbolKind.CONSTANT
        assert kinds["Greeter.greet"] is SymbolKind.METHOD
        assert kinds["helper"] is SymbolKind.FUNCTION

    def test_recognises_the_php_constructor(self, result) -> None:
        kinds = {s.qualified_name: s.kind for s in result.symbols}
        assert kinds["Greeter.__construct"] is SymbolKind.CONSTRUCTOR

    def test_strips_the_sigil_from_a_property_name(self, result) -> None:
        names = {s.name for s in result.symbols}
        assert "field" in names
        assert not any(name.startswith("$") for name in names)

    def test_records_a_used_trait_alongside_inheritance(self, result) -> None:
        classes = {r.name for r in result.references if r.kind == "class"}
        assert classes == {"Base", "Greets", "HasName"}

    def test_records_the_imported_name_and_its_alias(self, result) -> None:
        imports = {r.name for r in result.references if r.kind == "import"}
        assert imports == {"Helper", "H"}


class TestSymbolIds:
    def test_ids_are_stable_across_repeated_extraction(self) -> None:
        first = extract("src/demo.py", PYTHON_SOURCE)
        second = extract("src/demo.py", PYTHON_SOURCE)
        assert [s.id for s in first.symbols] == [s.id for s in second.symbols]

    def test_ids_survive_an_edit_elsewhere_in_the_file(self) -> None:
        before = extract("src/demo.py", PYTHON_SOURCE)
        after = extract("src/demo.py", PYTHON_SOURCE.replace(b"LIMIT = 10", b"LIMIT = 99"))
        assert [s.id for s in before.symbols] == [s.id for s in after.symbols]

    def test_ids_shift_only_for_a_renamed_symbol(self) -> None:
        before = extract("src/demo.py", PYTHON_SOURCE)
        after = extract("src/demo.py", PYTHON_SOURCE.replace(b"def main", b"def run"))
        changed = {s.id for s in before.symbols} ^ {s.id for s in after.symbols}
        assert changed == {"src/demo.py#main", "src/demo.py#run"}

    def test_two_definitions_of_one_name_get_distinct_ids(self) -> None:
        source = b"""\
def duplicated():
    pass


def duplicated():
    pass
"""
        result = extract("src/dup.py", source)
        ids = [s.id for s in result.symbols]
        assert ids == ["src/dup.py#duplicated", "src/dup.py#duplicated$2"]

    def test_the_id_carries_the_containing_scope(self) -> None:
        result = extract("src/demo.py", PYTHON_SOURCE)
        greet = next(s for s in result.symbols if s.name == "greet")
        assert greet.id == "src/demo.py#Greeter.greet"


class TestBrokenSources:
    def test_counts_errors_without_giving_up_on_the_file(self) -> None:
        source = b"""\
def good():
    return 1

class Broken(:

def also_good():
    return 2
"""
        result = extract("src/broken.py", source)
        assert result.has_errors
        assert result.error_count >= 1
        # Extraction continues past the damage.
        assert "good" in {s.name for s in result.symbols}

    def test_an_empty_file_yields_nothing_and_no_error(self) -> None:
        result = extract("src/empty.py", b"")
        assert result.symbols == []
        assert result.references == []
        assert not result.has_errors

    def test_a_file_of_only_comments_yields_nothing(self) -> None:
        result = extract("src/comments.py", b"# just a note\n# and another\n")
        assert result.symbols == []

    def test_handles_non_ascii_identifiers_and_text(self) -> None:
        source = "def привіт():\n    return 'ок'\n".encode()
        result = extract("src/ua.py", source)
        assert [s.name for s in result.symbols] == ["привіт"]


class TestExtractFile:
    def test_reads_from_disk(self, tmp_path) -> None:
        from repoatlas.parse.extract import extract_file

        source = tmp_path / "demo.py"
        source.write_bytes(PYTHON_SOURCE)
        result = extract_file("src/demo.py", str(source))
        assert result is not None
        assert result.path == "src/demo.py"
        assert any(s.name == "Greeter" for s in result.symbols)

    def test_returns_none_for_an_unhandled_language(self, tmp_path) -> None:
        from repoatlas.parse.extract import extract_file

        source = tmp_path / "notes.md"
        source.write_text("# hello", encoding="utf-8")
        assert extract_file("notes.md", str(source)) is None
