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

    # command dispatch (Phase C3) -- stands in for csms/dispatch.py
    python -m tests.fixtures.fake_csms --set-limit-w 0        # curtail
    python -m tests.fixtures.fake_csms --set-limit-w 3700
    python -m tests.fixtures.fake_csms --set-limit-w 16 --limit-unit A
    python -m tests.fixtures.fake_csms --set-limit-w 0 --clear-after 8
    python -m tests.fixtures.fake_csms --stop-after 6
    python -m tests.fixtures.fake_csms --trigger-after 5
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
from ocpp.v201 import call, call_result

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


# =====================================================================
# COMMAND DISPATCH (Phase C3)
# =====================================================================
#
# *** WHY THIS LIVES IN A TEST FIXTURE ***
#
# csms/dispatch.py -- Track A's Days 8-9 work, the real server's
# outbound command path -- DOES NOT EXIST YET. Phase C3 builds the
# agent's half: the handlers that receive SetChargingProfile,
# RequestStopTransaction and friends, and act on them physically.
#
# Waiting for Track A would mean either blocking C3 or, worse, writing
# the agent's handlers untested and finding out at integration time.
# Teaching this fixture to originate the same commands unblocks C3
# completely, and when the real dispatcher lands NOTHING IN agent/
# CHANGES -- the agent cannot tell which server sent the command.
#
# The rule from the top of this file still applies: this fixture proves
# the agent's behaviour, it does not prove the integration. Once
# csms/dispatch.py exists, the happy paths must be re-run against it.


@dataclass
class CommandPlan:
    """
    Commands to send to a station, and when.

    "When" is measured in messages received from that station, not in
    seconds. Timing a command by the clock makes a test that passes on a
    fast machine and fails on a loaded one; counting messages puts the
    command at a deterministic point in the session every time.
    """

    set_limit_w: float | None = None
    """Send SetChargingProfile with this limit. 0 is the curtailment
    case -- the one E5 is built on."""

    limit_unit: str = "W"
    """"W" or "A". Sending "A" exercises the agent's amps conversion,
    which is the single most plausible place for a silent factor-of-230
    error in the whole actuation path."""

    after_messages: int = 3
    """Fire once the station has sent this many messages. 3 puts it
    shortly after the transaction starts (boot, status, authorize,
    status, Started...) without depending on exact ordering."""

    clear_after_messages: int = 0
    """Send ClearChargingProfile at this message count. 0 disables."""

    stop_after_messages: int = 0
    """Send RequestStopTransaction at this message count. 0 disables."""

    trigger_after_messages: int = 0
    """Send TriggerMessage at this message count. 0 disables."""

    trigger_message: str = "StatusNotification"
    """What to ask for."""

    def is_empty(self) -> bool:
        return not any((
            self.set_limit_w is not None,
            self.clear_after_messages,
            self.stop_after_messages,
            self.trigger_after_messages,
        ))


def build_charging_profile(
    limit: float,
    *,
    unit: str = "W",
    profile_id: int = 100,
    stack_level: int = 0,
    purpose: str = "TxDefaultProfile",
    number_phases: int | None = None,
) -> dict:
    """
    A minimal, schema-valid OCPP 2.0.1 charging profile.

    Written out in full rather than hidden behind defaults, because the
    nesting is the part that is easy to get wrong and this is the
    reference the real csms/dispatch.py should copy:

        chargingProfile
          └─ chargingSchedule            (a LIST)
               └─ chargingSchedulePeriod (also a LIST)
                    └─ limit             <- the number that matters

    Keys are snake_case here. The ocpp library converts them to the
    camelCase the wire requires, recursively, when the call is sent.
    """
    period: dict[str, Any] = {"start_period": 0, "limit": limit}
    if number_phases is not None:
        period["number_phases"] = number_phases

    return {
        "id": profile_id,
        "stack_level": stack_level,
        "charging_profile_purpose": purpose,
        "charging_profile_kind": "Absolute",
        "charging_schedule": [
            {
                "id": 1,
                "charging_rate_unit": unit,
                "charging_schedule_period": [period],
            }
        ],
    }


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

        # -- what this server saw, for tests to assert on (Phase C3) ----
        #
        # A fake server that only logs is a fake server every test has to
        # scrape stdout to use. Recording the interesting fields turns
        # "did the curtailment take effect" into one list comparison.

        self.statuses: list[str] = []
        """Every connector_status received, in order."""

        self.charging_states: list[str] = []
        """Every charging_state received, in order."""

        self.trigger_reasons: list[str] = []
        """Every TransactionEvent trigger_reason, in order. This is how a
        test proves a curtailment is IDENTIFIABLE in the event log and
        not just present -- ChargingStateChanged rather than
        MeterValuePeriodic."""

        self.power_readings: list[float] = []
        """Every Power.Active.Import value received, in watts. The
        actual evidence that actuation worked."""

        self.energy_readings: list[float] = []

        # -- Phase C4 additions, for reconnection assertions -----------

        self.seq_numbers: list[int] = []
        """Every seq_no received, in arrival order. Proving a replay
        arrived in order and without duplicates is a list comparison."""

        self.event_types: list[str] = []
        """Started / Updated / Ended, in order."""

        self.event_timestamps: list[str] = []
        """
        The OUTER TransactionEvent timestamp of each event.

        ISO-8601 UTC strings sort chronologically as plain strings, so a
        test can assert that replayed events predate the live ones that
        follow without parsing anything. That assertion is how a replay
        stamped at send time instead of at reading time gets caught --
        which would otherwise silently disable csms/registry.py's
        last_meter_at staleness guard.
        """

        self.offline_flags: list[bool] = []
        """Whether each event carried offline=True. A replay that
        forgot the flag is invisible in Track A's log."""

        self.last_transaction_id: str | None = None
        """So a RequestStopTransaction can name the right transaction
        without the test having to guess it."""

        self.command_results: list[tuple[str, str]] = []
        """(action, status) for every command this server SENT. A
        command the agent rejected shows up here, which is the
        difference between "the agent ignored us" and "the agent told us
        why it would not comply"."""

        self._message_event = asyncio.Event()
        """Pulsed on every inbound message so wait_for_messages() can
        fire a command at a deterministic point in the session."""

    # -- shared behaviour for every handler ------------------------------

    async def _pre(self, action: str) -> None:
        """
        Runs at the top of every handler: count, delay, fault, drop.

        Kept in one place so a new handler cannot accidentally skip the
        fault injection and make a test silently pass.
        """
        self.message_count += 1
        self.log.debug("<- %s (message #%d)", action, self.message_count)

        # Wake anything waiting on a message count. Set-then-clear is a
        # pulse: waiters re-check the count themselves, so a waiter that
        # arrives late is not left hanging on an event that was already
        # consumed.
        self._message_event.set()
        self._message_event.clear()

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
        self.statuses.append(connector_status)
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

        info = transaction_info or {}
        tx_id = info.get("transaction_id", "?")
        meter_values = kwargs.get("meter_value") or []
        readings = _summarise_meter_values(meter_values)

        # -- record, so tests can assert instead of scraping the log ----
        self.last_transaction_id = info.get("transaction_id") or self.last_transaction_id
        if info.get("charging_state"):
            self.charging_states.append(info["charging_state"])
        self.trigger_reasons.append(trigger_reason)

        self.seq_numbers.append(seq_no)
        self.event_types.append(event_type)
        self.event_timestamps.append(timestamp)
        self.offline_flags.append(bool(kwargs.get("offline", False)))

        power, energy = _extract_power_and_energy(meter_values)
        if power is not None:
            self.power_readings.append(power)
        if energy is not None:
            self.energy_readings.append(energy)

        self.log.info(
            "transaction %s seq=%s tx=%s trigger=%s state=%s%s",
            event_type, seq_no, tx_id, trigger_reason,
            info.get("charging_state", "-"),
            f" [{readings}]" if readings else "",
        )
        return call_result.TransactionEvent()

    # =================================================================
    # OUTBOUND — the server telling the station what to do (Phase C3)
    # =================================================================
    #
    # *** THESE MUST BE CALLED FROM A SEPARATE TASK, NEVER FROM A
    #     HANDLER ABOVE. ***
    #
    # call() awaits a response that arrives through this ChargePoint's
    # own receive loop -- the one running inside start(). A handler that
    # calls one of these is waiting for a message that cannot be read
    # until the handler returns. That is a deadlock, and it presents as
    # the connection freezing until the response timeout fires.
    #
    # FakeCSMS._run_command_plan() runs them in their own task, which is
    # the pattern csms/dispatch.py must also follow.

    async def wait_for_messages(self, count: int, timeout: float = 15.0) -> bool:
        """
        Block until this station has sent `count` messages.

        Returns False on timeout rather than raising, so a command plan
        that never fires ends the run with a clear log line instead of a
        traceback that buries whatever actually went wrong.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while self.message_count < count:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self.log.warning(
                    "timed out waiting for %d messages (saw %d); the command "
                    "plan will not fire", count, self.message_count,
                )
                return False
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._message_event.wait(), remaining)
        return True

    async def _dispatch(self, action: str, request: Any) -> str:
        """
        Send one command and report the status the station answered.

        suppress=False for the same reason agent/client.py uses it: a
        CALLError would otherwise come back as None and this fixture
        would record a command as having been delivered when the station
        in fact refused to parse it.
        """
        self.log.info("-> %s", action)
        try:
            response = await self.call(request, suppress=False)
        except Exception as exc:  # noqa: BLE001 - a fixture reports, never crashes
            self.log.error("%s failed: %s: %s", action, type(exc).__name__, exc)
            self.command_results.append((action, f"error:{type(exc).__name__}"))
            return "error"

        status = str(getattr(response, "status", "?"))
        info = getattr(response, "status_info", None)
        self.command_results.append((action, status))

        if status in ("Accepted",):
            self.log.info("%s -> %s", action, status)
        else:
            # The station refusing is a RESULT, not a fault. Logged at
            # warning with the reason it gave, because "the agent said
            # no and here is why" is the most useful line in the file
            # when an actuation test fails.
            self.log.warning("%s -> %s (%s)", action, status, info)
        return status

    async def send_set_charging_profile(
        self,
        limit: float,
        *,
        unit: str = "W",
        evse_id: int = 1,
        profile_id: int = 100,
        purpose: str = "TxDefaultProfile",
        number_phases: int | None = None,
    ) -> str:
        """Cap the station's power. limit=0 is curtailment."""
        return await self._dispatch(
            "SetChargingProfile",
            call.SetChargingProfile(
                evse_id=evse_id,
                charging_profile=build_charging_profile(
                    limit,
                    unit=unit,
                    profile_id=profile_id,
                    purpose=purpose,
                    number_phases=number_phases,
                ),
            ),
        )

    async def send_clear_charging_profile(
        self, profile_id: int | None = None
    ) -> str:
        """Remove the cap; the station returns to its own maximum."""
        request = (
            call.ClearChargingProfile(charging_profile_id=profile_id)
            if profile_id is not None
            else call.ClearChargingProfile()
        )
        return await self._dispatch("ClearChargingProfile", request)

    async def send_request_stop_transaction(
        self, transaction_id: str | None = None
    ) -> str:
        """
        Stop the transaction. OCPP 2.0.1 spelling.

        NOTE FOR TRACK A: this action is RequestStopTransaction. OCPP
        1.6's RemoteStopTransaction does not exist in 2.0.1 and the
        library will not serialise it.
        """
        tx_id = transaction_id or self.last_transaction_id or ""
        return await self._dispatch(
            "RequestStopTransaction",
            call.RequestStopTransaction(transaction_id=tx_id),
        )

    async def send_trigger_message(
        self, requested_message: str = "StatusNotification"
    ) -> str:
        """Ask for one message to be re-sent now."""
        return await self._dispatch(
            "TriggerMessage",
            call.TriggerMessage(requested_message=requested_message),
        )


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


def _extract_power_and_energy(
    meter_values: list[dict],
) -> tuple[float | None, float | None]:
    """
    Pull the two numbers a test actually cares about out of the nesting.

    Matches by measurand label rather than by position, exactly as
    csms/metering.py does, so a test asserting on these is asserting on
    what the REAL server would have understood -- not on the order the
    agent happened to put them in.
    """
    power: float | None = None
    energy: float | None = None
    for entry in meter_values:
        for sample in entry.get("sampled_value", []) or []:
            measurand = sample.get("measurand")
            value = sample.get("value")
            if measurand == "Power.Active.Import":
                power = float(value) if value is not None else None
            elif measurand == "Energy.Active.Import.Register":
                energy = float(value) if value is not None else None
    return power, energy


class FakeCSMS:
    """The server itself: accepts connections and hands each to a handler."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S,
        faults: FaultConfig | None = None,
        tokens: dict[str, str] | None = None,
        commands: CommandPlan | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.interval_s = interval_s
        self.faults = faults or FaultConfig()
        self.tokens = dict(tokens or DEFAULT_ID_TOKENS)
        self.commands = commands or CommandPlan()
        self.log = get_logger(__name__)

        self.connections: list[FakeCSMSHandlers] = []
        """Every handler created, for tests that want to assert on what
        the server saw."""

        self._server: Any = None
        self._command_tasks: list[asyncio.Task] = []

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

        # The command plan runs in ITS OWN TASK, concurrently with the
        # receive loop below. See the deadlock note on the outbound
        # methods: a command awaited from inside a handler can never be
        # answered.
        if not self.commands.is_empty():
            self._command_tasks.append(
                asyncio.ensure_future(self._run_command_plan(handler))
            )

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

    async def _run_command_plan(self, handler: FakeCSMSHandlers) -> None:
        """
        Fire the configured commands at the configured points.

        Stands in for csms/dispatch.py. Ordered by message count, so the
        sequence is deterministic regardless of machine speed.

        Every failure is swallowed and logged: this is a fixture, and a
        command plan that dies must not take the server down with it --
        that would turn one broken assertion into every test in the file
        failing for an unrelated reason.
        """
        plan = self.commands
        try:
            if plan.set_limit_w is not None:
                if await handler.wait_for_messages(plan.after_messages):
                    await handler.send_set_charging_profile(
                        plan.set_limit_w, unit=plan.limit_unit
                    )

            if plan.trigger_after_messages:
                if await handler.wait_for_messages(plan.trigger_after_messages):
                    await handler.send_trigger_message(plan.trigger_message)

            if plan.clear_after_messages:
                if await handler.wait_for_messages(plan.clear_after_messages):
                    await handler.send_clear_charging_profile()

            if plan.stop_after_messages:
                if await handler.wait_for_messages(plan.stop_after_messages):
                    await handler.send_request_stop_transaction()

        except asyncio.CancelledError:
            raise
        except ConnectionClosed:
            self.log.info("command plan ended: the station disconnected")
        except Exception:  # noqa: BLE001 - a fixture reports, never crashes
            self.log.exception("command plan failed")

    async def start(self) -> None:
        """Begin listening. Returns once the socket is open."""
        self._server = await serve(
            self._on_connect, self.host, self.port, subprotocols=SUBPROTOCOLS
        )
        self.log.info(
            "fake CSMS listening on ws://%s:%d  interval=%ds  faults=%s",
            self.host, self.port, self.interval_s, _describe_faults(self.faults),
        )

    # =================================================================
    # OUTAGE SIMULATION (Phase C4)
    # =================================================================
    #
    # *** AN OUTAGE IS NOT THE SAME AS A DROPPED CONNECTION. ***
    #
    # --drop-after closes one connection while the server stays up. The
    # agent reconnects on its first attempt, succeeds instantly, and
    # never enters backoff at all. That path is worth testing and it is
    # NOT what E2 does.
    #
    # E2 kills the CSMS. The port stops accepting, every attempt is
    # refused for the length of the outage, and the agent has to back
    # off repeatedly before anything works. That is the path with the
    # jitter in it, the offline queue, and the recovery measurement --
    # so it needs a server that can actually go away and come back.
    #
    # go_down() / come_back() do that on the same port. Sessions are
    # NOT carried across: coming back is a fresh server with an empty
    # registry, exactly as a restarted csms/server.py would be, and the
    # agent must rebuild the server's picture of it from scratch.

    async def go_down(self) -> None:
        """
        Stop accepting connections and cut every live one.

        Connection attempts during the outage are refused at the TCP
        level -- ConnectionRefusedError on the agent's side, which is
        what a killed CSMS produces and what run()'s retry loop is
        written against.
        """
        if self._server is None:
            return

        live = len(self.connections)
        self.log.warning(
            "*** OUTAGE: the fake CSMS is going down (%d live connection(s)) ***",
            live,
        )

        for task in self._command_tasks:
            if not task.done():
                task.cancel()

        self._server.close()
        await self._server.wait_closed()
        self._server = None

        # close() stops the listener; existing sockets may linger. Cut
        # them explicitly so the agent notices immediately rather than
        # at its response timeout -- the same 30-second blind spot the
        # reader-race in station.py exists to avoid, seen from here.
        for handler in list(self.connections):
            connection = getattr(handler, "_connection", None)
            if connection is not None:
                with contextlib.suppress(Exception):
                    await connection.close(code=1012, reason="server going down")

    async def come_back(self, clear_history: bool = True) -> None:
        """
        Start listening again on the same port.

        clear_history empties `connections`, so a test can assert on
        what arrived AFTER the outage without filtering out what came
        before. That is usually what a replay assertion wants -- the
        question is "did the queued events arrive on the new
        connection", and the old handler's records are noise.
        """
        if clear_history:
            self.connections.clear()
        self._command_tasks.clear()

        await self.start()
        self.log.warning("*** RECOVERED: the fake CSMS is accepting again ***")

    async def outage(self, seconds: float) -> None:
        """
        Go down, stay down, come back. The E2 shape in one call.

        Used as `await server.outage(1.5)` from a test, usually from a
        task running alongside the agent.
        """
        await self.go_down()
        await asyncio.sleep(seconds)
        await self.come_back()

    @property
    def is_listening(self) -> bool:
        return self._server is not None

    async def stop(self) -> None:
        """Stop listening and wait for the socket to close."""
        # Cancel command tasks first. A dispatch still in flight when the
        # socket closes raises ConnectionClosed from inside a task nobody
        # is awaiting, which asyncio reports at interpreter shutdown as
        # "Task exception was never retrieved" -- noise that looks like a
        # real fault in an otherwise clean test run.
        for task in self._command_tasks:
            if not task.done():
                task.cancel()
        for task in self._command_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._command_tasks.clear()

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
    commands = CommandPlan(
        set_limit_w=args.set_limit_w,
        limit_unit=args.limit_unit,
        after_messages=args.after_messages,
        clear_after_messages=args.clear_after,
        stop_after_messages=args.stop_after,
        trigger_after_messages=args.trigger_after,
        trigger_message=args.trigger_message,
    )
    server = FakeCSMS(
        host=args.host, port=args.port,
        interval_s=args.interval, faults=faults,
        commands=commands,
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

    # -- Phase C3: stand in for csms/dispatch.py ------------------------
    commands = parser.add_argument_group(
        "command dispatch",
        "Send server-initiated commands to the station. Stands in for "
        "csms/dispatch.py, which does not exist yet.",
    )
    commands.add_argument(
        "--set-limit-w", type=float, default=None, metavar="WATTS",
        help="send SetChargingProfile with this limit; 0 is curtailment",
    )
    commands.add_argument(
        "--limit-unit", default="W", choices=["W", "A"],
        help="units for --set-limit-w; A exercises the agent's conversion",
    )
    commands.add_argument(
        "--after-messages", type=int, default=3, metavar="N",
        help="fire --set-limit-w once the station has sent N messages",
    )
    commands.add_argument(
        "--clear-after", type=int, default=0, metavar="N",
        help="send ClearChargingProfile after N messages; 0 disables",
    )
    commands.add_argument(
        "--stop-after", type=int, default=0, metavar="N",
        help="send RequestStopTransaction after N messages; 0 disables",
    )
    commands.add_argument(
        "--trigger-after", type=int, default=0, metavar="N",
        help="send TriggerMessage after N messages; 0 disables",
    )
    commands.add_argument(
        "--trigger-message", default="StatusNotification",
        help="what --trigger-after asks for",
    )

    args = parser.parse_args()

    configure_logging("fake-csms", args.log_level)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main_async(args))


if __name__ == "__main__":
    main()