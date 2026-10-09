"""
COMPARE — put runs side by side: classical vs hybrid vs post-quantum, small vs large fleet.

Track C (analysis). Phase C6.

In plain words: one run tells you what happened; the project's claims come
from COMPARING runs. This builds the cross-run series the results page
draws as "cost against fleet size, one line per security mode":

    E1  median and p95 connection time against number of chargers
    E2  time for 95% of the fleet to recover against number of chargers
    overhead  for the same experiment, size and TLS setting: how many times
              slower is hybrid / post-quantum than classical?

Runs are only compared when they share the TLS setting -- a plain-text run
and an encrypted run differ for reasons that have nothing to do with the
security MODE, and mixing them would put that difference on the wrong axis.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _series_name(slot: dict[str, Any]) -> str:
    """One line per experiment + mode + TLS setting: only like is joined to like."""
    return (f"{slot['experiment']} · {slot['crypto_mode']}"
            f"{' · TLS' if slot['tls'] else ' · no TLS'}")


def measure(slots: list[dict[str, Any]]) -> dict[str, Any]:
    e1: dict[str, list[dict[str, Any]]] = defaultdict(list)
    e2: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for s in slots:
        name = _series_name(s)
        d = (s.get("e1") or {}).get("station_connect_ms") or {}
        if d.get("n"):
            ci = d.get("median_ci95") or [None, None]
            e1[name].append({
                "experiment": s["experiment"],
                "n": s["n_stations"], "median": d["median"], "p95": d["p95"],
                "ci_low": ci[0], "ci_high": ci[1], "slot": s["key"],
                "mode": s["crypto_mode"], "tls": s["tls"],
            })
        storm = s.get("e2")
        if storm:
            e2[name].append({
                "experiment": s["experiment"],
                "n": s["n_stations"], "t50": storm.get("t50_s"), "t95": storm.get("t95_s"),
                "t100": storm.get("t100_s"), "unrecovered": storm.get("unrecovered"),
                "slot": s["key"], "mode": s["crypto_mode"], "tls": s["tls"],
            })

    for table in (e1, e2):
        for rows in table.values():
            rows.sort(key=lambda r: r["n"])

    # -- overhead against classical, like for like -------------------------
    groups: dict[tuple, dict[str, dict[str, Any]]] = defaultdict(dict)
    for s in slots:
        d = (s.get("e1") or {}).get("station_connect_ms") or {}
        if d.get("n"):
            groups[(s["experiment"], s["n_stations"], s["tls"])][s["crypto_mode"]] = d
    overhead = []
    for (exp, n, tls), by_mode in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        base = by_mode.get("classical")
        if not base:
            continue
        for mode, d in by_mode.items():
            if mode == "classical":
                continue
            overhead.append({
                "experiment": exp, "n": n, "tls": tls, "mode": mode,
                "median_ratio": (d["median"] / base["median"]) if base.get("median") else None,
                "p95_ratio": (d["p95"] / base["p95"]) if base.get("p95") else None,
                "median_ms": d["median"], "classical_median_ms": base["median"],
            })

    # -- C-P4: "ready" time, like for like (Contract 7) ---------------------
    # Classical is ready at boot accepted; hybrid once its key check after
    # boot is answered (e1_handshake.ready_ms). Same dial start for both.
    ready: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ready_groups: dict[tuple, dict[str, dict[str, Any]]] = defaultdict(dict)
    for s in slots:
        e = s.get("e1") or {}
        d = e.get("ready_ms") or {}
        if not d.get("n"):
            continue
        ready[_series_name(s)].append({
            "experiment": s["experiment"], "n": s["n_stations"], "median": d["median"],
            "p95": d["p95"], "basis": e.get("ready_basis"), "slot": s["key"],
            "mode": s["crypto_mode"], "tls": s["tls"],
        })
        ready_groups[(s["experiment"], s["n_stations"], s["tls"])][s["crypto_mode"]] = e
    for rows in ready.values():
        rows.sort(key=lambda r: r["n"])
    ready_overhead = []
    for (exp, n, tls), by_mode in sorted(ready_groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        base = (by_mode.get("classical") or {}).get("ready_ms")
        if not base:
            continue
        for mode, e in by_mode.items():
            if mode == "classical":
                continue
            d = e["ready_ms"]
            ready_overhead.append({
                "experiment": exp, "n": n, "tls": tls, "mode": mode, "basis": e.get("ready_basis"),
                "median_ms": d["median"], "classical_median_ms": base["median"],
                "added_median_ms": (d["median"] - base["median"])
                if d.get("median") is not None and base.get("median") is not None else None,
                "median_ratio": (d["median"] / base["median"]) if base.get("median") else None,
                "p95_ratio": (d["p95"] / base["p95"]) if base.get("p95") else None,
            })

    return {"e1_vs_n": dict(e1), "e2_vs_n": dict(e2), "overhead_vs_classical": overhead,
            "ready_vs_n": dict(ready), "ready_overhead_vs_classical": ready_overhead}
