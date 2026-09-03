"""A scoped re-index must produce exactly the edges a rebuild would.

The rule is that a reference can only change its answer if a symbol with
its name was added, removed or moved, or, for a framework convention, if a
file appeared or vanished. These tests try the ways that rule could be
wrong, and after each change compare the incrementally updated store with
a store built fresh from the final tree. Same edges, same ranks, or the
narrowing is not a narrowing but a bug that looks fine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoatlas.store import IndexStore, update_store

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")


def write(root: Path, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def edge_set(store: IndexStore) -> set[tuple[object, ...]]:
    return {
        (
            edge.site_path,
            edge.site_range.start.line if edge.site_range else None,
            edge.site_range.start.character if edge.site_range else None,
            edge.src_id,
            edge.dst_id,
            edge.kind.value,
            edge.tier.label,
        )
        for edge in store.edges()
    }


def fresh_edges(root: Path, tmp_path: Path) -> tuple[set[tuple[object, ...]], dict[str, tuple[float, int]]]:
    with IndexStore(tmp_path / "fresh.db") as fresh:
        update_store(root, fresh, use_git=False)
        return edge_set(fresh), fresh.ranks()


def project(root: Path) -> None:
    write(
        root,
        "lib/core.py",
        "class Engine:\n    def run(self):\n        return 1\n\n\ndef run():\n    return Engine().run()\n",
    )
    write(
        root,
        "lib/util.py",
        "from lib.core import Engine\n\n\ndef helper():\n    return Engine()\n\n\ndef run():\n    return helper()\n",
    )
    write(
        root,
        "app/main.py",
        "from lib.util import helper\n\n\ndef main():\n    helper()\n    run()\n",
    )
    write(root, "app/late.py", "from lib.missing import Ghost\n\n\ndef use():\n    return Ghost()\n")


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, IndexStore]:
    root = tmp_path / "repo"
    project(root)
    store = IndexStore(tmp_path / "inc.db")
    update_store(root, store, use_git=False)
    yield root, store
    store.close()


def assert_matches_fresh(root: Path, store: IndexStore, tmp_path: Path) -> None:
    edges, ranks = fresh_edges(root, tmp_path)
    assert edge_set(store) == edges
    stored = store.ranks()
    assert set(stored) == set(ranks)
    for symbol_id, (score, degree) in ranks.items():
        assert stored[symbol_id][0] == pytest.approx(score)
        assert stored[symbol_id][1] == degree


class TestScopedMatchesFull:
    def test_a_body_edit_that_changes_no_definition(self, workspace, tmp_path: Path) -> None:
        root, store = workspace
        write(root, "app/main.py", "from lib.util import helper\n\n\ndef main():\n    helper()\n    helper()\n    run()\n")
        result = update_store(root, store, use_git=False)
        assert result.scoped
        assert_matches_fresh(root, store, tmp_path)

    def test_a_popular_name_gaining_a_definition(self, workspace, tmp_path: Path) -> None:
        # `run` is defined twice and referenced from an untouched file. A
        # third definition changes which one the bottom rung picks, and the
        # untouched reference must follow.
        root, store = workspace
        write(root, "app/extra.py", "def run():\n    return 3\n")
        result = update_store(root, store, use_git=False)
        assert result.scoped
        assert result.revisited > 0
        assert_matches_fresh(root, store, tmp_path)

    def test_removing_a_file_others_import(self, workspace, tmp_path: Path) -> None:
        root, store = workspace
        (root / "lib" / "util.py").unlink()
        result = update_store(root, store, use_git=False)
        assert result.scoped
        assert_matches_fresh(root, store, tmp_path)

    def test_a_new_file_that_satisfies_an_old_import(self, workspace, tmp_path: Path) -> None:
        # `app/late.py` imports `Ghost` from a module that did not exist.
        # Creating it must resolve that reference without touching late.py.
        root, store = workspace
        write(root, "lib/missing.py", "class Ghost:\n    pass\n")
        result = update_store(root, store, use_git=False)
        assert result.scoped
        assert_matches_fresh(root, store, tmp_path)
        assert any(edge.dst_id == "lib/missing.py#Ghost" for edge in store.edges())

    def test_renaming_a_definition(self, workspace, tmp_path: Path) -> None:
        root, store = workspace
        write(
            root,
            "lib/core.py",
            "class Motor:\n    def run(self):\n        return 1\n\n\ndef run():\n    return Motor().run()\n",
        )
        result = update_store(root, store, use_git=False)
        assert result.scoped
        assert_matches_fresh(root, store, tmp_path)

    def test_a_framework_view_appearing(self, tmp_path: Path) -> None:
        root = tmp_path / "laravel"
        write(root, "composer.json", json.dumps({"require": {"laravel/framework": "^11"}}))
        write(
            root,
            "app/Http/Controllers/HomeController.php",
            "<?php\nclass HomeController\n{\n    public function index()\n    {\n        return view('home.index');\n    }\n}\n",
        )
        with IndexStore(tmp_path / "inc.db") as store:
            update_store(root, store, use_git=False)
            assert not any(edge.dst_id.endswith("index.blade.php#<module>") for edge in store.edges())
            write(root, "resources/views/home/index.blade.php", "<h1>hi</h1>\n")
            result = update_store(root, store, use_git=False)
            assert result.scoped
            assert_matches_fresh(root, store, tmp_path)
            assert any(edge.dst_id.endswith("index.blade.php#<module>") for edge in store.edges())

    def test_several_changes_in_a_row(self, workspace, tmp_path: Path) -> None:
        root, store = workspace
        write(root, "app/extra.py", "def run():\n    return 3\n")
        update_store(root, store, use_git=False)
        (root / "app" / "late.py").unlink()
        update_store(root, store, use_git=False)
        write(root, "lib/util.py", "def helper():\n    return None\n")
        update_store(root, store, use_git=False)
        assert_matches_fresh(root, store, tmp_path)


class TestScopeIsNarrow:
    def test_an_unrelated_edit_revisits_only_its_own_file(self, workspace, tmp_path: Path) -> None:
        root, store = workspace
        total = len(store.references())
        write(root, "app/main.py", "from lib.util import helper\n\n\ndef main():\n    helper()\n    helper()\n    run()\n")
        result = update_store(root, store, use_git=False)
        assert result.scoped
        assert 0 < result.revisited < total

    def test_a_no_op_resolves_nothing(self, workspace) -> None:
        root, store = workspace
        result = update_store(root, store, use_git=False)
        assert not result.scoped
        assert result.revisited == 0
        assert result.resolve_seconds == 0.0

    def test_the_first_index_is_never_scoped(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        project(root)
        with IndexStore(tmp_path / "first.db") as store:
            result = update_store(root, store, use_git=False)
            assert not result.scoped

    def test_a_plugin_appearing_forces_a_full_pass(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        write(root, "app/a.php", "<?php\nfunction f() { return view('x.y'); }\n")
        with IndexStore(tmp_path / "p.db") as store:
            update_store(root, store, use_git=False)
            write(root, "composer.json", json.dumps({"require": {"laravel/framework": "^11"}}))
            write(root, "resources/views/x/y.blade.php", "<p></p>\n")
            result = update_store(root, store, use_git=False)
            # The manifest is not an indexed file, so nothing in the change
            # set says the plugin appeared; the stored plugin list does.
            assert not result.scoped
            assert any(edge.dst_id.endswith("y.blade.php#<module>") for edge in store.edges())
