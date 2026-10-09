"""
E2 — RECONNECTION STORM: after the server crashes, how fast does the fleet come back?

Track C (analysis). Phase C6. The project's headline experiment.

--------------------------------------------------------------------
IN PLAIN WORDS

Mid-run the server is killed on purpose, then restarted. Every charger
notices, waits a random short time (so they do not all hit the server in
the same instant), and reconnects -- each doing a full secure handshake at
once. With post-quantum handshakes being heavier, the question is: how long
until the whole fleet is back, and was any charging data lost?

"RECOVERED" IS DEFINED ONCE, HERE, AND NEVER CHANGED BETWEEN RUNS:
a charger has recovered when it is connected AND the server has accepted
its BootNotification again. That is Track A's own definition
(StationView.is_recovered) -- measured here from the server's "booted"
line, whose timestamp is exact rather than rounded to a one-second poll.

PHASE C-P4 (Contract 7 section 7.7): in hybrid mode an ENROLLED charger
must also pass its key check after the boot. So, per charger:
    not key-checked  recovered = its first "booted" line after the restart
    key-checked      recovered = the later of that "booted" line and its
                     first PASSED boot key check (pq_auth, trigger "boot")
                     after the restart; no passed check -> not recovered.
"Key-checked" is read from the server's own lines: the boot verifier only
challenges enrolled chargers, so a charger with a boot key check anywhere in
the run is enrolled. This is the same rule as Track A's is_recovered.

The clock starts at the RESTART, not the kill: time spent with no server at
all is the length of the outage we chose, not a property of the fleet.

Measured:
    T50 / T95 / T100   seconds until half / 95% / all of the fleet was back
    recovery curve     % of fleet recovered against seconds since restart
    data integrity     meter readings lost (and WHERE: in the charger's own
                       offline queue, or in transit), readings replayed,
                       stale readings rejected, sessions that survived
    cross-check        the same curve from the tester's once-a-second polls
                       of /api/fleet, when the fleet watcher was running
"""

from __future__ import annotations

from typing import Any

from analysis import stats
from analysis.match import MatchedRun

MAX_CURVE_POINTS = 300


def _storm_times(run: MatchedRun) -> tuple[float | None, float | None, str]:
    """(kill_t, restart_t, how_detected). restart_t None = no storm in this run."""
    kills = run.harness.of_type("storm_kill")
    supervisor_kills = [k for k in kills if k.get("detected_by") == "supervisor"]
    kill_row = (supervisor_kills or kills or [None])[0]
    kill_t = kill_row.get("_t") if kill_row else None

    run_start = run.harness.started_at or 0.0
    server_starts = [
        e["_t"] for e in run.events("server_started")
        if e["_t"] > run_start and (kill_t is None or e["_t"] >= kill_t)
    ]
    if server_starts:
        return kill_t, server_starts[0], "server_started"

    if kill_t is not None:
        restarts = [
            r["_t"] for r in run.harness.of_type("storm_restart")
            if r.get("_t") is not None and r["_t"] >= kill_t
        ]
        if restarts:
            return kill_t, restarts[0], "harness_storm_restart"
    return kill_t, None, "none"


def _population_curve(times: list[float], population: int) -> list[list[float]]:
    """[[seconds, % of population recovered], ...], thinned, starting at 0."""
    times = sorted(times)
    pts = [[0.0, 0.0]] + [[t, 100.0 * (i + 1) / population] for i, t in enumerate(times)]
    if len(pts) > MAX_CURVE_POINTS:
        step = (len(pts) - 1) / (MAX_CURVE_POINTS - 1)
        pts = [pts[round(i * step)] for i in range(MAX_CURVE_POINTS)]
    return [[round(x, 4), round(y, 3)] for x, y in pts]


def _snapshot_curve(run: MatchedRun, restart_t: float, population: int) -> list[list[float]]:
    """The same curve from the fleet watcher's polls, using Track A's predicate."""
    try:
        from harness.load_generator import FleetWatcher
        count = FleetWatcher._recovered
    except Exception:  # noqa: BLE001 - cross-check only
        def count(snapshot: dict[str, Any]) -> int:
            return sum(
                1 for r in snapshot.get("stations", [])
                if r.get("connection_state") == "connected" and r.get("boot_accepted")
            )

    ours = set(run.stations)
    points: list[list[float]] = []
    for row in run.harness.of_type("fleet_snapshot"):
        t = row.get("_t")
        snap = row.get("snapshot") or {}
        if t is None or t < restart_t:
            continue
        mine = {**snap, "stations": [s for s in snap.get("stations", []) if s.get("station_id") in ours]}
        points.append([round(t - restart_t, 3), round(100.0 * count(mine) / population, 3)])
    return points


def measure(run: MatchedRun) -> dict[str, Any] | None:
    kill_t, restart_t, detected = _storm_times(run)
    if restart_t is None:
        return None

    booted = run.transitions("booted")
    cutoff = kill_t if kill_t is not None else restart_t

    # WHO had to recover: every charger that was up before the outage.
    population_ids = {e["station_id"] for e in booted if e["_t"] < cutoff}
    population = len(population_ids)

    booted_after: dict[str, float] = {}
    for e in booted:
        sid = e["station_id"]
        if sid in population_ids and e["_t"] >= restart_t and sid not in booted_after:
            booted_after[sid] = e["_t"]

    # C-P4: the key check after boot, for enrolled chargers (section 7.7).
    boot_checks = [c for c in run.pq_checks("boot") if c.get("station_id") in population_ids]
    key_checked = {c["station_id"] for c in boot_checks}
    passed_after: dict[str, float] = {}
    failed_after = 0
    for c in boot_checks:
        if c["_t"] < restart_t:
            continue
        if (c.get("outcome") or "") == "success":
            passed_after.setdefault(c["station_id"], c["_t"])
        else:
            failed_after += 1

    recovery: dict[str, float] = {}
    for sid, tb in booted_after.items():
        if sid in key_checked:
            tp = passed_after.get(sid)
            if tp is None:
                continue  # booted, but never passed its key check: not recovered
            recovery[sid] = max(tb, tp) - restart_t
        else:
            recovery[sid] = tb - restart_t
    times = sorted(recovery.values())

    # -- data integrity across the outage ---------------------------------
    gaps_after = [g for g in run.transitions("sequence_gap") if g["_t"] >= cutoff]
    lost_in_transit = sum(
        int((g.get("payload") or {}).get("missing_events") or 0)
        for g in gaps_after if (g.get("payload") or {}).get("loss_site") == "in_transit"
    )
    lost_in_queue = sum(
        int((g.get("payload") or {}).get("missing_events") or 0)
        for g in gaps_after if (g.get("payload") or {}).get("loss_site") == "agent_offline_queue"
    )

    tx_events = run.events("transaction_started", "transaction_updated", "transaction_ended")
    started_before = {
        (e.get("payload") or {}).get("transaction_id")
        for e in tx_events if e.get("event_type") == "transaction_started" and e["_t"] < cutoff
    }
    ended_before = {
        (e.get("payload") or {}).get("transaction_id")
        for e in tx_events if e.get("event_type") == "transaction_ended" and e["_t"] < cutoff
    }
    in_flight = {t for t in started_before - ended_before if t}
    seen_after = {
        (e.get("payload") or {}).get("transaction_id")
        for e in tx_events if e["_t"] >= restart_t
    }
    ended_after = {
        (e.get("payload") or {}).get("transaction_id")
        for e in tx_events if e.get("event_type") == "transaction_ended" and e["_t"] >= restart_t
    }
    replayed = sum(
        1 for e in tx_events
        if e["_t"] >= restart_t and (e.get("payload") or {}).get("offline")
        and (e.get("payload") or {}).get("transition") is None
    )
    rejected = sum(
        1 for e in tx_events
        if e["_t"] >= restart_t and (e.get("payload") or {}).get("applied_to_live_state") is False
    )

    rows = run.harness.of_type("station_finished", "station_crashed")
    # C6.2: station_connected lines and/or station_finished rows.
    reconnect_ms = [
        t for times in run.harness.connect_times().values() for t in times[1:]
    ]

    snap = _snapshot_curve(run, restart_t, population) if population else []
    snap_t95 = next((x for x, y in snap if y >= 95.0), None)

    return {
        "recovered_definition": (
            "connected AND BootNotification accepted, AND (enrolled chargers in hybrid mode) "
            "key check after boot passed (Contract 7 section 7.7)"
            if key_checked else
            "connected AND BootNotification accepted (Track A StationView.is_recovered)"),
        "recovery_rule": "boot + key check" if key_checked else "boot",
        "key_checked_population": len(key_checked),
        "boot_checks_failed_after_restart": failed_after,
        "booted_but_key_check_not_passed": sorted(
            sid for sid in booted_after if sid in key_checked and sid not in passed_after)[:50],
        "kill_at_s": (kill_t - run.harness.started_at) if kill_t and run.harness.started_at else None,
        "restart_at_s": restart_t - (run.harness.started_at or restart_t),
        "outage_s": (restart_t - kill_t) if kill_t is not None else None,
        "restart_detected_by": detected,
        "population": population,
        "recovered": len(times),
        "unrecovered": population - len(times),
        "unrecovered_ids": sorted(population_ids - set(recovery))[:50],
        "t50_s": stats.time_to_share(times, population, 0.50),
        "t95_s": stats.time_to_share(times, population, 0.95),
        "t100_s": stats.time_to_share(times, population, 1.00),
        "recovery_s": stats.describe(times),
        "curve": _population_curve(times, population) if population else [],
        "snapshot_curve": snap,
        "snapshot_t95_s": snap_t95,
        "reconnect_connect_ms": stats.describe(reconnect_ms),
        "integrity": {
            "sessions_in_flight_at_kill": len(in_flight),
            "sessions_resumed_after_restart": len(in_flight & seen_after),
            "sessions_completed_after_restart": len(in_flight & ended_after),
            "readings_replayed_from_offline_queue": replayed,
            "readings_rejected_as_stale": rejected,
            "events_lost_in_transit": lost_in_transit,
            "events_dropped_by_agent_queue": lost_in_queue,
            "offline_dropped_reported_by_agents": float(
                sum((r.get("offline_dropped") or 0) for r in rows)
            ),
        },
        "connect_timeouts": float(sum((r.get("connect_timeouts") or 0) for r in rows)),
        "connection_attempts_per_station_p95": stats.percentile(
            [float(r.get("connection_attempts") or 0) for r in rows], 95
        ) if rows else None,
    }

