"""
MACHINE — was the laptop the bottleneck? (S1, the scale test)

Track C (analysis). Phase C-P6.

--------------------------------------------------------------------
IN PLAIN WORDS

With --watch-machine the load generator writes one `machine_sample` line per
second (harness/machine.py): whole-machine CPU and memory, the tester's and
the server's CPU and memory, and the tester's EVENT-LOOP LAG -- how late a
1-second timer fired inside the program that runs all the chargers.

If the laptop is saturated, every time measured in that run is partly the
laptop's fault, and must not be reported as the cost of the protocol or of
post-quantum cryptography. So this module:

    - summarises each reading (median, p95, max),
    - draws them over time (for the run's Machine card),
    - says "saturated" when either limit below is crossed; check.py turns
      that into the trust warning `machine_saturated`.

LIMITS (agreed by Track C, 2026-10-09):

    whole-machine CPU, p95      > 85 %
    tester event-loop lag, p95  > 50 ms

Tester and server CPU are in % of ONE core (psutil's convention), so they can
exceed 100 on a multi-core laptop; the whole-machine figure is 0-100.
"""

from __future__ import annotations

from typing import Any

from analysis import stats
from analysis.match import MatchedRun

CPU_P95_LIMIT_PCT = 85.0
LOOP_LAG_P95_LIMIT_MS = 50.0
MAX_POINTS = 600


def _summ(values: list[float]) -> dict[str, Any]:
    data = [float(v) for v in values if isinstance(v, (int, float))]
    if not data:
        return {"n": 0}
    return {
        "n": len(data),
        "median": stats.percentile(data, 50),
        "p95": stats.percentile(data, 95),
        "max": max(data),
    }


def _thin(points: list[list[float]]) -> list[list[float]]:
    if len(points) <= MAX_POINTS:
        return points
    step = (len(points) - 1) / (MAX_POINTS - 1)
    return [points[round(i * step)] for i in range(MAX_POINTS)]


def measure(run: MatchedRun) -> dict[str, Any] | None:
    """None when the run was recorded without --watch-machine."""
    rows = [r for r in run.harness.of_type("machine_sample") if r.get("_t") is not None]
    if not rows:
        return None
    start = run.harness.started_at or rows[0]["_t"]

    def series(get) -> list[list[float]]:
        pts = []
        for r in rows:
            v = get(r)
            if isinstance(v, (int, float)):
                pts.append([round(r["_t"] - start, 3), float(v)])
        return _thin(pts)

    def tester(r, key):
        return (r.get("tester") or {}).get(key)

    def server(r, key):
        return (r.get("server") or {}).get(key)

    cpu = [r.get("cpu_pct") for r in rows]
    lag = [r.get("loop_lag_ms") for r in rows]
    server_rows = [r for r in rows if r.get("server")]

    cpu_s, lag_s = _summ(cpu), _summ(lag)
    reasons = []
    if cpu_s.get("p95") is not None and cpu_s["p95"] > CPU_P95_LIMIT_PCT:
        reasons.append(f"whole-machine CPU p95 {cpu_s['p95']:.0f}% > {CPU_P95_LIMIT_PCT:.0f}%")
    if lag_s.get("p95") is not None and lag_s["p95"] > LOOP_LAG_P95_LIMIT_MS:
        reasons.append(f"tester event-loop lag p95 {lag_s['p95']:.0f} ms > "
                       f"{LOOP_LAG_P95_LIMIT_MS:.0f} ms")

    return {
        "samples": len(rows),
        "cpu_count": next((r.get("cpu_count") for r in rows if r.get("cpu_count")), None),
        "cpu_pct": cpu_s,
        "mem_pct": _summ([r.get("mem_pct") for r in rows]),
        "mem_used_mb_max": max((r.get("mem_used_mb") or 0.0) for r in rows),
        "loop_lag_ms": lag_s,
        "tester": {
            "cpu_pct": _summ([tester(r, "cpu_pct") for r in rows]),
            "rss_mb_max": max((tester(r, "rss_mb") or 0.0) for r in rows),
            "threads_max": max((tester(r, "threads") or 0) for r in rows),
        },
        "server": {
            "found": bool(server_rows),
            "samples": len(server_rows),
            "cpu_pct": _summ([server(r, "cpu_pct") for r in server_rows]),
            "rss_mb_max": max((server(r, "rss_mb") or 0.0) for r in server_rows) if server_rows else None,
            "threads_max": max((server(r, "threads") or 0) for r in server_rows) if server_rows else None,
        },
        "timeline": {
            "cpu_pct": series(lambda r: r.get("cpu_pct")),
            "loop_lag_ms": series(lambda r: r.get("loop_lag_ms")),
            "tester_cpu_pct": series(lambda r: tester(r, "cpu_pct")),
            "server_cpu_pct": series(lambda r: server(r, "cpu_pct")),
            "tester_rss_mb": series(lambda r: tester(r, "rss_mb")),
        },
        "limits": {"cpu_p95_pct": CPU_P95_LIMIT_PCT, "loop_lag_p95_ms": LOOP_LAG_P95_LIMIT_MS},
        "saturated": bool(reasons),
        "saturation_reasons": reasons,
    }
