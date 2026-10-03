"""
STEP 1 of the analysis — COLLECT: read the two diaries and split them into runs.

Track C (analysis). Phase C6.

--------------------------------------------------------------------
IN PLAIN WORDS

Two programs keep a diary while a test runs:

    the SERVER diary   logs/events.jsonl
                       written by the CSMS (Track A, Contract 3). One line per
                       thing the server saw: a charger connected, a meter
                       reading arrived, the server restarted.

    the TESTER diary   logs/<experiment>_n<N>_<mode>.jsonl
                       written by our load generator (Track C, Phase C5). One
                       line per thing the tester did: started a charger, the
                       charger finished, the server was killed on purpose.

This file only READS them. It never writes to either, and it never changes
a single value -- every later step works from exactly what was recorded.

--------------------------------------------------------------------
WHY WE PARSE THE SERVER DIARY OURSELVES INSTEAD OF csms.events.read_events

read_events() rebuilds each line with `Event(**fields)`. If Track A ever adds
a new top-level field, EVERY line then fails that constructor and is skipped
silently -- the analysis would report an empty run with no error at all.
Here each line is plain JSON, unknown fields are kept and COUNTED, and the
trust check (check.py) reports them. Same file, same field names, no silent
loss. The event-type strings still come from Track A's own EventType enum.

A half-written last line (a run killed mid-write, which E2 does on purpose)
is counted and skipped, exactly as read_events does.

--------------------------------------------------------------------
PHASE C6.1 (A+B+C integration) — three additions

1. SEVERAL SERVER DIARIES. The integration runbook gives every run its own
   server diary (logs/s1_events.jsonl, logs/stage6_events.jsonl, ...). All
   of them are read and merged. A line that appears in two files (a copied
   backup, say) is counted ONCE: duplicates are recognised by their full
   content and dropped, and the number dropped is reported.

2. FILES ARE RECOGNISED BY WHAT IS INSIDE, NOT ONLY BY NAME. A server diary
   has `event_type` + `timestamp` + `monotonic_ns`; a tester diary has `ts` +
   `elapsed_ms`. A server file that happened to be named like a tester file
   (e1_n50_events.jsonl) is therefore never read as one, and vice versa.

3. THE CHARGER ID INSIDE THE PAYLOAD. Track B's orchestrator writes its
   per-charger lines (`connection_attempt` with transition "pq_auth", and
   `station_deferred`) through Track A's emitter, which leaves the top-level
   `station_id` empty and puts the charger in `payload.station`; the
   result goes in `payload.result`, with top-level `outcome` empty.
   (Confirmed in idmanager/orchestrator.py and csms/migration.py on main
   8a5922a.) So, in this program's in-memory copy only:
       station_id empty and payload.station present -> station_id = payload.station
       outcome empty and payload.result present     -> outcome    = payload.result
   and the line is marked `_normalised: True`. The diary file is never
   changed. If Track A later copies these up themselves, nothing here
   changes: a filled top-level field is always left as it is.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# The Contract 3 top-level fields, as Track A's Event dataclass defines them.
# Anything else at the top level means the schema changed under us.
SERVER_FIELDS = frozenset({
    "timestamp", "monotonic_ns", "event_type", "run_id", "crypto_mode",
    "station_id", "handshake_ms", "bytes_tx", "bytes_rx", "outcome", "payload",
})

# The harness file name pattern agreed with Track A: <experiment>_n<N>_<mode>.jsonl
HARNESS_GLOB = "*_n*_*.jsonl"


def to_epoch(iso: str | None) -> float | None:
    """ISO-8601 text -> seconds since 1970 (a plain number we can subtract)."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# =====================================================================
# THE SERVER DIARY
# =====================================================================


@dataclass
class ServerDiary:
    """Everything read from the server diaries, plus how cleanly it read."""

    path: str
    """The file read, or a comma-separated list when several were merged."""

    events: list[dict[str, Any]] = field(default_factory=list)
    """Every parsed line, in time order, each with an added `_t` (epoch s)."""

    lines_total: int = 0
    lines_unreadable: int = 0
    last_line_unreadable: bool = False
    unknown_fields: dict[str, int] = field(default_factory=dict)
    found: bool = True

    paths: list[str] = field(default_factory=list)
    """Every server diary that was read (C6.1)."""

    duplicates_dropped: int = 0
    """Lines seen in more than one file and counted once (C6.1)."""

    normalised: int = 0
    """Lines whose charger id / result were taken from the payload (C6.1)."""

    def by_run(self) -> dict[str, list[dict[str, Any]]]:
        runs: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ev in self.events:
            runs[ev.get("run_id") or ""].append(ev)
        return dict(runs)


def _normalise(obj: dict[str, Any]) -> bool:
    """
    Fill an empty top-level station_id / outcome from the payload (see the
    module docstring, point 3). Returns True when anything was filled.
    """
    payload = obj.get("payload") or {}
    changed = False
    station = payload.get("station")
    if not obj.get("station_id") and isinstance(station, str) and station:
        obj["station_id"] = station
        changed = True
    result = payload.get("result")
    if not obj.get("outcome") and isinstance(result, str) and result:
        obj["outcome"] = result
        changed = True
    if changed:
        obj["_normalised"] = True
    return changed


def _dedupe_key(raw_line: str) -> str:
    """The line itself: two lines are the same event only if identical."""
    return raw_line


def read_server_diary(path: str | Path) -> ServerDiary:
    """One server diary. Kept for callers that have exactly one file."""
    return read_server_diaries([path])


def read_server_diaries(paths: Iterable[str | Path]) -> ServerDiary:
    """
    Read and merge every given server diary.

    `found` is False only when NONE of the files exists. Lines appearing in
    more than one file are kept once. The result is in time order.
    """
    paths = [Path(p) for p in paths]
    seen_paths: list[str] = []
    for p in paths:
        if str(p) not in seen_paths:
            seen_paths.append(str(p))
    paths = [Path(p) for p in seen_paths]

    diary = ServerDiary(path=", ".join(seen_paths), paths=seen_paths)
    existing = [p for p in paths if p.exists()]
    if not existing:
        diary.found = False
        return diary

    unknown: dict[str, int] = defaultdict(int)
    seen_lines: set[str] = set()
    last_bad = False
    for path in existing:
        last_bad = _read_one_server_file(path, diary, unknown, seen_lines)
    diary.last_line_unreadable = last_bad
    diary.unknown_fields = dict(unknown)
    diary.events.sort(key=lambda e: (e["_t"] is None, e["_t"] or 0.0))
    return diary


def _read_one_server_file(path: Path, diary: ServerDiary,
                          unknown: dict[str, int], seen_lines: set[str]) -> bool:
    """Append one file's lines to `diary`. Returns whether its last line was bad."""

    last_bad = False
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            diary.lines_total += 1
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict) or "event_type" not in obj:
                    raise ValueError("not an event object")
            except (json.JSONDecodeError, ValueError):
                diary.lines_unreadable += 1
                last_bad = True
                continue
            last_bad = False
            key = _dedupe_key(line)
            if key in seen_lines:
                diary.duplicates_dropped += 1
                continue
            seen_lines.add(key)
            for field_name in obj.keys() - SERVER_FIELDS:
                unknown[field_name] += 1
            obj["_t"] = to_epoch(obj.get("timestamp"))
            if not isinstance(obj.get("payload"), dict):
                obj["payload"] = {}
            if _normalise(obj):
                diary.normalised += 1
            diary.events.append(obj)
    return last_bad


# =====================================================================
# THE TESTER (HARNESS) DIARY
# =====================================================================


@dataclass
class HarnessRun:
    """One run of the load generator, cut out of its (append-only) file."""

    path: str
    run_id: str
    experiment: str = ""
    n_stations: int = 0
    crypto_mode: str = ""
    tls: bool = False
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def started_at(self) -> float | None:
        for r in self.rows:
            if r.get("event_type") == "run_started":
                return r.get("_t")
        return self.rows[0].get("_t") if self.rows else None

    @property
    def finished_at(self) -> float | None:
        for r in reversed(self.rows):
            if r.get("event_type") == "run_finished":
                return r.get("_t")
        return None

    @property
    def last_seen_at(self) -> float | None:
        return self.rows[-1].get("_t") if self.rows else None

    @property
    def completed(self) -> bool:
        """False when the run was killed before writing its totals row."""
        return self.finished_at is not None

    def of_type(self, *event_types: str) -> list[dict[str, Any]]:
        wanted = set(event_types)
        return [r for r in self.rows if r.get("event_type") in wanted]

    def connect_times(self) -> dict[str, list[float]]:
        """
        PHASE C6.2. Each charger's connection times (ms, oldest first; the
        first is E1's handshake, later ones are reconnections).

        Two sources in the tester diary:
          * station_connected -- one line per connection, written the moment
            it opens (C6.2). Survives a run stopped with Ctrl-C.
          * station_finished / station_crashed -- the full connect_ms list,
            written when the charger stops (C5.1). Missing if the tester
            was stopped before chargers reported.
        Per charger the source with MORE entries wins (a tie goes to the
        final row), so old diaries read exactly as before and an early stop
        loses nothing that reached the disk. A charger appears only if one
        of the two sources mentions it.
        """
        live: dict[str, list[tuple[int, int, float]]] = {}
        for i, r in enumerate(self.of_type("station_connected")):
            sid, ms = r.get("station_id"), r.get("connect_ms")
            if not sid or ms is None:
                continue
            live.setdefault(sid, []).append((int(r.get("connection") or 0), i, float(ms)))
        final: dict[str, list[float]] = {}
        for r in self.of_type("station_finished", "station_crashed"):
            sid, times = r.get("station_id"), r.get("connect_ms")
            if not sid or times is None:
                continue
            final[sid] = [float(t) for t in times if t is not None]
        out: dict[str, list[float]] = {}
        for sid in sorted(set(live) | set(final)):
            from_live = [ms for _, _, ms in sorted(live.get(sid, []))]
            from_final = final.get(sid)
            out[sid] = (from_final if from_final is not None
                        and len(from_final) >= len(from_live) else from_live)
        return out

    @property
    def station_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for r in self.of_type("station_spawned"):
            if r.get("station_id"):
                seen[r["station_id"]] = None
        return list(seen)

    @property
    def run_config(self) -> dict[str, Any]:
        for r in self.of_type("run_started"):
            return r
        return {}


@dataclass
class HarnessFile:
    path: str
    runs: list[HarnessRun] = field(default_factory=list)
    lines_total: int = 0
    lines_unreadable: int = 0


def read_harness_file(path: str | Path) -> HarnessFile:
    """
    Read one tester diary. The file is APPEND-ONLY: running the same
    experiment twice adds a second run to the same file, and every line
    carries its run_id, so the runs are separated here by that id.
    """
    path = Path(path)
    hf = HarnessFile(path=str(path))
    runs: dict[str, HarnessRun] = {}

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            hf.lines_total += 1
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                hf.lines_unreadable += 1
                continue
            rid = row.get("run_id") or ""
            run = runs.get(rid)
            if run is None:
                run = HarnessRun(
                    path=str(path),
                    run_id=rid,
                    experiment=str(row.get("experiment", "")),
                    n_stations=int(row.get("n_stations") or 0),
                    crypto_mode=str(row.get("crypto_mode", "")),
                    tls=bool(row.get("tls", False)),
                )
                runs[rid] = run
            row["_t"] = to_epoch(row.get("ts"))
            run.rows.append(row)

    hf.runs = list(runs.values())
    return hf


SERVER_GLOB = "*events*.jsonl"


def sniff_kind(path: str | Path) -> str:
    """
    "server", "harness" or "unknown", from the first readable line.

    Server lines (Contract 3) carry event_type + timestamp + monotonic_ns;
    tester lines (harness/timing_log.py) carry ts + elapsed_ms. Reading the
    content means a file's NAME can never make it be read as the wrong kind.
    """
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(50):
                raw = fh.readline()
                if not raw:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if "ts" in obj and "elapsed_ms" in obj:
                    return "harness"
                if "event_type" in obj and "timestamp" in obj and "monotonic_ns" in obj:
                    return "server"
    except OSError:
        return "unknown"
    return "unknown"


def find_harness_files(log_dir: str | Path) -> list[Path]:
    """Every tester diary in the log folder: agreed name pattern AND content."""
    log_dir = Path(log_dir)
    if not log_dir.is_dir():
        return []
    return sorted(
        p for p in log_dir.glob(HARNESS_GLOB)
        if p.is_file() and sniff_kind(p) == "harness"
    )


def find_server_diaries(log_dir: str | Path) -> list[Path]:
    """
    Every server diary in the log folder: `*events*.jsonl` whose content is
    Contract 3. Covers the default logs/events.jsonl and the runbook's
    per-run names (s1_events.jsonl, stage6_events.jsonl, ...).
    """
    log_dir = Path(log_dir)
    if not log_dir.is_dir():
        return []
    return sorted(
        p for p in log_dir.glob(SERVER_GLOB)
        if p.is_file() and sniff_kind(p) == "server"
    )


def all_harness_runs(files: Iterable[HarnessFile]) -> list[HarnessRun]:
    return [run for hf in files for run in hf.runs]
