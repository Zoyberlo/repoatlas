"""Tests for languages written inside other languages.

A Vue single-file component is HTML holding TypeScript. Parsing only the
outer grammar loses every definition in the file; parsing the inner text on
its own loses every line number, which is worse, because a symbol at the
wrong line is a wrong answer rather than a missing one.

So the property under test throughout is that offsets survive. A function
declared on line 13 of the file must be reported at line 13, not at line 3
of the script block.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

from repoatlas.parse.embedded import embedded_regions, parse_embedded
from repoatlas.parse.extract import extract_source
from repoatlas.parse.languages import language_for_path
from repoatlas.parse.walk import iter_source_files

VUE = b"""<template>
  <div>
    <MyButton :label="label" />
    <my-card />
    <span>plain</span>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import MyButton from './MyButton.vue'

interface Props {
  label: string
}

function greet(name: string): string {
  return `hello ${name}`
}

const label = ref('hi')
</script>
"""

BLADE = b"""@extends('layouts.app')

<x-alert type="info" />
<x-forms.input name="q" />
@include('partials.header')
@includeIf('partials.footer')
"""


def extract(path: str, source: bytes):
    spec = language_for_path(path)
    assert spec is not None, path
    return extract_source(path, source, spec)


class TestLanguageRouting:
    def test_a_compound_suffix_beats_its_last_extension(self) -> None:
        # `index.blade.php` ends in `.php`, and a plain suffix rule would
        # hand every Blade template to the PHP grammar.
        spec = language_for_path("resources/views/index.blade.php")
        assert spec is not None and spec.name == "blade"

    def test_a_plain_php_file_is_still_php(self) -> None:
        spec = language_for_path("app/User.php")
        assert spec is not None and spec.name == "php"

    def test_a_vue_file_gets_the_vue_grammar(self) -> None:
        spec = language_for_path("src/App.vue")
        assert spec is not None and spec.name == "vue"

    def test_the_walker_finds_blade_templates(self, tmp_path: Path) -> None:
        target = tmp_path / "resources" / "views" / "home.blade.php"
        target.parent.mkdir(parents=True)
        target.write_bytes(BLADE)
        found = {source.path: source.language.name for source in iter_source_files(tmp_path, use_git=False)}
        assert found == {"resources/views/home.blade.php": "blade"}


class TestVueScriptBlocks:
    def test_the_script_block_is_found_as_typescript(self) -> None:
        from repoatlas.parse.languages import get_parser

        tree = get_parser("vue").parse(VUE)
        regions = list(embedded_regions(tree, "vue"))
        assert [region.language for region in regions] == ["typescript"]

    def test_a_block_without_lang_is_javascript(self) -> None:
        from repoatlas.parse.languages import get_parser

        source = b"<script setup>\nconst a = 1\n</script>\n"
        tree = get_parser("vue").parse(source)
        assert [region.language for region in embedded_regions(tree, "vue")] == ["javascript"]

    def test_the_inner_tree_keeps_absolute_offsets(self) -> None:
        from repoatlas.parse.languages import get_parser

        tree = get_parser("vue").parse(VUE)
        region = next(iter(embedded_regions(tree, "vue")))
        inner = parse_embedded(VUE, region)
        assert inner is not None
        # The first statement in the block is the `vue` import on line 10.
        assert inner.root_node.start_point[0] == 9

    def test_definitions_report_the_line_they_are_written_on(self) -> None:
        result = extract("src/App.vue", VUE)
        lines = {symbol.name: symbol.name_range.start.line + 1 for symbol in result.symbols}
        assert lines["Props"] == 13
        assert lines["greet"] == 17

    def test_imports_come_from_the_script_block(self) -> None:
        result = extract("src/App.vue", VUE)
        modules = {
            statement.module: [b.local for b in statement.bindings]
            for statement in result.imports.statements
        }
        assert modules == {"vue": ["ref"], "./MyButton.vue": ["MyButton"]}

    def test_template_tags_that_are_components_become_references(self) -> None:
        result = extract("src/App.vue", VUE)
        components = sorted(
            reference.name for reference in result.references if reference.kind == "component"
        )
        assert components == ["MyButton", "my-card"]

    def test_a_plain_html_element_is_not_a_reference(self) -> None:
        result = extract("src/App.vue", VUE)
        names = {reference.name for reference in result.references}
        assert "span" not in names and "div" not in names and "template" not in names

    def test_a_component_without_a_script_block_still_parses(self) -> None:
        result = extract("src/Bare.vue", b"<template><div>hi</div></template>\n")
        assert result.symbols == [] and not result.has_errors


class TestBladePhpIslands:
    ISLANDS = (
        b"<div>{{ $user->name }} {{ route('users.show', $user) }}</div>\n"
        b"@php\n"
        b"    $total = count($items);\n"
        b"@endphp\n"
        b"<p>{!! render($html) !!}</p>\n"
    )

    def references(self):
        result = extract("v.blade.php", self.ISLANDS)
        return {
            (ref.kind, ref.name): (ref.span.start.line + 1, ref.span.start.character)
            for ref in result.references
        }

    def test_an_echo_yields_a_member_reference_at_its_real_position(self) -> None:
        found = self.references()
        # `name` in `$user->name` sits on line 1 at column 15 of the host file.
        assert found[("member", "name")] == (1, 15)

    def test_a_route_call_inside_an_echo_is_a_route_reference(self) -> None:
        found = self.references()
        assert ("route", "users.show") in found

    def test_a_php_block_is_parsed_too(self) -> None:
        found = self.references()
        # `count` is called on line 3; the island's second line keeps its
        # own columns.
        assert found[("call", "count")] == (3, 13)

    def test_a_raw_echo_is_an_island_as_well(self) -> None:
        found = self.references()
        assert found[("call", "render")] == (5, 7)

    def test_islands_do_not_disturb_directives(self) -> None:
        result = extract("v.blade.php", b"@extends('layouts.app')\n<b>{{ $x->y }}</b>\n")
        kinds = {(ref.kind, ref.name) for ref in result.references}
        assert ("extends", "layouts.app") in kinds
        assert ("member", "y") in kinds


class TestBladeDirectives:
    def test_extends_include_and_components_are_read(self) -> None:
        result = extract("resources/views/home.blade.php", BLADE)
        found = sorted(
            (reference.span.start.line + 1, reference.kind, reference.name)
            for reference in result.references
        )
        assert found == [
            (1, "extends", "layouts.app"),
            (3, "component", "x-alert"),
            (4, "component", "x-forms.input"),
            (5, "include", "partials.header"),
            (6, "include", "partials.footer"),
        ]

    def test_directive_arguments_lose_their_quotes(self) -> None:
        result = extract("v.blade.php", b"@extends('layouts.app')\n")
        assert result.references[0].name == "layouts.app"

    def test_a_template_with_no_directives_yields_nothing(self) -> None:
        result = extract("v.blade.php", b"<p>hello</p>\n")
        assert result.references == [] and result.symbols == []


def test_the_tree_sitter_version_is_one_that_does_not_segfault() -> None:
    """A guard for a crash no other test can catch.

    tree-sitter 0.26.0 corrupts the heap when this extractor parses a Vue
    component. The corruption is silent: the process dies later, in an
    unrelated file's tag query, with an access violation and no Python
    traceback. It reproduced on four runs out of four, and never on 0.25.2
    with the same source.

    A segfault cannot be caught by a test, so the version range is the only
    thing standing between a future dependency bump and an index that kills
    the agent using it. This asserts the range is still in force.
    """
    from importlib.metadata import version

    installed = tuple(int(part) for part in version("tree-sitter").split(".")[:2])
    assert (0, 25) <= installed < (0, 26), (
        f"tree-sitter {version('tree-sitter')} is outside the supported range; "
        "0.26 segfaults on Vue components"
    )
