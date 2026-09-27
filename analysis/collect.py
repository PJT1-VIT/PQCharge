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
    """Everything read from logs/events.jsonl, plus how cleanly it read."""

    path: str
    events: list[dict[str, Any]] = field(default_factory=list)
    """Every parsed line, in file order, each with an added `_t` (epoch s)."""

    lines_total: int = 0
    lines_unreadable: int = 0
    last_line_unreadable: bool = False
    unknown_fields: dict[str, int] = field(default_factory=dict)
    found: bool = True

    def by_run(self) -> dict[str, list[dict[str, Any]]]:
        runs: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ev in self.events:
            runs[ev.get("run_id") or ""].append(ev)
        return dict(runs)


def read_server_diary(path: str | Path) -> ServerDiary:
    path = Path(path)
    diary = ServerDiary(path=str(path))
    if not path.exists():
        diary.found = False
        return diary

    unknown: dict[str, int] = defaultdict(int)
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        last_bad = False
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
            for key in obj.keys() - SERVER_FIELDS:
                unknown[key] += 1
            obj["_t"] = to_epoch(obj.get("timestamp"))
            if not isinstance(obj.get("payload"), dict):
                obj["payload"] = {}
            diary.events.append(obj)
        diary.last_line_unreadable = last_bad

    diary.unknown_fields = dict(unknown)
    return diary


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


def find_harness_files(log_dir: str | Path) -> list[Path]:
    """Every tester diary in the log folder, by the agreed name pattern."""
    log_dir = Path(log_dir)
    if not log_dir.is_dir():
        return []
    return sorted(p for p in log_dir.glob(HARNESS_GLOB) if p.is_file())


def all_harness_runs(files: Iterable[HarnessFile]) -> list[HarnessRun]:
    return [run for hf in files for run in hf.runs]
