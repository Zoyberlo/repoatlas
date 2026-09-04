"""Tests for the SQLite store and incremental updates.

One test here matters more than the rest: an incremental update must
produce exactly what a rebuild from scratch would. Everything else about
this stage is an optimisation, and an optimisation that quietly returns a
different answer is worse than no optimisation at all.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from repoatlas.model import (
    Edge,
    EdgeKind,
    ResolutionTier,
    SourceRange,
    Symbol,
    SymbolKind,
)
from repoatlas.parse.extract import Reference
from repoatlas.parse.imports import FileImports, ImportBinding, ImportStatement
from repoatlas.store import (
    SCHEMA_VERSION,
    FileRecord,
    IndexStore,
    content_digest,
    toolchain_version,
)

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

from repoatlas.store.incremental import (
    current_toolchain,
    detect_changes,
    update_store,
)


@pytest.fixture
def store(tmp_path: Path) -> IndexStore:
    with IndexStore(tmp_path / "index.db") as opened:
        yield opened


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "user.ts").write_text(
        "export class User {\n  greet(name: string): string {\n"
        "    return name;\n  }\n}\n\nexport const LIMIT = 10;\n",
        encoding="utf-8",
    )
    (root / "src" / "app.ts").write_text(
        'import { User, LIMIT } from "./user";\n\n'
        "export function run(): string {\n"
        "  return new User().greet(String(LIMIT));\n}\n",
        encoding="utf-8",
    )
    return root


def _fingerprint(store: IndexStore) -> tuple[list[str], list[tuple[str, ...]]]:
    """Everything the store claims, in a comparable form."""
    symbols = sorted(
        f"{s.id}|{s.kind.value}|{s.path}|{s.name_range}|{int(s.local)}"
        for s in store.symbols()
    )
    edges = sorted(
        (e.src_id, e.dst_id, e.kind.value, e.tier.label, e.site_path or "", str(e.site_range))
        for e in store.edges()
    )
    return symbols, edges


class TestSchema:
    def test_a_new_store_stamps_its_schema_version(self, store: IndexStore) -> None:
        assert store.schema_version == SCHEMA_VERSION

    def test_a_store_is_incompatible_until_a_toolchain_is_recorded(
        self, store: IndexStore
    ) -> None:
        assert not store.is_compatible("anything")

    def test_recording_a_toolchain_makes_it_compatible(self, store: IndexStore) -> None:
        with store.transaction():
            store.set_meta("toolchain", "abc")
        assert store.is_compatible("abc")
        assert not store.is_compatible("def")

    def test_reports_a_missing_fts5_clearly(self, tmp_path: Path, monkeypatch) -> None:
        # Some Python builds ship a SQLite without FTS5, and the failure is
        # otherwise a bare "no such module" from deep inside the schema.
        from repoatlas.store import StoreError, database

        monkeypatch.setattr(
            database,
            "SCHEMA",
            "CREATE VIRTUAL TABLE t USING fts5_that_does_not_exist(x);",
        )
        with pytest.raises(StoreError, match="FTS5"):
            IndexStore(tmp_path / "broken.db")


class TestRoundTrip:
    @staticmethod
    def _symbol(symbol_id: str = "a.py#f", **kwargs: object) -> Symbol:
        defaults: dict[str, object] = {
            "name": "f",
            "kind": SymbolKind.FUNCTION,
            "path": "a.py",
            "name_range": SourceRange.of(3, 4, 3, 5),
            "full_range": SourceRange.of(3, 0, 9, 0),
            "qualified_name": "C.f",
            "container_id": "a.py#C",
            "language": "python",
            "documentation": "does a thing",
        }
        defaults.update(kwargs)
        return Symbol(id=symbol_id, **defaults)  # type: ignore[arg-type]

    def _record(self) -> FileRecord:
        return FileRecord(
            path="a.py", language="python", digest="deadbeef", size=42, mtime_ns=1234
        )

    def test_a_symbol_survives_a_round_trip_whole(self, store: IndexStore) -> None:
        original = self._symbol()
        with store.transaction():
            store.put_file(self._record(), [original], [])
        assert store.symbol(original.id) == original

    def test_a_symbol_without_a_body_range_round_trips(self, store: IndexStore) -> None:
        original = self._symbol(full_range=None)
        with store.transaction():
            store.put_file(self._record(), [original], [])
        assert store.symbol(original.id) == original

    def test_local_and_synthetic_flags_survive(self, store: IndexStore) -> None:
        local = self._symbol("a.py#local", local=True)
        synthetic = self._symbol(
            "a.py#<module>",
            synthetic=True,
            container_id=None,
            qualified_name=None,
            name_range=SourceRange.of(0, 0, 0, 0),
            full_range=SourceRange.of(0, 0, 0, 0),
        )
        with store.transaction():
            store.put_file(self._record(), [local, synthetic], [])
        assert store.symbol("a.py#local").local
        assert store.symbol("a.py#<module>").synthetic

    def test_references_survive(self, store: IndexStore) -> None:
        reference = Reference("helper", "call", SourceRange.of(5, 4, 5, 10), "a.py#f")
        with store.transaction():
            store.put_file(self._record(), [], [reference])
        assert store.references() == [("a.py", reference)]

    def test_imports_and_their_bindings_survive(self, store: IndexStore) -> None:
        statement = ImportStatement(
            module="./user",
            bindings=(
                ImportBinding("User", None, SourceRange.of(0, 9, 0, 13)),
                ImportBinding("mk", "makeUser", SourceRange.of(0, 15, 0, 17)),
            ),
            span=SourceRange.of(0, 0, 0, 40),
            relative_level=2,
        )
        imports = FileImports(statements=[statement], namespace="App\\Http")
        with store.transaction():
            store.put_file(self._record(), [], [], imports)
        restored = store.imports()["a.py"]
        assert restored.namespace == "App\\Http"
        assert restored.statements == [statement]

    def test_edges_survive_with_their_tier_and_confidence(self, store: IndexStore) -> None:
        with store.transaction():
            store.put_file(self._record(), [self._symbol()], [])
            store.replace_edges(
                [
                    Edge(
                        src_id="a.py#f",
                        dst_id="b.py#g",
                        kind=EdgeKind.CALLS,
                        tier=ResolutionTier.SUFFIX,
                        site_path="a.py",
                        site_range=SourceRange.of(6, 8, 6, 9),
                    )
                ]
            )
        edge = store.edges()[0]
        assert edge.tier is ResolutionTier.SUFFIX
        assert edge.score == pytest.approx(0.55)
        assert edge.site_range == SourceRange.of(6, 8, 6, 9)

    def test_replacing_a_file_leaves_no_trace_of_the_old_one(
        self, store: IndexStore
    ) -> None:
        with store.transaction():
            store.put_file(self._record(), [self._symbol("a.py#old")], [])
        with store.transaction():
            store.put_file(self._record(), [self._symbol("a.py#new")], [])
        assert [s.id for s in store.symbols()] == ["a.py#new"]

    def test_removing_a_file_removes_everything_it_produced(
        self, store: IndexStore
    ) -> None:
        reference = Reference("x", "call", SourceRange.of(1, 0, 1, 1), None)
        with store.transaction():
            store.put_file(
                self._record(),
                [self._symbol()],
                [reference],
                FileImports(statements=[
                    ImportStatement("m", (ImportBinding("n", None, SourceRange.of(0, 0, 0, 1)),), SourceRange.of(0, 0, 0, 5))
                ]),
            )
        with store.transaction():
            store.remove_file("a.py")
        assert store.counts() == {
            "files": 0,
            "symbols": 0,
            "edges": 0,
            "refs": 0,
            "imports": 0,
        }

    def test_a_failed_transaction_leaves_the_store_untouched(
        self, store: IndexStore
    ) -> None:
        with store.transaction():
            store.put_file(self._record(), [self._symbol("a.py#keep")], [])
        with pytest.raises(RuntimeError, match="deliberate"), store.transaction():
            store.put_file(self._record(), [self._symbol("a.py#gone")], [])
            raise RuntimeError("deliberate")
        assert [s.id for s in store.symbols()] == ["a.py#keep"]


class TestSearch:
    @pytest.fixture
    def populated(self, store: IndexStore) -> IndexStore:
        record = FileRecord("a.py", "python", "d", 1, 1)
        symbols = [
            Symbol("a.py#User", "User", SymbolKind.CLASS, "a.py", SourceRange.of(0, 0, 0, 4)),
            Symbol(
                "a.py#UserRepositoryFactory",
                "UserRepositoryFactory",
                SymbolKind.CLASS,
                "a.py",
                SourceRange.of(4, 0, 4, 21),
            ),
            Symbol(
                "a.py#make_user",
                "make_user",
                SymbolKind.FUNCTION,
                "a.py",
                SourceRange.of(8, 0, 8, 9),
            ),
            Symbol(
                "a.py#hidden",
                "hidden_user",
                SymbolKind.VARIABLE,
                "a.py",
                SourceRange.of(12, 0, 12, 11),
                local=True,
            ),
            Symbol(
                "a.py#<module>",
                "a.py",
                SymbolKind.MODULE,
                "a.py",
                SourceRange.of(0, 0, 0, 0),
                synthetic=True,
            ),
        ]
        with store.transaction():
            store.put_file(record, symbols, [])
        return store

    def test_finds_a_substring_not_only_a_prefix(self, populated: IndexStore) -> None:
        names = {s.name for s in populated.search("Repository")}
        assert names == {"UserRepositoryFactory"}

    def test_an_exact_name_ranks_first(self, populated: IndexStore) -> None:
        assert populated.search("User")[0].name == "User"

    def test_a_shorter_name_outranks_a_longer_one(self, populated: IndexStore) -> None:
        names = [s.name for s in populated.search("User")]
        assert names.index("User") < names.index("UserRepositoryFactory")

    def test_search_is_case_insensitive(self, populated: IndexStore) -> None:
        assert {s.name for s in populated.search("user")} >= {"User", "make_user"}

    def test_local_and_synthetic_symbols_are_not_offered(
        self, populated: IndexStore
    ) -> None:
        names = {s.name for s in populated.search("user", limit=50)}
        assert "hidden_user" not in names
        assert "a.py" not in names

    def test_a_kind_filter_narrows_the_result(self, populated: IndexStore) -> None:
        names = {s.name for s in populated.search("user", kinds=("function",))}
        assert names == {"make_user"}

    def test_a_two_character_query_still_works(self, populated: IndexStore) -> None:
        # Too short for a trigram index, so this exercises the scan fallback.
        assert {s.name for s in populated.search("Us")} >= {"User"}

    def test_a_query_of_syntax_characters_finds_nothing_and_does_not_raise(
        self, populated: IndexStore
    ) -> None:
        assert populated.search('"OR" NEAR/3 x') == []

    def test_the_limit_is_honoured(self, populated: IndexStore) -> None:
        assert len(populated.search("user", limit=1)) == 1


class TestChangeDetection:
    def test_a_new_store_wants_every_file(self, project: Path, store: IndexStore) -> None:
        result = update_store(project, store)
        assert result.parsed == 2
        assert result.changes.was_empty
        assert result.changes.full_rebuild

    def test_a_second_run_parses_nothing(self, project: Path, store: IndexStore) -> None:
        update_store(project, store)
        second = update_store(project, store)
        assert second.parsed == 0
        assert len(second.changes.unchanged) == 2
        assert second.changes.stat_hits == 2

    def test_a_no_op_run_skips_resolution_entirely(
        self, project: Path, store: IndexStore
    ) -> None:
        update_store(project, store)
        second = update_store(project, store)
        assert second.resolve_seconds == 0.0
        assert second.changes.is_empty

    def test_an_edited_file_is_re_parsed(self, project: Path, store: IndexStore) -> None:
        update_store(project, store)
        target = project / "src" / "user.ts"
        target.write_text(
            target.read_text(encoding="utf-8") + "\nexport function extra() {}\n",
            encoding="utf-8",
        )
        second = update_store(project, store)
        assert second.parsed == 1
        assert [f.path for f in second.changes.modified] == ["src/user.ts"]
        assert any(s.name == "extra" for s in store.symbols())

    def test_a_new_file_is_picked_up(self, project: Path, store: IndexStore) -> None:
        update_store(project, store)
        (project / "src" / "extra.ts").write_text("export const X = 1;\n", encoding="utf-8")
        second = update_store(project, store)
        assert [f.path for f in second.changes.added] == ["src/extra.ts"]
        assert any(s.name == "X" for s in store.symbols())

    def test_a_deleted_file_takes_its_symbols_with_it(
        self, project: Path, store: IndexStore
    ) -> None:
        update_store(project, store)
        (project / "src" / "app.ts").unlink()
        second = update_store(project, store)
        assert second.changes.removed == ["src/app.ts"]
        assert not any(s.path == "src/app.ts" for s in store.symbols())
        assert not any(e.site_path == "src/app.ts" for e in store.edges())

    def test_a_touched_but_unchanged_file_is_seen_through_when_rehashing(
        self, project: Path, store: IndexStore
    ) -> None:
        update_store(project, store)
        target = project / "src" / "user.ts"
        # Same bytes, new mtime: the stat fast path would call it modified.
        os.utime(target, (time.time() + 10, time.time() + 10))
        result = update_store(project, store, trust_mtime=False)
        assert result.parsed == 0
        assert [f.path for f in result.changes.unchanged] == [
            "src/app.ts",
            "src/user.ts",
        ] or len(result.changes.unchanged) == 2

    def test_a_changed_toolchain_invalidates_everything(
        self, project: Path, store: IndexStore
    ) -> None:
        update_store(project, store)
        with store.transaction():
            store.set_meta("toolchain", "a query file changed")
        third = update_store(project, store)
        assert third.changes.full_rebuild
        assert third.parsed == 2

    def test_the_toolchain_stamp_moves_when_a_query_changes(self) -> None:
        first = toolchain_version([("python", "(x) @definition.function")])
        second = toolchain_version([("python", "(y) @definition.function")])
        assert first != second

    def test_detect_changes_alone_writes_nothing(
        self, project: Path, store: IndexStore
    ) -> None:
        from repoatlas.parse.walk import iter_source_files

        files = list(iter_source_files(project, use_git=False))
        changes = detect_changes(files, store, toolchain=current_toolchain())
        assert len(changes.added) == 2
        assert store.counts()["files"] == 0


class TestIncrementalEqualsRebuild:
    """The claim this whole stage rests on."""

    def test_after_an_edit_the_store_matches_a_rebuild(
        self, project: Path, tmp_path: Path
    ) -> None:
        incremental_path = tmp_path / "incremental.db"
        with IndexStore(incremental_path) as store:
            update_store(project, store)
            target = project / "src" / "user.ts"
            target.write_text(
                "export class User {\n  greet(name: string): string {\n"
                "    return name;\n  }\n  shout(): string {\n"
                "    return this.greet('x');\n  }\n}\n\n"
                "export const LIMIT = 20;\n",
                encoding="utf-8",
            )
            update_store(project, store)
            after_edit = _fingerprint(store)

        with IndexStore(tmp_path / "fresh.db") as fresh:
            update_store(project, fresh)
            rebuilt = _fingerprint(fresh)

        assert after_edit == rebuilt

    def test_after_a_deletion_the_store_matches_a_rebuild(
        self, project: Path, tmp_path: Path
    ) -> None:
        with IndexStore(tmp_path / "incremental.db") as store:
            update_store(project, store)
            (project / "src" / "app.ts").unlink()
            update_store(project, store)
            after_delete = _fingerprint(store)

        with IndexStore(tmp_path / "fresh.db") as fresh:
            update_store(project, fresh)
            assert after_delete == _fingerprint(fresh)

    def test_after_an_addition_the_store_matches_a_rebuild(
        self, project: Path, tmp_path: Path
    ) -> None:
        with IndexStore(tmp_path / "incremental.db") as store:
            update_store(project, store)
            (project / "src" / "extra.ts").write_text(
                'import { User } from "./user";\nexport function build(): User '
                "{\n  return new User();\n}\n",
                encoding="utf-8",
            )
            update_store(project, store)
            after_add = _fingerprint(store)

        with IndexStore(tmp_path / "fresh.db") as fresh:
            update_store(project, fresh)
            assert after_add == _fingerprint(fresh)

    def test_a_stale_edge_into_a_changed_file_is_not_left_behind(
        self, project: Path, tmp_path: Path
    ) -> None:
        # app.ts is untouched, but its edge into user.ts must move when the
        # symbol it points at does. This is what re-resolving everything,
        # rather than patching the changed file's edges, is there for.
        with IndexStore(tmp_path / "index.db") as store:
            update_store(project, store)
            before = {
                e.dst_id
                for e in store.edges()
                if e.site_path == "src/app.ts" and e.dst_id.endswith("#User")
            }
            assert before, "app.ts refers to User"
            (project / "src" / "user.ts").write_text(
                "\n\n\n\n\nexport class User {\n  greet(n: string): string "
                "{\n    return n;\n  }\n}\n\nexport const LIMIT = 10;\n",
                encoding="utf-8",
            )
            update_store(project, store)
            moved = store.symbol("src/user.ts#User")
            assert moved is not None
            assert moved.name_range.start.line == 5, "the class moved down the file"
            still = [
                e
                for e in store.edges()
                if e.site_path == "src/app.ts" and e.dst_id == "src/user.ts#User"
            ]
            assert still, "the edge from app.ts still resolves after the move"


class TestPersistence:
    def test_an_index_survives_being_closed_and_reopened(
        self, project: Path, tmp_path: Path
    ) -> None:
        path = tmp_path / "index.db"
        with IndexStore(path) as store:
            update_store(project, store)
            expected = _fingerprint(store)
        with IndexStore(path) as reopened:
            assert _fingerprint(reopened) == expected
            assert reopened.get_meta("producer", ) is not None

    def test_a_snapshot_from_the_store_can_be_compared(
        self, project: Path, store: IndexStore
    ) -> None:
        from repoatlas.eval.compare import compare_snapshots

        update_store(project, store)
        snapshot = store.snapshot()
        result = compare_snapshots(snapshot, snapshot)
        assert result.definitions.f1 == pytest.approx(1.0)
        assert result.references.f1 == pytest.approx(1.0)

    def test_the_digest_changes_with_the_content(self) -> None:
        assert content_digest(b"a") != content_digest(b"b")
        assert content_digest(b"a") == content_digest(b"a")


class TestStoreCli:
    def test_indexes_and_reports(self, project: Path, tmp_path: Path, capsys) -> None:
        from repoatlas.cli import main

        database = tmp_path / "i.db"
        assert main(["index", str(project), "--no-git", "--store", str(database)]) == 0
        out = capsys.readouterr().out
        assert "2 added" in out
        assert "stored:" in out

    def test_a_second_run_reports_nothing_changed(
        self, project: Path, tmp_path: Path, capsys
    ) -> None:
        from repoatlas.cli import main

        database = tmp_path / "i.db"
        main(["index", str(project), "--no-git", "--store", str(database)])
        capsys.readouterr()
        main(["index", str(project), "--no-git", "--store", str(database)])
        assert "0 added, 0 modified" in capsys.readouterr().out

    def test_json_output_carries_the_change_set(
        self, project: Path, tmp_path: Path, capsys
    ) -> None:
        from repoatlas.cli import main

        database = tmp_path / "i.db"
        main(
            [
                "index",
                str(project),
                "--no-git",
                "--store",
                str(database),
                "--format",
                "json",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["changes"]["added"] == 2
        assert payload["store"]["files"] == 2

    def test_search_finds_a_symbol(self, project: Path, tmp_path: Path, capsys) -> None:
        from repoatlas.cli import main

        database = tmp_path / "i.db"
        main(["index", str(project), "--no-git", "--store", str(database)])
        capsys.readouterr()
        assert main(["search", str(database), "greet"]) == 0
        assert "greet" in capsys.readouterr().out

    def test_search_says_so_when_nothing_matches(
        self, project: Path, tmp_path: Path, capsys
    ) -> None:
        from repoatlas.cli import main

        database = tmp_path / "i.db"
        main(["index", str(project), "--no-git", "--store", str(database)])
        capsys.readouterr()
        main(["search", str(database), "nothinglikethis"])
        assert "nothing matches" in capsys.readouterr().out

    def test_search_on_a_missing_index_says_so(self, tmp_path: Path) -> None:
        from repoatlas.cli import main

        with pytest.raises(SystemExit, match="no such index"):
            main(["search", str(tmp_path / "absent.db"), "x"])


def test_a_store_that_fails_to_open_does_not_leak_its_connection(
    tmp_path: Path, monkeypatch
) -> None:
    # A leaked handle holds a lock on the file, which on Windows stops the
    # caller from even deleting the half-made index and retrying.
    from repoatlas.store import StoreError, database

    monkeypatch.setattr(database, "SCHEMA", "CREATE VIRTUAL TABLE t USING nope(x);")
    target = tmp_path / "broken.db"
    with pytest.raises(StoreError):
        IndexStore(target)
    target.unlink()
    assert not target.exists()


class TestToolchainStamp:
    def test_the_stamp_changes_when_this_package_does(self) -> None:
        """A new release re-parses: the extractor decides what a file yields too.

        A store built before a signature was widened kept the old ones,
        because the stamp watched the queries and the queries had not
        moved.
        """
        import repoatlas
        from repoatlas.store.database import toolchain_version

        queries = [("python", "(module) @x")]
        before = toolchain_version(queries)
        original = repoatlas.__version__
        try:
            repoatlas.__version__ = original + "+next"
            import importlib

            import repoatlas.store.database as database

            importlib.reload(database)
            after = database.toolchain_version(queries)
        finally:
            repoatlas.__version__ = original
            import importlib

            import repoatlas.store.database as database

            importlib.reload(database)
        assert before != after

    def test_the_stamp_still_changes_when_a_query_does(self) -> None:
        from repoatlas.store.database import toolchain_version

        assert toolchain_version([("python", "(a) @x")]) != toolchain_version(
            [("python", "(b) @x")]
        )
