"""
Command dispatch — sending OCPP messages DOWN to a station.

Track A (csms). Days 8-9.

--------------------------------------------------------------------
WHAT THIS IS

Everything so far has been station-initiated: the station speaks, the
CSMS answers. This is the other direction, and it is the actuation half
of the cyber-physical loop -- §6 of the design document names it
non-negotiable. SetChargingProfile modulates the power a station may
draw; RequestStopTransaction opens its contactor.

It is also how Track B's migration orchestrator reaches a station at
all. SignCertificate, CertificateSigned and InstallCertificate travel
over the same live WebSocket connections, which is why the orchestrator
runs inside this process and why send() below is GENERIC: it takes any
ocpp call object, not only the two charging commands. Track B's plan
§8.4 asked for exactly that, so that Stage 5 rotation is not a retrofit.

--------------------------------------------------------------------
FOUR RULES, THREE OF THEM MEASURED BY TRACK C

1. NEVER DISPATCH FROM INSIDE AN @on HANDLER.

   call() awaits a response that can only arrive through the receive
   loop -- and inside a handler, that loop is what is currently blocked
   waiting for the handler to return. The command is sent, the reply
   arrives, and nothing is reading. That is a deadlock, not a slow path.

   Documenting it is not enough, so CSMSHandlers marks itself busy while
   a handler runs and dispatch refuses rather than hangs. A handler that
   genuinely needs to trigger a command uses dispatch_soon(), which
   schedules it as its own task and returns immediately.

2. RACE THE CALL AGAINST THE SOCKET CLOSING.

   Track C measured 30.03 seconds of this from the client side: when the
   peer disappears mid-message, the ocpp library waits on a response
   future that will never resolve, because nothing tells it the socket
   is gone. The same applies in this direction. So every call races
   against the connection's own wait_closed(), and a dead station
   returns in milliseconds rather than at the response timeout.

   This matters for E3 -- migration under load -- where the whole point
   is that some stations fail. A dispatcher that takes thirty seconds to
   notice would make the migration duration a measurement of our own
   timeout.

3. CHARGING LIMITS IN WATTS.

   Contract 5 works in watts. OCPP permits amps, and the agent converts
   them at an assumed 230 V -- sending watts means that assumption is
   never exercised and cannot be wrong.

4. "Unknown" FROM ClearChargingProfile IS SUCCESS.

   It means no matching profile was installed. For a station that was
   never curtailed that is the correct end state, not a failure. Scoring
   it as a failure would make an E5 cleanup step report errors for every
   station the attack did not reach.

--------------------------------------------------------------------
Track C offered a reference implementation of this file. It was not
readable from this session, so this is an independent implementation
against the same constraints -- worth diffing against theirs before
either is relied on.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ocpp.v201 import call

from csms.events import EventLog, EventType, Outcome
from csms.registry import SessionRegistry

LOGGER = logging.getLogger("csms.dispatch")

DEFAULT_DISPATCH_TIMEOUT_S = 10.0
"""How long to wait for a station to answer a command.

Deliberately shorter than the ocpp library's own 30 s response_timeout,
which is the value Track C measured a dead connection blocking for.
Ten seconds is long enough for a loaded station on a saturated laptop
and short enough that E3's migration duration is not dominated by
waiting on stations that are never going to reply.

Exposed as --dispatch-timeout because it lands in the results: a
migration that reports "wave completed in 90 s" where 60 of those were
timeouts is reporting this number, not the fleet's behaviour.
"""

STATUS_UNKNOWN = "Unknown"
"""ClearChargingProfile's "no matching profile existed" answer. Rule 4."""

CHARGING_RATE_UNIT_W = "W"
"""Rule 3. Never "A" -- see the module docstring."""


class DispatchError(RuntimeError):
    """A command could not be attempted at all."""


@dataclass
class DispatchResult:
    """
    What happened to one command.

    Returned rather than raised for the ordinary failures -- not
    connected, timed out, rejected -- because a migration wave over 500
    stations expects some of those, and exceptions would make the
    orchestrator's happy path the exceptional one.
    """

    station_id: str
    action: str
    outcome: str
    """One of Outcome's values: success, failure, timeout, rejected."""

    status: str | None = None
    """The OCPP status the station returned, verbatim, or None."""

    response: Any = None
    """The library's response object, for callers that need its fields."""

    duration_ms: float = 0.0
    """Monotonic round-trip, command sent to answer received."""

    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == Outcome.SUCCESS.value


def _action_name(request: Any) -> str:
    """The OCPP action a call object represents, for logging."""
    return type(request).__name__


def _status_of(response: Any) -> str | None:
    """
    The status field most OCPP responses carry.

    Read defensively: some responses have no status at all, and a
    dispatcher that assumed one would turn a successful command into an
    AttributeError inside a migration wave.
    """
    status = getattr(response, "status", None)
    return None if status is None else str(status)


class CommandDispatcher:
    """
    Sends OCPP commands to connected stations.

    One instance per CSMS process, constructed by csms/server.py and
    handed to Track B's orchestrator. Holds the registry rather than a
    connection, because a station's socket is replaced on every
    reconnect and a dispatcher caching one would be writing to a dead
    connection after the first reconnection.
    """

    def __init__(
        self,
        registry: SessionRegistry,
        event_log: EventLog,
        *,
        timeout_s: float = DEFAULT_DISPATCH_TIMEOUT_S,
    ) -> None:
        self.registry = registry
        self.event_log = event_log
        self.timeout_s = timeout_s
        self._in_flight: dict[str, int] = {}

    # -- the generic surface Track B asked for --------------------------

    async def send(
        self,
        station_id: str,
        request: Any,
        *,
        timeout_s: float | None = None,
    ) -> DispatchResult:
        """
        Send any OCPP call to one station and wait for its answer.

        Args:
            request: any ocpp.v201.call object. Deliberately untyped
                beyond that -- Track B's certificate messages go through
                here unchanged, and narrowing this to the charging
                commands would mean Stage 5 retrofitting a second path.

        Returns:
            A DispatchResult. Ordinary failures are outcomes, not
            exceptions; see that class.

        Raises:
            DispatchError: only when called from inside a message
                handler, which would deadlock. See rule 1.
        """
        action = _action_name(request)
        session = self.registry.get_session(station_id)

        if session is None:
            return self._finish(
                DispatchResult(
                    station_id=station_id,
                    action=action,
                    outcome=Outcome.FAILURE.value,
                    error="station not connected",
                )
            )

        charge_point = session.connection
        if getattr(charge_point, "handling_message", False):
            # Rule 1. Refusing turns a hang into a stack trace naming the
            # caller, which is the difference between a five-minute fix
            # and an afternoon.
            raise DispatchError(
                f"dispatch to {station_id} attempted from inside a message "
                f"handler; the response can only arrive through the receive "
                f"loop this handler is blocking. Use dispatch_soon()."
            )

        started_ns = time.monotonic_ns()
        self._in_flight[station_id] = self._in_flight.get(station_id, 0) + 1
        try:
            result = await self._call_racing_close(
                station_id, charge_point, request, action,
                timeout_s if timeout_s is not None else self.timeout_s,
                started_ns,
            )
        finally:
            remaining = self._in_flight.get(station_id, 1) - 1
            if remaining <= 0:
                self._in_flight.pop(station_id, None)
            else:
                self._in_flight[station_id] = remaining

        return self._finish(result)

    async def _call_racing_close(
        self,
        station_id: str,
        charge_point: Any,
        request: Any,
        action: str,
        timeout_s: float,
        started_ns: int,
    ) -> DispatchResult:
        """
        Rule 2: whichever finishes first -- the response or the socket.

        Without this, a station that vanished mid-command holds the
        caller for the ocpp library's full response timeout. Track C
        measured 30.03 s of exactly that from the other direction.
        """
        def elapsed_ms() -> float:
            return (time.monotonic_ns() - started_ns) / 1e6

        call_task = asyncio.ensure_future(
            charge_point.call(request, suppress=False)
        )

        waiters: list[asyncio.Future] = [call_task]
        closed_task: asyncio.Future | None = None
        connection = getattr(charge_point, "_connection", None)
        wait_closed = getattr(connection, "wait_closed", None)
        if callable(wait_closed):
            closed_task = asyncio.ensure_future(wait_closed())
            waiters.append(closed_task)

        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if closed_task is not None and not closed_task.done():
                closed_task.cancel()

        if call_task in done:
            try:
                response = call_task.result()
            except Exception as exc:  # noqa: BLE001 - any station fault
                return DispatchResult(
                    station_id=station_id, action=action,
                    outcome=Outcome.FAILURE.value,
                    duration_ms=elapsed_ms(),
                    error=f"{type(exc).__name__}: {exc}",
                )
            status = _status_of(response)
            return DispatchResult(
                station_id=station_id, action=action,
                outcome=self._outcome_for(action, status),
                status=status, response=response,
                duration_ms=elapsed_ms(),
            )

        call_task.cancel()

        if closed_task is not None and closed_task in done:
            return DispatchResult(
                station_id=station_id, action=action,
                outcome=Outcome.FAILURE.value,
                duration_ms=elapsed_ms(),
                error="connection closed before the station answered",
            )

        return DispatchResult(
            station_id=station_id, action=action,
            outcome=Outcome.TIMEOUT.value,
            duration_ms=elapsed_ms(),
            error=f"no answer within {timeout_s}s",
        )

    @staticmethod
    def _outcome_for(action: str, status: str | None) -> str:
        """
        Turn an OCPP status into an outcome.

        Rule 4 lives here: ClearChargingProfile answering "Unknown"
        means no matching profile was installed, which for a station
        that was never curtailed is the correct end state. Scoring it as
        a failure would have an E5 cleanup step report an error for
        every station the attack did not reach.
        """
        if status is None:
            return Outcome.SUCCESS.value
        if action.startswith("ClearChargingProfile") and status.endswith(
            STATUS_UNKNOWN
        ):
            return Outcome.SUCCESS.value
        if status.endswith("Accepted"):
            return Outcome.SUCCESS.value
        return Outcome.REJECTED.value

    def _finish(self, result: DispatchResult) -> DispatchResult:
        """
        Log every dispatched command, always.

        Not gated behind --log-messages, unlike routine traffic. A
        command is a deliberate act by the operator or the orchestrator,
        there are few of them, and E3 and E5 are both narrated from
        exactly these lines.
        """
        self.event_log.emit(
            EventType.MESSAGE_SENT,
            result.station_id,
            outcome=result.outcome,
            dispatched=True,
            action=result.action,
            status=result.status,
            duration_ms=result.duration_ms,
            error=result.error,
        )
        log = LOGGER.info if result.ok else LOGGER.warning
        log(
            "dispatch %s -> %s: %s%s (%.1f ms)",
            result.action, result.station_id, result.outcome,
            f" [{result.status}]" if result.status else "",
            result.duration_ms,
        )
        return result

    def dispatch_soon(self, station_id: str, request: Any) -> asyncio.Future:
        """
        Fire a command without waiting -- safe to call from a handler.

        Rule 1 forbids awaiting a response inside a handler, because the
        loop that would deliver it is the one the handler is blocking.
        Scheduling the send as its own task sidesteps that: the handler
        returns, the receive loop resumes, and the response is read.

        Returns the task so a caller that wants the result later can
        await it -- from outside the handler.
        """
        return asyncio.ensure_future(self.send(station_id, request))

    # -- the two charging commands --------------------------------------

    async def set_charging_profile(
        self,
        station_id: str,
        limit_w: float,
        *,
        evse_id: int = 0,
        profile_id: int = 1,
        stack_level: int = 0,
        purpose: str = "ChargingStationMaxProfile",
        timeout_s: float | None = None,
    ) -> DispatchResult:
        """
        Cap the power a station may draw. The actuation path.

        Args:
            limit_w: the cap in WATTS (rule 3). Zero means draw nothing
                without opening the contactor -- Contract 5's definition,
                and OCPP's SuspendedEVSE. That is the state E5's attack
                drives the fleet into, and the same mechanism a real
                operator uses to shed load.
            evse_id: 0 addresses the whole charging station rather than
                one connector, which is what a fleet-wide cap means.
            purpose: ChargingStationMaxProfile caps the station whatever
                transaction is running. TxProfile would apply to one
                transaction and require its id, which is not what
                curtailment is.

        Nested payload keys are snake_case; the ocpp library converts
        them to the camelCase the JSON schema expects. Verified by the
        transaction payloads already working in Stage 1.
        """
        request = call.SetChargingProfile(
            evse_id=evse_id,
            charging_profile={
                "id": profile_id,
                "stack_level": stack_level,
                "charging_profile_purpose": purpose,
                "charging_profile_kind": "Absolute",
                "charging_schedule": [
                    {
                        "id": profile_id,
                        "charging_rate_unit": CHARGING_RATE_UNIT_W,
                        "charging_schedule_period": [
                            {"start_period": 0, "limit": float(limit_w)}
                        ],
                    }
                ],
            },
        )
        return await self.send(station_id, request, timeout_s=timeout_s)

    async def request_stop_transaction(
        self,
        station_id: str,
        transaction_id: str,
        *,
        timeout_s: float | None = None,
    ) -> DispatchResult:
        """
        Force a charging session to end; the station opens its contactor.

        RequestStopTransaction, NOT RemoteStopTransaction. The latter is
        the OCPP 1.6 name and does not exist in ocpp.v201 -- Track C
        verified its absence from the installed library. The wrong name
        still appears in two project documents and in agent/power.py's
        docstring; all three tracks have agreed to correct them.
        """
        return await self.send(
            station_id,
            call.RequestStopTransaction(transaction_id=transaction_id),
            timeout_s=timeout_s,
        )

    async def clear_charging_profile(
        self,
        station_id: str,
        *,
        profile_id: int | None = None,
        timeout_s: float | None = None,
    ) -> DispatchResult:
        """
        Remove a curtailment. "Unknown" is success -- see rule 4.

        The cleanup step after E5: every station that was curtailed
        returns to normal, and every station that was not answers
        Unknown, which is correct rather than an error.
        """
        kwargs: dict[str, Any] = {}
        if profile_id is not None:
            kwargs["charging_profile_id"] = profile_id
        return await self.send(
            station_id, call.ClearChargingProfile(**kwargs), timeout_s=timeout_s
        )

    # -- fleet-wide -----------------------------------------------------

    async def broadcast(
        self,
        request_factory: Any,
        *,
        station_ids: list[str] | None = None,
        timeout_s: float | None = None,
    ) -> list[DispatchResult]:
        """
        Send a command to many stations at once.

        Args:
            request_factory: called with each station_id, returning that
                station's call object. A factory rather than one shared
                object because some commands carry per-station data --
                a transaction id, a certificate -- and reusing a single
                mutable request across 500 concurrent sends is the kind
                of bug that only appears at scale.

        Concurrent by design: E5's claim is that a forged identity can
        curtail a fleet, and a loop that walks 500 stations one at a
        time would measure iteration rather than the attack. Failures
        are results, not exceptions, so one dead station cannot abort
        the wave -- the same hardening the load generator needs.
        """
        targets = station_ids if station_ids is not None else self.registry.connected_ids
        if not targets:
            return []

        results = await asyncio.gather(
            *(
                self.send(sid, request_factory(sid), timeout_s=timeout_s)
                for sid in targets
            ),
            return_exceptions=True,
        )

        out: list[DispatchResult] = []
        for sid, result in zip(targets, results):
            if isinstance(result, BaseException):
                out.append(
                    DispatchResult(
                        station_id=sid, action="unknown",
                        outcome=Outcome.FAILURE.value,
                        error=f"{type(result).__name__}: {result}",
                    )
                )
            else:
                out.append(result)

        succeeded = sum(1 for r in out if r.ok)
        LOGGER.info(
            "broadcast to %d station(s): %d ok, %d not",
            len(out), succeeded, len(out) - succeeded,
        )
        return out

    async def set_fleet_power_limit(
        self,
        limit_w: float,
        *,
        station_ids: list[str] | None = None,
        timeout_s: float | None = None,
    ) -> list[DispatchResult]:
        """
        Cap every connected station at once. E5's mechanism, and a real
        operator's load-shedding control.
        """
        return await self.broadcast(
            lambda sid: call.SetChargingProfile(
                evse_id=0,
                charging_profile={
                    "id": 1,
                    "stack_level": 0,
                    "charging_profile_purpose": "ChargingStationMaxProfile",
                    "charging_profile_kind": "Absolute",
                    "charging_schedule": [
                        {
                            "id": 1,
                            "charging_rate_unit": CHARGING_RATE_UNIT_W,
                            "charging_schedule_period": [
                                {"start_period": 0, "limit": float(limit_w)}
                            ],
                        }
                    ],
                },
            ),
            station_ids=station_ids,
            timeout_s=timeout_s,
        )
