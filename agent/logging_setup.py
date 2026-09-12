"""
Logging setup — one consistent way for Track C to write a log line.

Track C (agent). Phase C1. Used by agent/, harness/, dashboard/ and
analysis/.

--------------------------------------------------------------------
THIS IS NOT THE EVENT LOG. READ THIS PARAGRAPH.

Two different things on this project are called "logging", and mixing
them corrupts the results:

  logs/events.jsonl      Contract 3. Machine-readable JSON Lines,
                         written ONLY by Track A's CSMS. Every number
                         in the results chapter is derived from it.

  what this module writes Human-readable text for debugging. Never
                         parsed, never analysed, never a data source.

The agent must never write to the first one. An agent-side event
appended to the CSMS's event log would be counted alongside the
server's own record of the same thing, and every figure derived from
that file would be quietly wrong. analysis/parse_events.py is the only
Track C module that touches events.jsonl, and only to read it.
--------------------------------------------------------------------

WHAT PROBLEM THIS SOLVES

Ordinary logging tells you what happened but not which station it
happened to. During a five-hundred-agent run that is useless: the
interesting line is buried among hundreds of identical ones from other
stations. So every record carries a component and a station id, and a
run can be filtered down to one station with grep.

    10:42:03 INFO     [agent/CP001] agent.client: connected in 41.2ms
    10:42:03 INFO     [agent/CP002] agent.client: connected in 38.7ms
    10:42:04 ERROR    [agent/CP001] agent.client: CALLError on
                      TransactionEvent: InternalError - ...

--------------------------------------------------------------------
WHY IT LIVES IN agent/

harness/, dashboard/ and analysis/ all import it, which makes agent/ a
slightly odd home. The alternative was a new top-level package, and
that would add a directory to the agreed repository structure that
neither other track has signed up to. agent/ is the package the rest of
Track C already depends on, so it is the least surprising place.

--------------------------------------------------------------------
DEPENDENCY RULE

This module must NOT import agent.config, and config must not import
this. See the same note in config.py. The entry point wires them:

    cfg = AgentConfig.from_args()
    log = configure_logging("agent", cfg.log_level, cfg.station_id)
--------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

# The format every Track C program shares.
#
#   component   which program: agent, harness, dashboard, analysis
#   station_id  which station, or "-" where the concept does not apply
#   name        the Python module that logged it
#
LOG_FORMAT = (
    "%(asctime)s %(levelname)-8s [%(component)s/%(station_id)s] "
    "%(name)s: %(message)s"
)

# Seconds resolution. Millisecond timestamps in the text log would
# imply this file is a timing source, and it is not -- all measured
# durations come from the CSMS's Contract 3 event log, which records a
# monotonic clock reading precisely because wall-clock text like this
# is not good enough to measure with.
LOG_DATEFMT = "%H:%M:%S"

# Placeholder when a record has no station -- harness-level lines, the
# dashboard, analysis scripts.
NO_STATION = "-"

# Third-party loggers that are far too chatty at DEBUG. websockets in
# particular logs every frame, which at fleet scale is thousands of
# lines a second and will slow the event loop enough to distort the
# very timings E1 and E2 exist to measure.
NOISY_LOGGERS = ("websockets", "websockets.client", "websockets.server",
                 "ocpp", "asyncio")

# Set by configure_logging() so a second call is a no-op rather than
# attaching a duplicate handler and printing everything twice.
_CONFIGURED = False


class _ContextFilter(logging.Filter):
    """
    Guarantees every record has `component` and `station_id`.

    This class is why the format string above is safe. A formatter
    referencing %(component)s raises if it meets a record without that
    attribute -- and records arrive from libraries that have never
    heard of this project. websockets logging a dropped connection
    would produce a formatting error on top of the real error, which is
    an unpleasant way to lose the actual message.

    So the filter fills in defaults for anything that did not come
    through our own adapter. Filters attached to a handler run on every
    record passing through it, including third-party ones, which makes
    this the right hook rather than doing it in the formatter.
    """

    def __init__(self, component: str, station_id: str) -> None:
        super().__init__()
        self.component = component
        self.station_id = station_id

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "component"):
            record.component = self.component
        if not hasattr(record, "station_id"):
            record.station_id = self.station_id
        # Always True: this filter annotates, it never drops records.
        return True


class StationLoggerAdapter(logging.LoggerAdapter):
    """
    A logger that stamps component and station id onto each record.

    Used instead of passing the station id into every message by hand,
    which is easy to forget in exactly the error paths where it matters
    most.

    Behaves like a normal logger: .debug(), .info(), .warning(),
    .error(), .exception() all work as usual.
    """

    def process(
        self, msg: Any, kwargs: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        # `extra` is how arbitrary attributes reach a LogRecord.
        # Anything the caller passes in its own `extra` wins, so a
        # single call can override the station id if it ever needs to.
        extra = dict(self.extra or {})
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs


def configure_logging(
    component: str,
    level: str = "INFO",
    station_id: str | None = None,
    *,
    log_to_file: bool = False,
    log_dir: str | Path = "logs",
    file_name: str | None = None,
    quiet_third_party: bool = True,
) -> StationLoggerAdapter:
    """
    Configure logging for this process, once, and return a logger.

    Call this exactly once, early, from whatever starts the program --
    agent/client.py's main(), the load generator, the dashboard, an
    analysis script. Every other module then calls get_logger().

    Args:
        component: which program this is: "agent", "harness",
            "dashboard", "analysis". Appears in every line.
        level: DEBUG, INFO, WARNING or ERROR. The harness passes
            WARNING for the agents it spawns; see the note on
            AgentConfig.log_level for why.
        station_id: default station for records that do not set one.
            None becomes "-".
        log_to_file: also write to a file under log_dir. Off by
            default: at five hundred agents in one process that is five
            hundred open file handles for no benefit.
        log_dir: directory for the file handler. Created if missing.
            .gitignore already ignores everything under logs/ except
            .gitkeep, so run output cannot be committed by accident.
        file_name: override the derived file name.
        quiet_third_party: hold websockets, ocpp and asyncio at
            WARNING even when this process is at DEBUG.

    Returns:
        A logger that already carries component and station id.

    Calling this twice is harmless: the second call adjusts the level
    and returns a logger, but does not attach a second handler. Without
    that guard, a harness that configured logging per agent would print
    every line five hundred times.
    """
    global _CONFIGURED

    station = station_id or NO_STATION
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    if not _CONFIGURED:
        formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
        context = _ContextFilter(component, station)

        # stderr, not stdout. Anything this program prints as actual
        # output -- a summary table, a JSON blob -- goes to stdout, and
        # keeping the two apart means `program > results.txt` captures
        # results without diagnostics mixed in.
        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(formatter)
        stream.addFilter(context)
        root.addHandler(stream)

        if log_to_file:
            directory = Path(log_dir)
            directory.mkdir(parents=True, exist_ok=True)
            name = file_name or f"{component}_{station}.log"

            # Append rather than truncate: a reconnection experiment
            # restarts agents, and truncating would throw away the part
            # of the run that explains why.
            file_handler = logging.FileHandler(
                directory / name, mode="a", encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(context)
            root.addHandler(file_handler)

        _CONFIGURED = True

    if quiet_third_party:
        for name in NOISY_LOGGERS:
            logging.getLogger(name).setLevel(max(numeric_level, logging.WARNING))

    return get_logger(component, component=component, station_id=station)


def get_logger(
    name: str,
    component: str | None = None,
    station_id: str | None = None,
) -> StationLoggerAdapter:
    """
    A logger for one module.

    Normal use, at the top of a module:

        log = get_logger(__name__)

    and inside a per-station class, where the station is known:

        self.log = get_logger(__name__, station_id=cfg.station_id)

    Fields left as None fall back to the defaults installed by
    configure_logging(), so a module need not know which program it is
    running inside.
    """
    extra: dict[str, Any] = {}
    if component is not None:
        extra["component"] = component
    if station_id is not None:
        extra["station_id"] = station_id
    return StationLoggerAdapter(logging.getLogger(name), extra)


def reset_logging() -> None:
    """
    Undo configure_logging(). For tests only.

    pytest runs many tests in one process. Without this, the first test
    to configure logging installs handlers that every later test
    inherits, and assertions about log output start depending on test
    order.
    """
    global _CONFIGURED
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    _CONFIGURED = False