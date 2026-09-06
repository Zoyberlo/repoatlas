"""The hook that answers a search instead of waiting to be called.

The measured problem it addresses is not accuracy but adoption: offline the
index beats `rg -w` on references, 1.000 F1 to 0.847, and an agent handed
the tool called it 0 times out of 8. So this runs after the search and adds
what the search could not say.

Which makes silence the important behaviour, and most of these tests are
about it. A hook that speaks on every search has replaced a tool nobody
calls with a tax everybody pays, and the whole point was to be cheaper.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoatlas.hook import HookInput, advise, run_hook, store_path
from repoatlas.store import IndexStore, update_store

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

# Two classes with a `save`, one of them actually called. A text search for
# `save` cannot tell the two apart; that difference is the whole product.
AD = """<?php

namespace App;

class Ad
{
    public function save()
    {
        return 1;
    }
}
"""

INVOICE = """<?php

namespace App;

class Invoice
{
    public function save()
    {
        return 2;
    }
}
"""

CALLER = """<?php

namespace App;

class Service
{
    public function run(Ad $ad)
    {
        return $ad->save();
    }
}
"""


@pytest.fixture
def store(tmp_path: Path):
    project = tmp_path / "project"
    (project / "app").mkdir(parents=True)
    (project / "app" / "Ad.php").write_bytes(AD.encode())
    (project / "app" / "Invoice.php").write_bytes(INVOICE.encode())
    (project / "app" / "Service.php").write_bytes(CALLER.encode())
    with IndexStore(tmp_path / "index.db") as opened:
        update_store(project, opened, use_git=False)
        yield opened


def grep(pattern: str, response: str = "") -> HookInput:
    return HookInput(
        tool_name="Grep", pattern=pattern, response=response, cwd=Path(".")
    )


class TestWhenItStaysSilent:
    def test_a_name_the_index_does_not_know(self, store: IndexStore) -> None:
        assert advise(store, grep("nosuchname")) == ""

    def test_a_unique_name_the_search_already_printed(self, store: IndexStore) -> None:
        # `run` is declared once and its uses, if any, are all in what the
        # search returned. There is nothing left to add, so add nothing.
        printed = "app/Service.php:8\napp/Ad.php:7\napp/Invoice.php:7\n"
        assert advise(store, grep("run", printed)) == ""

    def test_a_pattern_that_is_a_regex_not_a_name(self) -> None:
        assert HookInput.parse(
            {"tool_name": "Grep", "tool_input": {"pattern": r"save\(.*\)"}}
        ) is None

    def test_a_pattern_too_short_to_mean_anything(self) -> None:
        assert HookInput.parse(
            {"tool_name": "Grep", "tool_input": {"pattern": "id"}}
        ) is None

    def test_a_tool_that_is_not_a_search(self) -> None:
        assert HookInput.parse(
            {"tool_name": "Edit", "tool_input": {"pattern": "save"}}
        ) is None


class TestWhenItSpeaks:
    def test_it_separates_two_classes_sharing_a_method_name(
        self, store: IndexStore
    ) -> None:
        message = advise(store, grep("save", "app/Ad.php:7\napp/Invoice.php:7\n"))
        assert "Ad" in message and "Invoice" in message
        assert "names 2 indexed symbol(s)" in message

    def test_it_says_where_the_resolved_call_is(self, store: IndexStore) -> None:
        message = advise(store, grep("save", "app/Ad.php:7\napp/Invoice.php:7\n"))
        assert "app/Service.php:9" in message, message

    def test_it_never_lists_a_declaration_as_a_use(self, store: IndexStore) -> None:
        # `edges_to` carries the `contains` edge whose site is the
        # declaration itself. Printing that among the call sites would send
        # a reader back to the definition they already had. The declaration
        # still appears on the group's own header line, which is where it
        # belongs.
        message = advise(store, grep("save", "app/Ad.php:7\n"))
        used = [line for line in message.splitlines() if line.startswith("      ")]
        assert used, "the fixture has one real call site"
        assert not any("app/Ad.php:7" in line for line in used)

    def test_it_says_what_the_index_describes(self, store: IndexStore) -> None:
        # A hook never refreshes the index, so the reader has to be able to
        # see how far behind it might be.
        message = advise(store, grep("save"))
        assert "re-index if it is behind" in message or "index built from" in message


class TestTheProtocol:
    def payload(self, tmp_path: Path, pattern: str) -> str:
        return json.dumps(
            {
                "tool_name": "Grep",
                "tool_input": {"pattern": pattern},
                "tool_response": "",
                "cwd": str(tmp_path),
            }
        )

    def test_it_emits_post_tool_use_json(
        self, tmp_path: Path, store: IndexStore, monkeypatch
    ) -> None:
        # Named rather than copied: the store is in WAL mode, so copying
        # index.db alone leaves the recent writes behind in index.db-wal
        # and the hook would read an empty database.
        monkeypatch.setenv("REPOATLAS_STORE", str(tmp_path / "index.db"))
        raw = run_hook(self.payload(tmp_path, "save"))
        assert raw, "an ambiguous name should produce a message"
        decoded = json.loads(raw)
        assert decoded["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        assert "Invoice" in decoded["hookSpecificOutput"]["additionalContext"]

    def test_no_index_means_no_output(self, tmp_path: Path) -> None:
        assert run_hook(self.payload(tmp_path / "empty", "save")) == ""

    def test_malformed_input_is_survived(self) -> None:
        # A hook that raises takes the session's tool call with it.
        assert run_hook("not json at all") == ""
        assert run_hook("[]") == ""
        assert run_hook("") == ""

    def test_a_broken_store_is_survived(self, tmp_path: Path) -> None:
        home = tmp_path / "broken"
        (home / ".repoatlas").mkdir(parents=True)
        (home / ".repoatlas" / "index.db").write_bytes(b"this is not a database")
        assert run_hook(self.payload(home, "save")) == ""

    def test_an_explicit_store_wins(self, tmp_path: Path, monkeypatch) -> None:
        named = tmp_path / "somewhere.db"
        named.write_bytes(b"")
        monkeypatch.setenv("REPOATLAS_STORE", str(named))
        assert store_path(tmp_path / "elsewhere") == named

    def test_a_named_store_that_is_missing_is_not_guessed_around(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("REPOATLAS_STORE", str(tmp_path / "gone.db"))
        assert store_path(tmp_path) is None
