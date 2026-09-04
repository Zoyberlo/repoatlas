"""The call-site task class: selection, prompt and scoring."""

from __future__ import annotations

from pathlib import Path

from repoatlas.callsites import (
    CallSiteTask,
    SiteBenchResult,
    SiteRun,
    collision_rate,
    score_sites,
    select_tasks,
    site_prompt,
)
from repoatlas.model import (
    Edge,
    EdgeKind,
    IndexSnapshot,
    ResolutionTier,
    SourceRange,
    Symbol,
    SymbolKind,
)


def _oracle(tmp_path: Path) -> IndexSnapshot:
    """Two classes with a `client` method each, one used three times."""
    (tmp_path / "a.php").write_text(
        "<?php\nclass A { public function client() {} }\n", encoding="utf-8"
    )
    (tmp_path / "b.php").write_text(
        "<?php\nclass B { public function client() {} }\n", encoding="utf-8"
    )
    (tmp_path / "use.php").write_text(
        "<?php\n$a->client();\n$a->client();\n$b->client();\n$a->client();\n", encoding="utf-8"
    )
    snapshot = IndexSnapshot()
    for path, name, line in (("a.php", "A", 1), ("b.php", "B", 1)):
        snapshot.add_symbol(
            Symbol(id=f"{path}#{name}", name=name, kind=SymbolKind.CLASS, path=path,
                   name_range=SourceRange.of(line, 6, line, 7))
        )
        snapshot.add_symbol(
            Symbol(id=f"{path}#{name}.client", name="client", kind=SymbolKind.METHOD, path=path,
                   container_id=f"{path}#{name}", name_range=SourceRange.of(line, 27, line, 33))
        )
    for line in (1, 2, 4):
        snapshot.add_edge(
            Edge(src_id="use.php#<module>", dst_id="a.php#A.client", kind=EdgeKind.CALLS,
                 tier=ResolutionTier.ORACLE, site_path="use.php",
                 site_range=SourceRange.of(line, 4, line, 10))
        )
    snapshot.add_symbol(
        Symbol(id="use.php#<module>", name="use.php", kind=SymbolKind.MODULE, path="use.php",
               name_range=SourceRange.of(0, 0, 0, 0), synthetic=True)
    )
    return snapshot


class TestSelection:
    def test_a_symbol_with_enough_sites_and_a_shared_name_is_chosen(self, tmp_path: Path) -> None:
        tasks = select_tasks(_oracle(tmp_path), tmp_path, min_sites=3, limit=10)
        assert [t.symbol_id for t in tasks] == ["a.php#A.client"]
        task = tasks[0]
        assert task.sites == frozenset({("use.php", 2), ("use.php", 3), ("use.php", 5)})
        # Two symbols are called `client`, and grep for it hits every line.
        assert task.collisions == 2
        assert task.grep_lines == 6

    def test_a_symbol_with_too_few_sites_is_not_asked_about(self, tmp_path: Path) -> None:
        assert select_tasks(_oracle(tmp_path), tmp_path, min_sites=4, limit=10) == []

    def test_collisions_count_only_navigable_symbols(self, tmp_path: Path) -> None:
        counts = collision_rate(_oracle(tmp_path))
        assert counts["client"] == 2
        assert counts["A"] == 1


class TestPrompt:
    def test_the_question_names_the_declaration_and_warns_about_namesakes(self) -> None:
        task = CallSiteTask("a.php#A.client", "client", "a.php", 2, "method",
                            frozenset({("use.php", 2)}), 2, 6)
        text = site_prompt(task, "Use Grep.")
        assert "`client`" in text and "a.php:2" in text
        assert "same name do not count" in text
        assert "JSON array" in text and "Do not edit" in text


class TestScoring:
    TASK = CallSiteTask("x", "client", "a.php", 2, "method",
                        frozenset({("use.php", 2), ("use.php", 3), ("use.php", 5)}), 2, 6)

    def test_a_perfect_answer(self) -> None:
        answer = [("use.php", 2), ("use.php", 3), ("use.php", 5)]
        assert score_sites(answer, self.TASK) == (1.0, 1.0, 1.0)

    def test_a_line_either_side_still_counts(self) -> None:
        precision, recall, _ = score_sites([("use.php", 4)], self.TASK)
        assert (precision, recall) == (1.0, 1 / 3)

    def test_a_wrong_file_is_a_false_positive(self) -> None:
        precision, recall, _ = score_sites([("use.php", 2), ("other.php", 9)], self.TASK)
        assert (precision, recall) == (0.5, 1 / 3)

    def test_naming_the_same_site_twice_is_naming_it_once(self) -> None:
        precision, recall, _ = score_sites([("use.php", 2), ("use.php", 2)], self.TASK)
        assert (precision, recall) == (1.0, 1 / 3)

    def test_padding_with_wrong_sites_costs_precision(self) -> None:
        answer = [("use.php", 2), ("z.php", 1), ("z.php", 2), ("z.php", 3)]
        precision, recall, _ = score_sites(answer, self.TASK)
        assert (precision, recall) == (0.25, 1 / 3)

    def test_an_answer_with_no_lines_scores_nothing(self) -> None:
        assert score_sites([("use.php", None)], self.TASK) == (0.0, 0.0, 0.0)


class TestResult:
    def test_paired_deltas_and_the_table(self) -> None:
        result = SiteBenchResult(arms=("grep", "repoatlas"), tasks=2)
        result.runs += [
            SiteRun("s1", "client", "grep", 1, True, precision=0.5, recall=0.4, f1=0.44, tokens=30000),
            SiteRun("s1", "client", "repoatlas", 1, True, precision=1.0, recall=0.9, f1=0.95, tokens=12000, mcp_calls=3),
            SiteRun("s2", "user", "grep", 1, True, precision=0.3, recall=0.9, f1=0.45),
            SiteRun("s2", "user", "repoatlas", 1, False, reason="timeout"),
        ]
        assert result.paired("f1") == [0.51]
        payload = result.as_dict()
        assert payload["delta_f1"]["wins"] == 1
        assert payload["per_arm"]["repoatlas"]["mcp_calls"] == 3.0
        text = result.as_text()
        assert "repoatlas minus grep, f1: +0.510" in text
        assert "excluded user repoatlas: timeout" in text
