"""
The machine the test runs on — is the LAPTOP the bottleneck, or the protocol?

Track C (harness). Phase C-P6 (S1, the 500-charger stress test).

--------------------------------------------------------------------
IN PLAIN WORDS

At 500 chargers, the load generator, the 500 simulated chargers and (usually)
the CSMS all share one laptop. If that laptop runs out of CPU, every time we
measure gets longer -- and the slowdown would be reported as the cost of the
protocol or of post-quantum cryptography, when it is really the cost of an
overloaded laptop.

So, once a second, this records:

    machine     whole-machine CPU % and memory %
    tester      this process (the load generator + all its chargers):
                CPU (% of ONE core; can exceed 100 on several cores),
                memory (RSS) and thread count
    server      the CSMS process, if it can be found: same three numbers
    loop lag    how late a 1-second timer fired inside the tester's event
                loop. Every charger in the tester shares that one loop; when
                it is late, every time the chargers measure is late by the
                same amount. This is the most direct "is the tester itself
                distorting the timings?" number we have.

One `machine_sample` line per second goes into the tester diary. The
analysis (analysis/measures/machine.py) turns them into peaks and p95s and
warns when the laptop was saturated (check.py: machine_saturated).

--------------------------------------------------------------------
FINDING THE SERVER PROCESS

In order:
    1. --server-pid N                 that process, exactly
    2. --server-cmd (the harness started the CSMS itself)
                                      the process it started; after an E2
                                      restart, the new one
    3. otherwise                      the first process whose command line
                                      has the separate argument
                                      "csms.server" (i.e. `python -m
                                      csms.server ...`). A `cmd /c "python
                                      -m csms.server ..."` wrapper has it
                                      only inside one long string, so the
                                      wrapper is never mistaken for it.
When the server is not found (another machine, or not started yet), the
`server` field is null and the run is still fine.

--------------------------------------------------------------------
COST

psutil's per-process numbers are cheap; looking for the server among all
processes is not (tens of ms on Windows), so the search runs at most once
every SEARCH_EVERY_S while the server is missing. The sampling itself runs
in a worker thread, so it never adds to the loop lag it is measuring.

psutil is imported only when --watch-machine is used: nothing else in the
harness, the agent or the analysis needs it.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable

from agent.logging_setup import get_logger
from harness import timing_log as tl

SEARCH_EVERY_S = 5.0
SERVER_MODULE = "csms.server"


def load_psutil() -> Any:
    """psutil, or a clear error saying how to get it."""
    try:
        import psutil  # noqa: PLC0415 - optional, only for --watch-machine
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise SystemExit(
            "--watch-machine needs psutil. Run: python -m pip install -r requirements.txt"
        ) from exc
    return psutil


def is_server_cmdline(cmdline: list[str] | None) -> bool:
    """True for `python -m csms.server ...`; False for a `cmd /c "..."` wrapper."""
    return bool(cmdline) and SERVER_MODULE in cmdline


def _mb(n_bytes: float) -> float:
    return round(n_bytes / (1024 * 1024), 1)


class MachineWatcher:
    """Samples the machine once per interval and writes machine_sample lines."""

    def __init__(
        self,
        log: tl.TimingLog,
        *,
        interval_s: float = 1.0,
        server_pid: int | None = None,
        server_pid_fn: Callable[[], int | None] | None = None,
        psutil_module: Any = None,
    ) -> None:
        self.log = log
        self.interval_s = interval_s
        self.server_pid = server_pid
        self.server_pid_fn = server_pid_fn
        self.logger = get_logger(__name__)
        self.psutil = psutil_module or load_psutil()

        self.samples = 0
        self.errors = 0
        self._me = self.psutil.Process(os.getpid())
        self._server: Any = None
        self._last_search = -SEARCH_EVERY_S
        self._server_announced: int | None = None

        # cpu_percent() measures since the previous call; the first call
        # always returns 0.0, so prime both counters now.
        self.psutil.cpu_percent(interval=None)
        self._me.cpu_percent(interval=None)

    # -- the server process ------------------------------------------------

    def _wanted_pid(self) -> int | None:
        if self.server_pid is not None:
            return self.server_pid
        if self.server_pid_fn is not None:
            try:
                return self.server_pid_fn()
            except Exception:  # noqa: BLE001 - display data only
                return None
        return None

    def _find_server(self) -> Any:
        """The server's psutil.Process, or None. Re-found after a restart."""
        ps = self.psutil
        wanted = self._wanted_pid()
        if self._server is not None:
            try:
                alive = self._server.is_running()
            except Exception:  # noqa: BLE001
                alive = False
            if alive and (wanted is None or self._server.pid == wanted):
                return self._server
            self._server = None

        if wanted is not None:
            try:
                self._server = ps.Process(wanted)
            except Exception:  # noqa: BLE001 - not started yet / gone
                return None
        else:
            now = time.monotonic()
            if now - self._last_search < SEARCH_EVERY_S:
                return None
            self._last_search = now
            for proc in ps.process_iter(["pid", "cmdline"]):
                try:
                    if proc.pid != self._me.pid and is_server_cmdline(proc.info.get("cmdline")):
                        self._server = proc
                        break
                except Exception:  # noqa: BLE001 - processes come and go
                    continue
            if self._server is None:
                return None

        # A fresh Process object: prime its CPU counter (first value is 0.0).
        try:
            self._server.cpu_percent(interval=None)
        except Exception:  # noqa: BLE001
            self._server = None
            return None
        if self._server_announced != self._server.pid:
            self._server_announced = self._server.pid
            self.logger.info("machine watcher: CSMS process found (pid %d)", self._server.pid)
        return self._server

    @staticmethod
    def _proc(proc: Any) -> dict[str, Any]:
        with proc.oneshot():
            return {
                "pid": proc.pid,
                "cpu_pct": round(proc.cpu_percent(interval=None), 1),
                "rss_mb": _mb(proc.memory_info().rss),
                "threads": proc.num_threads(),
            }

    # -- one sample --------------------------------------------------------------

    def sample(self, loop_lag_ms: float | None) -> dict[str, Any]:
        """Blocking (psutil). Called in a worker thread by run()."""
        ps = self.psutil
        vm = ps.virtual_memory()
        row: dict[str, Any] = {
            "cpu_pct": round(ps.cpu_percent(interval=None), 1),
            "cpu_count": ps.cpu_count() or 1,
            "mem_pct": round(vm.percent, 1),
            "mem_used_mb": _mb(vm.total - vm.available),
            "loop_lag_ms": None if loop_lag_ms is None else round(loop_lag_ms, 2),
            "tester": self._proc(self._me),
            "server": None,
        }
        server = self._find_server()
        if server is not None:
            try:
                row["server"] = self._proc(server)
            except Exception:  # noqa: BLE001 - it exited between find and read
                self._server = None
        return row

    # -- the loop ------------------------------------------------------------------

    async def run(self) -> None:
        """Sample until cancelled. Never raises (instrumentation only)."""
        while True:
            before = time.perf_counter()
            await asyncio.sleep(self.interval_s)
            # How much later than asked the loop woke us: the loop lag.
            lag_ms = max(0.0, (time.perf_counter() - before - self.interval_s) * 1000.0)
            try:
                row = await asyncio.to_thread(self.sample, lag_ms)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a sample lost is not a run lost
                self.errors += 1
                if self.errors == 1:
                    self.logger.warning("machine sample failed", exc_info=True)
                continue
            self.samples += 1
            self.log.emit(tl.MACHINE_SAMPLE, None, **row)
