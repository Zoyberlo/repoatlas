"""Folding a type engine's answers into an index.

The judgement being tested is which of a dump's resolutions are worth
keeping: a target in vendor is not navigation, a site the cascade already
settled must not be touched, and a member nothing declares should land on
the class that owns it rather than nowhere. Written against hand-made
dumps, because that is where those decisions live and because the PHP
half is tested separately.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoatlas.enrich import enrich_from_phpstan
from repoatlas.model import ResolutionTier
from repoatlas.store import IndexStore, update_store

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

MODEL = """<?php
namespace App\\Models;

class Ad
{
    public function client()
    {
        return $this->id;
    }
}
"""

CALLER = """<?php
namespace App\\Http;

use App\\Models\\Ad;

class AdController
{
    public function show($ad)
    {
        return $ad->balance_due;
    }
}
"""


def offsets_of(text: str, needle: str, occurrence: int = 1) -> tuple[int, int]:
    data = text.encode("utf-8")
    start = -1
    for _ in range(occurrence):
        start = data.find(needle.encode("utf-8"), start + 1)
    return start, start + len(needle.encode("utf-8"))


def line_of(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines()):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} not in the fixture")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    (root / "Models").mkdir(parents=True)
    (root / "Http").mkdir(parents=True)
    (root / "Models" / "Ad.php").write_bytes(MODEL.encode("utf-8"))
    (root / "Http" / "AdController.php").write_bytes(CALLER.encode("utf-8"))
    return root


@pytest.fixture
def store(project: Path, tmp_path: Path):
    with IndexStore(tmp_path / "index.db") as opened:
        update_store(project, opened, use_git=False)
        yield opened


def dump(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )
    return path


def declaration(project: Path) -> dict[str, object]:
    start, end = offsets_of(MODEL, "Ad", occurrence=2)
    return {
        "kind": "def",
        "symbol_kind": "class",
        "name": "Ad",
        "container": "",
        "fqn": "App\\Models\\Ad",
        "path": str(project / "Models" / "Ad.php"),
        "line": line_of(MODEL, "class Ad"),
        "start": start,
        "end": end,
    }


def column_site(project: Path, **overrides: object) -> dict[str, object]:
    start, end = offsets_of(CALLER, "balance_due")
    record: dict[str, object] = {
        "kind": "property",
        "name": "balance_due",
        "receiver": "App\\Models\\Ad",
        "resolved": True,
        "class": "App\\Models\\Ad",
        "file": str(project / "Models" / "Ad.php"),
        "magic": True,
        "path": str(project / "Http" / "AdController.php"),
        "line": line_of(CALLER, "balance_due"),
        "start": start,
        "end": end,
    }
    record.update(overrides)
    return record


class TestWhatItAdds:
    def test_a_column_nothing_declares_lands_on_the_model(
        self, store: IndexStore, project: Path, tmp_path: Path
    ) -> None:
        # `$ad->balance_due` has no declaration anywhere: Eloquent invents
        # it from the table. The model is where an agent needs to go, and
        # it is a better answer than none.
        result = enrich_from_phpstan(
            store,
            dump(tmp_path / "d.jsonl", [declaration(project), column_site(project)]),
            phpstan_root=project,
        )
        assert result.added == 1
        assert result.magic_added == 1
        added = [e for e in store.edges() if e.tier is ResolutionTier.TYPE_ENGINE]
        assert len(added) == 1
        target = store.symbol(added[0].dst_id)
        assert target is not None and target.name == "Ad"
        source = store.symbol(added[0].src_id)
        assert source is not None and source.name == "show"

    def test_the_edge_says_it_came_from_inference_not_a_compiler(
        self, store: IndexStore, project: Path, tmp_path: Path
    ) -> None:
        # A tier of its own, below ORACLE: an inference can be wrong where
        # a compilation cannot, and a report has to be able to say which
        # edges are which.
        enrich_from_phpstan(
            store,
            dump(tmp_path / "d.jsonl", [declaration(project), column_site(project)]),
            phpstan_root=project,
        )
        edge = next(e for e in store.edges() if e.tier is ResolutionTier.TYPE_ENGINE)
        assert edge.score == pytest.approx(0.98)
        assert edge.tier.label == "type_engine"

    def test_a_prefix_puts_the_paths_where_the_index_keeps_them(
        self, store: IndexStore, project: Path, tmp_path: Path
    ) -> None:
        # Analysis is usually run over a copy, or over a backend on its
        # own; guessing between the two roots silently matches nothing.
        result = enrich_from_phpstan(
            store,
            dump(tmp_path / "d.jsonl", [declaration(project), column_site(project)]),
            phpstan_root=project,
            prefix="backend",
        )
        assert result.added == 0
        assert result.unplaceable == 1


class TestWhatItRefuses:
    def test_a_target_in_vendor_is_not_navigation(
        self, store: IndexStore, project: Path, tmp_path: Path
    ) -> None:
        # A real vendor record names the vendor file too. Overriding only
        # the class would leave the join hunting for Carbon inside the
        # repository, which is a different failure with a different count.
        site = column_site(
            project,
            **{
                "class": "Illuminate\\Support\\Carbon",
                "file": "/elsewhere/vendor/nesbot/carbon/src/Carbon.php",
                "magic": False,
            },
        )
        result = enrich_from_phpstan(
            store, dump(tmp_path / "d.jsonl", [site]), phpstan_root=project
        )
        assert result.resolved == 1
        assert result.outside_index == 1
        assert result.added == 0

    def test_an_unresolved_site_adds_nothing_but_is_counted(
        self, store: IndexStore, project: Path, tmp_path: Path
    ) -> None:
        site = column_site(project, resolved=False)
        result = enrich_from_phpstan(
            store, dump(tmp_path / "d.jsonl", [site]), phpstan_root=project
        )
        assert result.sites == 1 and result.resolved == 0 and result.added == 0

    def test_what_the_cascade_already_settled_is_left_alone(
        self, store: IndexStore, project: Path, tmp_path: Path
    ) -> None:
        # `$this->id` inside the model resolves without help. Enriching it
        # again would double the edge and inflate every count that reads
        # them.
        start, end = offsets_of(MODEL, "id")
        site = {
            "kind": "property",
            "name": "id",
            "receiver": "App\\Models\\Ad",
            "resolved": True,
            "class": "App\\Models\\Ad",
            "file": str(project / "Models" / "Ad.php"),
            "magic": True,
            "path": str(project / "Models" / "Ad.php"),
            "line": line_of(MODEL, "$this->id"),
            "start": start,
            "end": end,
        }
        before = len(store.edges())
        result = enrich_from_phpstan(
            store, dump(tmp_path / "d.jsonl", [declaration(project), site]),
            phpstan_root=project,
        )
        assert result.added + result.already_known + result.unplaceable == 1
        assert len(store.edges()) == before + result.added

    def test_a_dry_run_writes_nothing(
        self, store: IndexStore, project: Path, tmp_path: Path
    ) -> None:
        before = len(store.edges())
        result = enrich_from_phpstan(
            store,
            dump(tmp_path / "d.jsonl", [declaration(project), column_site(project)]),
            phpstan_root=project,
            dry_run=True,
        )
        assert result.added == 1
        assert len(store.edges()) == before

    def test_running_it_twice_does_not_double_the_edges(
        self, store: IndexStore, project: Path, tmp_path: Path
    ) -> None:
        records = [declaration(project), column_site(project)]
        first = enrich_from_phpstan(
            store, dump(tmp_path / "d.jsonl", records), phpstan_root=project
        )
        second = enrich_from_phpstan(
            store, dump(tmp_path / "d.jsonl", records), phpstan_root=project
        )
        assert first.added == 1
        assert second.added == 0
        assert second.already_known == 1
