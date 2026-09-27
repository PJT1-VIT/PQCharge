"""
E3 — MIGRATION UNDER LOAD: can the fleet be switched to post-quantum while it charges?

Track C (analysis). Phase C6.

--------------------------------------------------------------------
IN PLAIN WORDS

The operator presses "migrate". Track B's orchestrator upgrades a small
CANARY group first (a test group, watched closely), then the rest in WAVES.
If too many chargers in a wave fail, that wave is ROLLED BACK (returned to
its old security). All of this happens while chargers are mid-session.

What E3 has to show:
    - the migration's shape over time: how many chargers are pending /
      upgrading / upgraded / rolled back / incompatible, second by second
    - each wave: when it started and ended, and how it finished
    - that charging was NOT disturbed: sessions that were running when the
      migration began, and whether any of them lost their connection or
      their meter readings during it
    - that a rollback, when a failure was injected, actually happened

WHERE THE DATA COMES FROM

    1. the tester's once-a-second copies of /api/fleet (fleet_snapshot),
       which carry every charger's migration_state and the orchestrator's
       wave list -- Track A's and Track B's own fields, stored verbatim;
    2. the server's migration lines (migration_started, wave_started,
       wave_completed, wave_rolled_back, migration_completed).

Either source alone is enough; both are used when present. Run the load
generator with --watch-fleet for (1).

NOTE: the orchestrator is not yet wired into the live CSMS (A+B+C session),
so no real run contains migration data yet. This module is exercised by
the tests with synthetic diaries in exactly the shape those fields have.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from analysis.collect import to_epoch
from analysis.match import MatchedRun

STATES = ("pending", "in_progress", "migrated", "rolled_back", "incompatible")
TERMINAL_PHASES = {"completed", "rolled_back", "failed"}
MAX_POINTS = 600


def _thin(points: list[Any], limit: int = MAX_POINTS) -> list[Any]:
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def measure(run: MatchedRun) -> dict[str, Any] | None:
    start = run.harness.started_at or 0.0
    ours = set(run.stations)

    snapshots = [
        (r["_t"], r.get("snapshot") or {})
        for r in run.harness.of_type("fleet_snapshot") if r.get("_t") is not None
    ]
    markers = run.events(
        "migration_started", "wave_started", "wave_completed",
        "wave_rolled_back", "migration_completed",
    )

    active_snapshots = [
        (t, s) for t, s in snapshots
        if (s.get("migration") or {}).get("phase", "idle") not in ("idle", None)
        or any(
            st.get("migration_state") not in (None, "pending")
            for st in s.get("stations", []) if st.get("station_id") in ours
        )
    ]
    if not markers and not active_snapshots:
        return None

    # -- the migration window ----------------------------------------------
    begin_candidates = [m["_t"] for m in markers if m.get("event_type") == "migration_started"]
    if active_snapshots:
        begin_candidates.append(active_snapshots[0][0])
    if markers:
        begin_candidates.append(markers[0]["_t"])
    began = min(begin_candidates)

    end_candidates = [m["_t"] for m in markers if m.get("event_type") == "migration_completed"]
    terminal = [
        t for t, s in active_snapshots
        if str((s.get("migration") or {}).get("phase", "")).lower() in TERMINAL_PHASES
    ]
    if end_candidates:
        ended: float | None = min(end_candidates)
    elif terminal:
        ended = terminal[0]
    else:
        ended = None

    # -- state counts over time (from snapshots) ---------------------------
    state_series: dict[str, list[list[float]]] = {s: [] for s in STATES}
    for t, s in snapshots:
        counts: Counter[str] = Counter(
            str(st.get("migration_state") or "pending")
            for st in s.get("stations", []) if st.get("station_id") in ours
        )
        x = round(t - start, 3)
        for state in STATES:
            state_series[state].append([x, counts.get(state, 0)])
    state_series = {k: _thin(v) for k, v in state_series.items()}

    last = snapshots[-1][1] if snapshots else {}
    final_counts: Counter[str] = Counter(
        str(st.get("migration_state") or "pending")
        for st in last.get("stations", []) if st.get("station_id") in ours
    )
    status = last.get("migration") or {}

    # -- waves (orchestrator's own record, then server markers) -----------
    waves = []
    for w in status.get("waves") or []:
        ws, we = to_epoch(w.get("started_at")), to_epoch(w.get("completed_at"))
        waves.append({
            "wave_id": w.get("wave_id"),
            "is_canary": bool(w.get("is_canary")),
            "stations": len(w.get("station_ids") or []),
            "phase": w.get("phase"),
            "migrated": w.get("migrated_count", 0),
            "failed": w.get("failed_count", 0),
            "start_s": (ws - start) if ws else None,
            "end_s": (we - start) if we else None,
        })

    marker_rows = [
        {
            "event": m.get("event_type"),
            "at_s": round(m["_t"] - start, 3),
            "wave_id": (m.get("payload") or {}).get("wave_id"),
            "outcome": m.get("outcome"),
        }
        for m in markers
    ]

    # -- was charging disturbed? -------------------------------------------
    window_end = ended if ended is not None else (run.harness.finished_at or began)
    tx = run.events("transaction_started", "transaction_ended")
    running_at_start: dict[str, str] = {}
    for e in tx:
        if e["_t"] >= began:
            break
        sid = e.get("station_id")
        if e.get("event_type") == "transaction_started":
            running_at_start[sid] = (e.get("payload") or {}).get("transaction_id")
        else:
            running_at_start.pop(sid, None)

    dropped = {
        e.get("station_id") for e in run.events("connection_closed")
        if began <= e["_t"] <= window_end
        and (e.get("payload") or {}).get("reason") != "superseded_by_reconnect"
    }
    gap_stations = {
        g.get("station_id") for g in run.transitions("sequence_gap")
        if began <= g["_t"] <= window_end
    }
    disturbed = sorted((dropped | gap_stations) & set(running_at_start))

    return {
        "began_at_s": round(began - start, 3),
        "ended_at_s": round(ended - start, 3) if ended is not None else None,
        "duration_s": round(ended - began, 3) if ended is not None else None,
        "phase": status.get("phase"),
        "target_mode": status.get("target_mode"),
        "final_counts": {s: final_counts.get(s, 0) for s in STATES},
        "state_timeline": state_series,
        "waves": waves,
        # Counted from both records and the larger taken: the server marker and
        # the orchestrator's wave list describe the SAME rollback.
        "rollbacks": max(
            sum(1 for m in markers if m.get("event_type") == "wave_rolled_back"),
            sum(1 for w in waves if str(w.get("phase")).lower() == "rolled_back"),
        ),
        "markers": marker_rows,
        "charging": {
            "sessions_running_at_start": len(running_at_start),
            "sessions_disturbed": len(disturbed),
            "disturbed_station_ids": disturbed[:50],
            "connections_dropped_during": len(dropped),
        },
    }
