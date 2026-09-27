"""
The shared maths (analysis/stats.py). Track C, Phase C6.

Every number on the results page passes through these functions, so each
is checked against a value worked out by hand.
"""

from __future__ import annotations

import statistics

from analysis import stats


def test_percentile_matches_the_hand_calculation():
    data = [10, 20, 30, 40, 50]
    assert stats.percentile(data, 0) == 10
    assert stats.percentile(data, 50) == 30
    assert stats.percentile(data, 100) == 50
    # rank (5-1)*0.95 = 3.8 -> 40 + 0.8 * (50 - 40)
    assert abs(stats.percentile(data, 95) - 48.0) < 1e-9


def test_percentile_of_nothing_is_none_not_zero():
    assert stats.percentile([], 95) is None


def test_the_median_interval_is_reproducible_and_contains_the_median():
    data = [float(x) for x in range(1, 101)]
    a, b = stats.median_ci(data), stats.median_ci(data)
    assert a == b, "seeded: the same data must give the same interval"
    assert a[0] <= statistics.median(data) <= a[1]


def test_a_single_stalled_charger_is_an_outlier_not_a_long_whisker():
    data = [20.0] * 30 + [21.0] * 30 + [500.0]
    box = stats.box(data)
    assert box["outliers"] == [500.0]
    assert box["whisker_high"] == 21.0


def test_the_ecdf_ends_at_100_percent_and_is_thinned():
    data = list(range(1000))
    pts = stats.ecdf(data, max_points=50)
    assert len(pts) <= 50
    assert pts[0][0] == 0 and pts[-1] == [999, 100.0]


def test_time_to_share_counts_the_whole_fleet_not_just_the_recovered():
    # 10 chargers had to recover; only 9 did.
    times = [0.1 * i for i in range(1, 10)]
    assert stats.time_to_share(times, 10, 0.5) == times[4]
    assert abs(stats.time_to_share(times, 10, 0.9) - 0.9) < 1e-9
    assert stats.time_to_share(times, 10, 1.0) is None, "never reached is None, never a number"


def test_describe_reports_every_summary_field():
    d = stats.describe([1.0, 2.0, 3.0, 4.0])
    for key in ("n", "mean", "median", "median_ci95", "p95", "p99", "box", "ecdf"):
        assert key in d
    assert d["n"] == 4 and d["median"] == 2.5
    assert stats.describe([]) == {"n": 0}
