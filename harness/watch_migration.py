"""
A live console view of a fleet migration — what the panel watches.

Track C (harness). Phase C-P5 (limitations.md L02: "the migration must be
visibly obvious to the panel").

--------------------------------------------------------------------
IN PLAIN WORDS

Run it in its own window while a migration runs:

    python -m harness.watch_migration --url http://localhost:9000

Twice a second it asks the server (GET only, Contract 6) where the migration
is, and redraws one screen:

    - the phase in plain words (canary, waves, rolled back, ...)
    - the fleet: connected, charging, total power
    - a progress bar and the counts: upgraded / in progress / waiting /
      rolled back / incompatible
    - one line per wave: which chargers, how many upgraded or failed, result
    - a grid with one letter per charger, so a wave visibly sweeps across
    - the latest changes, e.g. "CP0036 in progress -> upgraded (key check
      passed)"

It stops by itself once it has watched a migration finish (completed,
rolled back or failed) and prints a final summary. Ctrl-C stops it at any
time, cleanly.

--------------------------------------------------------------------
WHERE THE NUMBERS COME FROM (nothing is computed twice)

    /api/fleet        Contract 6: every charger's connection_state and
                      migration_state, connected/charging counts, power.
                      Its `migration` field is Contract 4's MigrationStatus.
    /api/migration    only if /api/fleet has no `migration` field.

The counts and waves are the orchestrator's own (Track B); this file only
formats them. The "latest changes" are found by comparing one poll with the
previous one: a charger going in progress -> upgraded has passed its key
check (Track B marks it migrated only then); -> rolled back means it failed.
/api does not expose key_id or check times; the results page (C6) has those.

--------------------------------------------------------------------
START AND STOP

    server idle               "waiting for a migration to start"
    a migration already over  shown, and the view waits for a NEW one
    (same server, an earlier migration)  (a new migration_id)
    a migration ends          final screen + summary, exit code 0
    server not reachable      "not reachable, retrying"; keeps trying
    Ctrl-C                    stops cleanly, exit code 130

Plain ASCII only, so it reads the same in PowerShell 5, Windows Terminal and
a Linux terminal. The screen is redrawn in place with standard console codes
(switched on for Windows consoles at start); where that is not possible, or
with --no-clear, each update is printed below the previous one.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

STATE_LETTER = {
    "migrated": "U",
    "in_progress": "*",
    "pending": ".",
    "rolled_back": "R",
    "incompatible": "x",
}
OFFLINE_LETTER = "-"
STATE_WORDS = {
    "migrated": "upgraded",
    "in_progress": "in progress",
    "pending": "waiting",
    "rolled_back": "rolled back",
    "incompatible": "incompatible",
}
CHANGE_NOTE = {
    ("pending", "in_progress"): "upgrade started",
    ("in_progress", "migrated"): "key check passed",
    ("in_progress", "rolled_back"): "failed, rolled back",
    ("migrated", "rolled_back"): "wave rolled back",
    ("in_progress", "pending"): "skipped (offline)",
}
PHASE_WORDS = {
    "idle": "no migration running",
    "canary": "testing on the canary group first",
    "running": "upgrading the fleet in waves",
    "completed": "finished: every eligible charger upgraded",
    "rolled_back": "a wave failed and was rolled back; migration stopped",
    "failed": "the migration controller stopped with an error",
}
TERMINAL = frozenset({"completed", "rolled_back", "failed"})
BAR_WIDTH = 40
GRID_PER_GROUP = 10
GRID_GROUPS_PER_ROW = 5
GRID_MAX_ROWS = 20
RECENT_CHANGES = 8

CLEAR = "\x1b[H\x1b[J"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"


# =====================================================================
# READING /api
# =====================================================================


@dataclass
class Poll:
    """One look at the server."""

    fleet: dict[str, Any] | None = None
    migration: dict[str, Any] | None = None
    error: str | None = None


def _get_json(url: str, ssl_context: ssl.SSLContext | None, timeout_s: float) -> Any:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout_s, context=ssl_context) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch(base_url: str, ssl_context: ssl.SSLContext | None = None,
          timeout_s: float = 3.0) -> Poll:
    """Never raises: a failed poll comes back with `error` set."""
    base = base_url.rstrip("/")
    try:
        fleet = _get_json(f"{base}/api/fleet", ssl_context, timeout_s)
        migration = fleet.get("migration") if isinstance(fleet, dict) else None
        if not isinstance(migration, dict) or "phase" not in migration:
            migration = _get_json(f"{base}/api/migration", ssl_context, timeout_s)
        return Poll(fleet=fleet if isinstance(fleet, dict) else None,
                    migration=migration if isinstance(migration, dict) else None)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return Poll(error=f"{type(exc).__name__}: {reason}")


# =====================================================================
# FOLLOWING THE MIGRATION BETWEEN POLLS
# =====================================================================


@dataclass
class Tracker:
    """What changed since the previous poll, and when to stop."""

    started: float = field(default_factory=time.monotonic)
    states: dict[str, str] = field(default_factory=dict)
    changes: deque = field(default_factory=lambda: deque(maxlen=RECENT_CHANGES))
    ignore_migration_id: str | None = None
    """An earlier migration that was already over when the view started."""
    watching_id: str | None = None
    """The migration this view has seen running."""
    first_poll_done: bool = False
    finished: bool = False

    def update(self, poll: Poll, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if poll.error:
            return
        mig = poll.migration or {}
        phase = str(mig.get("phase") or "idle").lower()
        mid = mig.get("migration_id") or None

        if not self.first_poll_done:
            self.first_poll_done = True
            if phase in TERMINAL:
                # Left over from an earlier migration on this server.
                self.ignore_migration_id = mid or "?"

        active_new = phase not in ("idle",) and (mid or "?") != self.ignore_migration_id
        if active_new and self.watching_id is None:
            self.watching_id = mid or "?"
            self.states = {}          # start the change list fresh
            self.changes.clear()

        for st in (poll.fleet or {}).get("stations", []) or []:
            sid = st.get("station_id")
            if not sid:
                continue
            new = str(st.get("migration_state") or "pending")
            old = self.states.get(sid)
            if old is not None and old != new and self.watching_id is not None:
                self.changes.append((now - self.started, sid, old, new))
            self.states[sid] = new

        if self.watching_id is not None and phase in TERMINAL:
            self.finished = True

    @property
    def waiting_for_new(self) -> bool:
        return self.watching_id is None


# =====================================================================
# DRAWING
# =====================================================================


def _iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _secs(a: datetime | None, b: datetime | None) -> str:
    if a is None or b is None:
        return "-"
    return f"{max(0.0, (b - a).total_seconds()):.1f} s"


def _id_range(ids: list[str]) -> str:
    ids = sorted(ids)
    if not ids:
        return "-"
    return ids[0] if len(ids) == 1 else f"{ids[0]}-{ids[-1]}"


def _int(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) else 0


def _letter(station: dict[str, Any]) -> str:
    state = str(station.get("migration_state") or "pending")
    if state == "pending" and station.get("connection_state") != "connected":
        return OFFLINE_LETTER
    return STATE_LETTER.get(state, "?")


def render(poll: Poll, tracker: Tracker, *, url: str = "",
           now_utc: datetime | None = None) -> str:
    """The whole screen as text. Pure: same input, same output."""
    now_utc = now_utc or datetime.now(timezone.utc)
    mig = poll.migration or {}
    fleet = poll.fleet or {}
    phase = str(mig.get("phase") or "idle").lower()
    lines: list[str] = []

    started = _iso(mig.get("started_at"))
    ended = _iso(mig.get("completed_at"))
    clock = _secs(started, ended or now_utc) if started else "-"
    lines.append(f"PQCharge  -  fleet migration          target: {mig.get('target_mode') or '-'}"
                 f"    time: {clock}")
    if poll.error and not poll.migration:
        lines.append("Phase: -  (no answer from the server yet)")
    else:
        lines.append(f"Phase: {phase.upper()}  -  {PHASE_WORDS.get(phase, phase)}")
    if poll.error:
        lines.append(f"!! server not reachable at {url} (retrying): {poll.error}")
    elif tracker.waiting_for_new:
        if tracker.ignore_migration_id:
            lines.append("   (this migration ended before the view started; "
                         "waiting for a NEW migration to start)")
        else:
            lines.append("   waiting for a migration to start ...")
    lines.append("")

    stations = sorted(fleet.get("stations", []) or [], key=lambda s: str(s.get("station_id")))
    power_kw = fleet.get("aggregate_power_w")
    power = f"{power_kw / 1000.0:.1f} kW" if isinstance(power_kw, (int, float)) else "-"
    lines.append(f"Fleet   {_int(fleet.get('total_stations')) or len(stations)} chargers"
                 f"   connected {_int(fleet.get('connected_count'))}"
                 f"   charging {_int(fleet.get('charging_count'))}"
                 f"   power {power}")

    total = _int(mig.get("total_stations")) or len(stations)
    up, prog, wait = _int(mig.get("migrated")), _int(mig.get("in_progress")), _int(mig.get("pending"))
    back, incompat = _int(mig.get("rolled_back")), _int(mig.get("incompatible"))
    done = up + back + incompat
    filled = round(BAR_WIDTH * done / total) if total else 0
    lines.append(f"Progress [{'#' * filled}{'.' * (BAR_WIDTH - filled)}] {done}/{total} decided")
    lines.append(f"   upgraded {up}   in progress {prog}   waiting {wait}"
                 f"   rolled back {back}   incompatible {incompat}")
    lines.append("")

    waves = mig.get("waves") or []
    if waves:
        lines.append("Waves")
        for w in waves:
            name = "canary " if w.get("is_canary") else f"wave {_int(w.get('wave_id')):<2}"
            ids = [str(x) for x in (w.get("station_ids") or [])]
            ws, we = _iso(w.get("started_at")), _iso(w.get("completed_at"))
            took = _secs(ws, we) if (ws and we) else ""
            lines.append(
                f"  {name} {_id_range(ids):<15} {len(ids):>3} chargers"
                f"   upgraded {_int(w.get('migrated_count')):>3}"
                f"   failed {_int(w.get('failed_count')):>3}"
                f"   {str(w.get('phase') or '-'):<11} {took}")
        lines.append("")

    if stations:
        lines.append("Chargers  (U upgraded  * in progress  . waiting  - waiting, offline"
                     "  R rolled back  x incompatible)")
        per_row = GRID_PER_GROUP * GRID_GROUPS_PER_ROW
        rows = [stations[i:i + per_row] for i in range(0, len(stations), per_row)]
        for row in rows[:GRID_MAX_ROWS]:
            groups = [row[i:i + GRID_PER_GROUP] for i in range(0, len(row), GRID_PER_GROUP)]
            text = " ".join("".join(_letter(s) for s in g) for g in groups)
            lines.append(f"  {str(row[0].get('station_id')):<8} {text}")
        if len(rows) > GRID_MAX_ROWS:
            lines.append(f"  ... {len(stations) - GRID_MAX_ROWS * per_row} more chargers not shown")
        lines.append("")

    if tracker.changes:
        lines.append("Latest changes")
        for t, sid, old, new in list(tracker.changes)[::-1]:
            note = CHANGE_NOTE.get((old, new), "")
            lines.append(f"  {t:7.1f} s  {sid:<8} {STATE_WORDS.get(old, old)} -> "
                         f"{STATE_WORDS.get(new, new)}" + (f"  ({note})" if note else ""))
        lines.append("")

    lines.append("Ctrl-C to quit." if not tracker.finished else "")
    return "\n".join(lines)


def summary(poll: Poll) -> str:
    """The closing lines once a migration has ended."""
    mig = poll.migration or {}
    phase = str(mig.get("phase") or "?").lower()
    waves = mig.get("waves") or []
    rolled = [w for w in waves if str(w.get("phase")).lower() == "rolled_back"]
    parts = [
        f"Migration {mig.get('migration_id') or ''} ended: {phase.upper()} - "
        f"{PHASE_WORDS.get(phase, phase)}.",
        f"  {_int(mig.get('migrated'))} upgraded, {_int(mig.get('rolled_back'))} rolled back, "
        f"{_int(mig.get('incompatible'))} incompatible, {_int(mig.get('pending'))} still waiting, "
        f"of {_int(mig.get('total_stations'))}; {len(waves)} wave(s)"
        + (f", {len(rolled)} rolled back" if rolled else "") + ".",
        "  Full results (key checks, timings, sessions disturbed): analysis\\output\\report.html",
    ]
    return "\n".join(parts)


# =====================================================================
# THE CONSOLE
# =====================================================================


def enable_redraw(stream: Any = None) -> bool:
    """
    True if the console can redraw in place. Turns on console codes for
    Windows consoles (ENABLE_VIRTUAL_TERMINAL_PROCESSING); never raises.
    """
    stream = stream or sys.stdout
    try:
        if not stream.isatty():
            return False
    except Exception:  # noqa: BLE001
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes  # noqa: PLC0415 - Windows only

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001
        return False


def _ssl_context_for(args: argparse.Namespace) -> ssl.SSLContext | None:
    """Under TLS, borrow a charger's certificate like the fleet watcher (L08)."""
    if not args.url.lower().startswith("https://"):
        return None
    from agent.config import AgentConfig  # noqa: PLC0415
    from harness.load_generator import _watch_ssl_context  # noqa: PLC0415

    host = args.url.split("://", 1)[1].rstrip("/")
    config = AgentConfig(station_id=args.cert_station, csms_url=f"wss://{host}",
                         cert_dir=args.cert_dir)
    return _watch_ssl_context(config, args.cert_station)


def watch(args: argparse.Namespace, out: Any = None) -> int:
    out = out or sys.stdout
    redraw = (not args.no_clear) and enable_redraw(out)
    ssl_context = _ssl_context_for(args)
    tracker = Tracker()
    last_text = None
    last_good: Poll | None = None
    if redraw:
        out.write(HIDE_CURSOR)
    try:
        while True:
            poll = fetch(args.url, ssl_context, args.timeout)
            if poll.error and last_good is not None:
                # Keep showing the last good picture, with the error on top.
                poll = Poll(fleet=last_good.fleet, migration=last_good.migration, error=poll.error)
            elif not poll.error:
                last_good = poll
                tracker.update(poll)
            text = render(poll, tracker, url=args.url)
            if redraw:
                out.write(CLEAR + text + "\n")
            elif text != last_text:
                out.write(text + "\n" + "-" * 78 + "\n")
            last_text = text
            out.flush()
            if tracker.finished:
                out.write(summary(poll) + "\n")
                out.flush()
                return 0
            if args.once:
                return 0
            time.sleep(args.every)
    except KeyboardInterrupt:
        out.write("\nstopped (Ctrl-C).\n")
        return 130
    finally:
        if redraw:
            out.write(SHOW_CURSOR)
            out.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness.watch_migration",
        description="Live console view of a fleet migration (GET /api only).",
    )
    parser.add_argument("--url", default="http://localhost:9000",
                        help="the CSMS base address (https:// when it runs with --tls)")
    parser.add_argument("--every", type=float, default=0.5, help="seconds between polls")
    parser.add_argument("--timeout", type=float, default=3.0, help="seconds per request")
    parser.add_argument("--no-clear", action="store_true",
                        help="print each update below the last instead of redrawing")
    parser.add_argument("--once", action="store_true", help="draw one screen and exit")
    parser.add_argument("--cert-dir", default="certs",
                        help="TLS only: where bootstrap_pki wrote the certificates")
    parser.add_argument("--cert-station", default="CP0001",
                        help="TLS only: the charger certificate to borrow (L08 stand-in)")
    return parser


def main(argv: list[str] | None = None) -> int:
    return watch(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
