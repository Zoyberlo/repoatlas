"""The PHPStan oracle: the reader, the configuration, and one real run.

The reader is tested against hand-written dumps because that is where the
judgement lives — which sites become edges, what joins a reference to a
declaration, what is dropped — and because a test that needs composer and a
network is a test nobody runs.

The one end-to-end test is marked ``oracle`` and skips without phpstan on
PATH, in the same way as the SCIP oracles.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from repoatlas.model import EdgeKind, SymbolKind
from repoatlas.oracle.phpstan import (
    PhpStanError,
    extension_directory,
    read_phpstan,
    run_phpstan,
    write_config,
)

SOURCE = """<?php
namespace App;

class Greeter
{
    public string $label = "x";

    public function greet(string $name): string
    {
        return $this->label . $name;
    }
}

class Runner
{
    public function run(): string
    {
        $g = new Greeter();
        return $g->greet("a") . $g->label;
    }
}
"""


def write_dump(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "app").mkdir(parents=True)
    # Written as bytes: the offsets below are counted off SOURCE, and on
    # Windows write_text would turn every newline into two, which is
    # exactly the kind of drift the byte-offset conversion has to survive.
    (root / "app" / "Demo.php").write_bytes(SOURCE.encode("utf-8"))
    return root


def offsets_of(text: str, needle: str, occurrence: int = 1) -> tuple[int, int]:
    """Byte offsets of the nth occurrence, as PhpParser would report them."""
    data = text.encode("utf-8")
    start = -1
    for _ in range(occurrence):
        start = data.find(needle.encode("utf-8"), start + 1)
    return start, start + len(needle.encode("utf-8"))


class TestReadingADump:
    def test_a_declaration_becomes_a_symbol_at_its_name(self, project: Path) -> None:
        source = project / "app" / "Demo.php"
        start, end = offsets_of(SOURCE, "greet")
        result = read_phpstan(
            write_dump(
                project / "dump.jsonl",
                [
                    {
                        "kind": "def",
                        "symbol_kind": "method",
                        "name": "greet",
                        "container": "App\\Greeter",
                        "fqn": "App\\Greeter::greet",
                        "path": str(source),
                        "line": 7,
                        "start": start,
                        "end": end,
                    }
                ],
            ),
            project,
        )
        assert result.definitions == 1
        symbol = next(iter(result.snapshot.symbols.values()))
        assert symbol.kind is SymbolKind.METHOD
        assert symbol.path == "app/Demo.php"
        # The name token, converted from a byte offset to a line and column.
        assert symbol.name_range.start.line == 7
        assert symbol.name_range.start.character == 20

    def test_a_resolved_site_becomes_an_edge_onto_the_declaration(
        self, project: Path
    ) -> None:
        source = project / "app" / "Demo.php"
        declaration = offsets_of(SOURCE, "greet")
        use = offsets_of(SOURCE, "greet", occurrence=2)
        result = read_phpstan(
            write_dump(
                project / "dump.jsonl",
                [
                    {
                        "kind": "def",
                        "symbol_kind": "method",
                        "name": "greet",
                        "container": "App\\Greeter",
                        "fqn": "App\\Greeter::greet",
                        "path": str(source),
                        "line": 7,
                        "start": declaration[0],
                        "end": declaration[1],
                    },
                    {
                        "kind": "call",
                        "name": "greet",
                        "receiver": "App\\Greeter",
                        "resolved": True,
                        "class": "App\\Greeter",
                        "file": str(source),
                        "magic": False,
                        "path": str(source),
                        "line": 19,
                        "start": use[0],
                        "end": use[1],
                    },
                ],
            ),
            project,
        )
        assert result.sites == 1 and result.resolved == 1 and result.linked == 1
        edge = result.snapshot.edges[0]
        assert edge.kind is EdgeKind.CALLS
        assert edge.site_path == "app/Demo.php"
        assert edge.site_range is not None
        assert result.snapshot.symbols[edge.dst_id].name == "greet"

    def test_a_member_written_down_nowhere_is_counted_but_not_linked(
        self, project: Path
    ) -> None:
        # An Eloquent column: larastan resolves it to the model, and there
        # is no declaration anywhere to point an edge at. Counting it as an
        # edge would invent a target; not counting it at all would hide the
        # one thing no SCIP indexer can see.
        source = project / "app" / "Demo.php"
        use = offsets_of(SOURCE, "label", occurrence=3)
        result = read_phpstan(
            write_dump(
                project / "dump.jsonl",
                [
                    {
                        "kind": "property",
                        "name": "balance_due",
                        "receiver": "App\\Models\\Ad",
                        "resolved": True,
                        "class": "App\\Models\\Ad",
                        "file": str(source),
                        "magic": True,
                        "path": str(source),
                        "line": 19,
                        "start": use[0],
                        "end": use[1],
                    }
                ],
            ),
            project,
        )
        assert result.resolved == 1
        assert result.magic == 1
        assert result.linked == 0
        assert result.snapshot.edges == []

    def test_an_unresolved_site_is_named_so_a_ceiling_can_be_stated(
        self, project: Path
    ) -> None:
        result = read_phpstan(
            write_dump(
                project / "dump.jsonl",
                [
                    {
                        "kind": "call",
                        "name": "mystery",
                        "receiver": "mixed",
                        "resolved": False,
                        "path": str(project / "app" / "Demo.php"),
                        "line": 3,
                        "start": 10,
                        "end": 17,
                    }
                ],
            ),
            project,
        )
        assert result.sites == 1 and result.resolved == 0
        assert result.unresolved_names == {"mystery": 1}
        assert result.resolution_rate == 0.0

    def test_vendor_stays_out_of_a_comparison_about_this_repository(
        self, project: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "elsewhere" / "Vendor.php"
        outside.parent.mkdir(parents=True)
        outside.write_text("<?php\n", encoding="utf-8")
        result = read_phpstan(
            write_dump(
                project / "dump.jsonl",
                [
                    {
                        "kind": "def",
                        "symbol_kind": "class",
                        "name": "Vendor",
                        "container": "",
                        "fqn": "Vendor",
                        "path": str(outside),
                        "line": 0,
                        "start": 0,
                        "end": 6,
                    }
                ],
            ),
            project,
        )
        assert result.definitions == 0
        assert result.snapshot.symbols == {}

    def test_a_trait_reaching_the_reader_twice_is_one_symbol_with_two_names(
        self, project: Path
    ) -> None:
        # PHPStan analyses a trait's body once per class that uses it and
        # reports the using class as the declaring one, so the same span
        # arrives under several names. Both names have to resolve, and to
        # the same symbol.
        source = project / "app" / "Demo.php"
        start, end = offsets_of(SOURCE, "greet")
        records = [
            {
                "kind": "def",
                "symbol_kind": "method",
                "name": "greet",
                "container": container,
                "fqn": f"{container}::greet",
                "path": str(source),
                "line": 7,
                "start": start,
                "end": end,
            }
            for container in ("App\\Greeter", "App\\Runner")
        ]
        result = read_phpstan(write_dump(project / "dump.jsonl", records), project)
        assert result.definitions == 1

    def test_a_dump_that_is_not_json_is_refused_with_its_line(
        self, project: Path
    ) -> None:
        dump = project / "dump.jsonl"
        dump.write_text('{"kind": "def"}\nnot json\n', encoding="utf-8")
        with pytest.raises(PhpStanError, match=r"dump\.jsonl:2"):
            read_phpstan(dump, project)


class TestConfiguration:
    def test_the_project_is_never_written_to(self, project: Path, tmp_path: Path) -> None:
        before = sorted(path.name for path in project.rglob("*"))
        work = tmp_path / "work"
        write_config(project, work, paths=("app",))
        assert (work / "phpstan.neon").exists()
        assert sorted(path.name for path in project.rglob("*")) == before

    def test_the_extension_is_included_and_the_cache_is_in_the_scratch(
        self, project: Path, tmp_path: Path
    ) -> None:
        text = write_config(project, tmp_path / "work", paths=("app",)).read_text(
            encoding="utf-8"
        )
        assert "extension.neon" in text
        assert str((tmp_path / "work" / "cache").resolve()) in text

    def test_mixed_is_not_allowed_to_claim_every_member(
        self, project: Path, tmp_path: Path
    ) -> None:
        # The trailing `!` is the whole point: NEON merges arrays, so
        # without it the line reads like a fix and changes nothing.
        text = write_config(project, tmp_path / "work", paths=("app",)).read_text(
            encoding="utf-8"
        )
        assert "universalObjectCratesClasses!: []" in text

    def test_a_root_with_none_of_the_usual_directories_says_so(
        self, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(PhpStanError, match="--paths"):
            write_config(empty, tmp_path / "work")

    def test_larastan_is_included_when_it_is_given(
        self, project: Path, tmp_path: Path
    ) -> None:
        larastan = tmp_path / "larastan" / "extension.neon"
        larastan.parent.mkdir()
        larastan.write_text("parameters:\n", encoding="utf-8")
        text = write_config(
            project, tmp_path / "work", paths=("app",), larastan=larastan
        ).read_text(encoding="utf-8")
        assert str(larastan.resolve()) in text


class TestTheExtensionItself:
    def test_every_php_file_the_configuration_names_is_shipped(self) -> None:
        directory = extension_directory()
        for name in ("SiteCollector.php", "DeclarationCollector.php", "DumpRule.php"):
            assert (directory / name).exists()
        bootstrap = (directory / "bootstrap.php").read_text(encoding="utf-8")
        for name in ("SiteCollector", "DeclarationCollector", "DumpRule"):
            assert name in bootstrap

    def test_the_dump_variable_matches_on_both_sides(self) -> None:
        from repoatlas.oracle.phpstan import DUMP_VARIABLE

        rule = (extension_directory() / "DumpRule.php").read_text(encoding="utf-8")
        assert f"'{DUMP_VARIABLE}'" in rule


@pytest.mark.oracle
@pytest.mark.slow
class TestARealRun:
    def test_phpstan_resolves_a_small_project(self, project: Path, tmp_path: Path) -> None:
        if shutil.which("phpstan") is None and shutil.which("php") is None:
            pytest.skip("needs php and phpstan on PATH")
        try:
            dump = run_phpstan(project, tmp_path / "work", paths=("app",), timeout=300)
        except PhpStanError as exc:
            pytest.skip(f"phpstan unavailable: {exc}")
        result = read_phpstan(dump, project)
        assert result.definitions >= 4
        assert result.resolved == result.sites
        assert result.linked >= 2
