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
            # `self.value = 1` inside __init__ defines a field of the class,
            # hoisted out of the method it was written in.
            "Greeter.value",
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


class TestLocalsAndReceivers:
    """What stage two of the accuracy work added, each with its reason."""

    def test_a_parameter_name_does_not_reach_a_field_of_the_same_name(self) -> None:
        # `return new User(label)` inside makeUser(label): `label` is the
        # parameter, and the oracle says so. Resolving it to the field
        # `User.label` was a confident wrong edge.
        result = extract(
            "a.ts",
            b"class User { label: string; constructor(label: string) { this.label = label; } }\n"
            b"export function makeUser(label: string): User { return new User(label); }\n",
        )
        values = [r for r in result.references if r.kind == "value"]
        assert values == []

    def test_a_local_is_shadowed_in_nested_closures_too(self) -> None:
        result = extract(
            "a.py",
            b"def outer(count):\n"
            b"    def inner():\n"
            b"        return count\n"
            b"    return inner\n",
        )
        assert not [r for r in result.references if r.kind == "value" and r.name == "count"]

    def test_a_module_level_name_is_not_shadowed_by_an_unrelated_function(self) -> None:
        result = extract(
            "a.py",
            b"LIMIT = 3\n\n\ndef f(limit):\n    return limit\n\n\ndef g():\n    return LIMIT\n",
        )
        assert [r.name for r in result.references if r.kind == "value"] == ["LIMIT"]

    def test_a_member_read_records_its_receiver(self) -> None:
        result = extract("a.py", b"def f(greeter):\n    return greeter.greet()\n")
        call = next(r for r in result.references if r.name == "greet")
        assert call.receiver == "greeter"
        assert call.receiver_type is None

    def test_an_annotated_parameter_types_its_receiver(self) -> None:
        result = extract("a.py", b"def f(greeter: Greeter):\n    return greeter.greet()\n")
        call = next(r for r in result.references if r.name == "greet")
        assert call.receiver_type == "Greeter"

    def test_a_typescript_declarator_types_its_receiver(self) -> None:
        result = extract(
            "a.ts",
            b"function run() { const user: Greets = make(); return user.greet(); }\n",
        )
        call = next(r for r in result.references if r.name == "greet")
        assert call.receiver_type == "Greets"

    def test_a_typescript_new_types_its_receiver(self) -> None:
        result = extract(
            "a.ts", b"function run() { let admin = new Admin(); return admin.audit(); }\n"
        )
        call = next(r for r in result.references if r.name == "audit")
        assert call.receiver_type == "Admin"

    def test_a_php_parameter_and_new_type_their_receivers(self) -> None:
        result = extract(
            "a.php",
            b"<?php\nfunction run(Greeter $g) { $l = new LoudGreeter(); return $g->greet() . $l->greet(); }\n",
        )
        calls = {r.receiver: r.receiver_type for r in result.references if r.name == "greet"}
        assert calls == {"g": "Greeter", "l": "LoudGreeter"}

    def test_a_php_static_call_names_its_class_as_receiver(self) -> None:
        result = extract("a.php", b"<?php\nfunction run() { return Util::helper(); }\n")
        call = next(r for r in result.references if r.name == "helper")
        assert call.receiver == "Util"

    def test_a_php_promoted_parameter_is_a_field_of_the_class(self) -> None:
        result = extract(
            "a.php",
            b"<?php\nclass A { public function __construct(private string $prefix) {} }\n",
        )
        names = {s.qualified_name: s.kind.value for s in result.symbols}
        assert names.get("A.prefix") == "field"

    def test_a_second_self_assignment_is_a_use_not_a_second_field(self) -> None:
        result = extract(
            "a.py",
            b"class A:\n"
            b"    def __init__(self):\n"
            b"        self.x = 1\n"
            b"    def reset(self):\n"
            b"        self.x = 0\n",
        )
        fields = [s for s in result.symbols if s.kind.value == "field"]
        assert [s.qualified_name for s in fields] == ["A.x"]
        assert any(r.name == "x" and r.kind == "member" for r in result.references)


class TestReceiversAndAnonymousClasses:
    def test_a_chained_call_records_an_expression_receiver(self) -> None:
        from repoatlas.parse.extract import EXPRESSION_RECEIVER

        php = extract("a.php", b"<?php\nfunction f($a) { return $a->b()->c(); }\n")
        assert next(r for r in php.references if r.name == "c").receiver == EXPRESSION_RECEIVER
        assert next(r for r in php.references if r.name == "b").receiver == "a"
        ts = extract("a.ts", b"function f() { return make().run(); }\n")
        assert next(r for r in ts.references if r.name == "run").receiver == EXPRESSION_RECEIVER

    def test_instanceof_and_a_qualified_class_constant_are_type_references(self) -> None:
        result = extract(
            "a.php",
            b"<?php\nfunction f($x) { if ($x instanceof Ad) {} return \\App\\M\\Trust::class; }\n",
        )
        types = {r.name for r in result.references if r.kind == "type"}
        assert {"Ad", "Trust"} <= types

    def test_an_anonymous_class_is_a_type_with_members(self) -> None:
        result = extract(
            "a.php",
            b"<?php\nfunction make() { return new class { protected $data; public function f() {} }; }\n",
        )
        names = {s.qualified_name: s.kind.value for s in result.symbols}
        assert names["make.class@anonymous"] == "class"
        assert names["make.class@anonymous.data"] == "field"
        assert names["make.class@anonymous.f"] == "method"

    def test_a_typed_property_types_this_dot_name(self) -> None:
        result = extract(
            "a.php",
            b"<?php\nclass C { private Svc $svc; public function h() { $this->svc->go(); } }\n",
        )
        call = next(r for r in result.references if r.name == "go")
        assert (call.receiver, call.receiver_type) == ("this.svc", "Svc")

    def test_a_promoted_parameter_types_both_the_local_and_the_property(self) -> None:
        result = extract(
            "a.php",
            b"<?php\nclass C { public function __construct(private Svc $svc) { $svc->go(); } "
            b"public function h() { $this->svc->run(); } }\n",
        )
        by_name = {r.name: r for r in result.references if r.name in ("go", "run")}
        assert by_name["go"].receiver_type == "Svc"
        assert by_name["run"].receiver_type == "Svc"

    def test_this_is_a_receiver_in_typescript(self) -> None:
        result = extract("a.ts", b"class C { x = 1; f() { return this.x; } }\n")
        assert next(r for r in result.references if r.name == "x").receiver == "this"


class TestTypesByCall:
    def test_a_local_assigned_from_a_call_carries_the_call(self) -> None:
        from repoatlas.parse.extract import CALL_TYPE_PREFIX

        result = extract(
            "a.php",
            b"<?php\nclass A { function r(Svc $s) { $g = $this->build(); $h = make(); $i = $s->go(); "
            b"return $g->x() . $h->y() . $i->z(); } }\n",
        )
        by_name = {r.name: r.receiver_type for r in result.references if r.name in ("x", "y", "z")}
        assert by_name["x"] == f"{CALL_TYPE_PREFIX}build|this|"
        assert by_name["y"] == f"{CALL_TYPE_PREFIX}make||"
        assert by_name["z"] == f"{CALL_TYPE_PREFIX}go|s|Svc"

    def test_a_qualified_instanceof_is_a_type_reference(self) -> None:
        result = extract(
            "a.php", b"<?php\nfunction f($m) { return $m instanceof \\App\\Models\\Schedule; }\n"
        )
        assert any(r.kind == "type" and r.name == "Schedule" for r in result.references)
