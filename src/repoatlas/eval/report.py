"""Rendering of a :class:`~repoatlas.eval.compare.Comparison`.

Two renderings: JSON for regression tracking in CI, and Markdown for a human
deciding whether a change to the extractor helped. The Markdown form leads
with recall on resolved edges, because that is the number a heuristic
resolver loses on, and buries nothing behind an average.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from .compare import Comparison
from .metrics import Score

__all__ = ["format_score_table", "to_json", "to_markdown"]


def to_json(comparison: Comparison, *, indent: int = 2) -> str:
    return json.dumps(comparison.as_dict(), indent=indent, sort_keys=False)


def format_score_table(rows: Iterable[tuple[str, Score]]) -> list[str]:
    lines = [
        "| kind | precision | recall | F1 | tp | fp | fn |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, score in rows:
        lines.append(
            f"| {label} | {score.precision:.3f} | {score.recall:.3f} | {score.f1:.3f} "
            f"| {score.true_positives} | {score.false_positives} | {score.false_negatives} |"
        )
    return lines


def to_markdown(comparison: Comparison, *, samples: int = 5) -> str:
    """Render a comparison as a Markdown report."""
    candidate = comparison.candidate_producer or "candidate"
    oracle = comparison.oracle_producer or "oracle"
    lines: list[str] = [
        f"# Index accuracy: {candidate} vs {oracle}",
        "",
        f"Compared {comparison.compared_paths} files.",
    ]
    if comparison.skipped_paths:
        lines.append(
            f"Skipped {comparison.skipped_paths} files the oracle does not cover."
        )
    if comparison.encoding_assumed:
        lines.append(
            "At least one index does not declare how it counts columns; UTF-8 "
            "was assumed and column offsets were matched with tolerance."
        )
    if comparison.encoding_mismatch:
        lines.append(
            "Position encodings differ between the two indexes, so column offsets "
            "were matched with tolerance."
        )
    if comparison.symbol_kind_scope is not None:
        kinds = ", ".join(sorted(kind.value for kind in comparison.symbol_kind_scope))
        lines.append("")
        lines.append(f"Symbol kinds in scope: {kinds}.")
    if comparison.unscored_edge_kinds:
        lines.append(
            f"Not scored, because the oracle emits no such edge: "
            f"{comparison.unscored_edges} edges of kind "
            f"{', '.join(comparison.unscored_edge_kinds)}."
        )
    lines.append("")

    lines.append("## Headline")
    lines.extend(
        format_score_table(
            [("definitions", comparison.definitions), ("references", comparison.references)]
        )
    )
    lines.append("")
    if comparison.definition_interval is not None:
        lines.append(
            f"Definition F1 {comparison.definition_interval} "
            f"({comparison.definition_interval.method}, "
            f"{comparison.definition_interval.resamples} resamples over files)."
        )
    if comparison.reference_interval is not None:
        lines.append(
            f"Reference F1 {comparison.reference_interval} "
            f"({comparison.reference_interval.method}, "
            f"{comparison.reference_interval.resamples} resamples over files)."
        )
    lines.append("")

    if comparison.references_by_kind:
        lines.append("## By edge kind")
        lines.extend(
            format_score_table(sorted(comparison.references_by_kind.items()))
        )
        lines.append("")

    lines.append("## Honesty checks")
    lines.append("")
    lines.append(
        f"- Dangling edges, pointing at symbols the index never defined: "
        f"{len(comparison.dangling_edges)} ({comparison.dangling_rate:.1%})"
    )
    lines.append(
        f"- Edges with no evidence site, scored against the declaring symbol: "
        f"{len(comparison.unsited_edges)}"
    )
    lines.append(
        f"- Matches that needed column tolerance: "
        f"{comparison.tolerant_definition_matches} definitions, "
        f"{comparison.tolerant_reference_matches} references"
    )
    lines.append("")

    calibration = comparison.calibration
    if calibration is not None and calibration.total:
        lines.append("## Confidence calibration")
        lines.append("")
        lines.append(
            f"Expected calibration error {calibration.expected_error:.3f}, "
            f"worst bin {calibration.max_error:.3f}, over {calibration.total} edges."
        )
        lines.append("")
        lines.append("| confidence bin | edges | claimed | observed | gap |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for row in calibration.bins:
            if not row.count:
                continue
            lines.append(
                f"| {row.low:.2f} to {row.high:.2f} | {row.count} | "
                f"{row.mean_confidence:.3f} | {row.observed_precision:.3f} | "
                f"{row.gap:+.3f} |"
            )
        worst = calibration.worst_bin()
        if worst is not None and worst.gap > 0.05:
            lines.append("")
            lines.append(
                f"The {worst.low:.2f} to {worst.high:.2f} rung is overconfident by "
                f"{worst.gap:.3f}. Lower its tier confidence before shipping."
            )
        lines.append("")

    false_positives = comparison.sample_false_positives(samples)
    false_negatives = comparison.sample_false_negatives(samples)
    if false_positives or false_negatives:
        lines.append("## Examples")
        lines.append("")
        if false_positives:
            lines.append("Claimed but absent from the oracle:")
            lines.append("")
            lines.extend(f"- `{item}`" for item in false_positives)
            lines.append("")
        if false_negatives:
            lines.append("In the oracle but missed:")
            lines.append("")
            lines.extend(f"- `{item}`" for item in false_negatives)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
