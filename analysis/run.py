"""
THE ONE COMMAND — run every analysis step, in order.

Track C (analysis). Phase C6.

    python -m analysis.run

then open analysis/output/report.html in any browser.

--------------------------------------------------------------------
WHAT HAPPENS, IN ORDER

    1. COLLECT  read the server diary and every tester diary        collect.py
    2. CHECK    is the server diary sound?                           check.py
    3. MATCH    for each tester run, find the server's lines for it  match.py
    4. MEASURE  overview + E1 + E2 + E3 + E5 per run;                measures/
                E4 (sizes) and E6/hardware (external chargers) once;
                then the cross-run comparisons
       CHECK    can each run's numbers be trusted?                   check.py
    5. SAVE     newest run per slot, one results file                store.py
    6. SHOW     results page written next to it                      web/

It runs automatically at the end of every load-generator run (unless
--no-analyse is given there), and can be run by hand at any time -- it
always rebuilds everything from the diaries, so running it twice gives the
same answer.

--------------------------------------------------------------------
SAFETY

Read-only on every log file. Writes only inside --out (default
analysis/output/, ignored by git). Never writes the server's event log.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis import check, collect, match, store
from analysis.measures import (
    compare, e1_handshake, e2_storm, e3_migration, e4_sizes, e5_security, e6_nodes, machine,
    overview,
)

LOG = logging.getLogger("analysis")

DEFAULT_LOG_DIR = "logs"
DEFAULT_EVENTS = "logs/events.jsonl"
"""Kept for reference: since C6.1 the default is every server diary in --logs."""
DEFAULT_OUT = "analysis/output"


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def analyse_run(matched: match.MatchedRun) -> dict[str, Any]:
    """Every per-run measure, plus the trust check, as one slot."""
    h = matched.harness
    ov = overview.measure(matched)
    e1 = e1_handshake.measure(matched)
    e3 = e3_migration.measure(matched)
    mach = machine.measure(matched)         # C-P6 (S1); None without --watch-machine
    trust = check.check_run(matched, ov, e1, e3, machine=mach)
    return {
        "key": store.slot_key(h.experiment, h.n_stations, h.crypto_mode, h.tls),
        "experiment": h.experiment,
        "n_stations": h.n_stations,
        "crypto_mode": h.crypto_mode,
        "tls": h.tls,
        "harness_run_id": h.run_id,
        "harness_log": h.path,
        "server_run_ids": matched.server_run_ids,
        "started_at": _iso(h.started_at),
        "started_at_epoch": h.started_at,
        "finished_at": _iso(h.finished_at),
        "wall_s": (h.finished_at - h.started_at) if h.finished_at and h.started_at else None,
        "completed": h.completed,
        "trust": trust,
        "overview": ov,
        "e1": e1,
        "e2": e2_storm.measure(matched),
        "e3": e3,
        "e5": e5_security.measure(matched),
        "machine": mach,
    }


def server_diary_paths(events: str | Path | list[str | Path] | None,
                       log_dir: str | Path) -> list[Path]:
    """
    Which server diaries to read (C6.1).

    Given explicitly (one path or a list) -> exactly those.
    Not given -> every server diary found in the log folder
    (`*events*.jsonl` whose content is Contract 3), which covers both the
    default logs/events.jsonl and the runbook's per-run files. If none is
    found, the default path is returned so the "not found" message names it.
    """
    if events:
        if isinstance(events, (str, Path)):
            return [Path(events)]
        return [Path(e) for e in events]
    found = collect.find_server_diaries(log_dir)
    return found or [Path(log_dir) / "events.jsonl"]


def build(
    events_path: str | Path | list[str | Path] | None = None,
    log_dir: str | Path = DEFAULT_LOG_DIR,
    harness_paths: list[str | Path] | None = None,
) -> dict[str, Any]:
    server_paths = server_diary_paths(events_path, log_dir)
    diary = collect.read_server_diaries(server_paths)
    paths = [Path(p) for p in harness_paths] if harness_paths else collect.find_harness_files(log_dir)
    files = [collect.read_harness_file(p) for p in paths]
    runs = collect.all_harness_runs(files)

    slots = [analyse_run(match.match_run(r, diary)) for r in runs if r.station_ids]
    kept = store.latest_per_slot(slots)

    every_tester_station = {sid for r in runs for sid in r.station_ids}

    return {
        "format_version": store.FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "server_diary": diary.path,
            "server_diaries": diary.paths,
            "server_diary_lines": diary.lines_total,
            "server_duplicates_dropped": diary.duplicates_dropped,
            "server_lines_normalised": diary.normalised,
            "tester_diaries": [
                {"path": hf.path, "runs": len(hf.runs), "lines": hf.lines_total,
                 "unreadable": hf.lines_unreadable}
                for hf in files
            ],
            "runs_found": len(slots),
            "runs_kept": len(kept),
        },
        "diary_check": check.check_diary(diary),
        "slots": {s["key"]: s for s in kept},
        "slot_order": [s["key"] for s in kept],
        "comparisons": compare.measure(kept),
        "e4": e4_sizes.measure(),
        "external_nodes": e6_nodes.measure(diary, every_tester_station),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m analysis.run",
        description="Turn the server and tester diaries into results (Track C, C6).",
    )
    parser.add_argument(
        "--events", action="append", default=None,
        help="a server diary (Contract 3); repeatable. Default: every "
             "*events*.jsonl server diary in --logs",
    )
    parser.add_argument("--logs", default=DEFAULT_LOG_DIR, help="folder holding tester diaries")
    parser.add_argument("--harness-log", action="append", default=None,
                        help="analyse only this tester diary (repeatable)")
    parser.add_argument("--out", default=DEFAULT_OUT, help="where results and the page go")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(levelname)s analysis: %(message)s")

    results = build(args.events, args.logs, args.harness_log)
    path = store.write(results, args.out)

    if not args.quiet:
        print(summary(results))
        print(f"\nresults: {path}")
        print(f"page:    {Path(args.out) / 'report.html'}")
    return 0 if results["diary_check"]["status"] != "fail" else 1


def summary(results: dict[str, Any]) -> str:
    diaries = results["sources"].get("server_diaries") or [results["sources"].get("server_diary")]
    lines = [f"server diaries ({len(diaries)}): {results['diary_check']['status']}"]
    for d in diaries:
        lines.append(f"  - {d}")
    for i in results["diary_check"]["issues"]:
        lines.append(f"  [{i['level']}] {i['message']}")
    lines.append(f"runs: {results['sources']['runs_found']} found, "
                 f"{results['sources']['runs_kept']} kept (newest per slot)")
    for key in results["slot_order"]:
        s = results["slots"][key]
        e1 = (s.get("e1") or {}).get("station_connect_ms") or {}
        med = f"{e1['median']:.1f} ms" if e1.get("n") else "n/a"
        rd = (s.get("e1") or {}).get("ready_ms") or {}
        if rd.get("n"):
            med += f"  ready median={rd['median']:.1f} ms ({s['e1'].get('ready_basis')})"
        e2 = s.get("e2")
        storm = f"  T95={e2['t95_s']:.2f}s" if e2 and e2.get("t95_s") is not None else ""
        e3 = s.get("e3")
        mig = ""
        if e3:
            fc = e3["final_counts"]
            mig = (f"  migration: {fc['migrated']} migrated / {fc['rolled_back']} rolled back / "
                   f"{fc['incompatible']} incompatible / {fc['pending']} pending"
                   f" -- {e3['verification']}, {e3['pq_checks']['passed']} key check(s) passed")
            if (e3.get("halt") or {}).get("shown"):
                mig += f"; HALTED after wave {e3['halt']['rolled_back_wave']}"
            if e3.get("rotation"):
                mig += (f"; rotation: {e3['rotation']['completed']} rotated / "
                        f"{e3['rotation']['failed']} failed (old key kept)")
        m = s.get("machine")
        mach = ""
        if m:
            mach = (f"  machine: CPU p95 {m['cpu_pct'].get('p95') or 0:.0f}%, loop lag p95 "
                    f"{m['loop_lag_ms'].get('p95') or 0:.1f} ms"
                    + (" -- SATURATED" if m.get("saturated") else ""))
        lines.append(f"  {key:<32} trust={s['trust']['status']:<5} connect median={med}{storm}{mig}{mach}")
    if results["external_nodes"]:
        lines.append("chargers not started by the tester: "
                     + ", ".join(n["station_id"] for n in results["external_nodes"]))
    return "\n".join(lines)


def analyse_after_run(harness_log: str | Path,
                      events: str | Path | list[str | Path] | None = None,
                      out: str | Path = DEFAULT_OUT) -> None:
    """
    Called by the load generator when a run ends. Rebuilds everything (the
    new run replaces its slot) and NEVER raises: the run's data is already
    safe on disk, so a problem here must not turn a good run into a failed one.

    Server diaries: the ones passed with the load generator's --events-log,
    plus every server diary in the tester diary's folder (C6.1). The
    explicit ones are what make a run whose CSMS logged somewhere else
    (e.g. logs/s1_events.jsonl named by the runbook) analyse correctly.
    """
    try:
        log_dir = Path(harness_log).parent
        explicit = [events] if isinstance(events, (str, Path)) else list(events or [])
        paths = [Path(p) for p in explicit] + collect.find_server_diaries(log_dir)
        results = build(paths or None, log_dir)
        store.write(results, out)
        print(f"\nanalysis updated: {Path(out) / 'report.html'}")
    except Exception as exc:  # noqa: BLE001
        print(f"\nanalysis skipped ({type(exc).__name__}: {exc}); "
              "run `python -m analysis.run` by hand to see why.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
