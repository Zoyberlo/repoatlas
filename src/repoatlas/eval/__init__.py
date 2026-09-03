"""Evaluation harness: does the index describe the code correctly?

Tier one of the plan in ``docs/evaluation.md``. Later tiers (retrieval
metrics on localisation benchmarks, agent-level A/B runs) build on the same
fact projection, so the vocabulary here is shared.
"""

from __future__ import annotations

from .compare import Comparison, ComparisonOptions, compare_snapshots
from .facts import (
    DefFact,
    FactSet,
    MatchResult,
    RefFact,
    definition_facts,
    match_facts,
    normalise_path,
    reference_facts,
)
from .metrics import (
    CalibrationReport,
    ConfidenceBin,
    Interval,
    Score,
    bootstrap_interval,
    calibrate,
    precision_recall_curve,
    score_counts,
    score_from_paths,
)
from .report import to_json, to_markdown

__all__ = [
    "CalibrationReport",
    "Comparison",
    "ComparisonOptions",
    "ConfidenceBin",
    "DefFact",
    "FactSet",
    "Interval",
    "MatchResult",
    "RefFact",
    "Score",
    "bootstrap_interval",
    "calibrate",
    "compare_snapshots",
    "definition_facts",
    "match_facts",
    "normalise_path",
    "precision_recall_curve",
    "reference_facts",
    "score_counts",
    "score_from_paths",
    "to_json",
    "to_markdown",
]
