"""Checks on the prose, not the code.

The documentation makes claims a reader will act on: a command to run, a
number to compare against, a table to read. These tests cover the two ways
that goes wrong without anyone noticing.

Markdown that renders as source. The comparison report is itself Markdown,
so pasting a sample of it into a fenced block shows a reader the pipes and
alignment markers instead of a table. It looks like a formatting mistake
because it is one, and nothing else in the suite would catch it.

Numbers that drift. A README quoting an accuracy the code no longer
achieves is worse than one quoting none, and this project's whole argument
is that its numbers are checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DOCUMENTS = sorted(
    path
    for path in [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]
    if path.exists()
)


def _fence_state(lines: list[str]):
    """Walk lines, reporting whether each is inside a fenced block."""
    fenced = False
    for number, line in enumerate(lines, 1):
        if line.strip().startswith("```"):
            fenced = not fenced
            yield number, line, True
            continue
        yield number, line, fenced


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
class TestMarkdown:
    def test_no_table_is_trapped_in_a_code_fence(self, document: Path) -> None:
        # A table inside a fence renders as raw pipes. The comparison report
        # is Markdown, so quoting its output verbatim is exactly how this
        # happens.
        lines = document.read_text(encoding="utf-8").splitlines()
        trapped = [
            number
            for number, line, fenced in _fence_state(lines)
            if fenced and line.lstrip().startswith("|") and not line.strip().startswith("```")
        ]
        assert not trapped, f"{document.name} lines {trapped}"

    def test_every_fence_is_closed(self, document: Path) -> None:
        lines = document.read_text(encoding="utf-8").splitlines()
        opens = sum(1 for line in lines if line.strip().startswith("```"))
        assert opens % 2 == 0, f"{document.name} has an unclosed code fence"

    def test_no_heading_is_trapped_in_a_code_fence(self, document: Path) -> None:
        # The same mistake in its other form: a `##` from quoted report
        # output would otherwise be invisible, which is harmless, but a
        # `##` outside one that was meant to be quoted breaks the outline.
        lines = document.read_text(encoding="utf-8").splitlines()
        trapped = [
            number
            for number, line, fenced in _fence_state(lines)
            if fenced and re.match(r"^#{1,3} \w", line)
        ]
        assert not trapped, f"{document.name} lines {trapped}"


class TestQuotedNumbers:
    """The README's headline figures have to be the ones the code produces."""

    @pytest.fixture(scope="module")
    @staticmethod
    def measured():
        pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")
        from repoatlas.eval.compare import compare_snapshots
        from repoatlas.oracle.scip import read_scip
        from repoatlas.parse.build import build_snapshot

        fixture = ROOT / "tests" / "fixtures" / "tsdemo"
        candidate = build_snapshot(fixture, use_git=False).snapshot
        return compare_snapshots(candidate, read_scip(fixture / "index.scip"))

    @pytest.fixture(scope="module")
    @staticmethod
    def readme() -> str:
        return (ROOT / "README.md").read_text(encoding="utf-8")

    def test_the_definition_row_matches(self, measured, readme: str) -> None:
        row = (
            f"| definitions | {measured.definitions.precision:.3f} "
            f"| {measured.definitions.recall:.3f} | {measured.definitions.f1:.3f} |"
        )
        assert row in readme, f"README should quote: {row}"

    def test_the_reference_row_matches(self, measured, readme: str) -> None:
        row = (
            f"| references | {measured.references.precision:.3f} "
            f"| {measured.references.recall:.3f} | {measured.references.f1:.3f} |"
        )
        assert row in readme, f"README should quote: {row}"

    def test_the_calibration_error_matches(self, measured, readme: str) -> None:
        assert measured.calibration is not None
        quoted = f"Expected calibration error {measured.calibration.expected_error:.3f}"
        assert quoted in readme, f"README should quote: {quoted}"

    def test_the_test_count_matches(self, readme: str, request) -> None:
        # This used to check only that *a* number was quoted, and the README
        # sat at 635 while the suite grew to 968 — a check that looked alive
        # and was not. The running session already knows its own collected
        # count, so ask it, and hold the README to it exactly, the way every
        # other number on this page is held.
        match = re.search(r"\((\d+) tests\)", readme)
        assert match, "the README should say how many tests there are"
        quoted = int(match.group(1))

        config = request.config
        selected = bool(config.option.keyword or config.option.markexpr)
        whole_suite = not selected and list(config.args) in ([], ["tests"])
        if not whole_suite:
            # A subset was asked for, so the session's count is not the
            # suite's. Fall back to the loose bound rather than fail a run
            # that never claimed to be complete.
            assert 100 <= quoted <= 100_000
            return
        assert quoted == len(request.session.items), (
            f"README says {quoted} tests; this run collected "
            f"{len(request.session.items)}"
        )
