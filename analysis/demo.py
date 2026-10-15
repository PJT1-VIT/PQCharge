"""
DEMO RUNS — one complete demo run (demo1, demo2, ...) analysed as ONE unit.

Track C (analysis). Runbook v1.3, 2026-10-14.

--------------------------------------------------------------------
IN PLAIN WORDS

A demo run is everything one rehearsal (or the real demo) wrote, named by
its run name, e.g. demo3 (the runbook's $RUN):

    main server   logs/demo3_events.jsonl
    halt server   logs/demo3_halt_events.jsonl                 (V6, if run)
    main fleet    logs/demo3_n44_hybrid.jsonl,
                  logs/demo3_legacy_n4_hybrid.jsonl, ...        (experiment demo3, demo3_*)
    halt fleet    logs/halt_demo3_n20_hybrid.jsonl,
                  logs/halt_demo3_n25_hybrid.jsonl              (experiment halt_demo3, halt_demo3_*)

`python -m analysis.run --run demo3`:

    1. finds exactly those files (nothing from other runs: no sw1, no s1);
    2. copies them to evidence/demos/demo3/ -- only *.jsonl, never the
       server's *.log or *.db (L04) -- replacing what an earlier analysis of
       demo3 put there;
    3. analyses the MAIN server with the main fleet, and the HALT server with
       the halt fleet, SEPARATELY. Both fleets use the ids CP0001-CP0050 at
       the same time; analysed together, their lines would mix (this is why
       the v1.1 runbook called demo logs "not evidence");
    4. saves the result as analysis/output/runs/demo3.json -- replacing the
       previous analysis of demo3, never adding a new file;
    5. rebuilds the page (store.rebuild_bundle): demo3 is shown first, the
       other analysed demo runs can be picked, plus "All runs combined" and
       "Experiments" (the step-13 runs).

If logs/ has no file of that run (another laptop, after `git pull`), the
copy in evidence/demos/<name>/ is analysed instead.

Every number comes from the existing measures (overview, E1-E5) on each
part; this file only picks files, merges the two parts into one view, and
adds the per-run summary row and the raw samples the combined view pools.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis import collect, stats

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
"""A run name: letters, digits and '-'. No '_': the files are cut at '_'
(demo3_legacy_n4 belongs to demo3), so demo_3 would be ambiguous."""

DEFAULT_LOGS = "logs"
DEFAULT_EVIDENCE = "evidence/demos"
EVIDENCE_SUFFIXES = (".jsonl", ".json")
"""The only files ever written to (or removed from) an evidence folder."""

KEY_CHECK_TRIGGERS = ("migration", "boot", "rotation")
HIST_BIN_MS = 10.0


class RunNotFound(FileNotFoundError):
    """No file of this run in logs/ nor in its evidence folder."""


@dataclass
class RunFiles:
    name: str
    folder: Path
    main_events: Path | None = None
    halt_events: Path | None = None
    main_testers: list[Path] = field(default_factory=list)
    halt_testers: list[Path] = field(default_factory=list)

    def all(self) -> list[Path]:
        return [p for p in [self.main_events, self.halt_events, *self.main_testers, *self.halt_testers]
                if p is not None]

    def empty(self) -> bool:
        return not self.all()


def check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise ValueError(f"run name {name!r}: use letters, digits and '-' only (e.g. demo3)")
    return name


def _experiment_of(path: Path) -> str | None:
    """The experiment name a tester diary was written for (its first readable line)."""
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    return json.loads(raw).get("experiment")
                except (ValueError, AttributeError):
                    continue
    except OSError:
        return None
    return None


def _belongs(experiment: str | None, prefix: str) -> bool:
    return bool(experiment) and (experiment == prefix or experiment.startswith(prefix + "_"))


def find_run_files(name: str, folder: str | Path) -> RunFiles:
    """Every file of run `name` in `folder` (see the module notes)."""
    folder = Path(folder)
    files = RunFiles(name=name, folder=folder)
    if not folder.is_dir():
        return files
    main_ev, halt_ev = folder / f"{name}_events.jsonl", folder / f"{name}_halt_events.jsonl"
    if main_ev.is_file():
        files.main_events = main_ev
    if halt_ev.is_file():
        files.halt_events = halt_ev
    for p in sorted(folder.glob("*.jsonl")):
        if p in (main_ev, halt_ev):
            continue
        if not (p.name.startswith(name + "_") or p.name.startswith(f"halt_{name}_")):
            continue
        if collect.sniff_kind(p) != "harness":
            continue
        exp = _experiment_of(p)
        if _belongs(exp, f"halt_{name}"):
            files.halt_testers.append(p)
        elif _belongs(exp, name):
            files.main_testers.append(p)
    return files


def locate(name: str, logs: str | Path = DEFAULT_LOGS,
           evidence_root: str | Path = DEFAULT_EVIDENCE) -> tuple[RunFiles, str]:
    """The run's files and where they came from: "logs" or "evidence"."""
    check_name(name)
    in_logs = find_run_files(name, logs)
    if not in_logs.empty():
        return in_logs, "logs"
    in_evidence = find_run_files(name, Path(evidence_root) / name)
    if not in_evidence.empty():
        return in_evidence, "evidence"
    raise RunNotFound(
        f"no file of run {name!r}: looked for {name}_events.jsonl, {name}_halt_events.jsonl, "
        f"{name}_*_n*_*.jsonl and halt_{name}_*.jsonl in {Path(logs)} and in "
        f"{Path(evidence_root) / name}")


def save_evidence(files: RunFiles, evidence_root: str | Path = DEFAULT_EVIDENCE) -> RunFiles:
    """
    Copy the run's diaries to evidence_root/<name>/, replacing the previous
    copy (only *.jsonl / *.json are ever removed there). Returns the copied set.
    """
    dest = Path(evidence_root) / files.name
    if dest.resolve() == files.folder.resolve():
        return files
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.iterdir():
        if old.is_file() and old.suffix in EVIDENCE_SUFFIXES:
            old.unlink()
    for src in files.all():
        if src.suffix in EVIDENCE_SUFFIXES:          # never a .log or .db (L04)
            shutil.copyfile(src, dest / src.name)
    return find_run_files(files.name, dest)


# --------------------------------------------------------------------- samples

def samples(files: RunFiles) -> dict[str, Any]:
    """
    The raw numbers the combined view pools: every charger's FIRST connect
    time (ms, from the tester diaries) and every passed key check (ms, from
    the server diaries, by trigger), plus how many migration runs the main
    server logged (a second Start re-enrols everyone, L46).
    """
    connect: dict[tuple[str, str], float] = {}
    for path in files.main_testers + files.halt_testers:
        for run in collect.all_harness_runs([collect.read_harness_file(path)]):
            for sid, times in run.connect_times().items():
                if times:
                    connect.setdefault((run.run_id, sid), float(times[0]))
    checks: dict[str, list[float]] = {t: [] for t in KEY_CHECK_TRIGGERS}
    migration_runs = rotation_runs = 0
    for part, path in (("main", files.main_events), ("halt", files.halt_events)):
        if path is None:
            continue
        for ev in collect.read_server_diaries([path]).events:
            p = ev.get("payload") or {}
            if part == "main" and ev.get("event_type") == "migration_started":
                if p.get("kind") == "rotation":
                    rotation_runs += 1
                else:
                    migration_runs += 1
            if ev.get("event_type") != "connection_attempt" or p.get("transition") != "pq_auth":
                continue
            ok = (ev.get("outcome") or p.get("result")) == "success"
            d = p.get("duration_ms")
            if ok and isinstance(d, (int, float)):
                checks.setdefault(p.get("trigger") or "migration", []).append(float(d))
    return {"connect_ms": [round(v, 3) for v in connect.values()],
            "key_check_ms": {k: [round(v, 3) for v in vs] for k, vs in checks.items()},
            "migration_runs": migration_runs, "rotation_runs": rotation_runs}


# ----------------------------------------------------------------- one view

_STATUS_RANK = {"pass": 0, "info": 0, "warn": 1, "fail": 2}


def _worst(*statuses: str | None) -> str:
    present = [s for s in statuses if s]
    return max(present, key=lambda s: _STATUS_RANK.get(s, 1)) if present else "pass"


def merge_views(main: dict[str, Any] | None, halt: dict[str, Any] | None) -> dict[str, Any]:
    """Main + halt results in the page's usual shape (slots, comparisons, ...)."""
    from analysis.measures import compare, e4_sizes

    parts = [(label, res) for label, res in (("main", main), ("halt", halt)) if res]
    slots: dict[str, Any] = {}
    order: list[str] = []
    issues: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    srcs: dict[str, Any] = {"server_diaries": [], "tester_diaries": [], "server_diary_lines": 0,
                            "tester_runs": 0, "runs_joined": 0, "runs_found": 0, "runs_kept": 0,
                            "server_duplicates_dropped": 0, "server_lines_normalised": 0}
    statuses = []
    for label, res in parts:
        for k in res["slot_order"]:
            slot = dict(res["slots"][k])
            slot["server"] = label
            slots[k] = slot
            order.append(k)
        dc = res.get("diary_check") or {}
        statuses.append(dc.get("status"))
        name = "Main server" if label == "main" else "Halt server"
        issues += [dict(i, message=f"{name}: {i.get('message', '')}") for i in dc.get("issues", [])]
        nodes += [dict(n, server=label) for n in res.get("external_nodes", [])]
        s = res.get("sources", {})
        srcs["server_diaries"] += list(s.get("server_diaries") or [])
        srcs["tester_diaries"] += list(s.get("tester_diaries") or [])
        for k in ("server_diary_lines", "tester_runs", "runs_joined", "runs_found", "runs_kept",
                  "server_duplicates_dropped", "server_lines_normalised"):
            srcs[k] += s.get(k) or 0
    srcs["server_diary"] = ", ".join(srcs["server_diaries"])
    return {
        "slots": slots,
        "slot_order": order,
        "comparisons": compare.measure([slots[k] for k in order]),
        "e4": e4_sizes.measure(),
        "external_nodes": nodes,
        "diary_check": {"status": _worst(*statuses), "issues": issues},
        "sources": srcs,
    }


def _first_slot(view: dict[str, Any], server: str) -> dict[str, Any] | None:
    cands = [view["slots"][k] for k in view["slot_order"] if view["slots"][k].get("server") == server]
    return max(cands, key=lambda s: s.get("n_stations") or 0) if cands else None


def _median(d: dict[str, Any] | None) -> float | None:
    return (d or {}).get("median") if (d or {}).get("n") else None


def summary_row(name: str, view: dict[str, Any], smp: dict[str, Any]) -> dict[str, Any]:
    """One row per demo run: the variants V1-V7 in numbers (all from the measures)."""
    main = _first_slot(view, "main")
    halt = _first_slot(view, "halt")
    row: dict[str, Any] = {"name": name, "trust": _worst(*(view["slots"][k]["trust"]["status"]
                                                          for k in view["slot_order"]),
                                                        view["diary_check"]["status"])}
    starts = [view["slots"][k].get("started_at_epoch") for k in view["slot_order"]]
    starts = [s for s in starts if s]
    row["started_at"] = (datetime.fromtimestamp(min(starts), tz=timezone.utc).isoformat()
                         if starts else None)
    outside_main = [n["station_id"] for n in view["external_nodes"] if n.get("server") == "main"]
    if main:
        e1, e2, e3 = main.get("e1") or {}, main.get("e2"), main.get("e3")
        row["chargers"] = (main.get("n_stations") or 0) + len(outside_main)
        row["tester_chargers"] = main.get("n_stations")
        row["outside_chargers"] = outside_main
        row["connect_median_ms"] = _median(e1.get("station_connect_ms"))
        row["ready_median_ms"] = _median(e1.get("ready_ms"))
        if e3:
            fc = e3.get("final_counts") or {}
            row["migration"] = {
                "hybrid": fc.get("migrated"), "legacy": fc.get("incompatible"),
                "rolled_back": fc.get("rolled_back"), "pending": fc.get("pending"),
                "eligible": (e3.get("fleet_size") or 0) - (fc.get("incompatible") or 0),
                "server_s": e3.get("duration_s"), "runs": smp.get("migration_runs"),
                "key_check_median_ms": _median((e3.get("pq_checks") or {}).get("round_trip_ms")),
                "sessions_disturbed": (e3.get("charging") or {}).get("sessions_disturbed"),
                "sessions_running": (e3.get("charging") or {}).get("sessions_running_at_start"),
            }
            if e3.get("rotation"):
                r = e3["rotation"]
                row["rotation"] = {"rotated": r.get("completed"), "failed": r.get("failed"),
                                   "key_check_median_ms": _median((r.get("checks") or {}).get("round_trip_ms"))}
            if e3.get("boot_checks"):
                b = e3["boot_checks"]
                row["boot_checks"] = {"passed": b.get("passed"), "total": b.get("total"),
                                      "median_ms": _median(b.get("round_trip_ms"))}
        if e2:
            row["storm"] = {"t95_s": e2.get("t95_s"), "recovered": e2.get("recovered"),
                            "population": e2.get("population"), "rule": e2.get("recovery_rule")}
    h3 = (halt or {}).get("e3")
    if h3 and h3.get("fleet_size") and h3.get("phase"):
        fc = h3.get("final_counts") or {}
        row["halt"] = {"shown": bool((h3.get("halt") or {}).get("shown")),
                       "wave": (h3.get("halt") or {}).get("rolled_back_wave"),
                       "hybrid": fc.get("migrated"), "rolled_back": fc.get("rolled_back"),
                       "never_attempted": (h3.get("halt") or {}).get("never_attempted"),
                       "pending": fc.get("pending")}
    elif halt:                          # the halt fleet ran, but no migration on its server
        row["halt"] = {"shown": False, "not_started": True}
    attacks = {"tls_refused": 0, "identity_rejected": 0, "boot_rejected": 0, "cut_off": 0,
               "curtailment": "not_seen"}
    for k in view["slot_order"]:
        e5 = view["slots"][k].get("e5") or {}
        attacks["tls_refused"] += (e5.get("tls_refusals") or {}).get("total", 0)
        attacks["identity_rejected"] += e5.get("identity_rejected") or 0
        attacks["boot_rejected"] += ((e5.get("pq_by_trigger") or {}).get("boot") or {}).get("rejected", 0)
        attacks["cut_off"] += e5.get("pq_cut_off") or 0
        if (e5.get("verdicts") or {}).get("curtailment") == "done":
            attacks["curtailment"] = "done"
    row["attacks"] = attacks
    return row


def analyse(name: str, logs: str | Path = DEFAULT_LOGS,
            evidence_root: str | Path = DEFAULT_EVIDENCE, keep_evidence: bool = True) -> dict[str, Any]:
    """Analyse one demo run (see the module notes). Returns its view; saves nothing."""
    from analysis import run as arun

    files, source = locate(name, logs, evidence_root)
    if source == "logs" and keep_evidence:
        files = save_evidence(files, evidence_root)

    def part(events: Path | None, testers: list[Path]) -> dict[str, Any] | None:
        if not testers:                 # no tester run -> nothing to measure against
            return None
        return arun.build([events] if events else [files.folder / f"{name}__no_server_diary.jsonl"],
                          files.folder, testers)

    main = part(files.main_events, files.main_testers)
    halt = part(files.halt_events, files.halt_testers)
    if main is None and halt is None:
        raise RunNotFound(f"run {name!r} has no tester diary (load generator file), so nothing can be "
                          f"measured; found: {[p.name for p in files.all()]}")
    view = merge_views(main, halt)
    smp = samples(files)
    view.update({
        "kind": "demo",
        "name": name,
        "analysed_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "folder": str(files.folder),
        "files": [p.name for p in files.all()],
        "parts": {"main": main is not None, "halt": halt is not None,
                  "halt_server_log": files.halt_events is not None},
        "samples": smp,
    })
    view["summary"] = summary_row(name, view, smp)
    return view


# ------------------------------------------------------------------ combined

def _hist(values: list[float], bin_ms: float = HIST_BIN_MS) -> list[list[float]]:
    """[[bin start ms, count], ...] up to the 99th percentile (the tail is one last bin)."""
    if not values:
        return []
    top = stats.percentile(values, 99) or max(values)
    nbins = max(1, int(top // bin_ms) + 1)
    counts = [0] * (nbins + 1)
    for v in values:
        counts[min(int(v // bin_ms), nbins)] += 1
    return [[i * bin_ms, c] for i, c in enumerate(counts)]


def combine(views: list[dict[str, Any]]) -> dict[str, Any]:
    """'All runs combined': one row per run, plus every run's samples pooled."""
    rows = [v["summary"] for v in views]
    connect = [x for v in views for x in v.get("samples", {}).get("connect_ms", [])]
    checks = {t: [x for v in views for x in v.get("samples", {}).get("key_check_ms", {}).get(t, [])]
              for t in KEY_CHECK_TRIGGERS}
    per_run_checks = [{"name": v["name"],
                       **{t: stats.describe(v.get("samples", {}).get("key_check_ms", {}).get(t, []))
                          for t in KEY_CHECK_TRIGGERS}} for v in views]
    for r in per_run_checks:                            # keep the page light: no ECDFs per run
        for t in KEY_CHECK_TRIGGERS:
            r[t].pop("ecdf", None)
    totals = {"runs": len(views), "chargers_migrated": 0, "keys_rotated": 0, "tls_refused": 0,
              "identity_rejected": 0, "cut_off": 0, "sessions_disturbed": 0, "halts_shown": 0}
    for r in rows:
        totals["chargers_migrated"] += (r.get("migration") or {}).get("hybrid") or 0
        totals["keys_rotated"] += (r.get("rotation") or {}).get("rotated") or 0
        totals["sessions_disturbed"] += (r.get("migration") or {}).get("sessions_disturbed") or 0
        totals["halts_shown"] += 1 if (r.get("halt") or {}).get("shown") else 0
        for k in ("tls_refused", "identity_rejected", "cut_off"):
            totals[k] += (r.get("attacks") or {}).get(k) or 0
    return {
        "kind": "combined",
        "runs": rows,
        "totals": totals,
        "pooled": {
            "connect_ms": stats.describe(connect),
            "key_check_ms": {t: stats.describe(vs) for t, vs in checks.items()},
            "key_check_hist": {t: _hist(vs) for t, vs in checks.items()},
            "hist_bin_ms": HIST_BIN_MS,
        },
        "per_run_key_checks": per_run_checks,
    }
