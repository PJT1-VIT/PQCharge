"""
The harness's own record of a run — what the server cannot see.

Track C (harness). Phase C5.

--------------------------------------------------------------------
WHERE THIS FITS

    harness/load_generator.py   runs N agents
          |
    harness/timing_log.py    <- you are here. One JSONL file per run.
          |
    analysis/parse_events.py    reads it alongside Track A's event log

--------------------------------------------------------------------
*** THIS IS NOT CONTRACT 3 AND MUST NEVER PRETEND TO BE ***

`logs/events.jsonl` is Track A's, written by `csms/events.py`, and it is
the single source of every number in the results chapter. Track B's
guardrail 2 says never write to it. The same rule binds Track C.

This file is a SECOND, SEPARATE log, and it exists because there is one
class of fact the CSMS structurally cannot record: *what happened to
attempts that never arrived*.

    A station tried eleven times before getting through.
        The server saw the twelfth. The first eleven are only here.

    An agent slept 1.43s of jittered backoff.
        The server has no idea it existed during that time.

    Five hundred agents were spawned but only 487 reached the socket.
        The server's denominator is 487. Ours is 500.

E2 measures fleet recovery. Recovery is a fraction, and the server owns
the numerator while the harness owns the denominator. Both files are
needed; neither replaces the other.

--------------------------------------------------------------------
TIME: TWO CLOCKS, AND elapsed_ms IS THE ONE THAT MEANS ANYTHING

Every record carries both:

    ts          wall clock, ISO-8601 UTC. For lining this file up
                against Track A's log, which is also wall clock.
    elapsed_ms  milliseconds since the run started, from
                time.monotonic(). For every duration and every chart.

Contract 3's own note says durations come from monotonic differences
and never from wall-clock subtraction, because an NTP correction
mid-run would silently move an interval. A 500-agent run takes minutes;
that is long enough for a correction to land inside it.

--------------------------------------------------------------------
fsync: DELIBERATELY NOT PER EVENT

Track A's §7 finding is that `EventLog.emit` fsyncs on every event, and
that at fleet scale that may inflate the very timings E2 exists to
measure. Their file has a reason to: E2 kills THEIR process mid-write.

This one is written by the harness process, which E2 does not kill. So
it buffers, and flushes at phase boundaries, on a timer, and on close.
The cost is stated plainly rather than hidden: a hard kill of the
harness loses up to one flush window. Since the harness is not the
measurement surface for anything except its own timings, and since
those timings are worthless if the act of recording them changes them,
that is the right trade.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.logging_setup import get_logger

# -- event types -------------------------------------------------------
#
# Plain strings rather than an Enum, matching the convention Track A set
# in csms/events.py for the same reason: these values end up in a file
# that outlives the code, and a renamed enum member would make an old
# run unreadable by a new analysis script.

RUN_STARTED = "run_started"
RUN_FINISHED = "run_finished"

STATION_SPAWNED = "station_spawned"
"""An agent task was created. The harness's denominator."""

STATION_FINISHED = "station_finished"
"""An agent returned. Carries its own counters: attempts, reconnections,
downtime, CALLErrors, offline queue outcome."""

STATION_CRASHED = "station_crashed"
"""An agent raised something the station itself did not handle. Should
be zero; a non-zero count is a Track C bug, not a finding."""

PROGRESS = "progress"
"""A periodic snapshot of how many agents are still running. What tells
an operator watching a 500-agent run that it has not hung."""

FLEET_SNAPSHOT = "fleet_snapshot"
"""One poll of Contract 6's /api/fleet, stored verbatim. This is how
recovery becomes measurable after the fact -- see load_generator's
FleetWatcher for why the raw snapshot is stored rather than a computed
verdict."""

STORM_KILL = "storm_kill"
STORM_RESTART = "storm_restart"
"""The E2 event itself: the moment the CSMS was taken down, and the
moment it came back. Everything in the recovery chart is measured
relative to these two lines."""

WATCHER_ERROR = "watcher_error"
"""A fleet poll failed. Recorded rather than swallowed: during an E2
outage these are EXPECTED, and their span is itself evidence of how
long the server was unreachable from outside."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_path(
    experiment: str,
    n_stations: int,
    crypto_mode: str,
    log_dir: str | Path = "logs",
) -> Path:
    """
    The agreed per-run filename.

        logs/<experiment>_n<N>_<mode>.jsonl

    Agreed with Track A (their §9.4). It matters because
    analysis/parse_events.py reads these by pattern at Stage 9, and a
    run whose file is called `test2_final.jsonl` is a run that has to be
    identified by hand, weeks later, from its contents.
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in experiment)
    return Path(log_dir) / f"{safe}_n{n_stations}_{crypto_mode}.jsonl"


class TimingLog:
    """
    One JSONL file for one harness run.

    Every record is one line of JSON with a fixed envelope:

        ts, elapsed_ms, run_id, experiment, n_stations, crypto_mode,
        tls, event_type, station_id, + whatever the caller passed

    The envelope repeats on every line on purpose. It costs bytes and it
    buys the property that ANY line is self-describing -- so a file can
    be grepped, split, concatenated with another run's, or read from the
    middle, and every record still says which run it came from. Track
    A's Contract 3 made the same choice.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str,
        experiment: str,
        n_stations: int,
        crypto_mode: str = "classical",
        tls: bool = False,
        flush_every: int = 50,
        flush_interval_s: float = 2.0,
    ) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.experiment = experiment
        self.n_stations = n_stations
        self.crypto_mode = crypto_mode
        self.tls = tls

        self.flush_every = max(1, flush_every)
        self.flush_interval_s = flush_interval_s

        self.log = get_logger(__name__)

        self.records_written = 0
        self.write_errors = 0
        """A logging failure must never take down a run. Counted so a
        run that lost records says so in its summary rather than
        appearing complete."""

        self._started_monotonic = time.monotonic()
        self._since_flush = 0
        self._last_flush = self._started_monotonic

        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Line-buffered would fsync-ish on every newline on some
        # platforms; explicit buffering with explicit flushes is the
        # behaviour this file's docstring promises, so it is set here
        # rather than left to the default.
        self._handle = open(self.path, "a", encoding="utf-8", buffering=1 << 16)

        self.log.info("timing log: %s", self.path)

    # -- writing --------------------------------------------------------

    def emit(
        self,
        event_type: str,
        station_id: str | None = None,
        **fields: Any,
    ) -> None:
        """
        Write one record. Never raises.

        Instrumentation that can crash the thing it instruments is worse
        than no instrumentation: the run dies, and the reason looks like
        a fault in the system under test. Section 14 of the design
        document makes the same point about instrumentation bugs staying
        invisible until analysis -- this is the other half of it.
        """
        record = {
            "ts": _now_iso(),
            "elapsed_ms": (time.monotonic() - self._started_monotonic) * 1000.0,
            "run_id": self.run_id,
            "experiment": self.experiment,
            "n_stations": self.n_stations,
            "crypto_mode": self.crypto_mode,
            "tls": self.tls,
            "event_type": event_type,
            "station_id": station_id,
        }
        record.update(fields)

        try:
            # default=str so an unexpected datetime or Path in a caller's
            # fields degrades to a string instead of killing the run at
            # the one moment the record was worth having.
            self._handle.write(json.dumps(record, default=str) + "\n")
            self.records_written += 1
            self._since_flush += 1
        except Exception as exc:  # noqa: BLE001 - see the docstring
            self.write_errors += 1
            self.log.error("timing log write failed: %s", exc)
            return

        now = time.monotonic()
        if (
            self._since_flush >= self.flush_every
            or (now - self._last_flush) >= self.flush_interval_s
        ):
            self.flush()

    def flush(self) -> None:
        """
        Push buffered records to the OS. No fsync -- see the module
        docstring.

        Called at phase boundaries (spawn complete, storm kill, storm
        restart, run end) so that the records either side of an
        interesting moment are on disk before the next phase starts.
        """
        try:
            self._handle.flush()
            self._since_flush = 0
            self._last_flush = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            self.write_errors += 1
            self.log.error("timing log flush failed: %s", exc)

    def sync(self) -> None:
        """
        Force to disk, fsync included.

        Used exactly twice: after RUN_STARTED and before the process
        exits. Everywhere else the cost is not worth paying -- that is
        Track A's §7 finding applied to our own file.
        """
        self.flush()
        try:
            os.fsync(self._handle.fileno())
        except Exception as exc:  # noqa: BLE001
            self.log.debug("timing log fsync unavailable: %s", exc)

    def close(self) -> None:
        if self._handle.closed:
            return
        self.sync()
        self._handle.close()
        self.log.info(
            "timing log closed: %d record(s), %d write error(s) -> %s",
            self.records_written, self.write_errors, self.path,
        )

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> TimingLog:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- reading back ------------------------------------------------------

    @staticmethod
    def read(path: str | Path) -> list[dict[str, Any]]:
        """
        Parse a timing log, tolerating a truncated final line.

        The tolerance is not hypothetical. If the harness is killed --
        Ctrl-C on a long run, or the OS -- the last buffered write can
        be a partial line. Track A's plan makes the same allowance for
        their log under the fsync experiment. A parser that raises on
        the last line makes an otherwise complete run unreadable.
        """
        rows: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    # Only the final line may legitimately be partial.
                    # Anything earlier is corruption worth shouting about.
                    get_logger(__name__).warning(
                        "timing log %s: skipping unparseable line", path
                    )
        return rows
