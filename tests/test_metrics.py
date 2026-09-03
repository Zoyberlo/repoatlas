"""Tests for scoring, calibration and bootstrap intervals.

These are the numbers the whole project will be judged by, so each is
checked against a hand-computed value rather than against itself.
"""

from __future__ import annotations

import pytest

from repoatlas.eval.metrics import (
    Score,
    bootstrap_interval,
    calibrate,
    expected_calibration_error,
    precision_recall_curve,
    score_counts,
    score_from_paths,
)


class TestScore:
    def test_computes_precision_recall_and_f1(self) -> None:
        # 8 of 10 predictions right, 8 of 12 oracle facts found.
        score = Score(true_positives=8, false_positives=2, false_negatives=4)
        assert score.precision == pytest.approx(0.8)
        assert score.recall == pytest.approx(8 / 12)
        assert score.f1 == pytest.approx(2 * 0.8 * (8 / 12) / (0.8 + 8 / 12))

    def test_a_perfect_score_is_one(self) -> None:
        assert Score(5, 0, 0).f1 == pytest.approx(1.0)

    def test_predicting_nothing_scores_zero_rather_than_dividing_by_zero(self) -> None:
        score = Score(0, 0, 7)
        assert score.precision == 0.0
        assert score.recall == 0.0
        assert score.f1 == 0.0

    def test_an_empty_comparison_is_flagged_not_scored(self) -> None:
        assert Score(0, 0, 0).is_empty
        assert not Score(1, 0, 0).is_empty

    def test_scores_add_componentwise(self) -> None:
        total = Score(1, 2, 3) + Score(10, 20, 30)
        assert (total.true_positives, total.false_positives, total.false_negatives) == (
            11,
            22,
            33,
        )

    def test_aggregates_per_file_counts(self) -> None:
        total = score_from_paths({"a.py": (3, 1, 2), "b.py": (5, 0, 1)})
        assert total.true_positives == 8
        assert total.false_positives == 1
        assert total.false_negatives == 3

    def test_rejects_negative_counts(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            score_counts(1, -1, 0)


class TestBootstrapInterval:
    def test_is_deterministic_for_a_fixed_seed(self) -> None:
        units = [(8, 2, 1), (5, 5, 5), (10, 0, 0), (0, 3, 4), (6, 1, 2)]
        first = bootstrap_interval(units, resamples=500, seed=7)
        second = bootstrap_interval(units, resamples=500, seed=7)
        assert (first.low, first.point, first.high) == (
            second.low,
            second.point,
            second.high,
        )

    def test_brackets_the_observed_value(self) -> None:
        units = [(8, 2, 1), (5, 5, 5), (10, 0, 0), (0, 3, 4), (6, 1, 2)]
        interval = bootstrap_interval(units, resamples=500, seed=7)
        assert interval.low <= interval.point <= interval.high

    def test_point_estimate_matches_the_pooled_score(self) -> None:
        units = [(8, 2, 1), (5, 5, 5)]
        interval = bootstrap_interval(units, resamples=200, seed=1)
        assert interval.point == pytest.approx(Score(13, 7, 6).f1)

    def test_a_perfect_index_gets_a_degenerate_interval(self) -> None:
        units = [(4, 0, 0), (9, 0, 0), (2, 0, 0)]
        interval = bootstrap_interval(units, resamples=200, seed=3)
        assert interval.point == pytest.approx(1.0)
        assert interval.low == pytest.approx(1.0)
        assert interval.method == "percentile"

    def test_more_files_give_a_narrower_interval(self) -> None:
        pattern = [(8, 2, 1), (5, 5, 5), (10, 0, 0), (0, 3, 4)]
        narrow = bootstrap_interval(pattern * 10, resamples=400, seed=11)
        wide = bootstrap_interval(pattern, resamples=400, seed=11)
        assert narrow.width < wide.width

    def test_a_single_file_cannot_be_resampled(self) -> None:
        interval = bootstrap_interval([(5, 1, 1)], resamples=100, seed=1)
        assert interval.method == "degenerate"
        assert interval.width == 0.0

    def test_no_files_yields_an_empty_interval(self) -> None:
        assert bootstrap_interval([]).method == "empty"

    def test_can_score_a_different_statistic(self) -> None:
        units = [(8, 2, 1), (5, 5, 5)]
        interval = bootstrap_interval(
            units, statistic=lambda s: s.recall, resamples=200, seed=5
        )
        assert interval.point == pytest.approx(Score(13, 7, 6).recall)

    def test_excludes_reports_whether_a_value_is_outside(self) -> None:
        units = [(10, 0, 0), (9, 1, 0), (10, 0, 1)]
        interval = bootstrap_interval(units, resamples=300, seed=2)
        assert interval.excludes(0.2)
        assert not interval.excludes(interval.point)

    @pytest.mark.parametrize("resamples", [0, -5])
    def test_rejects_a_nonsense_resample_count(self, resamples: int) -> None:
        with pytest.raises(ValueError, match="resamples must be positive"):
            bootstrap_interval([(1, 1, 1), (2, 2, 2)], resamples=resamples)

    @pytest.mark.parametrize("level", [0.0, 1.0, 1.5])
    def test_rejects_a_nonsense_confidence_level(self, level: float) -> None:
        with pytest.raises(ValueError, match="strictly between"):
            bootstrap_interval([(1, 1, 1), (2, 2, 2)], level=level)


class TestCalibration:
    def test_a_perfectly_calibrated_producer_has_no_error(self) -> None:
        # Ten edges claiming 0.9, of which exactly nine are right.
        outcomes = [(0.9, True)] * 9 + [(0.9, False)]
        report = calibrate(outcomes)
        assert report.expected_error == pytest.approx(0.0, abs=1e-9)

    def test_detects_overconfidence(self) -> None:
        # Claims 0.95, right only half the time.
        outcomes = [(0.95, True)] * 5 + [(0.95, False)] * 5
        report = calibrate(outcomes)
        worst = report.worst_bin()
        assert worst is not None
        assert worst.gap == pytest.approx(0.45)
        assert report.expected_error == pytest.approx(0.45)

    def test_detects_underconfidence_as_a_negative_gap(self) -> None:
        outcomes = [(0.35, True)] * 10
        worst = calibrate(outcomes).worst_bin()
        assert worst is not None
        assert worst.gap == pytest.approx(-0.65)

    def test_separates_the_cascade_rungs(self) -> None:
        outcomes = [(0.95, True)] * 4 + [(0.35, False)] * 4
        populated = [b for b in calibrate(outcomes).bins if b.count]
        assert len(populated) == 2
        assert {b.count for b in populated} == {4}

    def test_weights_bins_by_population(self) -> None:
        # One badly calibrated edge among ninety-nine good ones barely moves
        # the expected error, but the max error still shows it.
        outcomes = [(0.9, True)] * 89 + [(0.9, False)] * 10 + [(0.35, True)]
        report = calibrate(outcomes)
        assert report.expected_error < 0.02
        assert report.max_error > 0.6

    def test_counts_every_outcome_exactly_once(self) -> None:
        outcomes = [(0.0, True), (0.5, False), (1.0, True), (0.75, True)]
        report = calibrate(outcomes)
        assert report.total == 4
        assert sum(b.count for b in report.bins) == 4

    def test_places_the_boundary_value_one_in_the_top_bin(self) -> None:
        report = calibrate([(1.0, True)])
        top = report.bins[-1]
        assert top.count == 1

    def test_rejects_a_confidence_outside_the_unit_interval(self) -> None:
        with pytest.raises(ValueError, match="confidence out of range"):
            calibrate([(1.2, True)])

    def test_accepts_custom_bin_edges(self) -> None:
        report = calibrate([(0.2, True), (0.8, False)], edges=(0.0, 0.5, 1.0001))
        assert len(report.bins) == 2

    def test_rejects_a_single_bin_edge(self) -> None:
        with pytest.raises(ValueError, match="at least two bin edges"):
            calibrate([(0.5, True)], edges=(0.5,))

    def test_shorthand_matches_the_full_report(self) -> None:
        outcomes = [(0.95, True)] * 5 + [(0.95, False)] * 5
        assert expected_calibration_error(outcomes) == pytest.approx(
            calibrate(outcomes).expected_error
        )


class TestPrecisionRecallCurve:
    def test_raising_the_threshold_trades_recall_for_precision(self) -> None:
        outcomes = [(0.95, True), (0.9, True), (0.5, False), (0.35, False)]
        curve = precision_recall_curve(outcomes, total_oracle_facts=4)
        precisions = [score.precision for _, score in curve]
        recalls = [score.recall for _, score in curve]
        assert precisions[0] >= precisions[-1]
        assert recalls[0] <= recalls[-1]

    def test_the_top_threshold_keeps_only_the_most_confident_edges(self) -> None:
        outcomes = [(0.95, True), (0.5, False)]
        threshold, score = precision_recall_curve(outcomes, total_oracle_facts=1)[0]
        assert threshold == pytest.approx(0.95)
        assert score.true_positives == 1
        assert score.false_positives == 0

    def test_counts_unreachable_oracle_facts_as_false_negatives(self) -> None:
        # Two oracle facts exist but only one was ever predicted.
        outcomes = [(0.9, True)]
        _, score = precision_recall_curve(outcomes, total_oracle_facts=2)[0]
        assert score.false_negatives == 1
        assert score.recall == pytest.approx(0.5)

    def test_produces_one_row_per_distinct_confidence(self) -> None:
        outcomes = [(0.9, True), (0.9, False), (0.5, True)]
        assert len(precision_recall_curve(outcomes, total_oracle_facts=3)) == 2

    def test_rejects_a_negative_oracle_count(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            precision_recall_curve([(0.5, True)], total_oracle_facts=-1)
