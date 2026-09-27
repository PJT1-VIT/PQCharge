"""
Shared maths for every experiment.

Track C (analysis). Phase C6.

--------------------------------------------------------------------
IN PLAIN WORDS — the numbers this file produces

    median     the middle value: half the chargers were faster, half slower.
    p95        the value 95 out of 100 chargers beat. Shows the slow tail
               that an average hides -- the chargers an operator gets
               complaints about.
    p99        the same, for 99 out of 100.
    95% CI     "confidence interval": the range the TRUE median very likely
               sits in. A narrow range means the result is solid; a wide one
               means run more chargers or repeat the run before claiming it.
    box        min / lower quarter / median / upper quarter / max -- the five
               numbers a box plot draws.
    ECDF       for every time t: what share of chargers finished within t.
               The recovery curve in E2 is one of these.

Standard library only (no numpy), so nobody needs a new install. The
percentile method is the common "linear interpolation" definition (the same
as numpy's default), and the confidence interval is a seeded bootstrap, so
the same data always produces the same numbers.
"""

from __future__ import annotations

import math
import random
import statistics
from typing import Any, Sequence

BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 20260927
ECDF_MAX_POINTS = 200


def percentile(values: Sequence[float], q: float) -> float | None:
    """q in [0, 100]. Linear interpolation between closest ranks."""
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    if len(data) == 1:
        return float(data[0])
    pos = (len(data) - 1) * (q / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(data[lo])
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def median_ci(values: Sequence[float], level: float = 0.95) -> tuple[float, float] | None:
    """
    Bootstrap confidence interval for the median: resample the data with
    replacement many times, take each resample's median, and read off the
    middle `level` share of those medians. Seeded, so it is reproducible.
    """
    data = [v for v in values if v is not None]
    if len(data) < 2:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(data)
    medians = sorted(
        statistics.median(rng.choices(data, k=n)) for _ in range(BOOTSTRAP_RESAMPLES)
    )
    tail = (1.0 - level) / 2.0 * 100.0
    return (percentile(medians, tail), percentile(medians, 100.0 - tail))


def box(values: Sequence[float]) -> dict[str, Any] | None:
    """
    Five numbers for a box plot, with Tukey whiskers (1.5 x the box height).
    Points beyond the whiskers are returned separately as outliers, so a
    single stalled charger is visible rather than stretching the whole box.
    """
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    q1, q2, q3 = percentile(data, 25), percentile(data, 50), percentile(data, 75)
    iqr = q3 - q1
    lo_fence, hi_fence = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    inside = [v for v in data if lo_fence <= v <= hi_fence] or data
    return {
        "whisker_low": inside[0],
        "q1": q1,
        "median": q2,
        "q3": q3,
        "whisker_high": inside[-1],
        "outliers": [v for v in data if v < lo_fence or v > hi_fence],
    }


def ecdf(values: Sequence[float], max_points: int = ECDF_MAX_POINTS) -> list[list[float]]:
    """
    [[value, share_percent], ...] ascending. Thinned to at most max_points so
    a 500-charger run still draws instantly; the first and last points are
    always kept so the curve starts and ends exactly.
    """
    data = sorted(v for v in values if v is not None)
    n = len(data)
    if n == 0:
        return []
    idx = list(range(n))
    if n > max_points:
        step = (n - 1) / (max_points - 1)
        idx = sorted({round(i * step) for i in range(max_points)})
    return [[data[i], 100.0 * (i + 1) / n] for i in idx]


def describe(values: Sequence[float]) -> dict[str, Any]:
    """The standard summary used by every distribution in the results."""
    data = [float(v) for v in values if v is not None]
    if not data:
        return {"n": 0}
    ci = median_ci(data)
    return {
        "n": len(data),
        "mean": statistics.fmean(data),
        "stdev": statistics.stdev(data) if len(data) > 1 else 0.0,
        "min": min(data),
        "median": statistics.median(data),
        "median_ci95": list(ci) if ci else None,
        "p90": percentile(data, 90),
        "p95": percentile(data, 95),
        "p99": percentile(data, 99),
        "max": max(data),
        "box": box(data),
        "ecdf": ecdf(data),
    }


def time_to_share(recovery_times: Sequence[float], population: int, share: float) -> float | None:
    """
    Seconds until `share` (0-1) of `population` had recovered, given each
    recovered charger's recovery time. None if that share never recovered --
    reported as "not reached", never as a number.
    """
    if population <= 0:
        return None
    needed = math.ceil(share * population)
    times = sorted(t for t in recovery_times if t is not None)
    if needed == 0:
        return 0.0
    if len(times) < needed:
        return None
    return times[needed - 1]
