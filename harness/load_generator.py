"""
N stations at once — the fleet, and the storm.

Track C (harness). Phase C5. Entry point: python -m harness.load_generator

--------------------------------------------------------------------
WHERE THIS FITS

    agent/station.py            ONE station's whole life
          |
    harness/load_generator.py <- you are here. N of them, as asyncio
          |                      tasks in ONE process, plus the storm
          |                      and the run's own record.
    harness/timing_log.py       what the server cannot see
          |
    analysis/parse_events.py    Stage 9, reading both logs together

Everything below builds ChargingStation objects and gets out of the way.
There is no second implementation of a station here, and that is the
point: E2's agents must behave identically to the single agent that was
verified in C1-C4, or the experiment measures the harness.

--------------------------------------------------------------------
THE EXPERIMENTS THIS SERVES

    E1  handshake cost          N small, --tls on and off
    E2  reconnection storm      N large, --storm-at, THE headline
    E5  fleet power spike       N moderate, curtailment via dispatch

--------------------------------------------------------------------
ONE PROCESS, ONE EVENT LOOP, N TASKS

Five hundred agents are five hundred coroutines sharing one loop, not
five hundred processes. That is deliberate and it has one consequence
worth stating: **the harness competes with nothing except itself for
CPU, and it competes with the CSMS only if they share a machine.**

Track A's §9.5 is precisely about this. Their words: the algorithms are
not the risk, five hundred Python processes contending for one laptop's
CPU is. A saturated harness makes handshakes look slow, and "slow
handshake" is the finding E1 reports. So:

  * every run records `--connect-timeout` and how many attempts hit it
  * the summary prints abandoned attempts separately from refusals
  * the CSMS should run on a different machine for any recorded run,
    and if it cannot, the run says so in its own log

--------------------------------------------------------------------
*** WHY THE START IS STAGGERED, AND WHY THE STAGGER IS JITTERED ***

Spawning five hundred agents in a tight loop makes five hundred TCP
connects land inside a few milliseconds. The server sheds most of them,
every shed agent backs off, and what E1 then measures is the harness's
own arrival pattern rather than handshake cost.

This is the same failure as unjittered backoff, one layer earlier, and
it is worth being just as careful about. `--stagger` spreads the
spawns; `--stagger-jitter` keeps them from landing on a fixed grid,
which at small intervals is its own kind of lockstep.

A fleet that arrives over ten seconds is not "slower to start" in any
way that matters: E2's clock starts at the storm, long after everyone
is connected.

--------------------------------------------------------------------
ONE FAILED STATION MUST NEVER ABORT THE RUN

`asyncio.gather(..., return_exceptions=True)`. Track A hardened their
own fixture the same way after a single refused connection aborted a
50-station run.

A 500-agent run is minutes of setup and it is the only artefact of that
attempt. Losing all of it because station 373 raised something
unexpected is the most expensive avoidable failure in this phase.

--------------------------------------------------------------------
USAGE

    # smoke test, 5 stations, no server needed beyond the fake
    python -m harness.load_generator --n 5 --experiment smoke

    # E1: handshake cost, classical, over TLS
    python -m harness.load_generator --n 50 --experiment e1 \\
        --csms-url wss://localhost:9000 --crypto-mode classical

    # E2: the reconnection storm, with the harness supervising the CSMS
    python -m harness.load_generator --n 500 --experiment e2 \\
        --storm-at 60 --storm-for 30 \\
        --server-cmd "python -m csms.server --ws-ping-interval 0"

    # E2 with the server started by hand elsewhere: the watcher still
    # records the kill and the recovery, by noticing run_id change
    python -m harness.load_generator --n 500 --experiment e2 \\
        --watch-fleet http://localhost:9000/api/fleet
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import random
import shlex
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from agent.config import AgentConfig
from agent.logging_setup import configure_logging, get_logger
from agent.station import ChargingStation
from harness import timing_log as tl
from harness.timing_log import TimingLog, default_path

# Every E2 run must disable WebSocket keepalive on the SERVER
# (--ws-ping-interval 0). Track A's §10: a CPU-saturated agent may pong
# late and be dropped, landing in the dataset as a failed recovery.
#
# The harness cannot set a flag on a server it did not start, so it
# CHECKS instead: --server-cmd is inspected, and a storm run without the
# flag is refused rather than silently producing a contaminated dataset.
WS_PING_DISABLE_FLAG = "--ws-ping-interval"


@dataclass
class FleetSpec:
    """How many stations, called what, arriving when."""

    n: int = 5
    id_prefix: str = "CP"
    id_width: int = 4
    """CP0001. Four digits so 500 stations sort lexicographically, which
    is how they appear in every log grep and every dashboard list. Three
    would put CP10 before CP9."""

    start_index: int = 1

    stagger_s: float = 0.02
    """Gap between spawns. 0.02 x 500 = 10 seconds for a full fleet."""

    stagger_jitter: float = 0.5
    """Fraction of the gap to randomise, 0-1. See the module docstring:
    a fixed grid is its own kind of lockstep."""

    def station_ids(self) -> list[str]:
        return [
            f"{self.id_prefix}{i:0{self.id_width}d}"
            for i in range(self.start_index, self.start_index + self.n)
        ]


@dataclass
class StationOutcome:
    """What one station did. Aggregated into the run summary."""

    station_id: str
    ok: bool = False
    crashed: bool = False
    error: str = ""

    connection_attempts: int = 0
    reconnections: int = 0
    connect_timeouts: int = 0
    total_downtime_s: float = 0.0
    callerrors: int = 0
    commands_received: int = 0
    state_transitions: int = 0

    offline_queued: int = 0
    offline_replayed: int = 0
    offline_dropped: int = 0

    wall_s: float = 0.0


@dataclass
class FleetResult:
    """The run, in numbers. Printed and written to the timing log."""

    experiment: str
    run_id: str
    n_stations: int
    crypto_mode: str
    tls: bool
    wall_s: float = 0.0
    outcomes: list[StationOutcome] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if not o.ok and not o.crashed)

    @property
    def crashed(self) -> int:
        return sum(1 for o in self.outcomes if o.crashed)

    def total(self, attr: str) -> float:
        return sum(getattr(o, attr) for o in self.outcomes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment": self.experiment,
            "run_id": self.run_id,
            "n_stations": self.n_stations,
            "crypto_mode": self.crypto_mode,
            "tls": self.tls,
            "wall_s": self.wall_s,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "crashed": self.crashed,
            "connection_attempts": self.total("connection_attempts"),
            "reconnections": self.total("reconnections"),
            "connect_timeouts": self.total("connect_timeouts"),
            "callerrors": self.total("callerrors"),
            "offline_queued": self.total("offline_queued"),
            "offline_replayed": self.total("offline_replayed"),
            "offline_dropped": self.total("offline_dropped"),
            "total_downtime_s": self.total("total_downtime_s"),
        }

    def describe(self) -> str:
        """The block a person reads when a run ends."""
        lines = [
            "",
            "=" * 68,
            f"  {self.experiment}  n={self.n_stations}  "
            f"mode={self.crypto_mode}  tls={'on' if self.tls else 'off'}",
            f"  run_id={self.run_id}   wall={self.wall_s:.1f}s",
            "=" * 68,
            f"  succeeded            {self.succeeded}/{self.n_stations}",
            f"  failed               {self.failed}",
            f"  crashed              {self.crashed}",
            "",
            f"  connection attempts  {self.total('connection_attempts'):.0f}",
            f"  reconnections        {self.total('reconnections'):.0f}",
            f"  connect timeouts     {self.total('connect_timeouts'):.0f}",
            f"  total downtime       {self.total('total_downtime_s'):.1f}s",
            "",
            f"  CALLErrors           {self.total('callerrors'):.0f}",
            f"  offline queued       {self.total('offline_queued'):.0f}",
            f"  offline replayed     {self.total('offline_replayed'):.0f}",
            f"  offline DROPPED      {self.total('offline_dropped'):.0f}",
        ]

        # The three lines that decide whether this run's data is usable.
        # Printed as warnings rather than left for someone to notice in
        # the numbers above, because a run is normally read once.
        if self.crashed:
            lines += ["", "  *** STATIONS CRASHED. That is a Track C bug, not a",
                      "      finding. Investigate before using this run. ***"]
        if self.total("offline_dropped"):
            lines += ["", "  *** OFFLINE EVENTS WERE DROPPED. This run's event log",
                      "      has gaps. Raise --offline-queue-max or record it. ***"]
        if self.total("connect_timeouts"):
            lines += ["", "  *** ATTEMPTS WERE ABANDONED AT --connect-timeout.",
                      "      At high N this usually means the cap is too low,",
                      "      not that the server is slow. Raise it and re-run",
                      "      before reporting any recovery figure. ***"]
        lines.append("=" * 68)
        return "\n".join(lines)


# =====================================================================
# WATCHING THE FLEET — Contract 6, from outside the server
# =====================================================================


class FleetWatcher:
    """
    Polls `/api/fleet` and writes each snapshot to the timing log.

    *** IT STORES THE SNAPSHOT, IT DOES NOT COMPUTE A VERDICT. ***

    It would be easy to have the watcher decide "47 of 500 recovered"
    and log that number. It would also be wrong. `is_recovered` is
    defined once, in `csms/fleet.py`, precisely so the dashboard, the
    load generator and the analysis scripts cannot disagree about it --
    Track A's comment says deciding what "recovered" means during
    analysis silently invalidates the comparison across all twelve runs.

    So the raw snapshot goes in the log and `analysis/` applies the
    predicate at Stage 9, from Track A's own code. The watcher does
    reconstruct a StationView to report a live count for the operator
    watching the run, and even that goes through the real property
    rather than re-deriving it.

    It also detects the storm without being told: `run_id` changes when
    the CSMS restarts, which is the visible marker of an E2 kill.
    """

    def __init__(
        self,
        url: str,
        log: TimingLog,
        *,
        interval_s: float = 1.0,
        ssl_context: ssl.SSLContext | None = None,
        timeout_s: float = 3.0,
    ) -> None:
        self.url = url
        self.log = log
        self.interval_s = interval_s
        self.ssl_context = ssl_context
        self.timeout_s = timeout_s
        self.logger = get_logger(__name__)

        self.polls = 0
        self.failures = 0
        self.last_run_id: str | None = None
        self.recovered_count = 0
        self.connected_count = 0

        self._was_reachable = True

    def _fetch(self) -> dict[str, Any]:
        """Blocking. Always called via asyncio.to_thread."""
        request = urllib.request.Request(self.url, method="GET")
        with urllib.request.urlopen(
            request, timeout=self.timeout_s, context=self.ssl_context
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _recovered(snapshot: dict[str, Any]) -> int:
        """
        Count recovered stations using Track A's own predicate.

        Reconstructs StationView from the serialised dict rather than
        re-implementing `connection_state == connected and boot_accepted`
        here. If that definition ever changes, this follows it; a copy
        would not, and the disagreement would be invisible.
        """
        try:
            from csms.fleet import StationView

            count = 0
            for row in snapshot.get("stations", []):
                fields = {f.name for f in dataclasses.fields(StationView)}
                view = StationView(**{k: v for k, v in row.items() if k in fields})
                if view.is_recovered:
                    count += 1
            return count
        except Exception:  # noqa: BLE001 - a display number, never load-bearing
            return sum(
                1 for r in snapshot.get("stations", [])
                if r.get("connection_state") == "connected" and r.get("boot_accepted")
            )

    async def run(self) -> None:
        """Poll until cancelled."""
        while True:
            try:
                snapshot = await asyncio.to_thread(self._fetch)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self.failures += 1
                if self._was_reachable:
                    # The transition matters, not each failure: this is
                    # the moment the CSMS became unreachable from
                    # outside, which during E2 is the kill itself.
                    self._was_reachable = False
                    self.log.emit(
                        tl.STORM_KILL,
                        detected_by="fleet_poll_failure",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    self.log.flush()
                    self.logger.warning("fleet unreachable: %s", exc)
                self.log.emit(
                    tl.WATCHER_ERROR, error=f"{type(exc).__name__}: {exc}"
                )
                await asyncio.sleep(self.interval_s)
                continue

            self.polls += 1
            run_id = snapshot.get("run_id")

            if not self._was_reachable:
                self._was_reachable = True
                self.log.emit(
                    tl.STORM_RESTART,
                    detected_by="fleet_poll_recovered",
                    server_run_id=run_id,
                )
                self.log.flush()
                self.logger.warning("fleet reachable again (run_id=%s)", run_id)

            if self.last_run_id is not None and run_id != self.last_run_id:
                # The server restarted without the poll ever failing --
                # possible when the kill and restart fall between polls.
                self.log.emit(
                    tl.STORM_RESTART,
                    detected_by="run_id_changed",
                    server_run_id=run_id,
                    previous_run_id=self.last_run_id,
                )
                self.logger.warning(
                    "CSMS run_id changed %s -> %s: the server restarted",
                    self.last_run_id, run_id,
                )
            self.last_run_id = run_id

            self.connected_count = int(snapshot.get("connected_count", 0))
            self.recovered_count = self._recovered(snapshot)

            self.log.emit(tl.FLEET_SNAPSHOT, snapshot=snapshot)
            await asyncio.sleep(self.interval_s)


# =====================================================================
# THE STORM — optionally supervising the CSMS
# =====================================================================


class ServerSupervisor:
    """
    Starts the CSMS, kills it at T, restarts it after D. E2, repeatably.

    OPTIONAL, AND OFF BY DEFAULT. Track A owns the server and every
    recorded run should use their real flags. This exists because "kill
    the server at exactly sixty seconds" done by hand is not repeatable
    across twelve runs, and E2's whole value is the comparison between
    those runs.

    Without --server-cmd the storm still gets recorded: FleetWatcher
    notices the server going away and coming back, and stamps both. The
    operator kills it however they like. That path is less precise and
    entirely valid.

    *** WINDOWS CAVEAT. *** terminate() is not a clean SIGINT there, so
    the CSMS does not get to run its shutdown path and may lose up to
    one persistence flush interval. That is the same loss window Track A
    already documents for a hard kill, and E2 is *meant* to be a hard
    kill -- but it means a supervised run and a Ctrl-C run are not
    byte-identical, and that belongs in docs/limitations.md.
    """

    def __init__(self, command: str, log: TimingLog) -> None:
        self.command = command
        self.log = log
        self.logger = get_logger(__name__)
        self.process: subprocess.Popen | None = None
        self.starts = 0

    def _argv(self) -> list[str]:
        # posix=False on Windows so backslashes in paths survive.
        return shlex.split(self.command, posix=(sys.platform != "win32"))

    def start(self) -> None:
        self.process = subprocess.Popen(self._argv())
        self.starts += 1
        self.logger.info(
            "CSMS started under supervision (pid %s): %s",
            self.process.pid, self.command,
        )

    def kill(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        pid = self.process.pid
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.logger.warning("CSMS pid %s did not exit; killing", pid)
            self.process.kill()
            self.process.wait(timeout=5)
        self.logger.warning("CSMS pid %s is down", pid)

    async def storm(self, at_s: float, for_s: float) -> None:
        """Wait, kill, wait, restart. The E2 timeline."""
        await asyncio.sleep(at_s)

        self.log.emit(tl.STORM_KILL, detected_by="supervisor", planned_for_s=for_s)
        self.log.flush()
        self.kill()

        await asyncio.sleep(for_s)

        self.start()
        self.log.emit(tl.STORM_RESTART, detected_by="supervisor")
        self.log.flush()

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.kill()


# =====================================================================
# THE FLEET
# =====================================================================


class FleetRunner:
    """
    Builds N ChargingStations from one base config and runs them.

    It holds no charging logic of its own. Every station is the same
    object verified in C1-C4; the harness only decides how many there
    are, what they are called, and when each one starts.
    """

    def __init__(
        self,
        base_config: AgentConfig,
        spec: FleetSpec,
        log: TimingLog,
        *,
        progress_every_s: float = 5.0,
        rng: random.Random | None = None,
    ) -> None:
        self.base_config = base_config
        self.spec = spec
        self.log = log
        self.progress_every_s = progress_every_s
        self.rng = rng or random.Random()
        self.logger = get_logger(__name__)

        self.outcomes: dict[str, StationOutcome] = {}
        self.finished = 0
        self._started_monotonic = 0.0

    # -- one station ------------------------------------------------------

    def config_for(self, station_id: str) -> AgentConfig:
        """
        This station's configuration.

        `dataclasses.replace` rather than constructing a new config, so
        every field the operator set on the command line reaches all N
        agents and a field added later cannot be forgotten here.

        run_id is shared across the fleet on purpose: it is what ties
        five hundred agents together as ONE run, and it matches the
        run_id in this harness's timing log.

        Per-agent FILE logging is forced off. Five hundred agents would
        otherwise open five hundred log files, and on top of the file
        handles it makes the run's record five hundred files that have
        to be collated before anything can be read. The timing log is
        the fleet's record; a single agent debugged on its own still
        gets --log-to-file.
        """
        return dataclasses.replace(
            self.base_config,
            station_id=station_id,
            run_id=self.log.run_id,
            log_to_file=False,
        )

    async def _run_station(self, station_id: str, delay_s: float) -> StationOutcome:
        """Wait out this station's stagger, then live its whole life."""
        outcome = StationOutcome(station_id=station_id)
        self.outcomes[station_id] = outcome

        if delay_s > 0:
            await asyncio.sleep(delay_s)

        self.log.emit(tl.STATION_SPAWNED, station_id)
        started = time.monotonic()
        station: ChargingStation | None = None

        try:
            station = ChargingStation(self.config_for(station_id))
            outcome.ok = await station.run()

        except asyncio.CancelledError:
            # Ctrl-C, or the run ending. ChargingStation.run()'s own
            # finally has already opened the contactor on the way past.
            raise

        except Exception as exc:  # noqa: BLE001 - one station, not the fleet
            # Reaching here means the STATION failed to handle something
            # it should have. It is a Track C bug and is reported as
            # such, separately from an ordinary failed run, so it can
            # never be mistaken for a finding about the server.
            outcome.crashed = True
            outcome.error = f"{type(exc).__name__}: {exc}"
            self.logger.exception("station %s crashed", station_id)

        finally:
            outcome.wall_s = time.monotonic() - started
            if station is not None:
                outcome.connection_attempts = station.connection_attempts
                outcome.reconnections = station.reconnections
                outcome.connect_timeouts = station.connect_timeouts
                outcome.total_downtime_s = station.total_downtime_s
                outcome.callerrors = station.callerror_count
                outcome.commands_received = station.commands_received
                outcome.state_transitions = station.state.transition_count
                outcome.offline_queued = station.offline_queue.queued_total
                outcome.offline_replayed = station.offline_queue.replayed_total
                outcome.offline_dropped = station.offline_queue.dropped_total

            self.finished += 1
            self.log.emit(
                tl.STATION_CRASHED if outcome.crashed else tl.STATION_FINISHED,
                station_id,
                **{k: v for k, v in dataclasses.asdict(outcome).items()
                   if k != "station_id"},
            )

        return outcome

    # -- progress ------------------------------------------------------------

    async def _progress_loop(self, watcher: FleetWatcher | None) -> None:
        """
        A heartbeat for the human watching. Cancelled when the run ends.

        A 500-agent run is minutes of near-silence at INFO. Without this
        there is no way to tell a working run from a hung one, and the
        instinct when unsure is to Ctrl-C -- which destroys the run.
        """
        while True:
            await asyncio.sleep(self.progress_every_s)
            elapsed = time.monotonic() - self._started_monotonic
            running = len(self.outcomes) - self.finished

            fields: dict[str, Any] = {
                "spawned": len(self.outcomes),
                "running": running,
                "finished": self.finished,
                "elapsed_s": elapsed,
            }
            if watcher is not None:
                fields["server_connected"] = watcher.connected_count
                fields["server_recovered"] = watcher.recovered_count

            self.log.emit(tl.PROGRESS, **fields)
            self.logger.info(
                "t=%5.1fs  spawned=%d running=%d finished=%d%s",
                elapsed, len(self.outcomes), running, self.finished,
                (f"  server: {watcher.connected_count} connected, "
                 f"{watcher.recovered_count} recovered") if watcher else "",
            )

    # -- the run --------------------------------------------------------------

    async def run(
        self,
        watcher: FleetWatcher | None = None,
        storm: asyncio.Task | None = None,
    ) -> FleetResult:
        """
        Spawn the fleet, wait for all of it, and total up what happened.
        """
        self._started_monotonic = time.monotonic()
        ids = self.spec.station_ids()

        self.logger.info(
            "spawning %d station(s) %s..%s over ~%.1fs",
            len(ids), ids[0], ids[-1],
            len(ids) * self.spec.stagger_s,
        )

        tasks = [
            asyncio.ensure_future(self._run_station(sid, self._delay_for(i)))
            for i, sid in enumerate(ids)
        ]

        progress = asyncio.ensure_future(self._progress_loop(watcher))

        try:
            # return_exceptions=True is the whole reason a 500-agent run
            # survives one bad station. See the module docstring.
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in (progress, storm):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

        result = FleetResult(
            experiment=self.log.experiment,
            run_id=self.log.run_id,
            n_stations=len(ids),
            crypto_mode=self.base_config.crypto_mode,
            tls=self.base_config.uses_tls,
            wall_s=time.monotonic() - self._started_monotonic,
            outcomes=[self.outcomes[sid] for sid in ids if sid in self.outcomes],
        )
        return result

    def _delay_for(self, index: int) -> float:
        """
        When station `index` starts.

        Jittered around the grid position for the reason in the module
        docstring. Clamped at zero so the first station never waits.
        """
        base = index * self.spec.stagger_s
        if self.spec.stagger_jitter <= 0 or self.spec.stagger_s <= 0:
            return base
        spread = self.spec.stagger_s * self.spec.stagger_jitter
        return max(0.0, base + self.rng.uniform(-spread, spread))


# =====================================================================
# ENTRY POINT
# =====================================================================


def _watch_ssl_context(config: AgentConfig, station_id: str) -> ssl.SSLContext | None:
    """
    The TLS context the fleet watcher polls `/api/fleet` with.

    *** THIS IS THE OPEN QUESTION IN TRACK A's §9.1, MADE CONCRETE. ***

    The OCPP endpoint and `/api/` share one socket, so `--tls` turns TLS
    on for both -- and with client certificates required (which is what
    Security Profile 3 means) every caller must present one, including
    this poll. There is no way to exempt the HTTP paths: the certificate
    exchange happens in the handshake, before a byte of HTTP is read.

    Track A's preferred answer is an "operator" certificate issued by
    Track B's CA. Until that exists, the watcher borrows a station's
    certificate, which WORKS and is NOT RIGHT: the operator console is
    an identity of its own, and once `--tls-identity-check enforce` is
    on, a poll presenting CP0001's certificate from something that is
    not CP0001 is exactly the impersonation the check exists to catch.

    So it is loud, and it names the fix.
    """
    if not config.uses_tls:
        return None

    from agent.tls import build_station_context

    get_logger(__name__).warning(
        "fleet watcher is presenting station %s's certificate to poll /api/. "
        "This is a STAND-IN for the operator certificate Track A requested "
        "from Track B (their §9.1). It will be refused once "
        "--tls-identity-check is set to enforce.", station_id,
    )
    return build_station_context(
        station_id,
        config.cert_dir,
        cert=config.cert,
        key=config.key,
        ca=config.ca,
        check_hostname=config.tls_check_hostname,
    )


def _check_storm_preconditions(args: argparse.Namespace) -> None:
    """
    Refuse a storm run that would produce a contaminated dataset.

    Track A's §10: during E2 a CPU-saturated agent may pong late and be
    dropped by WebSocket keepalive, landing in the dataset as a failed
    recovery. Every E2 run needs `--ws-ping-interval 0` on the SERVER.

    "Put it in the run script, not in anyone's memory" is the agreement.
    This is the run script, so it is checked here -- and it fails fast,
    before five hundred agents spend minutes producing numbers that
    would have to be thrown away.
    """
    if args.storm_at is None or not args.server_cmd:
        return

    if WS_PING_DISABLE_FLAG not in args.server_cmd:
        raise SystemExit(
            f"\n--server-cmd does not set {WS_PING_DISABLE_FLAG}.\n\n"
            "Every E2 run needs `--ws-ping-interval 0` on the CSMS. Without\n"
            "it, a CPU-saturated agent that pongs late is dropped by\n"
            "WebSocket keepalive and lands in the dataset as a FAILED\n"
            "RECOVERY -- which is the number E2 exists to measure.\n\n"
            "Add it to --server-cmd, or pass --allow-ws-ping to override\n"
            "deliberately and record the deviation.\n"
        )


async def main_async(args: argparse.Namespace, config: AgentConfig) -> int:
    run_id = args.run_id or uuid.uuid4().hex[:12]
    spec = FleetSpec(
        n=args.n,
        id_prefix=args.id_prefix,
        id_width=args.id_width,
        start_index=args.start_index,
        stagger_s=args.stagger,
        stagger_jitter=args.stagger_jitter,
    )

    path = args.timing_log or default_path(
        args.experiment, args.n, config.crypto_mode, config.log_dir
    )

    rng = random.Random(args.seed) if args.seed is not None else random.Random()

    with TimingLog(
        path,
        run_id=run_id,
        experiment=args.experiment,
        n_stations=args.n,
        crypto_mode=config.crypto_mode,
        tls=config.uses_tls,
    ) as log:
        log.emit(
            tl.RUN_STARTED,
            config=config.describe(),
            stagger_s=spec.stagger_s,
            stagger_jitter=spec.stagger_jitter,
            seed=args.seed,
            server_cmd=args.server_cmd,
            storm_at_s=args.storm_at,
            storm_for_s=args.storm_for,
            python=sys.version.split()[0],
            platform=sys.platform,
        )
        log.sync()

        supervisor: ServerSupervisor | None = None
        storm_task: asyncio.Task | None = None
        watcher: FleetWatcher | None = None
        watcher_task: asyncio.Task | None = None

        try:
            if args.server_cmd:
                supervisor = ServerSupervisor(args.server_cmd, log)
                supervisor.start()
                # Give it a moment to bind before five hundred agents
                # arrive; otherwise the first wave measures start-up.
                await asyncio.sleep(args.server_warmup)

            if args.watch_fleet:
                watcher = FleetWatcher(
                    args.watch_fleet,
                    log,
                    interval_s=args.watch_every,
                    ssl_context=_watch_ssl_context(config, spec.station_ids()[0]),
                )
                watcher_task = asyncio.ensure_future(watcher.run())

            if args.storm_at is not None and supervisor is not None:
                storm_task = asyncio.ensure_future(
                    supervisor.storm(args.storm_at, args.storm_for)
                )
            elif args.storm_at is not None:
                get_logger(__name__).warning(
                    "--storm-at was given without --server-cmd: the harness "
                    "will not kill anything. Kill the CSMS yourself at about "
                    "t=%.0fs; the fleet watcher will stamp both the outage "
                    "and the recovery.", args.storm_at,
                )

            runner = FleetRunner(
                config, spec, log,
                progress_every_s=args.progress_every,
                rng=rng,
            )
            result = await runner.run(watcher=watcher, storm=storm_task)

        finally:
            for task in (watcher_task, storm_task):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            if supervisor is not None:
                supervisor.stop()

        log.emit(tl.RUN_FINISHED, **result.to_dict())
        print(result.describe())

    # Non-zero when the run is not trustworthy, so a shell script driving
    # twelve runs notices rather than carrying on.
    return 0 if (result.crashed == 0 and result.succeeded > 0) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness.load_generator",
        description="Run N PQCharge station agents against a CSMS (Track C).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    fleet = parser.add_argument_group("fleet")
    fleet.add_argument("--n", type=int, default=5, help="how many stations")
    fleet.add_argument("--id-prefix", default="CP")
    fleet.add_argument("--id-width", type=int, default=4,
                       help="digits in the station id; 4 keeps 500 sorting")
    fleet.add_argument("--start-index", type=int, default=1)
    fleet.add_argument(
        "--stagger", type=float, default=0.02,
        help="seconds between spawns. 0.02 x 500 = a 10s arrival window",
    )
    fleet.add_argument(
        "--stagger-jitter", type=float, default=0.5,
        help="0-1; randomises the stagger so arrivals are not on a grid",
    )
    fleet.add_argument("--seed", type=int, default=None,
                       help="make one run's stagger reproducible")

    run = parser.add_argument_group("run")
    run.add_argument("--experiment", default="run",
                     help="names the timing log: logs/<experiment>_n<N>_<mode>.jsonl")
    run.add_argument("--timing-log", default=None, help="override that path")
    run.add_argument("--progress-every", type=float, default=5.0)

    storm = parser.add_argument_group("storm (E2)")
    storm.add_argument("--server-cmd", default=None,
                       help="start and supervise the CSMS, so the kill is "
                            "repeatable across runs")
    storm.add_argument("--server-warmup", type=float, default=1.5)
    storm.add_argument("--storm-at", type=float, default=None,
                       help="seconds after start to kill the CSMS")
    storm.add_argument("--storm-for", type=float, default=30.0,
                       help="how long it stays down")
    storm.add_argument("--allow-ws-ping", action="store_true",
                       help="permit a storm run without --ws-ping-interval 0. "
                            "Record the deviation if you use it")

    watch = parser.add_argument_group("fleet watcher (Contract 6)")
    watch.add_argument("--watch-fleet", default=None,
                       help="poll this /api/fleet URL and record each snapshot")
    watch.add_argument("--watch-every", type=float, default=1.0)

    # Every agent flag, so the fleet is configured exactly as one station
    # would be. --station-id is accepted and ignored; the ids come from
    # --id-prefix / --n.
    AgentConfig.add_arguments(parser)
    return parser


def main() -> None:
    """
    python -m harness.load_generator --n 50 --experiment e1

    Run as a module from the repository root, per the convention every
    track follows.
    """
    args = build_parser().parse_args()

    if args.n < 1:
        raise SystemExit("--n must be at least 1")
    if not args.allow_ws_ping:
        _check_storm_preconditions(args)

    config = AgentConfig.from_namespace(args)

    # Logging is configured ONCE, here, for the whole process. Five
    # hundred ChargingStation objects deliberately do not configure it
    # themselves -- that is the entry point's job, and it is why they can
    # be constructed in bulk at all.
    log = configure_logging("harness", config.log_level, "fleet")
    log.info(
        "fleet run: n=%d experiment=%s url=%s tls=%s mode=%s",
        args.n, args.experiment, config.csms_url,
        "on" if config.uses_tls else "off", config.crypto_mode,
    )

    exit_code = 1
    try:
        exit_code = asyncio.run(main_async(args, config))
    except KeyboardInterrupt:
        # Cancels every station task, each of which opens its contactor
        # on the way out. On the Raspberry Pi bench node that is a relay
        # with current behind it.
        log.warning("interrupted -- cancelling the fleet")
        exit_code = 130

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
