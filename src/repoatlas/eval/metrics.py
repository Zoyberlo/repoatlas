"""Scoring for index correctness.

Three things are measured, in increasing order of what they tell you:

precision / recall / F1
    How well predicted facts match the oracle. Reported per fact type and
    per edge kind, because a producer can be excellent at containment and
    poor at calls, and one blended number hides that.

confidence calibration
    An edge that claims 0.95 confidence should be right about 95% of the
    time. The calibration table checks that claim against the oracle. This
    is what turns the resolution cascade from a guess into a tuned ladder,
    and no published code-graph tool reports it.

bootstrap intervals
    Whether a difference between two runs is real. Files are the resampling
    unit, not individual facts, because facts within a file are strongly
    correlated: one badly parsed file produces many wrong edges at once.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from statistics import NormalDist

__all__ = [
    "CalibrationReport",
    "ConfidenceBin",
    "Interval",
    "Score",
    "bootstrap_interval",
    "calibrate",
    "expected_calibration_error",
    "precision_recall_curve",
    "score_counts",
    "score_from_paths",
]

_NORMAL = NormalDist()


@dataclass(frozen=True, slots=True)
class Score:
    """Precision, recall and F1 with the counts they came from."""

    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def predicted(self) -> int:
        return self.true_positives + self.false_positives

    @property
    def actual(self) -> int:
        return self.true_positives + self.false_negatives

    @property
    def precision(self) -> float:
        return self.true_positives / self.predicted if self.predicted else 0.0

    @property
    def recall(self) -> float:
        return self.true_positives / self.actual if self.actual else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def is_empty(self) -> bool:
        return self.predicted == 0 and self.actual == 0

    def __add__(self, other: Score) -> Score:
        return Score(
            self.true_positives + other.true_positives,
            self.false_positives + other.false_positives,
            self.false_negatives + other.false_negatives,
        )

    def as_dict(self) -> dict[str, float | int]:
        return {
            "tp": self.true_positives,
            "fp": self.false_positives,
            "fn": self.false_negatives,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }

    def __str__(self) -> str:
        return (
            f"P {self.precision:.3f} / R {self.recall:.3f} / F1 {self.f1:.3f} "
            f"(tp={self.true_positives} fp={self.false_positives} fn={self.false_negatives})"
        )


def score_counts(true_positives: int, false_positives: int, false_negatives: int) -> Score:
    if min(true_positives, false_positives, false_negatives) < 0:
        raise ValueError("counts must not be negative")
    return Score(true_positives, false_positives, false_negatives)


def score_from_paths(per_path: Mapping[str, tuple[int, int, int]]) -> Score:
    """Aggregate per-file ``(tp, fp, fn)`` counts into one score."""
    total = Score(0, 0, 0)
    for tp, fp, fn in per_path.values():
        total = total + Score(tp, fp, fn)
    return total


@dataclass(frozen=True, slots=True)
class Interval:
    """A point estimate with a confidence interval."""

    point: float
    low: float
    high: float
    level: float = 0.95
    method: str = "bca"
    resamples: int = 0

    @property
    def width(self) -> float:
        return self.high - self.low

    def excludes(self, value: float) -> bool:
        """True when ``value`` falls outside the interval."""
        return value < self.low or value > self.high

    def as_dict(self) -> dict[str, float | str | int]:
        return {
            "point": round(self.point, 4),
            "low": round(self.low, 4),
            "high": round(self.high, 4),
            "level": self.level,
            "method": self.method,
            "resamples": self.resamples,
        }

    def __str__(self) -> str:
        return f"{self.point:.3f} [{self.low:.3f}, {self.high:.3f}]"


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile of an already sorted sequence."""
    if not sorted_values:
        raise ValueError("no values")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[int(position)]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def bootstrap_interval(
    units: Sequence[tuple[int, int, int]],
    statistic: Callable[[Score], float] = lambda s: s.f1,
    *,
    level: float = 0.95,
    resamples: int = 2000,
    seed: int = 20260903,
) -> Interval:
    """Bias-corrected and accelerated bootstrap interval over per-file counts.

    Each unit is one file's ``(tp, fp, fn)``. Resampling whole files rather
    than individual facts respects the fact that errors cluster: a file whose
    grammar failed contributes a burst of wrong edges, and treating those as
    independent draws would make the interval far too narrow.

    Falls back to the percentile interval when the acceleration term is
    undefined, which happens when every file is identical or only one file
    has any facts at all.
    """
    if resamples < 1:
        raise ValueError("resamples must be positive")
    if not 0 < level < 1:
        raise ValueError("level must lie strictly between 0 and 1")
    units = list(units)
    if not units:
        return Interval(0.0, 0.0, 0.0, level, "empty", 0)

    observed = statistic(score_from_paths({str(i): u for i, u in enumerate(units)}))
    if len(units) == 1:
        return Interval(observed, observed, observed, level, "degenerate", 0)

    rng = random.Random(seed)
    size = len(units)
    replicates: list[float] = []
    for _ in range(resamples):
        # Sum three integers rather than building a Score per file: at
        # 50 000 files and 2 000 resamples the object churn alone cost
        # over a minute per interval.
        tp = fp = fn = 0
        for unit_tp, unit_fp, unit_fn in rng.choices(units, k=size):
            tp += unit_tp
            fp += unit_fp
            fn += unit_fn
        replicates.append(statistic(Score(tp, fp, fn)))
    replicates.sort()

    alpha = (1 - level) / 2
    below = sum(1 for value in replicates if value < observed)
    proportion = below / len(replicates)

    # Jackknife acceleration: leave one file out at a time.
    jackknife: list[float] = []
    grand = Score(0, 0, 0)
    for tp, fp, fn in units:
        grand = grand + Score(tp, fp, fn)
    for tp, fp, fn in units:
        reduced = Score(
            grand.true_positives - tp,
            grand.false_positives - fp,
            grand.false_negatives - fn,
        )
        jackknife.append(statistic(reduced))
    mean_jack = sum(jackknife) / len(jackknife)
    deviations = [mean_jack - value for value in jackknife]
    numerator = sum(d**3 for d in deviations)
    denominator = 6 * (sum(d * d for d in deviations) ** 1.5)

    usable = 0.0 < proportion < 1.0 and denominator != 0
    if not usable:
        return Interval(
            observed,
            _percentile(replicates, alpha),
            _percentile(replicates, 1 - alpha),
            level,
            "percentile",
            resamples,
        )

    bias = _NORMAL.inv_cdf(proportion)
    acceleration = numerator / denominator
    lower_z = _NORMAL.inv_cdf(alpha)
    upper_z = _NORMAL.inv_cdf(1 - alpha)

    def adjust(z: float) -> float:
        denom = 1 - acceleration * (bias + z)
        if denom == 0:
            return 0.5
        return _NORMAL.cdf(bias + (bias + z) / denom)

    low_fraction = min(max(adjust(lower_z), 0.0), 1.0)
    high_fraction = min(max(adjust(upper_z), 0.0), 1.0)
    if low_fraction > high_fraction:
        low_fraction, high_fraction = high_fraction, low_fraction
    return Interval(
        observed,
        _percentile(replicates, low_fraction),
        _percentile(replicates, high_fraction),
        level,
        "bca",
        resamples,
    )


@dataclass(frozen=True, slots=True)
class ConfidenceBin:
    """One row of the calibration table."""

    low: float
    high: float
    count: int
    correct: int
    mean_confidence: float

    @property
    def observed_precision(self) -> float:
        return self.correct / self.count if self.count else 0.0

    @property
    def gap(self) -> float:
        """Claimed confidence minus observed precision.

        Positive means the producer is overconfident, which is the failure
        mode that matters: it makes an agent trust an edge that is wrong.
        """
        return self.mean_confidence - self.observed_precision

    def as_dict(self) -> dict[str, float | int]:
        return {
            "low": round(self.low, 3),
            "high": round(self.high, 3),
            "count": self.count,
            "correct": self.correct,
            "mean_confidence": round(self.mean_confidence, 4),
            "observed_precision": round(self.observed_precision, 4),
            "gap": round(self.gap, 4),
        }


@dataclass(slots=True)
class CalibrationReport:
    """How well stated confidence predicts being right."""

    bins: list[ConfidenceBin] = field(default_factory=list)
    expected_error: float = 0.0
    max_error: float = 0.0
    total: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "expected_calibration_error": round(self.expected_error, 4),
            "max_calibration_error": round(self.max_error, 4),
            "bins": [b.as_dict() for b in self.bins],
        }

    def worst_bin(self, *, min_count: int = 1) -> ConfidenceBin | None:
        """The bin whose claim is furthest from what was observed.

        ``min_count`` leaves out bins too thin to judge: one edge in a
        bin claiming 0.55 is a gap of 0.45 whichever way it lands.
        """
        populated = [b for b in self.bins if b.count >= max(min_count, 1)]
        return max(populated, key=lambda b: abs(b.gap)) if populated else None

    def overconfident_bins(self, *, min_count: int = 5, tolerance: float = 0.10) -> list[ConfidenceBin]:
        """Bins that claim more than they deliver, by more than ``tolerance``.

        This is the direction that hurts an agent: a rung claiming 0.95
        that is right half the time will be trusted and be wrong. The
        other direction, a rung that undersells itself, costs at most a
        weaker rank, so it is not a failure here.
        """
        return [b for b in self.bins if b.count >= min_count and b.gap > tolerance]


def calibrate(
    outcomes: Iterable[tuple[float, bool]],
    *,
    edges: Sequence[float] | None = None,
) -> CalibrationReport:
    """Build a calibration table from ``(confidence, was_correct)`` pairs.

    Bin edges default to the resolution-tier confidences, so each row lines
    up with one rung of the cascade and a miscalibrated rung is immediately
    visible.
    """
    if edges is None:
        edges = (0.0, 0.45, 0.65, 0.80, 0.875, 0.925, 0.975, 1.0001)
    edge_list = sorted(edges)
    if len(edge_list) < 2:
        raise ValueError("need at least two bin edges")

    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(len(edge_list) - 1)]
    total = 0
    for confidence, was_correct in outcomes:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence out of range: {confidence}")
        total += 1
        for index in range(len(edge_list) - 1):
            upper = edge_list[index + 1]
            is_last = index == len(edge_list) - 2
            if confidence < upper or (is_last and confidence <= upper):
                buckets[index].append((confidence, was_correct))
                break

    report = CalibrationReport(total=total)
    weighted_error = 0.0
    for index, bucket in enumerate(buckets):
        if not bucket:
            report.bins.append(
                ConfidenceBin(edge_list[index], edge_list[index + 1], 0, 0, 0.0)
            )
            continue
        correct = sum(1 for _, ok in bucket if ok)
        mean_confidence = sum(c for c, _ in bucket) / len(bucket)
        bin_row = ConfidenceBin(
            low=edge_list[index],
            high=edge_list[index + 1],
            count=len(bucket),
            correct=correct,
            mean_confidence=mean_confidence,
        )
        report.bins.append(bin_row)
        weighted_error += len(bucket) * abs(bin_row.gap)
        report.max_error = max(report.max_error, abs(bin_row.gap))
    report.expected_error = weighted_error / total if total else 0.0
    return report


def expected_calibration_error(outcomes: Iterable[tuple[float, bool]]) -> float:
    """Shorthand for the expected calibration error alone."""
    return calibrate(outcomes).expected_error


def precision_recall_curve(
    outcomes: Sequence[tuple[float, bool]], total_oracle_facts: int
) -> list[tuple[float, Score]]:
    """Score the prediction set at every confidence threshold that changes it.

    ``total_oracle_facts`` is needed because false negatives include oracle
    facts the producer never predicted at any confidence, which no amount of
    threshold sweeping can recover.
    """
    if total_oracle_facts < 0:
        raise ValueError("oracle fact count must not be negative")
    ordered = sorted(outcomes, key=lambda pair: pair[0], reverse=True)
    thresholds = sorted({confidence for confidence, _ in ordered}, reverse=True)
    curve: list[tuple[float, Score]] = []
    true_positives = 0
    false_positives = 0
    index = 0
    for threshold in thresholds:
        while index < len(ordered) and ordered[index][0] >= threshold:
            if ordered[index][1]:
                true_positives += 1
            else:
                false_positives += 1
            index += 1
        curve.append(
            (
                threshold,
                Score(
                    true_positives,
                    false_positives,
                    max(0, total_oracle_facts - true_positives),
                ),
            )
        )
    return curve
