"""
E3 — MIGRATION UNDER LOAD: can the fleet be switched to post-quantum while it charges?

Track C (analysis). Phase C6, extended in C6.1 for the A+B+C integration.

--------------------------------------------------------------------
IN PLAIN WORDS

The operator presses "migrate". Track B's orchestrator upgrades a small
CANARY group first (a test group, watched closely), then the rest in WAVES.
For each charger it (1) sends the charger a post-quantum key, then (2)
CHALLENGES it: the charger must sign a fresh random number with that key,
and the server checks the signature. Only then is the charger "migrated".
If too many chargers in a wave fail, that wave is ROLLED BACK. All of this
happens while chargers are mid-session.

What E3 shows:
    - the migration's shape over time: how many chargers are pending /
      upgrading / upgraded / rolled back / incompatible, second by second,
      for the WHOLE fleet (C6.1: in Stage 6 the failing chargers are Track
      A's test chargers, not the tester's -- counting only the tester's
      would hide the rollback)
    - each wave: when it started and ended, and how it finished
    - the post-quantum checks (C6.1): how many passed / failed, and how long
      each round trip took -- the S2 latency Track B pointed to
    - chargers SKIPPED because they were offline (C6.1): left pending, never
      sent a key -- "not attempted", not "failed"
    - whether the result is "authenticated" or only "key installed" (C6.1):
      if chargers were migrated but no check ever ran, the server was not
      wired to challenge, and the report must say so
    - the controller's own counts adding up (C6.1): pending + upgrading +
      upgraded + rolled back + incompatible must equal its total
    - that charging was NOT disturbed (the tester's own charging chargers)
    - that each charger agrees with the server (C6.1): a charger the server
      calls migrated must itself report holding a key

WHERE THE DATA COMES FROM

    1. the tester's once-a-second copies of /api/fleet (fleet_snapshot):
       every charger's migration_state and the orchestrator's own status
       (counts and waves) -- Track A's and Track B's fields, verbatim;
    2. the orchestrator's lines in the server diary (source "orchestrator"):
       migration_started, wave_started, wave_completed, wave_rolled_back,
       migration_completed, migration_failed, station_deferred, and
       connection_attempt with transition "pq_auth" (charger in
       payload.station, result in payload.result -- see collect.py);
    3. the tester's own per-charger record (station_finished), which since
       C6.1 includes whether the charger holds a key.

Run the load generator with --watch-fleet for (1).

--------------------------------------------------------------------
PHASE C-P4 (Contract 7)

    - The key checks counted here are the MIGRATION's (trigger
      "migration", or no trigger in older diaries). The checks after every
      boot (trigger "boot", hybrid mode) are reported apart, in boot_checks:
      they are not part of the migration and must not inflate its numbers.
    - pq_enrolled: chargers that made their own key during the migration
      (certificate_installed, transition "pq_enrolled"), with their key_ids.
    - keys_at_start: the tester's chargers that started the
      run already holding a key saved by an earlier run. For them this
      migration is a rotation, not a first enrolment (see check.py).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from analysis import stats
from analysis.collect import to_epoch
from analysis.match import MatchedRun

STATES = ("pending", "in_progress", "migrated", "rolled_back", "incompatible")
TERMINAL_PHASES = {"completed", "rolled_back", "failed"}
MAX_POINTS = 600

VERIFIED = "authenticated"
KEY_ONLY = "key installed (not authenticated)"
PARTIAL = "partly authenticated"
NOTHING_MIGRATED = "nothing migrated"


def _thin(points: list[Any], limit: int = MAX_POINTS) -> list[Any]:
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def _state_counts(snapshot: dict[str, Any]) -> Counter[str]:
    """Every charger in the snapshot, by migration state (whole fleet)."""
    return Counter(
        str(st.get("migration_state") or "pending")
        for st in snapshot.get("stations", [])
    )


def _controller_sum_ok(status: dict[str, Any]) -> bool | None:
    """The orchestrator's own invariant. None when it reports no counts."""
    if not status or "total_stations" not in status:
        return None
    parts = [status.get(k) for k in STATES]
    if any(not isinstance(v, int) for v in parts):
        return None
    return sum(parts) == status.get("total_stations")


def _result(ev: dict[str, Any]) -> str:
    return str(ev.get("outcome") or (ev.get("payload") or {}).get("result") or "")


def measure(run: MatchedRun) -> dict[str, Any] | None:
    start = run.harness.started_at or 0.0
    ours = set(run.stations)

    snapshots = [
        (r["_t"], r.get("snapshot") or {})
        for r in run.harness.of_type("fleet_snapshot") if r.get("_t") is not None
    ]
    markers = run.migration(
        "migration_started", "wave_started", "wave_completed",
        "wave_rolled_back", "migration_completed", "migration_failed",
    )
    # C-P4: migration checks only; boot checks are reported apart.
    checks = run.pq_checks("migration")
    boot_checks = run.pq_checks("boot")
    deferrals = run.migration("station_deferred")
    enrolled_lines = [
        e for e in run.migration("certificate_installed")
        if (e.get("payload") or {}).get("transition") == "pq_enrolled"
    ]

    active_snapshots = [
        (t, s) for t, s in snapshots
        if (s.get("migration") or {}).get("phase", "idle") not in ("idle", None)
        or any(st.get("migration_state") not in (None, "pending") for st in s.get("stations", []))
    ]
    if (not markers and not active_snapshots and not checks and not deferrals
            and not enrolled_lines):
        return None

    # -- the migration window ----------------------------------------------
    begin_candidates = [m["_t"] for m in markers if m.get("event_type") == "migration_started"]
    if active_snapshots:
        begin_candidates.append(active_snapshots[0][0])
    for group in (markers, checks, deferrals, enrolled_lines):
        if group:
            begin_candidates.append(group[0]["_t"])
    began = min(begin_candidates)

    # The migration ends when it completes, when the orchestrator halts it
    # with a rollback, or when the orchestrator itself crashes. A MANUAL
    # rollback (the server's own line, trigger "manual") does not end it.
    end_candidates = [
        m["_t"] for m in markers
        if m.get("event_type") in ("migration_completed", "migration_failed")
        or (m.get("event_type") == "wave_rolled_back"
            and (m.get("payload") or {}).get("source") == "orchestrator")
    ]
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

    # -- state counts over time: WHOLE fleet --------------------------------
    state_series: dict[str, list[list[float]]] = {s: [] for s in STATES}
    sum_violations = 0
    for t, s in snapshots:
        counts = _state_counts(s)
        x = round(t - start, 3)
        for state in STATES:
            state_series[state].append([x, counts.get(state, 0)])
        if _controller_sum_ok(s.get("migration") or {}) is False:
            sum_violations += 1
    state_series = {k: _thin(v) for k, v in state_series.items()}

    last = snapshots[-1][1] if snapshots else {}
    final_counts = _state_counts(last)
    status = last.get("migration") or {}
    final_by_station = {
        st.get("station_id"): str(st.get("migration_state") or "pending")
        for st in last.get("stations", [])
    }

    # -- waves (orchestrator's own record) ---------------------------------
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
            "trigger": (m.get("payload") or {}).get("trigger"),
        }
        for m in markers
    ]

    failures = [
        {"at_s": round(m["_t"] - start, 3), "error": (m.get("payload") or {}).get("error")}
        for m in markers if m.get("event_type") == "migration_failed"
    ]

    # -- post-quantum checks (pq_auth) --------------------------------------
    passed = [c for c in checks if _result(c) == "success"]
    rejected = [c for c in checks if _result(c) and _result(c) != "success"]
    durations = [
        float((c.get("payload") or {}).get("duration_ms"))
        for c in checks if isinstance((c.get("payload") or {}).get("duration_ms"), (int, float))
    ]
    first_seen: set[str] = set()
    first_durations: list[float] = []
    later_durations: list[float] = []
    for c in checks:
        d = (c.get("payload") or {}).get("duration_ms")
        if not isinstance(d, (int, float)):
            continue
        sid = c.get("station_id")
        (later_durations if sid in first_seen else first_durations).append(float(d))
        first_seen.add(sid)

    if snapshots:
        migrated_count = final_counts.get("migrated", 0)
    else:
        # No fleet snapshots (run without --watch-fleet): fall back to the
        # orchestrator's own per-wave totals from completed waves.
        migrated_count = sum(
            int((m.get("payload") or {}).get("migrated") or 0)
            for m in markers if m.get("event_type") == "wave_completed"
        )
    if not migrated_count and not passed:
        verification = NOTHING_MIGRATED
    elif not checks:
        verification = KEY_ONLY
    else:
        migrated_ids = {sid for sid, st in final_by_station.items() if st == "migrated"}
        passed_ids = {c.get("station_id") for c in passed}
        if migrated_ids:
            verification = VERIFIED if passed_ids >= migrated_ids else PARTIAL
        else:
            verification = VERIFIED if len(passed_ids) >= migrated_count else PARTIAL

    # -- offline chargers skipped --------------------------------------------
    deferred_ids = sorted({d.get("station_id") for d in deferrals if d.get("station_id")})

    # -- charger's own view vs the server's (tester's chargers only) --------
    no_key = []
    for row in run.harness.of_type("station_finished", "station_crashed"):
        sid = row.get("station_id")
        if "pq_key_installed" not in row:
            continue  # recorded before C6.1: nothing to compare
        if final_by_station.get(sid) == "migrated" and not row.get("pq_key_installed"):
            no_key.append(sid)
    agent_view_available = any(
        "pq_key_installed" in r for r in run.harness.of_type("station_finished", "station_crashed")
    )
    keys_at_start = run.harness.keys_at_start()
    started_with_keys = sorted(sid for sid, held in keys_at_start.items() if held)

    # -- C-P4: boot checks (hybrid), apart from the migration ----------------
    boot_passed = [c for c in boot_checks if _result(c) == "success"]
    boot_durations = [
        float((c.get("payload") or {}).get("duration_ms"))
        for c in boot_checks if isinstance((c.get("payload") or {}).get("duration_ms"), (int, float))
    ]
    enrolled_key_ids: dict[str, str] = {}
    for e in enrolled_lines:
        sid = e.get("station_id")
        kid = (e.get("payload") or {}).get("key_id")
        if sid:
            enrolled_key_ids[sid] = kid

    # -- was charging disturbed? (the tester's charging chargers) ----------
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
        "fleet_size": sum(final_counts.values()),
        "migrated_count": migrated_count,
        "snapshots_available": bool(snapshots),
        "tester_chargers": len(ours),
        "final_counts": {s: final_counts.get(s, 0) for s in STATES},
        "controller_counts": {
            k: status.get(k) for k in ("total_stations",) + STATES if k in status
        },
        "controller_sum_violations": sum_violations,
        "state_timeline": state_series,
        "waves": waves,
        # Counted from both records and the larger taken: the orchestrator's
        # line and its wave list describe the SAME rollback.
        "rollbacks": max(
            sum(1 for m in markers if m.get("event_type") == "wave_rolled_back"),
            sum(1 for w in waves if str(w.get("phase")).lower() == "rolled_back"),
        ),
        "manual_rollbacks": sum(
            1 for m in markers
            if m.get("event_type") == "wave_rolled_back"
            and (m.get("payload") or {}).get("trigger") == "manual"
        ),
        "markers": marker_rows,
        "failures": failures,
        "verification": verification,
        "pq_checks": {
            "total": len(checks),
            "passed": len(passed),
            "rejected": len(rejected),
            "rejections": [
                {
                    "station_id": c.get("station_id"),
                    "detail": (c.get("payload") or {}).get("detail"),
                    "wave_id": (c.get("payload") or {}).get("wave_id"),
                    "at_s": round(c["_t"] - start, 3),
                }
                for c in rejected[:50]
            ],
            "algorithm": next(
                ((c.get("payload") or {}).get("algorithm") for c in checks
                 if (c.get("payload") or {}).get("algorithm")), None),
            "round_trip_ms": stats.describe(durations),
            "first_per_charger_ms": stats.describe(first_durations),
            "later_ms": stats.describe(later_durations),
        },
        "deferred": {"count": len(deferred_ids), "station_ids": deferred_ids[:50]},
        "boot_checks": {
            "total": len(boot_checks),
            "passed": len(boot_passed),
            "rejected": len(boot_checks) - len(boot_passed),
            "round_trip_ms": stats.describe(boot_durations),
        },
        "pq_enrolled": {
            "count": len(enrolled_key_ids),
            "key_ids": dict(sorted(enrolled_key_ids.items())[:50]),
        },
        "agent_view": {
            "available": agent_view_available,
            "migrated_but_no_key": sorted(no_key)[:50],
        },
        # C-P4: the tester's chargers that started with a saved key.
        "keys_at_start": {
            "known": bool(keys_at_start),
            "count": len(started_with_keys),
            "station_ids": started_with_keys[:50],
        },
        "charging": {
            "sessions_running_at_start": len(running_at_start),
            "sessions_disturbed": len(disturbed),
            "disturbed_station_ids": disturbed[:50],
            "connections_dropped_during": len(dropped),
        },
    }
