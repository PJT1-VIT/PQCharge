"""
STEP 2 of the analysis — CHECK: can this run's numbers be trusted?

Track C (analysis). Phase C6.

--------------------------------------------------------------------
IN PLAIN WORDS

Before any number is reported, the diaries are checked against each other.
An instrumentation mistake is invisible in a graph -- a chart drawn from
half the data looks just as confident as one drawn from all of it -- so
the problems are found HERE and printed on the results page next to the
run, rather than discovered by the panel.

Every problem has a level:

    fail   the run's numbers should not be reported (e.g. the server's diary
           has nothing for this run, or a charger program crashed -- a
           Track C bug, never a finding about post-quantum cryptography)
    warn   the numbers stand, but with a stated caveat (e.g. the run was
           stopped early, or an older run predates a measurement)
    info   worth knowing, not a problem

A finding about the SYSTEM (a lost meter reading, a slow recovery) is not a
trust problem -- it is a result, and it is reported in the experiment's
section. This file is only about whether the MEASUREMENT is sound.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from analysis.collect import HarnessRun, ServerDiary
from analysis.match import MatchedRun

LEVELS = ("pass", "info", "warn", "fail")


def _issue(level: str, code: str, message: str) -> dict[str, str]:
    return {"level": level, "code": code, "message": message}


def _status(issues: list[dict[str, str]]) -> str:
    worst = "pass"
    for i in issues:
        if LEVELS.index(i["level"]) > LEVELS.index(worst):
            worst = i["level"]
    return "pass" if worst == "info" else worst


def check_diary(diary: ServerDiary) -> dict[str, Any]:
    """Checks on the server diary as a whole."""
    issues: list[dict[str, str]] = []
    if not diary.found:
        issues.append(_issue(
            "fail", "server_diary_missing",
            f"Server diary not found at {diary.path}. Every run will be unmatched. "
            "If the CSMS ran on another machine, copy its logs/events.jsonl here.",
        ))
        return {"status": _status(issues), "issues": issues}

    bad = diary.lines_unreadable
    if bad == 1 and diary.last_line_unreadable:
        issues.append(_issue(
            "info", "truncated_last_line",
            "The last line of the server diary was half-written (the server was "
            "stopped mid-write, as E2 does on purpose). It was skipped.",
        ))
    elif bad:
        issues.append(_issue(
            "warn", "unreadable_lines",
            f"{bad} of {diary.lines_total} server diary lines could not be read and were skipped.",
        ))
    if diary.unknown_fields:
        names = ", ".join(sorted(diary.unknown_fields))
        issues.append(_issue(
            "warn", "schema_changed",
            f"The server diary has top-level fields Contract 3 does not define ({names}). "
            "Kept and ignored; ask Track A whether the event schema changed.",
        ))
    return {"status": _status(issues), "issues": issues}


def check_run(run: MatchedRun, overview: dict[str, Any], e1: dict[str, Any]) -> dict[str, Any]:
    """Checks on one matched run."""
    h: HarnessRun = run.harness
    issues: list[dict[str, str]] = []
    st = overview["stations"]

    if not h.completed:
        issues.append(_issue(
            "warn", "run_incomplete",
            "The tester stopped before writing its final totals (Ctrl-C or a crash). "
            "Numbers cover only what happened before that.",
        ))
    if not run.station_events:
        issues.append(_issue(
            "fail", "no_server_data",
            "The server diary has no lines for this run's chargers in this time window. "
            "Either the CSMS logged elsewhere, or the two machines' clocks disagree.",
        ))
    if st["crashed"]:
        issues.append(_issue(
            "fail", "agent_crashed",
            f"{st['crashed']} charger program(s) crashed. That is a Track C bug, "
            "not a result -- fix it and re-run.",
        ))
    if st["never_reported"]:
        issues.append(_issue(
            "warn", "unreported_stations",
            f"{st['never_reported']} charger(s) were started but never reported back.",
        ))

    # -- the two diaries must agree on who charged ---------------------------
    ended_by_station = Counter(e.get("station_id") for e in run.events("transaction_ended"))
    finished = {
        r["station_id"]: r for r in h.of_type("station_finished") if r.get("station_id")
    }
    silent = [sid for sid, r in finished.items() if r.get("ok") and not ended_by_station.get(sid)]
    if silent and run.station_events:
        issues.append(_issue(
            "warn", "session_missing_on_server",
            f"{len(silent)} charger(s) reported a completed session that the server "
            f"never recorded as ended (e.g. {', '.join(sorted(silent)[:3])}).",
        ))

    # -- connection counts: charger's view vs server's view -------------------
    if e1.get("station_side_available"):
        server_conns = Counter(e.get("station_id") for e in run.events("connection_established"))
        mismatched = [
            sid for sid, r in finished.items()
            if len(r.get("connect_ms") or []) != server_conns.get(sid, 0)
        ]
        if mismatched and run.station_events:
            issues.append(_issue(
                "info", "connection_count_mismatch",
                f"{len(mismatched)} charger(s) counted a different number of connections "
                "than the server saw (a connection can open on the charger and be dropped "
                "before the server registers it, e.g. during a restart).",
            ))
    else:
        issues.append(_issue(
            "warn", "no_station_side_timing",
            "This run was recorded before chargers logged their own connection time "
            "(connect_ms). E1's headline number is unavailable for it; re-run to include it.",
        ))

    if overview["meter"]["unrecognised_meter_values"]:
        issues.append(_issue(
            "warn", "unrecognised_meter_values",
            "The server could not read some meter values (unknown measurand or unit), "
            "so power and energy for those readings are missing.",
        ))

    return {"status": _status(issues), "issues": issues}
