"""Building an index from git's objects, with no working tree.

The claim these tests hold is narrow and total: an index built from a bare
repository is the *same index* as one built from a checkout of the same
commit. Not similar, not close on counts — the same symbols, the same
edges, the same content digests. Anything less and the diff-review result,
which was measured on checkout-built indexes, would not transfer to the
setting it was measured for.

The second thing they hold is the part that would fail silently. Framework
conventions come from reading `composer.json`, and a revision has no file
to read. If that quietly returns nothing, the index still builds, still
looks healthy, and has lost every Blade view — which is most of what makes
the PHP numbers what they are.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from repoatlas.parse.gitobjects import GitObjectError, RevisionTree, resolve_revision
from repoatlas.store import IndexStore, update_store

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(repo: Path, *arguments: str) -> str:
    """Run git in ``repo`` with the ambient configuration held still.

    Identity and signing come from the developer's global config, which a
    test must not depend on, and autocrlf would rewrite line endings on the
    way into the checkout and make the comparison test measure the wrong
    thing.
    """
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.autocrlf=false",
            "-C",
            str(repo),
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


LARAVEL_MANIFEST = '{"require": {"laravel/framework": "^11.0"}}'

CONTROLLER = """<?php

namespace App\\Http\\Controllers;

class AdController
{
    public function index()
    {
        return view('ads.index');
    }
}
"""

MODEL = """<?php

namespace App\\Models;

class Ad
{
    public function save()
    {
        return true;
    }
}
"""


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A small Laravel-shaped repository with one commit."""
    root = tmp_path / "origin"
    root.mkdir()
    git(root, "init", "--quiet", "-b", "main")
    write(root, "composer.json", LARAVEL_MANIFEST)
    write(root, "app/Http/Controllers/AdController.php", CONTROLLER)
    write(root, "app/Models/Ad.php", MODEL)
    write(root, "resources/views/ads/index.blade.php", "<h1>{{ $ad->title }}</h1>\n")
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", "first")
    return root


@pytest.fixture
def bare(tmp_path: Path, origin: Path) -> Path:
    """The same repository with no working tree at all."""
    mirror = tmp_path / "mirror.git"
    subprocess.run(
        ["git", "clone", "--bare", "--quiet", str(origin), str(mirror)],
        capture_output=True,
        check=True,
    )
    return mirror


def build_from_tree(root: Path, store_path: Path) -> None:
    with IndexStore(store_path) as store:
        update_store(root, store)


def build_from_objects(repo: Path, store_path: Path, revision: str = "HEAD") -> None:
    with IndexStore(store_path) as store, RevisionTree(repo, revision) as tree:
        update_store(None, store, tree=tree)


def contents(store_path: Path, sql: str) -> set[tuple[object, ...]]:
    connection = sqlite3.connect(store_path)
    try:
        return set(connection.execute(sql).fetchall())
    finally:
        connection.close()


def meta(store_path: Path) -> dict[str, str]:
    connection = sqlite3.connect(store_path)
    try:
        return dict(connection.execute("select key, value from meta").fetchall())
    finally:
        connection.close()


SYMBOLS = (
    "select id, path, name, kind, qualified_name, signature, "
    "name_start_line, name_start_char, full_end_line from symbols"
)
EDGES = (
    "select site_path, src_id, dst_id, kind, tier, confidence, "
    "site_start_line, site_start_char from edges"
)
FILES = "select path, language, digest, size, has_errors, error_count from files"


class TestItProducesTheSameIndex:
    """No working tree, same answers. This is the whole point."""

    @pytest.fixture
    def both(self, tmp_path: Path, origin: Path, bare: Path) -> tuple[Path, Path]:
        from_tree = tmp_path / "tree.db"
        from_objects = tmp_path / "objects.db"
        build_from_tree(origin, from_tree)
        build_from_objects(bare, from_objects)
        return from_tree, from_objects

    def test_the_symbols_are_identical(self, both: tuple[Path, Path]) -> None:
        from_tree, from_objects = both
        assert contents(from_objects, SYMBOLS) == contents(from_tree, SYMBOLS)

    def test_the_edges_are_identical(self, both: tuple[Path, Path]) -> None:
        from_tree, from_objects = both
        edges = contents(from_objects, EDGES)
        assert edges == contents(from_tree, EDGES)
        assert edges, "the fixture should produce at least one edge"

    def test_the_content_digests_are_identical(self, both: tuple[Path, Path]) -> None:
        # The strongest of the three: the bytes that came off the pipe hash
        # to what the bytes on disk hash to, so nothing was transformed on
        # the way through git.
        from_tree, from_objects = both
        assert contents(from_objects, FILES) == contents(from_tree, FILES)


class TestFrameworkConventionsSurvive:
    def test_the_manifest_is_still_read(self, tmp_path: Path, bare: Path) -> None:
        # Detection reads composer.json off a filesystem. A revision has no
        # filesystem, and the failure mode is silent: the index builds, and
        # every `view('ads.index')` edge is simply gone.
        store_path = tmp_path / "objects.db"
        build_from_objects(bare, store_path)
        assert "laravel" in meta(store_path).get("plugins", "")

    def test_the_view_edge_is_resolved(self, tmp_path: Path, bare: Path) -> None:
        store_path = tmp_path / "objects.db"
        build_from_objects(bare, store_path)
        targets = contents(store_path, "select dst_id from edges")
        assert any(
            "resources/views/ads/index.blade.php" in str(row[0]) for row in targets
        ), "the controller's view() call should reach the Blade file"


class TestWhatItRecords:
    def test_it_names_the_commit_it_read(self, tmp_path: Path, bare: Path) -> None:
        store_path = tmp_path / "objects.db"
        build_from_objects(bare, store_path)
        recorded = meta(store_path)
        assert recorded["source"] == "git-objects"
        assert recorded["commit"] == resolve_revision(bare)

    def test_it_claims_no_project_root(self, tmp_path: Path, bare: Path) -> None:
        # An empty root is what makes `index_status` say bodies cannot be
        # read. A path here would be a directory holding no source, and
        # every include_body would fail one call at a time instead.
        store_path = tmp_path / "objects.db"
        build_from_objects(bare, store_path)
        assert meta(store_path)["project_root"] == ""


class TestReadingTheObjects:
    def test_an_earlier_revision_gives_the_earlier_files(
        self, tmp_path: Path, origin: Path
    ) -> None:
        first = resolve_revision(origin)
        write(origin, "app/Models/Tag.php", "<?php\n\nclass Tag {}\n")
        git(origin, "add", "-A")
        git(origin, "commit", "--quiet", "-m", "second")

        older = tmp_path / "older.db"
        newer = tmp_path / "newer.db"
        build_from_objects(origin, older, first)
        build_from_objects(origin, newer, "HEAD")
        paths = {row[0] for row in contents(older, "select path from files")}
        assert "app/Models/Tag.php" not in paths
        assert "app/Models/Tag.php" in {
            row[0] for row in contents(newer, "select path from files")
        }

    def test_a_file_whose_content_holds_newlines_reads_whole(
        self, tmp_path: Path, origin: Path
    ) -> None:
        # `cat-file --batch` announces a length and then writes that many
        # bytes. Reading to the next newline instead would truncate every
        # file at its first line, which is the kind of bug that still
        # produces a plausible-looking index.
        body = "<?php\n\nclass Many\n{\n" + "".join(
            f"    public function m{n}() {{ return {n}; }}\n" for n in range(50)
        )
        body += "}\n"
        write(origin, "app/Models/Many.php", body)
        git(origin, "add", "-A")
        git(origin, "commit", "--quiet", "-m", "many")

        with RevisionTree(origin) as tree:
            files = {source.path: source for source in tree.files()}
            read = tree.read(files["app/Models/Many.php"])
        assert read == body.encode("utf-8")

    def test_a_missing_revision_is_reported(self, origin: Path) -> None:
        with pytest.raises(GitObjectError):
            RevisionTree(origin, "no-such-branch")


class TestTheCommandLine:
    def test_rev_without_a_store_is_refused(self, bare: Path) -> None:
        # The in-memory path would print numbers for a working tree that
        # does not exist, so refuse rather than answer about the wrong
        # thing.
        from repoatlas.cli import main

        with pytest.raises(SystemExit) as raised:
            main(["index", str(bare), "--rev", "HEAD"])
        assert "--store" in str(raised.value)

    def test_it_builds_through_the_cli(self, tmp_path: Path, bare: Path) -> None:
        from repoatlas.cli import main

        store_path = tmp_path / "cli.db"
        assert main(["index", str(bare), "--rev", "HEAD", "--store", str(store_path)]) == 0
        assert meta(store_path)["source"] == "git-objects"


class TestServingWhatWasBuilt:
    def test_index_status_names_the_commit(self, tmp_path: Path, bare: Path) -> None:
        # A store built this way travels to machines that hold no source, and
        # then the only way to know which commit it describes is that it says.
        from repoatlas.server.tools import index_status

        store_path = tmp_path / "objects.db"
        build_from_objects(bare, store_path)
        with IndexStore(store_path) as store:
            reported = index_status(store)
        assert f"at {resolve_revision(bare)[:12]}" in reported
        assert "include_body cannot read source" in reported
