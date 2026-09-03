"""Tests for framework conventions.

A convention is not a heuristic. `view('users.index')` names exactly one
file, and the only question is whether that file is in this repository. So
these tests check two symmetrical things: that a name whose file exists
produces an edge at import-map confidence, and that a name whose file does
not produces no edge at all. The second matters more. A template name that
falls through to the identifier cascade would happily match any function
called `index`, and that wrong edge would carry the same confidence as a
right one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoatlas.plugins import LaravelPlugin, active_plugins
from repoatlas.plugins.base import register, registered_plugins

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

from repoatlas.parse.build import build_snapshot


def write(root: Path, path: str, text: str = "") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def laravel_project(root: Path) -> None:
    """A minimal repository that Laravel's own conventions describe."""
    write(root, "composer.json", json.dumps({"require": {"laravel/framework": "^11.0"}}))
    write(
        root,
        "app/Http/Controllers/UserController.php",
        "<?php\n"
        "namespace App\\Http\\Controllers;\n"
        "class UserController\n"
        "{\n"
        "    public function index()\n"
        "    {\n"
        "        return view('users.index');\n"
        "    }\n"
        "    public function missing()\n"
        "    {\n"
        "        return view('users.nope');\n"
        "    }\n"
        "}\n",
    )
    write(
        root,
        "resources/views/users/index.blade.php",
        "@extends('layouts.app')\n"
        "<x-alert type=\"info\" />\n"
        '<x-forms.input name="q" />\n'
        "@include('partials.header')\n",
    )
    write(root, "resources/views/layouts/app.blade.php", "<html></html>\n")
    write(root, "resources/views/components/alert.blade.php", "<div></div>\n")
    write(root, "resources/views/components/forms/input.blade.php", "<input>\n")
    write(root, "resources/views/partials/header.blade.php", "<header></header>\n")


def edge_targets(root: Path) -> dict[tuple[str, int], str]:
    """Every non-containment edge, keyed by where it was written."""
    result = build_snapshot(root, use_git=False)
    targets = {}
    for edge in result.snapshot.edges:
        if edge.kind.value == "contains" or edge.site_range is None:
            continue
        targets[(edge.site_path or "", edge.site_range.start.line + 1)] = edge.dst_id
    return targets


class TestDetection:
    def test_composer_requiring_laravel_activates_the_plugin(self, tmp_path: Path) -> None:
        write(tmp_path, "composer.json", json.dumps({"require": {"laravel/framework": "^11"}}))
        assert [p.name for p in active_plugins(tmp_path, frozenset())] == ["laravel"]

    def test_a_dev_requirement_counts_too(self, tmp_path: Path) -> None:
        write(tmp_path, "composer.json", json.dumps({"require-dev": {"illuminate/view": "^11"}}))
        assert active_plugins(tmp_path, frozenset())

    def test_a_views_directory_alone_is_not_laravel(self, tmp_path: Path) -> None:
        # Plenty of projects have `resources/views`. Detection reads what the
        # project declares, not what its folders are called.
        write(tmp_path, "resources/views/home.blade.php", "hi")
        assert active_plugins(tmp_path, frozenset({"resources/views/home.blade.php"})) == ()

    def test_a_php_project_without_laravel_stays_inactive(self, tmp_path: Path) -> None:
        write(tmp_path, "composer.json", json.dumps({"require": {"symfony/console": "^7"}}))
        assert active_plugins(tmp_path, frozenset()) == ()

    def test_a_broken_manifest_disables_rather_than_crashes(self, tmp_path: Path) -> None:
        # A malformed file the plugin does not control must not take the
        # whole index down with it.
        write(tmp_path, "composer.json", "{not json")
        assert active_plugins(tmp_path, frozenset()) == ()

    def test_detection_survives_a_plugin_that_raises(self, tmp_path: Path) -> None:
        class Exploding:
            name = "exploding"
            kinds = ("view",)

            def detect(self, root: Path, files: frozenset[str]) -> bool:
                raise RuntimeError("boom")

            def resolve(self, kind, name, *, from_path, files):  # pragma: no cover
                return None

        try:
            register(Exploding())
            assert "exploding" not in [p.name for p in active_plugins(tmp_path, frozenset())]
        finally:
            registry = registered_plugins()
            assert any(p.name == "exploding" for p in registry)
            from repoatlas.plugins import base

            base._REGISTRY = [p for p in base._REGISTRY if p.name != "exploding"]

    def test_registering_the_same_name_twice_replaces_it(self) -> None:
        before = len(registered_plugins())
        register(LaravelPlugin())
        assert len(registered_plugins()) == before


class TestLaravelNames:
    @pytest.fixture
    def plugin(self) -> LaravelPlugin:
        return LaravelPlugin()

    @pytest.fixture
    def files(self) -> frozenset[str]:
        return frozenset(
            {
                "resources/views/users/index.blade.php",
                "resources/views/components/alert.blade.php",
                "resources/views/components/forms/input.blade.php",
                "resources/views/components/tabs/index.blade.php",
                "app/View/Components/DataTable.php",
            }
        )

    def resolve(self, plugin, kind, name, files):
        return plugin.resolve(kind, name, from_path="a.php", files=files)

    def test_a_dotted_view_name_is_a_directory_path(self, plugin, files) -> None:
        assert self.resolve(plugin, "view", "users.index", files) == (
            "resources/views/users/index.blade.php"
        )

    def test_extends_and_include_read_the_same_way(self, plugin, files) -> None:
        for kind in ("extends", "include"):
            assert self.resolve(plugin, kind, "users.index", files) == (
                "resources/views/users/index.blade.php"
            )

    def test_a_view_that_does_not_exist_resolves_to_nothing(self, plugin, files) -> None:
        assert self.resolve(plugin, "view", "users.nope", files) is None

    def test_a_namespaced_view_belongs_to_a_package(self, plugin, files) -> None:
        # `mail::message` lives in a vendor directory this index does not
        # cover, so claiming a local file for it would be wrong.
        assert self.resolve(plugin, "view", "mail::message", files) is None

    def test_a_component_tag_finds_its_anonymous_view(self, plugin, files) -> None:
        assert self.resolve(plugin, "component", "x-alert", files) == (
            "resources/views/components/alert.blade.php"
        )

    def test_a_dotted_component_tag_nests(self, plugin, files) -> None:
        assert self.resolve(plugin, "component", "x-forms.input", files) == (
            "resources/views/components/forms/input.blade.php"
        )

    def test_a_component_directory_falls_back_to_its_index(self, plugin, files) -> None:
        assert self.resolve(plugin, "component", "x-tabs", files) == (
            "resources/views/components/tabs/index.blade.php"
        )

    def test_a_component_class_answers_when_no_view_does(self, plugin, files) -> None:
        assert self.resolve(plugin, "component", "x-data-table", files) == (
            "app/View/Components/DataTable.php"
        )

    def test_a_plain_html_tag_is_not_a_component(self, plugin, files) -> None:
        assert self.resolve(plugin, "component", "div", files) is None

    def test_an_unknown_kind_is_declined(self, plugin, files) -> None:
        assert self.resolve(plugin, "call", "users.index", files) is None

    def test_an_empty_name_is_declined(self, plugin, files) -> None:
        assert self.resolve(plugin, "view", "   ", files) is None
        assert self.resolve(plugin, "component", "x-", files) is None


class TestLaravelEdges:
    def test_every_convention_becomes_an_edge(self, tmp_path: Path) -> None:
        laravel_project(tmp_path)
        targets = edge_targets(tmp_path)
        assert targets[("app/Http/Controllers/UserController.php", 7)] == (
            "resources/views/users/index.blade.php#<module>"
        )
        assert targets[("resources/views/users/index.blade.php", 1)] == (
            "resources/views/layouts/app.blade.php#<module>"
        )
        assert targets[("resources/views/users/index.blade.php", 2)] == (
            "resources/views/components/alert.blade.php#<module>"
        )
        assert targets[("resources/views/users/index.blade.php", 3)] == (
            "resources/views/components/forms/input.blade.php#<module>"
        )
        assert targets[("resources/views/users/index.blade.php", 4)] == (
            "resources/views/partials/header.blade.php#<module>"
        )

    def test_a_convention_edge_carries_import_map_confidence(self, tmp_path: Path) -> None:
        laravel_project(tmp_path)
        result = build_snapshot(tmp_path, use_git=False)
        conventions = [
            edge
            for edge in result.snapshot.edges
            if edge.site_path == "resources/views/users/index.blade.php"
        ]
        assert conventions
        assert {edge.tier.label for edge in conventions} == {"import_map"}

    def test_a_missing_template_produces_no_edge_at_all(self, tmp_path: Path) -> None:
        # The controller also defines `index`, so a fall-through to the
        # identifier cascade would resolve `view('users.nope')` to something.
        laravel_project(tmp_path)
        assert ("app/Http/Controllers/UserController.php", 11) not in edge_targets(tmp_path)

    def test_without_the_plugin_a_view_name_resolves_to_nothing(self, tmp_path: Path) -> None:
        laravel_project(tmp_path)
        (tmp_path / "composer.json").write_text(
            json.dumps({"require": {"symfony/console": "^7"}}), encoding="utf-8"
        )
        targets = edge_targets(tmp_path)
        assert ("app/Http/Controllers/UserController.php", 7) not in targets

    def test_a_template_that_defines_nothing_is_still_a_target(self, tmp_path: Path) -> None:
        # `layouts/app.blade.php` yields no symbols and no references. It
        # still needs a module symbol, or the edge into it disappears.
        laravel_project(tmp_path)
        result = build_snapshot(tmp_path, use_git=False)
        assert "resources/views/layouts/app.blade.php#<module>" in result.snapshot.symbols


class TestVueComponents:
    def vue_project(self, root: Path) -> None:
        write(
            root,
            "src/App.vue",
            '<script setup lang="ts">\n'
            "import MyButton from './components/MyButton.vue'\n"
            "import MyCard from './components/MyCard.vue'\n"
            "</script>\n"
            "<template>\n"
            "  <MyButton />\n"
            "  <my-card />\n"
            "  <span>plain</span>\n"
            "</template>\n",
        )
        write(root, "src/components/MyButton.vue", "<template><button /></template>\n")
        write(root, "src/components/MyCard.vue", "<template><div /></template>\n")

    def test_a_pascal_tag_resolves_through_the_import(self, tmp_path: Path) -> None:
        self.vue_project(tmp_path)
        assert edge_targets(tmp_path)[("src/App.vue", 6)] == (
            "src/components/MyButton.vue#<module>"
        )

    def test_a_kebab_tag_means_the_same_component(self, tmp_path: Path) -> None:
        # Vue accepts `<my-card />` for a component imported as `MyCard`, so
        # the two spellings must land on the same file.
        self.vue_project(tmp_path)
        assert edge_targets(tmp_path)[("src/App.vue", 7)] == (
            "src/components/MyCard.vue#<module>"
        )

    def test_a_plain_element_is_not_a_reference(self, tmp_path: Path) -> None:
        self.vue_project(tmp_path)
        assert ("src/App.vue", 8) not in edge_targets(tmp_path)

    def test_laravel_conventions_do_not_apply_to_a_vue_project(self, tmp_path: Path) -> None:
        self.vue_project(tmp_path)
        assert active_plugins(tmp_path, frozenset()) == ()


class TestStoreAgreement:
    def test_the_store_resolves_conventions_the_same_way(self, tmp_path: Path) -> None:
        # The store and the batch builder are two paths to one answer. An
        # edge that exists in only one of them is the kind of divergence
        # that makes an index untrustworthy without looking wrong.
        from repoatlas.store.database import IndexStore
        from repoatlas.store.incremental import update_store

        project = tmp_path / "project"
        project.mkdir()
        laravel_project(project)
        with IndexStore(tmp_path / "index.db") as store:
            update_store(project, store, use_git=False)
            stored = {
                (edge.site_path, edge.site_range.start.line + 1, edge.dst_id)
                for edge in store.snapshot().edges
                if edge.kind.value != "contains" and edge.site_range is not None
            }
        direct = {
            (edge.site_path, edge.site_range.start.line + 1, edge.dst_id)
            for edge in build_snapshot(project, use_git=False).snapshot.edges
            if edge.kind.value != "contains" and edge.site_range is not None
        }
        assert stored == direct
