"""Where the tokens go, and the gate on the total."""

from __future__ import annotations

from pathlib import Path

from repoatlas.cli import main
from repoatlas.cost import token_tree
from repoatlas.store import IndexStore, update_store


def _project(root: Path) -> None:
    (root / "app" / "Models").mkdir(parents=True)
    (root / "app" / "Http").mkdir(parents=True)
    (root / "app" / "Models" / "Ad.php").write_text(
        "<?php\nclass Ad { public function client() {} public function tags() {} }\n",
        encoding="utf-8",
    )
    (root / "app" / "Http" / "Ctl.php").write_text(
        "<?php\nclass Ctl { public function index() {} }\n", encoding="utf-8"
    )
    (root / "helpers.php").write_text("<?php\nfunction helper() {}\n", encoding="utf-8")


class TestTokenTree:
    def test_costs_add_up_from_files_to_directories_to_the_root(self, tmp_path: Path) -> None:
        project = tmp_path / "p"
        project.mkdir()
        _project(project)
        with IndexStore(tmp_path / "i.db") as store:
            update_store(project, store, use_git=False)
            tree = token_tree(store, depth=2)
        assert tree.root.files == 3
        assert tree.root.symbols == 6
        app = next(node for node in tree.root.children if node.path == "app")
        assert app.files == 2
        assert app.tokens == sum(child.tokens for child in app.children)
        assert tree.root.tokens == app.tokens + sum(
            node.tokens for node in tree.root.children if node.path != "app"
        )
        # Heaviest first, so the eye lands on what to trim.
        assert [node.path for node in app.children] == ["app/Models", "app/Http"]

    def test_the_gate_fails_above_the_ceiling_and_passes_below(
        self, tmp_path: Path, capsys
    ) -> None:
        project = tmp_path / "p"
        project.mkdir()
        _project(project)
        with IndexStore(tmp_path / "i.db") as store:
            update_store(project, store, use_git=False)
        assert main(["tokens", str(tmp_path / "i.db"), "--max-total", "5"]) == 1
        assert "above the 5 allowed" in capsys.readouterr().err
        assert main(["tokens", str(tmp_path / "i.db"), "--max-total", "100000"]) == 0
        out = capsys.readouterr().out
        assert "skeleton of 3 files, 6 symbols" in out
        assert "app/" in out
