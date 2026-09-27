"""
OVERVIEW — the basic facts of any run, whatever the experiment.

Track C (analysis). Phase C6.

In plain words: how many chargers ran, how many finished their charging
session, how much energy was delivered, whether any meter readings went
missing, and three timelines -- chargers connected, chargers charging, and
total fleet power -- second by second.

The fleet power timeline is also the E5 picture: a forged command that
switches every charger to maximum shows up here as a spike.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from analysis.match import MatchedRun

MAX_TIMELINE_POINTS = 1200


def _harness_outcomes(run: MatchedRun) -> dict[str, dict[str, Any]]:
    """The last finished/crashed row per charger (a charger reports once)."""
    out: dict[str, dict[str, Any]] = {}
    for row in run.harness.of_type("station_finished", "station_crashed"):
        if row.get("station_id"):
            out[row["station_id"]] = row
    return out


def _sum(rows: list[dict[str, Any]], key: str) -> float:
    return float(sum((r.get(key) or 0) for r in rows))


def timeline(run: MatchedRun) -> dict[str, list[list[float]]]:
    """
    Replay the server's lines in order and sample, once per second: how many
    chargers are connected, how many are in a charging session, and the sum of
    their latest accepted power readings.

    A charger that disconnects stops counting towards power at once -- the
    same rule as Track A's aggregate_power_w, which excludes stale readings.
    A reading the server REJECTED as stale (applied_to_live_state false) never
    enters the total.
    """
    start = run.harness.started_at
    if start is None or not run.station_events:
        return {"connected": [], "charging": [], "power_w": []}

    end = max(run.station_events[-1]["_t"], run.harness.finished_at or start)
    span = max(1.0, end - start)
    step = max(0.5, span / MAX_TIMELINE_POINTS)

    connected: set[str] = set()
    charging: set[str] = set()
    power: dict[str, float] = {}
    series: dict[str, list[list[float]]] = {"connected": [], "charging": [], "power_w": []}

    def sample(at: float) -> None:
        x = round(at - start, 3)
        series["connected"].append([x, len(connected)])
        series["charging"].append([x, len(charging)])
        series["power_w"].append([x, round(sum(power.values()), 1)])

    # When the server dies (E2's kill) it writes nothing -- a hard kill has no
    # goodbye. Every connection is gone at that instant regardless, so the
    # tester's kill marker (and any server restart) resets the live picture.
    resets = [
        {"_t": r["_t"], "event_type": "_server_down"}
        for r in run.harness.of_type("storm_kill") if r.get("_t") is not None
    ] + [
        {"_t": e["_t"], "event_type": "_server_down"}
        for e in run.events("server_started") if e["_t"] > start
    ]
    stream = sorted(run.station_events + resets, key=lambda e: e["_t"])

    next_sample = start
    for ev in stream:
        t = ev["_t"]
        while next_sample < t:
            sample(next_sample)
            next_sample += step
        sid = ev.get("station_id")
        et = ev.get("event_type")
        p = ev.get("payload") or {}
        if et == "_server_down":
            connected.clear()
            power.clear()
            continue
        if et == "connection_established":
            connected.add(sid)
        elif et == "connection_closed":
            if p.get("reason") != "superseded_by_reconnect":
                connected.discard(sid)
                power.pop(sid, None)
        elif et == "transaction_started":
            charging.add(sid)
        elif et == "transaction_ended":
            charging.discard(sid)
            power.pop(sid, None)

        if (
            et in ("transaction_started", "transaction_updated")
            and p.get("transition") is None
            and p.get("applied_to_live_state", True)
            and p.get("power_w") is not None
        ):
            power[sid] = float(p["power_w"])

    while next_sample <= end:
        sample(next_sample)
        next_sample += step
    return series


def measure(run: MatchedRun) -> dict[str, Any]:
    outcomes = _harness_outcomes(run)
    rows = list(outcomes.values())
    spawned = run.harness.station_ids

    started = run.events("transaction_started")
    ended = run.events("transaction_ended")
    updates = [
        e for e in run.events("transaction_updated")
        if (e.get("payload") or {}).get("transition") is None
    ]
    gaps = run.transitions("sequence_gap")
    unrecognised = run.transitions("unrecognised_meter_value")

    gap_sites: Counter[str] = Counter()
    missing_total = 0
    for g in gaps:
        p = g.get("payload") or {}
        n = int(p.get("missing_events") or 0)
        missing_total += n
        gap_sites[str(p.get("loss_site") or "unknown")] += n

    all_readings = started + updates + ended
    energy_wh = sum(
        float((e.get("payload") or {}).get("energy_wh") or 0.0) for e in ended
    )

    return {
        "stations": {
            "spawned": len(spawned),
            "finished_ok": sum(1 for r in rows if r.get("ok")),
            "finished_not_ok": sum(
                1 for r in rows if not r.get("ok") and r.get("event_type") != "station_crashed"
            ),
            "crashed": sum(1 for r in rows if r.get("event_type") == "station_crashed"),
            "never_reported": len(set(spawned) - set(outcomes)),
        },
        "connections": {
            "attempts": _sum(rows, "connection_attempts"),
            "reconnections": _sum(rows, "reconnections"),
            "connect_timeouts": _sum(rows, "connect_timeouts"),
            "callerrors": _sum(rows, "callerrors"),
            "server_established": len(run.events("connection_established")),
            "server_closed": len(run.events("connection_closed")),
        },
        "sessions": {
            "started": len(started),
            "ended": len(ended),
            "stations_with_completed_session": len({e.get("station_id") for e in ended}),
            "energy_wh_total": round(energy_wh, 3),
        },
        "meter": {
            "readings": len(all_readings),
            "replayed_from_offline_queue": sum(
                1 for e in all_readings if (e.get("payload") or {}).get("offline")
            ),
            "rejected_as_stale": sum(
                1 for e in all_readings
                if (e.get("payload") or {}).get("applied_to_live_state") is False
            ),
            "sequence_gaps": len(gaps),
            "missing_events": missing_total,
            "missing_by_site": dict(gap_sites),
            "unrecognised_meter_values": len(unrecognised),
        },
        "offline_queue": {
            "queued": _sum(rows, "offline_queued"),
            "replayed": _sum(rows, "offline_replayed"),
            "dropped": _sum(rows, "offline_dropped"),
        },
        "timeline": timeline(run),
    }
