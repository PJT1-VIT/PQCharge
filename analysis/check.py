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
            f"No server diary found (looked for: {diary.path or 'nothing given'}). "
            "Every run will be unmatched. "
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
    if diary.duplicates_dropped:
        issues.append(_issue(
            "info", "duplicate_lines",
            f"{diary.duplicates_dropped} line(s) appeared in more than one server diary "
            "and were counted once.",
        ))
    if diary.unknown_fields:
        names = ", ".join(sorted(diary.unknown_fields))
        issues.append(_issue(
            "warn", "schema_changed",
            f"The server diary has top-level fields Contract 3 does not define ({names}). "
            "Kept and ignored; ask Track A whether the event schema changed.",
        ))
    return {"status": _status(issues), "issues": issues}


def check_run(run: MatchedRun, overview: dict[str, Any], e1: dict[str, Any],
              e3: dict[str, Any] | None = None) -> dict[str, Any]:
    """Checks on one matched run. `e3` adds the migration checks (C6.1)."""
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
        if h.completed:
            text = f"{st['never_reported']} charger(s) were started but never reported back."
        else:
            # C6.2: the usual cause is the early stop itself, not the chargers.
            text = (f"{st['never_reported']} charger(s) never sent their final totals "
                    "because the tester stopped early. Their connection times still "
                    "count where the diary has station_connected lines.")
        issues.append(_issue("warn", "unreported_stations", text))

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
            sid for sid, times in h.connect_times().items()
            if len(times) != server_conns.get(sid, 0)
        ]
        if mismatched and run.station_events:
            issues.append(_issue(
                "info", "connection_count_mismatch",
                f"{len(mismatched)} charger(s) counted a different number of connections "
                "than the server saw (a connection can open on the charger and be dropped "
                "before the server registers it, e.g. during a restart).",
            ))
    else:
        if h.completed:
            text = ("This run was recorded before chargers logged their own connection time "
                    "(connect_ms). E1's headline number is unavailable for it; re-run to include it.")
        else:
            # C6.2: the s1b case -- stopped before any charger reported, and
            # the diary predates station_connected lines.
            text = ("No charger connection times were recorded: the tester stopped before "
                    "the chargers reported, and this diary has no station_connected lines "
                    "(recorded before C6.2). E1's headline number is unavailable for it.")
        issues.append(_issue("warn", "no_station_side_timing", text))

    issues.extend(_contract7_issues(run, e1, e3))

    if e3 is not None:
        issues.extend(_migration_issues(e3))

    if overview["meter"]["unrecognised_meter_values"]:
        issues.append(_issue(
            "warn", "unrecognised_meter_values",
            "The server could not read some meter values (unknown measurand or unit), "
            "so power and energy for those readings are missing.",
        ))

    return {"status": _status(issues), "issues": issues}


def _contract7_issues(run: MatchedRun, e1: dict[str, Any],
                      e3: dict[str, Any] | None) -> list[dict[str, str]]:
    """
    PHASE C-P4 — checks that Contract 7's security modes were set up right.

    mode_mismatch     the tester and the server must name the same mode
                      (section 7.1); otherwise the slot's label is wrong.
    started_with_saved_keys
                      chargers loaded keys saved by an earlier run. Keys
                      persist on BOTH sides (certs/pq/ and the server's
                      --db), so they must be reset or reused TOGETHER. In a
                      migration run this makes it a rotation, not a first
                      enrolment: warn. Otherwise just note it.
    hybrid_without_boot_checks
                      a hybrid run whose chargers held keys, but the server
                      never checked a key after boot: the server is not
                      running the boot check, and E1's secure-ready time is
                      missing.
    """
    h: HarnessRun = run.harness
    issues: list[dict[str, str]] = []

    server_mode = run.server_mode
    if server_mode and h.crypto_mode and server_mode != h.crypto_mode:
        issues.append(_issue(
            "warn", "mode_mismatch",
            f"The tester ran in '{h.crypto_mode}' mode but the server logged "
            f"'{server_mode}'. Contract 7 requires both to name the same mode; this "
            "run is filed under the tester's mode, which may be wrong.",
        ))

    started = sorted(sid for sid, held in h.keys_at_start().items() if held)
    if started:
        sample = ", ".join(started[:3])
        if e3 is not None:
            issues.append(_issue(
                "warn", "started_with_saved_keys",
                f"{len(started)} charger(s) started with a post-quantum key saved by an "
                f"earlier run (e.g. {sample}), so this migration is a key rotation for them, "
                "not a first enrolment. For a clean E3, clear certs/pq/ AND use a fresh "
                "server --db together.",
            ))
        else:
            issues.append(_issue(
                "info", "started_with_saved_keys",
                f"{len(started)} charger(s) started with a post-quantum key saved by an "
                f"earlier run (e.g. {sample}).",
            ))

    held = e1.get("chargers_with_key_at_connect") or 0
    if h.crypto_mode == "hybrid" and held and not run.pq_checks("boot"):
        issues.append(_issue(
            "warn", "hybrid_without_boot_checks",
            f"{held} charger(s) held a key in this hybrid run, but the server never "
            "checked a key after boot. The server is not running Contract 7's boot "
            "check, so E1's secure-ready time is missing and E2 uses the boot-only rule.",
        ))
    return issues


def _migration_issues(e3: dict[str, Any]) -> list[dict[str, str]]:
    """
    PHASE C6.1 — checks on a run that contains a migration.

    These protect the claims the integration session makes: that chargers
    were AUTHENTICATED (not just handed a key), that the controller's
    numbers are consistent, and that every charger agrees with the server.
    """
    issues: list[dict[str, str]] = []

    for f in e3.get("failures") or []:
        issues.append(_issue(
            "fail", "migration_failed",
            f"The migration controller crashed at {f.get('at_s')} s "
            f"({f.get('error') or 'no error text'}). The migration did not finish; "
            "its numbers are not a result.",
        ))

    if e3.get("controller_sum_violations"):
        issues.append(_issue(
            "fail", "migration_counts_do_not_add_up",
            f"In {e3['controller_sum_violations']} fleet snapshot(s) the controller's "
            "pending + upgrading + upgraded + rolled back + incompatible did not equal "
            "its total. That is a counting bug (Track B); E3's totals cannot be trusted.",
        ))

    verification = e3.get("verification")
    if verification == "key installed (not authenticated)":
        issues.append(_issue(
            "warn", "key_installed_not_authenticated",
            "Chargers were migrated but no post-quantum key check (pq_auth) was run: "
            "the server was not wired to challenge them. Report these as "
            "'key installed', NOT 'authenticated'.",
        ))
    elif verification == "partly authenticated":
        issues.append(_issue(
            "warn", "partly_authenticated",
            "Some chargers counted as migrated have no passed key check. "
            "Report only the checked ones as authenticated.",
        ))

    no_key = (e3.get("agent_view") or {}).get("migrated_but_no_key") or []
    if no_key:
        issues.append(_issue(
            "warn", "migrated_but_charger_has_no_key",
            f"{len(no_key)} charger(s) the server calls migrated report holding no key "
            f"themselves (e.g. {', '.join(no_key[:3])}). The two sides disagree.",
        ))

    if not e3.get("snapshots_available"):
        issues.append(_issue(
            "warn", "no_fleet_snapshots",
            "This migration run was recorded without --watch-fleet, so there is no "
            "second-by-second state timeline and no per-charger final state.",
        ))

    if (e3.get("deferred") or {}).get("count"):
        issues.append(_issue(
            "info", "chargers_deferred",
            f"{e3['deferred']['count']} charger(s) were offline when their wave ran and "
            "were skipped (not attempted, not failed).",
        ))
    return issues

