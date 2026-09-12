"""
Throwaway CSMS, for Track C's own testing.

Track C (tests). Phase C1. Dev/test only, never imported by shipped code.

--------------------------------------------------------------------
THIS IS THE MIRROR IMAGE OF fake_station.py — DO NOT MERGE THEM

    tests/fixtures/fake_station.py   Track A's. A fake CLIENT. It
                                     pretends to be a charging station
                                     so the real server can be tested.

    tests/fixtures/fake_csms.py      Track C's, this file. A fake
                                     SERVER. It pretends to be the CSMS
                                     so the real agent can be tested.

Same folder, opposite jobs. Both are needed and neither replaces the
other.

--------------------------------------------------------------------
WHY A FAKE SERVER AT ALL, NOW THAT THE REAL ONE WORKS

Two reasons that do not go away:

1. Speed and isolation. csms/server.py starts an event log, a fleet
   registry, signal handlers and (from Day 6) SQLite. For a unit test
   asking only "does the agent send a well-formed BootNotification",
   this file is faster and leaves no shared state behind.

2. Failure injection, which is the real reason. The real server will
   not, on request, refuse a connection, stall, return an error, or
   hang up mid-session. Phase C4 -- reconnection with backoff -- is
   precisely the code that needs those conditions, and E2 is built on
   C4. A server you control can produce them on demand.

THE RULE THAT KEEPS THIS HONEST: this fake is for failure paths and
fast tests. Every happy path must ALSO be verified against the real
csms/server.py. A fake server that is the only evidence something
works is a test that passes while the integration is broken -- worse
than having no test, because it is believed.

--------------------------------------------------------------------
IT ANSWERS EXACTLY WHAT THE REAL SERVER ANSWERS

Response shapes are copied from csms/handlers.py so the agent cannot
tell the two apart:

    BootNotification    -> current_time, interval, status
    Heartbeat           -> current_time
    StatusNotification  -> (empty)
    Authorize           -> id_token_info={"status": ...}
    TransactionEvent    -> (empty)

Status strings are sent as plain strings rather than imported from
ocpp.v201.enums, following the convention fake_station.py sets out: the
enum CLASS names have moved between releases of that library, while the
wire values are fixed by OCPP 2.0.1 and cannot. The library's schema
validator rejects a wrong value, so a typo fails at the first message
rather than silently.

--------------------------------------------------------------------
THE DEFAULT HEARTBEAT INTERVAL IS 7 SECONDS, AND THAT IS DELIBERATE

The real server issues 20. This one issues 7, so that an agent which
hardcodes 20 instead of reading the value out of the BootNotification
response FAILS VISIBLY against this fixture. A test that cannot catch
that mistake is not worth writing -- and it is an easy mistake, because
20 works right up until the server's interval changes.

--------------------------------------------------------------------
USAGE

    # plain run, port 9100
    python tests/fixtures/fake_csms.py

    # prove the fixture speaks OCPP, using Track A's real client
    python tests/fixtures/fake_csms.py --port 9100
    python tests/fixtures/fake_station.py CP001 --url ws://localhost:9100

    # failure injection
    python tests/fixtures/fake_csms.py --fail-boot
    python tests/fixtures/fake_csms.py --callerror-on TransactionEvent
    python tests/fixtures/fake_csms.py --delay-s 2.0
    python tests/fixtures/fake_csms.py --drop-after 5
    python tests/fixtures/fake_csms.py --reject-connections
--------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

# The asyncio server API, matching csms/server.py rather than the older
# top-level websockets.serve() used by experiments/smoke_test.py. Pinned
# to websockets >= 13 in requirements.txt; the top-level function is the
# legacy interface and warns on newer releases.
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from ocpp.routing import on
from ocpp.v201 import ChargePoint as CpBase
from ocpp.v201 import call_result

# Track C's shared logging, so this fixture exercises the same setup the
# real agent uses. If logging_setup is broken, running this file shows it
# immediately.
from agent.logging_setup import configure_logging, get_logger

# 9100, not 9000. The real CSMS owns 9000; a fixture that defaults to
# the same port will one day connect a test to the real server, or the
# reverse, and the failure looks like a protocol bug rather than a port
# collision.
DEFAULT_PORT = 9100
DEFAULT_HOST = "localhost"

# See the module docstring: deliberately not 20.
DEFAULT_HEARTBEAT_INTERVAL_S = 7

# Both tags csms/server.py accepts.
SUBPROTOCOLS = ["ocpp2.0.1", "ocpp2.0.1+pqc"]

# Plain wire strings, per the convention above.
STATUS_ACCEPTED = "Accepted"
STATUS_REJECTED = "Rejected"
STATUS_BLOCKED = "Blocked"
STATUS_INVALID = "Invalid"

# Mirrors the defaults in csms/authorization.py.
#
# Deliberately a local copy rather than an import of Track A's module.
# This fixture has to keep working when csms/ is mid-refactor or broken
# -- being independent of it is part of the point. The cost is that the
# two can drift, so: THE REAL SERVER IS THE AUTHORITY. If a token
# behaves differently there, csms/authorization.py is right and this
# table is stale.
DEFAULT_ID_TOKENS: dict[str, str] = {
    "TAG-0001": STATUS_ACCEPTED,
    "TAG-0002": STATUS_ACCEPTED,
    "TAG-0003": STATUS_ACCEPTED,
    "TAG-0004": STATUS_ACCEPTED,
    "TAG-BLOCKED": STATUS_BLOCKED,
}


def _now_iso() -> str:
    """Timezone-aware UTC, ISO-8601 — what OCPP timestamps require."""
    return datetime.now(timezone.utc).isoformat()


def _station_id_from_path(path: str) -> str:
    """
    Take the station identity from the LAST path segment.

    Copied from csms/server.py on purpose. The real server does this so
    that a third-party client using /ocpp/{id} still works, and a
    fixture that instead stripped slashes off the whole path would turn
    "/ocpp/CP001" into "ocpp/CP001" -- behaving differently from the
    thing it is standing in for, which defeats its purpose.
    """
    segments = [s for s in urlparse(path).path.split("/") if s]
    return unquote(segments[-1]) if segments else ""


@dataclass
class FaultConfig:
    """
    Which ways this server should misbehave.

    All off by default, so an unconfigured fixture is a well-behaved
    CSMS. Each switch exists because some Track C code path needs the
    matching failure and the real server will not produce it on demand.
    """

    fail_boot: bool = False
    """Answer BootNotification with Rejected. The agent should not
    proceed to charge. Tests that it reads the status at all."""

    callerror_on: set[str] = field(default_factory=set)
    """
    Action names to raise inside, e.g. {"TransactionEvent"}.

    The ocpp library turns an exception in a handler into a CALLError
    sent back to the station, and -- crucially -- the connection
    survives. Track A lost an entire class of event to exactly this on
    their Day 5, silently, because the station carried on charging.
    Track C's agent is required to log these loudly; this switch is how
    that requirement gets tested.
    """

    delay_s: float = 0.0
    """Stall this long before answering. Tests client-side timeouts."""

    drop_after: int = 0
    """Close the connection after this many messages. 0 disables.
    Drives the Phase C4 reconnect path without stopping the server."""

    reject_connections: bool = False
    """Close every connection immediately after it opens. Approximates
    a server that is up but refusing work."""

    auth_mode: str = "allowlist"
    """"allowlist" uses the token table; "accept-all" authorises
    everything, mirroring the real server's --auth-mode escape hatch."""


class FakeCSMSHandlers(CpBase):
    """
    One instance per connected station, for the length of that connection.

    Subclasses the ocpp library's ChargePoint, the same way
    csms/handlers.py does. Each handler answers the station and logs;
    unlike the real server it keeps no registry and writes no event log,
    because a fixture that wrote to logs/events.jsonl would pollute the
    measurement surface -- see the warning at the top of
    agent/logging_setup.py.
    """

    def __init__(
        self,
        station_id: str,
        connection: Any,
        interval_s: int,
        faults: FaultConfig,
        tokens: dict[str, str],
    ) -> None:
        super().__init__(station_id, connection)
        self.interval_s = interval_s
        self.faults = faults
        self.tokens = tokens
        self.message_count = 0
        self.log = get_logger(__name__, station_id=station_id)

    # -- shared behaviour for every handler ------------------------------

    async def _pre(self, action: str) -> None:
        """
        Runs at the top of every handler: count, delay, fault, drop.

        Kept in one place so a new handler cannot accidentally skip the
        fault injection and make a test silently pass.
        """
        self.message_count += 1
        self.log.debug("<- %s (message #%d)", action, self.message_count)

        if self.faults.delay_s > 0:
            self.log.debug("stalling %.2fs before answering %s",
                           self.faults.delay_s, action)
            await asyncio.sleep(self.faults.delay_s)

        if action in self.faults.callerror_on:
            # Raising here is the point. The ocpp library catches it and
            # returns a CALLError to the station; the socket stays open.
            self.log.warning(
                "fault injection: raising inside %s so the agent receives "
                "a CALLError", action,
            )
            raise RuntimeError(f"injected fault in {action}")

        if self.faults.drop_after and self.message_count >= self.faults.drop_after:
            self.log.warning(
                "fault injection: closing after %d messages",
                self.message_count,
            )
            # Close from under the handler. The agent should notice, log
            # a warning, and start its backoff loop.
            await self._connection.close(code=1011, reason="injected drop")

    # -- BootNotification -------------------------------------------------

    @on("BootNotification")
    async def on_boot_notification(
        self, charging_station: dict, reason: str, **kwargs: Any
    ):
        """
        The station announcing itself. Answers with the interval it must use.

        **kwargs for the same reason csms/handlers.py uses it: OCPP
        payloads carry optional fields this fixture does not read, and a
        handler that rejected an unexpected key would fail against a
        conformant client.
        """
        await self._pre("BootNotification")

        model = charging_station.get("model", "?")
        vendor = charging_station.get("vendor_name", "?")
        status = STATUS_REJECTED if self.faults.fail_boot else STATUS_ACCEPTED

        self.log.info(
            "boot from %s/%s reason=%s -> %s, interval=%ds",
            vendor, model, reason, status, self.interval_s,
        )

        return call_result.BootNotification(
            current_time=_now_iso(),
            interval=self.interval_s,
            status=status,
        )

    # -- Heartbeat ---------------------------------------------------------

    @on("Heartbeat")
    async def on_heartbeat(self, **kwargs: Any):
        """Keepalive. DEBUG only — at fleet scale this dominates a log."""
        await self._pre("Heartbeat")
        self.log.debug("heartbeat")
        return call_result.Heartbeat(current_time=_now_iso())

    # -- StatusNotification ------------------------------------------------

    @on("StatusNotification")
    async def on_status_notification(
        self,
        timestamp: str,
        connector_status: str,
        evse_id: int,
        connector_id: int,
        **kwargs: Any,
    ):
        """A physical connector state change — the CPS sensing path."""
        await self._pre("StatusNotification")
        self.log.info(
            "status -> %s (evse=%s connector=%s)",
            connector_status, evse_id, connector_id,
        )
        return call_result.StatusNotification()

    # -- Authorize ----------------------------------------------------------

    @on("Authorize")
    async def on_authorize(self, id_token: dict, **kwargs: Any):
        """
        Whether a driver may charge.

        A refusal is a result, not an error: the agent is expected to
        stop and start no transaction, which is what makes TAG-BLOCKED a
        usable demonstration rather than a crash.
        """
        await self._pre("Authorize")

        token = (id_token or {}).get("id_token", "")
        if self.faults.auth_mode == "accept-all":
            status = STATUS_ACCEPTED
        else:
            status = self.tokens.get(token, STATUS_INVALID)

        self.log.info("authorize %s -> %s", token or "<empty>", status)
        return call_result.Authorize(id_token_info={"status": status})

    # -- TransactionEvent ----------------------------------------------------

    @on("TransactionEvent")
    async def on_transaction_event(
        self,
        event_type: str,
        timestamp: str,
        trigger_reason: str,
        seq_no: int,
        transaction_info: dict,
        **kwargs: Any,
    ):
        """
        A charging session starting, progressing or ending.

        Logs the meter readings it was sent, so a test can confirm the
        agent is reporting power and energy in the shape and units
        csms/metering.py expects -- watts under Power.Active.Import and
        watt-hours under Energy.Active.Import.Register. A mismatch there
        shows up on the real server as a fleet drawing zero power, which
        is the failure E5's whole demonstration depends on not having.
        """
        await self._pre("TransactionEvent")

        tx_id = (transaction_info or {}).get("transaction_id", "?")
        readings = _summarise_meter_values(kwargs.get("meter_value") or [])

        self.log.info(
            "transaction %s seq=%s tx=%s%s",
            event_type, seq_no, tx_id,
            f" [{readings}]" if readings else "",
        )
        return call_result.TransactionEvent()


def _summarise_meter_values(meter_values: list[dict]) -> str:
    """
    Flatten OCPP meter values into something readable in one log line.

    Reads the measurand labels rather than assuming positions, matching
    how csms/metering.py does it, so this summary reflects what the real
    server would actually understand.
    """
    parts: list[str] = []
    for entry in meter_values:
        for sample in entry.get("sampled_value", []) or []:
            measurand = sample.get("measurand", "(none)")
            unit = ((sample.get("unit_of_measure") or {}).get("unit", ""))
            parts.append(f"{measurand}={sample.get('value')}{unit}")
    return " ".join(parts)


class FakeCSMS:
    """The server itself: accepts connections and hands each to a handler."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S,
        faults: FaultConfig | None = None,
        tokens: dict[str, str] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.interval_s = interval_s
        self.faults = faults or FaultConfig()
        self.tokens = dict(tokens or DEFAULT_ID_TOKENS)
        self.log = get_logger(__name__)

        self.connections: list[FakeCSMSHandlers] = []
        """Every handler created, for tests that want to assert on what
        the server saw."""

        self._server: Any = None

    async def _on_connect(self, connection: Any) -> None:
        """
        One station's connection, start to finish.

        The ocpp library's start() is a receive loop that runs until the
        socket closes, so this coroutine lives as long as the station
        does.
        """
        path = getattr(getattr(connection, "request", None), "path", "") or ""
        station_id = _station_id_from_path(path)

        if not station_id:
            # Same rule as the real server: no identity, no session.
            self.log.warning("connection with no station id in path %r", path)
            await connection.close(code=1008, reason="station id required in path")
            return

        if self.faults.reject_connections:
            self.log.warning(
                "fault injection: rejecting %s immediately", station_id
            )
            await connection.close(code=1013, reason="injected rejection")
            return

        handler = FakeCSMSHandlers(
            station_id, connection, self.interval_s, self.faults, self.tokens
        )
        self.connections.append(handler)
        self.log.info("connected: %s", station_id)

        try:
            await handler.start()
        except ConnectionClosed:
            # Normal: the station hung up, or a fault closed the socket.
            # Not an error, and not worth an ERROR line during E2 where
            # disconnection is the thing being measured.
            pass
        finally:
            self.log.info(
                "disconnected: %s after %d messages",
                station_id, handler.message_count,
            )

    async def start(self) -> None:
        """Begin listening. Returns once the socket is open."""
        self._server = await serve(
            self._on_connect, self.host, self.port, subprotocols=SUBPROTOCOLS
        )
        self.log.info(
            "fake CSMS listening on ws://%s:%d  interval=%ds  faults=%s",
            self.host, self.port, self.interval_s, _describe_faults(self.faults),
        )

    async def stop(self) -> None:
        """Stop listening and wait for the socket to close."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            self.log.info("fake CSMS stopped")

    async def __aenter__(self) -> FakeCSMS:
        """
        Lets a test write:

            async with FakeCSMS(port=9101) as server:
                ...

        and be sure the port is released even if the test fails, which
        otherwise leaves a dangling listener that makes the *next* test
        fail for an unrelated reason.
        """
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()


def _describe_faults(faults: FaultConfig) -> str:
    """Human-readable summary for the start-up line."""
    active: list[str] = []
    if faults.fail_boot:
        active.append("fail-boot")
    if faults.callerror_on:
        active.append(f"callerror-on={','.join(sorted(faults.callerror_on))}")
    if faults.delay_s:
        active.append(f"delay={faults.delay_s}s")
    if faults.drop_after:
        active.append(f"drop-after={faults.drop_after}")
    if faults.reject_connections:
        active.append("reject-connections")
    if faults.auth_mode != "allowlist":
        active.append(f"auth={faults.auth_mode}")
    return ",".join(active) if active else "none"


async def main_async(args: argparse.Namespace) -> None:
    """Run until interrupted."""
    faults = FaultConfig(
        fail_boot=args.fail_boot,
        callerror_on=set(args.callerror_on or []),
        delay_s=args.delay_s,
        drop_after=args.drop_after,
        reject_connections=args.reject_connections,
        auth_mode=args.auth_mode,
    )
    server = FakeCSMS(
        host=args.host, port=args.port,
        interval_s=args.interval, faults=faults,
    )
    async with server:
        # Sleep forever; Ctrl-C unwinds through the context manager and
        # closes the socket properly.
        await asyncio.Future()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="fake CSMS for Track C agent testing (dev/test only)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_HEARTBEAT_INTERVAL_S,
        help="heartbeat interval to issue; deliberately not 20 so that an "
             "agent hardcoding the real server's value fails here",
    )
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    faults = parser.add_argument_group("fault injection")
    faults.add_argument("--fail-boot", action="store_true",
                        help="answer BootNotification with Rejected")
    faults.add_argument("--callerror-on", action="append", metavar="ACTION",
                        help="raise inside this handler so the agent gets a "
                             "CALLError; repeatable")
    faults.add_argument("--delay-s", type=float, default=0.0,
                        help="stall this long before answering")
    faults.add_argument("--drop-after", type=int, default=0,
                        help="close the connection after N messages")
    faults.add_argument("--reject-connections", action="store_true",
                        help="close every connection immediately")
    faults.add_argument("--auth-mode", default="allowlist",
                        choices=["allowlist", "accept-all"])

    args = parser.parse_args()

    configure_logging("fake-csms", args.log_level)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main_async(args))


if __name__ == "__main__":
    main()