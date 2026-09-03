"""Tests for framework conventions.

A convention is not a heuristic. `view('users.index')` names exactly one
file, and the only question is whether that file is in this repository. So
these tests check two symmetrical things: that a name whose file exists
produces an edge at import-map confidence, and that a name whose file does
not produces no edge at all. The second matters more. A template name that
falls through to the identifier cascade would happily match any function
called `index`, and that wrong edge would carry the same confidence as a
right one.

The rules themselves live in `conventions.json`, so most of what is tested
here is the engine that reads it: that a malformed rule is refused loudly,
that two frameworks claiming one reference kind stay out of each other's
way, and that an explicit import always beats a convention.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoatlas.plugins import ConventionPlugin, active_plugins, load_registry
from repoatlas.plugins.base import register, registered_plugins
from repoatlas.plugins.registry import RegistryError, registry_source

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

from repoatlas.parse.build import build_snapshot


def write(root: Path, path: str, text: str = "") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def plugin(name: str) -> ConventionPlugin:
    for framework in load_registry():
        if framework.name == name:
            return ConventionPlugin(framework)
    raise AssertionError(f"no {name} entry in the registry")


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


def quasar_project(root: Path) -> None:
    """A Quasar front end, where components are used without importing them."""
    write(root, "package.json", json.dumps({"dependencies": {"vue": "^3.4", "quasar": "^2.16"}}))
    write(
        root,
        "src/pages/IndexPage.vue",
        "<template>\n"
        "  <q-page>\n"
        "    <UserCard />\n"
        "    <user-badge />\n"
        "    <FormField />\n"
        "    <q-btn label=\"go\" />\n"
        "    <router-link to=\"/\" />\n"
        "    <div>plain</div>\n"
        "  </q-page>\n"
        "</template>\n",
    )
    write(root, "src/components/UserCard.vue", "<template><div/></template>\n")
    write(root, "src/components/UserBadge.vue", "<template><span/></template>\n")
    write(root, "src/components/forms/FormField.vue", "<template><input/></template>\n")


def edge_targets(root: Path) -> dict[tuple[str, int], str]:
    """Every non-containment edge, keyed by where it was written."""
    result = build_snapshot(root, use_git=False)
    targets = {}
    for edge in result.snapshot.edges:
        if edge.kind.value == "contains" or edge.site_range is None:
            continue
        targets[(edge.site_path or "", edge.site_range.start.line + 1)] = edge.dst_id
    return targets


class TestRegistryFormat:
    def test_the_shipped_registry_loads(self) -> None:
        names = [framework.name for framework in load_registry()]
        assert names == ["laravel", "vue"]

    def test_every_framework_declares_what_it_claims(self) -> None:
        for framework in load_registry():
            assert framework.kinds, framework.name
            assert framework.detect, framework.name

    def test_a_framework_without_a_name_is_refused(self) -> None:
        with pytest.raises(RegistryError, match="needs a name"):
            load_registry('{"frameworks": [{"rules": []}]}')

    def test_a_rule_without_kinds_is_refused(self) -> None:
        source = '{"frameworks": [{"name": "x", "rules": [{"candidates": [{}]}]}]}'
        with pytest.raises(RegistryError, match="kinds"):
            load_registry(source)

    def test_a_rule_without_candidates_is_refused(self) -> None:
        source = '{"frameworks": [{"name": "x", "rules": [{"kinds": ["view"]}]}]}'
        with pytest.raises(RegistryError, match="candidates"):
            load_registry(source)

    def test_a_search_by_name_needs_a_stem(self) -> None:
        source = (
            '{"frameworks": [{"name": "x", "rules": [{"kinds": ["view"],'
            ' "candidates": [{"under": "src"}]}]}]}'
        )
        with pytest.raises(RegistryError, match="stem"):
            load_registry(source)

    def test_a_detection_needs_packages(self) -> None:
        source = (
            '{"frameworks": [{"name": "x", "detect": [{"file": "a.json"}],'
            ' "rules": []}]}'
        )
        with pytest.raises(RegistryError, match="packages"):
            load_registry(source)

    def test_a_malformed_registry_is_refused(self) -> None:
        with pytest.raises(RegistryError, match="list of frameworks"):
            load_registry('{"frameworks": {}}')

    def test_the_registry_is_part_of_the_toolchain_stamp(self) -> None:
        # A no-op re-index skips resolution, so a changed rule that did not
        # change the stamp would leave the old edges in place for ever.
        from repoatlas.store.incremental import current_toolchain

        first = current_toolchain()
        assert first == current_toolchain()
        assert "laravel" in registry_source()


class TestDetection:
    def test_composer_requiring_laravel_activates_the_plugin(self, tmp_path: Path) -> None:
        write(tmp_path, "composer.json", json.dumps({"require": {"laravel/framework": "^11"}}))
        assert [p.name for p in active_plugins(tmp_path, frozenset())] == ["laravel"]

    def test_package_json_requiring_quasar_activates_vue(self, tmp_path: Path) -> None:
        write(tmp_path, "package.json", json.dumps({"dependencies": {"quasar": "^2.16"}}))
        assert [p.name for p in active_plugins(tmp_path, frozenset())] == ["vue"]

    def test_both_are_active_in_one_repository(self, tmp_path: Path) -> None:
        write(tmp_path, "composer.json", json.dumps({"require": {"laravel/framework": "^11"}}))
        write(tmp_path, "package.json", json.dumps({"devDependencies": {"vue": "^3.4"}}))
        assert {p.name for p in active_plugins(tmp_path, frozenset())} == {"laravel", "vue"}

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
        write(tmp_path, "composer.json", "{not json")
        assert active_plugins(tmp_path, frozenset()) == ()

    def test_detection_survives_a_plugin_that_raises(self, tmp_path: Path) -> None:
        class Exploding:
            name = "exploding"
            kinds = ("view",)

            def detect(self, root: Path, files: frozenset[str]) -> bool:
                raise RuntimeError("boom")

            def resolve(self, kind, name, *, from_path, language=None, files):
                return None  # pragma: no cover

        from repoatlas.plugins import base

        try:
            register(Exploding())
            assert "exploding" not in [p.name for p in active_plugins(tmp_path, frozenset())]
        finally:
            base._REGISTRY = [p for p in base._REGISTRY if p.name != "exploding"]

    def test_registering_the_same_name_twice_replaces_it(self) -> None:
        before = len(registered_plugins())
        register(ConventionPlugin(load_registry()[0]))
        assert len(registered_plugins()) == before


class TestLaravelNames:
    @pytest.fixture
    def files(self) -> frozenset[str]:
        return frozenset(
            {
                "resources/views/users/index.blade.php",
                "resources/views/components/alert.blade.php",
                "resources/views/components/forms/input.blade.php",
                "resources/views/components/tabs/index.blade.php",
                "app/View/Components/DataTable.php",
                "app/Livewire/UserList.php",
            }
        )

    def resolve(self, kind: str, name: str, files: frozenset[str], language: str = "blade"):
        return plugin("laravel").resolve(
            kind, name, from_path="a.blade.php", language=language, files=files
        )

    def test_a_dotted_view_name_is_a_directory_path(self, files) -> None:
        assert self.resolve("view", "users.index", files, "php") == (
            "resources/views/users/index.blade.php"
        )

    def test_extends_and_include_read_the_same_way(self, files) -> None:
        for kind in ("extends", "include"):
            assert self.resolve(kind, "users.index", files) == (
                "resources/views/users/index.blade.php"
            )

    def test_a_view_that_does_not_exist_resolves_to_nothing(self, files) -> None:
        assert self.resolve("view", "users.nope", files, "php") is None

    def test_a_namespaced_view_belongs_to_a_package(self, files) -> None:
        # `mail::message` lives in a vendor directory this index does not
        # cover, so claiming a local file for it would be wrong.
        assert self.resolve("view", "mail::message", files, "php") is None

    def test_a_component_tag_finds_its_anonymous_view(self, files) -> None:
        assert self.resolve("component", "x-alert", files) == (
            "resources/views/components/alert.blade.php"
        )

    def test_a_dotted_component_tag_nests(self, files) -> None:
        assert self.resolve("component", "x-forms.input", files) == (
            "resources/views/components/forms/input.blade.php"
        )

    def test_a_component_directory_falls_back_to_its_index(self, files) -> None:
        assert self.resolve("component", "x-tabs", files) == (
            "resources/views/components/tabs/index.blade.php"
        )

    def test_a_component_class_answers_when_no_view_does(self, files) -> None:
        assert self.resolve("component", "x-data-table", files) == (
            "app/View/Components/DataTable.php"
        )

    def test_a_livewire_tag_finds_its_class(self, files) -> None:
        assert self.resolve("component", "livewire:user-list", files) == (
            "app/Livewire/UserList.php"
        )

    def test_a_plain_html_tag_is_not_a_component(self, files) -> None:
        assert self.resolve("component", "div", files) is None

    def test_an_unknown_kind_is_declined(self, files) -> None:
        assert self.resolve("call", "users.index", files) is None

    def test_an_empty_name_is_declined(self, files) -> None:
        assert self.resolve("view", "   ", files, "php") is None
        assert self.resolve("component", "x-", files) is None

    def test_a_vue_file_is_not_offered_laravel_components(self, files) -> None:
        # Both frameworks claim `component`. Without the language scope a
        # Blade rule would be asked about a Vue tag and the answer would
        # depend on registry order.
        assert self.resolve("component", "x-alert", files, "vue") is None


class TestVueNames:
    @pytest.fixture
    def files(self) -> frozenset[str]:
        return frozenset(
            {
                "src/components/UserCard.vue",
                "src/components/forms/FormField.vue",
                "src/layouts/MainLayout.vue",
                "src/widgets/Loose.vue",
            }
        )

    def resolve(self, name: str, files: frozenset[str], language: str = "vue"):
        return plugin("vue").resolve(
            "component", name, from_path="src/pages/A.vue", language=language, files=files
        )

    def test_a_pascal_tag_is_the_file_of_that_name(self, files) -> None:
        assert self.resolve("UserCard", files) == "src/components/UserCard.vue"

    def test_a_kebab_tag_means_the_same_component(self, files) -> None:
        assert self.resolve("user-card", files) == "src/components/UserCard.vue"

    def test_the_search_reaches_any_depth(self, files) -> None:
        # A build step that auto-imports components does not care how deeply
        # they are nested, so neither can this.
        assert self.resolve("FormField", files) == "src/components/forms/FormField.vue"

    def test_layouts_are_searched_too(self, files) -> None:
        assert self.resolve("MainLayout", files) == "src/layouts/MainLayout.vue"

    def test_a_file_outside_the_searched_roots_is_not_claimed(self, files) -> None:
        assert self.resolve("Loose", files) is None

    def test_quasar_components_are_not_ours_to_resolve(self, files) -> None:
        # `q-btn` ships in node_modules. Even if a file happened to be
        # called QBtn.vue, the tag does not mean it.
        assert self.resolve("q-btn", files) is None
        assert self.resolve("router-link", files) is None

    def test_a_blade_file_is_not_offered_vue_components(self, files) -> None:
        assert self.resolve("UserCard", files, "blade") is None


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
        assert ("app/Http/Controllers/UserController.php", 7) not in edge_targets(tmp_path)

    def test_a_template_that_defines_nothing_is_still_a_target(self, tmp_path: Path) -> None:
        # `layouts/app.blade.php` yields no symbols and no references. It
        # still needs a module symbol, or the edge into it disappears.
        laravel_project(tmp_path)
        result = build_snapshot(tmp_path, use_git=False)
        assert "resources/views/layouts/app.blade.php#<module>" in result.snapshot.symbols


class TestQuasarEdges:
    def test_an_auto_imported_component_resolves(self, tmp_path: Path) -> None:
        quasar_project(tmp_path)
        assert edge_targets(tmp_path)[("src/pages/IndexPage.vue", 3)] == (
            "src/components/UserCard.vue#<module>"
        )

    def test_a_kebab_tag_resolves_to_the_same_kind_of_file(self, tmp_path: Path) -> None:
        quasar_project(tmp_path)
        assert edge_targets(tmp_path)[("src/pages/IndexPage.vue", 4)] == (
            "src/components/UserBadge.vue#<module>"
        )

    def test_a_nested_component_is_found(self, tmp_path: Path) -> None:
        quasar_project(tmp_path)
        assert edge_targets(tmp_path)[("src/pages/IndexPage.vue", 5)] == (
            "src/components/forms/FormField.vue#<module>"
        )

    def test_framework_tags_and_plain_elements_produce_nothing(self, tmp_path: Path) -> None:
        quasar_project(tmp_path)
        targets = edge_targets(tmp_path)
        for line in (6, 7, 8):
            assert ("src/pages/IndexPage.vue", line) not in targets

    def test_an_import_beats_a_convention(self, tmp_path: Path) -> None:
        # Two files share a name and the page imports one of them. Following
        # the convention instead would silently point at the other.
        write(tmp_path, "package.json", json.dumps({"dependencies": {"vue": "^3.4"}}))
        write(
            tmp_path,
            "src/pages/A.vue",
            "<script setup lang=\"ts\">\n"
            "import Shadow from '../widgets/Shadow.vue'\n"
            "</script>\n"
            "<template><Shadow /></template>\n",
        )
        write(tmp_path, "src/components/Shadow.vue", "<template><div/></template>\n")
        write(tmp_path, "src/widgets/Shadow.vue", "<template><div/></template>\n")
        assert edge_targets(tmp_path)[("src/pages/A.vue", 4)] == (
            "src/widgets/Shadow.vue#<module>"
        )


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
        quasar_project(project)
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
