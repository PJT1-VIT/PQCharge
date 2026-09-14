"""
OCPP 2.0.1 client — the station's half of the conversation.

Track C (agent). Phase C2.

--------------------------------------------------------------------
WHERE THIS FITS

    agent/messages.py      builds the payload dicts
          |
    agent/client.py     <- you are here. One instance per CONNECTION.
          |                Sends messages, reads replies, logs failures.
    agent/station.py       one instance per STATION. Decides what to do
                           and, from Phase C4, reconnects when this
                           object's socket dies.

The split matters. An ocpp ChargePoint is bound to exactly one
WebSocket: when the socket closes the object is finished, and a
reconnect needs a brand new one. Anything that must survive an outage
-- the configuration, the power backend, the transaction in progress --
therefore lives one layer up, in station.py. Phase C4's retry loop sits
in that layer and creates a fresh StationClient per attempt.

--------------------------------------------------------------------
*** THE MOST IMPORTANT THING IN THIS FILE ***

The ocpp library's call() signature is:

    async def call(self, payload, suppress=True, ...)

and inside it:

    if response.message_type_id == MessageType.CallError:
        self.logger.warning("Received a CALLError: %s'", response)
        if suppress:
            return          # <-- returns None. No exception.
        raise response.to_exception()

suppress defaults to TRUE. So by default, when the CSMS returns an
error, call() hands back None and the caller carries on as though the
message succeeded.

That is exactly the silent failure Track A lost a whole class of events
to on their Day 5, seen from the other side. Their handler raised, the
library turned it into a CALLError, the station kept charging, and the
only casualty was an event that was never written to the log -- a hole
in the dataset discovered in Stage 9 when the runs are over.

Worse, the None propagates. tests/fixtures/fake_station.py does:

    response = await self.call(call.BootNotification(...))
    interval = int(getattr(response, "interval", 0) or 0)

With response None that yields interval 0, its heartbeat loop returns
immediately, and the station stops sending heartbeats for the rest of
the run without a single error message.

SO: every call in this file passes suppress=False, catches the
resulting exception, logs it at ERROR naming the action and the error
code, and re-raises it as CallFailed for the caller to decide about.
This is Track A's explicit request in their plan (item 15) and it is
the team's cheapest early warning for a server-side handler fault.
--------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
from typing import Any

from ocpp.exceptions import OCPPError, UnknownCallErrorCodeError
from ocpp.routing import on
from ocpp.v201 import ChargePoint as CpBase
from ocpp.v201 import call, call_result

from agent import messages as msg
from agent.logging_setup import get_logger

# How long to wait for the CSMS to answer before giving up on one
# message. The ocpp library's own default is 30s, set on ChargePoint's
# constructor. Kept explicit here so the value appears in our source
# rather than being inherited invisibly from a dependency -- Track A
# made the same observation about response_timeout on their side and
# flagged it as something that will appear in the results while not
# being under anyone's deliberate control.
DEFAULT_RESPONSE_TIMEOUT_S = 30

# Used only if the CSMS answers BootNotification with interval 0 or a
# missing interval. See send_boot() for why that needs a fallback
# rather than being taken literally.
FALLBACK_HEARTBEAT_INTERVAL_S = 20


class CallFailed(Exception):
    """
    One OCPP message did not get a usable answer.

    Carries the action name so the caller can decide per-message what
    to do: a failed Heartbeat is survivable, a failed BootNotification
    is not. The original library exception is kept as __cause__.
    """

    def __init__(self, action: str, reason: str) -> None:
        super().__init__(f"{action} failed: {reason}")
        self.action = action
        self.reason = reason


class BootResult:
    """
    What the CSMS said when this station announced itself.

    A small object rather than a tuple because three things come back
    and callers need all of them: whether to proceed, how often to
    heartbeat, and -- at Stage 6, once Track B's capability registry
    exists -- whether to wait and try again.
    """

    def __init__(self, status: str, interval: int) -> None:
        self.status = status
        self.interval = interval

    @property
    def accepted(self) -> bool:
        return self.status == "Accepted"

    @property
    def pending(self) -> bool:
        """
        The CSMS wants the station to wait and re-send BootNotification.

        Today csms/handlers.py always answers Accepted. From Stage 6 it
        must not: Track A's plan says a station whose declared
        capabilities exclude the migration target has to be marked
        INCOMPATIBLE rather than accepted into a wave it cannot
        complete, and OCPP's Pending and Rejected become usable then.

        Handling it now costs a few lines. Discovering at Stage 6 that
        every agent treats Pending as a hard failure costs a day in the
        middle of the hardest part of the project.
        """
        return self.status == "Pending"

    def __repr__(self) -> str:
        return f"BootResult(status={self.status!r}, interval={self.interval})"


class StationCommands:
    """
    What this connection does when the CSMS tells it to do something.

    Phase C3. agent/station.py subclasses nothing -- it simply provides
    an object with these four methods, because the station is what owns
    the state machine and the power backend, and this class is only the
    shape of the conversation between them.

    --------------------------------------------------------------
    *** EVERY METHOD HERE IS SYNCHRONOUS, AND THAT IS NOT OPTIONAL ***

    The ocpp library's start() is a single receive loop:

        while True:
            message = await self._connection.recv()
            await self.route_message(message)

    One message is handled at a time, and the NEXT recv() does not
    happen until the current handler returns. So a handler that awaits
    an outbound call() -- say, sending a StatusNotification to report
    the state it just changed -- waits for a response that can only
    arrive through the recv() that is waiting for the handler. That is a
    deadlock, and it would present as "the station froze the first time
    the operator curtailed it", thirty seconds before the response
    timeout eventually fires.

    So these methods CHANGE STATE AND RETURN. They send nothing. The
    station's metering loop notices the change on its next tick -- woken
    immediately, not on the next sleep -- and sends whatever the new
    state requires. See agent/station.py's _charging_session().
    --------------------------------------------------------------

    Each method returns (accepted, reason). The reason is sent back
    inside statusInfo so it reaches Track A's event log, where an
    operator can read why their command did not take effect.

    The defaults refuse everything, politely and in valid OCPP.
    """

    def handle_request_stop(self, transaction_id: str | None) -> tuple[bool, str]:
        """The CSMS wants this transaction stopped."""
        return False, "no station is attached to this connection"

    def handle_set_charging_profile(
        self, evse_id: Any, charging_profile: Any
    ) -> tuple[bool, str]:
        """The CSMS is capping this station's power. The actuation path."""
        return False, "no station is attached to this connection"

    def handle_clear_charging_profile(self, criteria: dict) -> tuple[bool, str]:
        """The CSMS is removing a cap it set earlier."""
        return False, "no station is attached to this connection"

    def handle_trigger_message(
        self, requested_message: str, evse: Any
    ) -> tuple[bool, str]:
        """The CSMS wants a particular message re-sent right now."""
        return False, "no station is attached to this connection"


class StationClient(CpBase):
    """
    One station's OCPP conversation over one WebSocket connection.

    Subclasses the ocpp library's ChargePoint, the same way
    csms/handlers.py does on the server side. Created by
    agent/station.py once per connection and discarded when that
    connection closes.

    It deliberately holds no charging logic and no physical state. It
    knows how to say things, not when to say them.
    """

    def __init__(
        self,
        station_id: str,
        connection: Any,
        response_timeout: int = DEFAULT_RESPONSE_TIMEOUT_S,
        commands: "StationCommands | None" = None,
    ) -> None:
        super().__init__(station_id, connection, response_timeout=response_timeout)
        self.log = get_logger(__name__, station_id=station_id)

        # Phase C3. Where server-initiated commands are decided. None
        # means "refuse everything politely", which is the correct
        # behaviour for a client with no station behind it -- a test
        # constructing a bare StationClient still answers valid OCPP.
        self.commands: StationCommands = commands or StationCommands()

        self.commands_received = 0
        """How many server-initiated commands arrived on this connection.
        Part of the session summary: a run where the dispatcher fired and
        nothing arrived is a run whose actuation results mean nothing."""

        self.callerror_count = 0
        """
        How many CALLErrors this connection has received.

        Surfaced so the load generator can report a per-run total. A
        storm run that quietly received four hundred CALLErrors has a
        corrupt dataset, and a number printed at the end of the run is
        how anyone finds out.
        """

        self.messages_sent = 0
        """Counts successful sends, for the session summary log line."""

    # -- the one place every outbound message goes through ---------------

    async def _call(self, request: Any, action: str) -> Any:
        """
        Send one message and return its response, loudly.

        Every send in this class goes through here so that the
        suppress=False decision, the CALLError logging and the timeout
        handling exist in exactly one place and cannot be forgotten by a
        method added later.

        Raises:
            CallFailed: the CSMS returned an error, did not answer in
                time, or answered with something unrecognisable.
            ConnectionClosed: the socket died. Deliberately NOT caught
                here -- that is a connection-level event and belongs to
                station.py's reconnect loop in Phase C4, not to a
                message-level handler.
        """
        try:
            # suppress=False is the whole point. See the module docstring.
            response = await self.call(request, suppress=False)

        except OCPPError as exc:
            # A CALLError came back: the server's handler raised, or it
            # rejected our payload. The connection is still open and the
            # station could carry on -- which is exactly why this must
            # be shouted about rather than swallowed.
            self.callerror_count += 1
            self.log.error(
                "CALLError on %s: %s - %s (details=%s)",
                action,
                getattr(exc, "code", type(exc).__name__),
                getattr(exc, "description", str(exc)),
                getattr(exc, "details", {}),
            )
            raise CallFailed(action, f"{getattr(exc, 'code', 'OCPPError')}") from exc

        except UnknownCallErrorCodeError as exc:
            # The server sent an error code not in the OCPP spec. Rare,
            # but it is raised from inside to_exception() and is NOT an
            # OCPPError subclass, so it would escape the clause above
            # and kill the session with an unhelpful traceback.
            self.callerror_count += 1
            self.log.error("CALLError on %s with unrecognised code: %s", action, exc)
            raise CallFailed(action, "unknown CALLError code") from exc

        except asyncio.TimeoutError as exc:
            # No answer within response_timeout. During an E2
            # reconnection storm this is a genuine and expected
            # condition -- hundreds of stations are competing for one
            # server -- so it is logged as an error but described
            # plainly, without implying a bug.
            self.log.error(
                "timeout after %ss waiting for a response to %s",
                self._response_timeout, action,
            )
            raise CallFailed(action, "response timeout") from exc

        if response is None:
            # Belt and braces. With suppress=False this should be
            # unreachable, but the library's contract has changed
            # between releases and a None slipping through would
            # reintroduce exactly the silent failure this file exists to
            # prevent. Fail loudly instead.
            self.callerror_count += 1
            self.log.error(
                "%s returned None despite suppress=False -- the ocpp library's "
                "behaviour may have changed; treating as a failure rather than "
                "continuing with no answer", action,
            )
            raise CallFailed(action, "empty response")

        self.messages_sent += 1
        return response

    # -- BootNotification ---------------------------------------------------

    async def send_boot(self, reason: str = msg.BOOT_POWER_UP) -> BootResult:
        """
        Announce this station and learn how often to heartbeat.

        The interval in the reply is not advisory. OCPP has the CSMS
        decide it, and a station that heartbeats on its own schedule
        will either flood the server or be marked missing. The real CSMS
        currently issues 20 seconds; tests/fixtures/fake_csms.py
        deliberately issues 7 so that an agent which hardcodes either
        value fails visibly.
        """
        response = await self._call(
            call.BootNotification(
                charging_station=msg.charging_station(),
                reason=reason,
            ),
            "BootNotification",
        )

        status = str(getattr(response, "status", "Unknown"))
        raw_interval = getattr(response, "interval", None)

        try:
            interval = int(raw_interval)
        except (TypeError, ValueError):
            interval = 0

        if interval <= 0:
            # OCPP permits 0, meaning the station may choose. Taking it
            # literally would mean never heartbeating, and Track A's
            # registry uses last_heartbeat_at for liveness -- a station
            # that never heartbeats looks stalled on the dashboard even
            # while it is charging normally.
            self.log.warning(
                "CSMS issued heartbeat interval %r; falling back to %ds",
                raw_interval, FALLBACK_HEARTBEAT_INTERVAL_S,
            )
            interval = FALLBACK_HEARTBEAT_INTERVAL_S

        self.log.info(
            "boot -> %s, heartbeat interval %ds (issued by the CSMS)",
            status, interval,
        )
        return BootResult(status, interval)

    # -- Heartbeat -----------------------------------------------------------

    async def send_heartbeat(self) -> None:
        """One keepalive. DEBUG only -- at fleet scale this dominates a log."""
        await self._call(call.Heartbeat(), "Heartbeat")
        self.log.debug("heartbeat")

    async def heartbeat_loop(self, interval_s: int) -> None:
        """
        Heartbeat forever at the CSMS's interval.

        Run as a background task and cancelled when the session ends.

        A failed heartbeat does NOT end the loop. A single CALLError or
        timeout is survivable -- the station is still charging and the
        next beat may well succeed -- and tearing down a working session
        because one keepalive was refused would turn a server hiccup
        into a fleet-wide disconnection during E2, inflating the very
        recovery figure the experiment is measuring. A dead socket is
        different: that raises ConnectionClosed, which is not caught
        here and propagates up to the reconnect logic.
        """
        if interval_s <= 0:
            self.log.warning("heartbeat loop not started: interval %s", interval_s)
            return

        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.send_heartbeat()
            except CallFailed as exc:
                self.log.warning(
                    "heartbeat failed (%s); continuing -- the session is still "
                    "alive and the next beat may succeed", exc.reason,
                )

    # -- StatusNotification ---------------------------------------------------

    async def send_status(
        self,
        status: str,
        evse_id: int = msg.DEFAULT_EVSE_ID,
        connector_id: int = msg.DEFAULT_CONNECTOR_ID,
    ) -> None:
        """
        Report the physical connector state. The CPS sensing path.

        Track A only emits a state-change event when the status actually
        differs from the last one, so re-sending the same value is
        harmless -- but it is still a message on the wire, and at five
        hundred stations reconnecting at once those add up. station.py
        is responsible for not sending redundant ones.
        """
        if status not in msg.CONNECTOR_STATUSES:
            raise ValueError(
                f"unknown connector status {status!r}; "
                f"expected one of {msg.CONNECTOR_STATUSES}"
            )

        await self._call(
            call.StatusNotification(
                timestamp=msg.now_iso(),
                connector_status=status,
                evse_id=evse_id,
                connector_id=connector_id,
            ),
            "StatusNotification",
        )
        self.log.info("status -> %s", status)

    # -- Authorize --------------------------------------------------------------

    async def send_authorize(self, token: str) -> tuple[bool, str]:
        """
        Ask whether this driver may charge.

        Returns (accepted, status). A refusal is a normal answer, not an
        error: csms/authorization.py distinguishes Blocked (the operator
        has barred this card) from Invalid (nobody has heard of it), and
        E5 later contrasts a policy refusal with a cryptographic one.

        Anything other than "Accepted" means do not charge. That
        includes the "Unknown" that messages.authorize_status() returns
        for a malformed reply -- failing closed is what a real charger
        does.
        """
        response = await self._call(
            call.Authorize(id_token=msg.id_token(token)),
            "Authorize",
        )

        status = msg.authorize_status(response)
        accepted = status == "Accepted"

        self.log.info("authorize %s -> %s", token or "<empty>", status)
        if not accepted:
            self.log.info(
                "not authorised (%s) -- no transaction will be started", status
            )
        return accepted, status

    # -- TransactionEvent ---------------------------------------------------------

    async def send_transaction_event(
        self,
        event_type: str,
        transaction_id: str,
        seq_no: int,
        power_w: float,
        energy_wh: float,
        *,
        trigger_reason: str,
        charging_state: str,
        token: str | None = None,
        offline: bool = False,
    ) -> None:
        """
        Report a charging session starting, progressing or ending.

        This carries the numbers. Meter values arrive at the server
        here, and they are what make aggregate_power_w non-zero -- the
        figure E5's attack demonstration spikes.

        seq_no must increase by exactly one per event within a
        transaction. Track A detects forward jumps and logs them as
        evidence of message loss during a storm, which is a finding
        worth having; a jump caused by our own counter being reset would
        be a fabricated finding, so station.py owns the counter and
        never restarts it mid-transaction.

        offline marks an event that was queued while the CSMS was
        unreachable and is being replayed now. Track A records the flag
        and refuses to let a replayed reading overwrite newer live
        state, so setting it honestly lets a run *measure* how much
        replay occurred instead of guessing. Phase C4 adds the queue;
        the parameter exists now so that adding it later does not change
        this signature.
        """
        if event_type not in msg.TX_EVENT_TYPES:
            raise ValueError(
                f"unknown transaction event type {event_type!r}; "
                f"expected one of {msg.TX_EVENT_TYPES}"
            )

        payload: dict[str, Any] = {
            "event_type": event_type,
            "timestamp": msg.now_iso(),
            "trigger_reason": trigger_reason,
            "seq_no": seq_no,
            "transaction_info": msg.transaction_info(transaction_id, charging_state),
            "evse": msg.evse(),
            "meter_value": [msg.meter_value(power_w, energy_wh)],
        }

        # Only Started carries the driver's token, matching what a real
        # station sends: the card was presented once, at the start.
        if token is not None:
            payload["id_token"] = msg.id_token(token)

        # Sent only when true. The library strips None before
        # validating, but an explicit False on every event would be
        # noise in Track A's payload column for no benefit.
        if offline:
            payload["offline"] = True

        await self._call(call.TransactionEvent(**payload), "TransactionEvent")

        if event_type == msg.TX_UPDATED:
            self.log.debug(
                "meter seq=%d %.1fW %.1fWh", seq_no, power_w, energy_wh
            )
        else:
            self.log.info(
                "transaction %s seq=%d tx=%s %.1fW %.1fWh%s",
                event_type, seq_no, transaction_id, power_w, energy_wh,
                " [offline replay]" if offline else "",
            )

    # =====================================================================
    # SERVER-INITIATED MESSAGES — the actuation path (Phase C3)
    # =====================================================================
    #
    # Everything above this line is the station TALKING. Everything below
    # is the station LISTENING. This is the half that makes PQCharge a
    # cyber-physical system rather than a telemetry feed: a command
    # arrives here and, milliseconds later, current stops flowing.
    #
    # THREE RULES, ALL OF THEM LEARNED THE EXPENSIVE WAY:
    #
    # 1. NO HANDLER AWAITS AN OUTBOUND CALL. See StationCommands above
    #    for the deadlock. Handlers mutate state and return; the
    #    station's loop sends the consequences.
    #
    # 2. NO HANDLER RAISES. An exception here becomes a CALLError, which
    #    tells the CSMS "this station is broken" when the truth is
    #    "that command was not valid". Every failure path returns a
    #    Rejected response with a reason instead. The blanket
    #    try/except in each handler is the backstop for a bug in
    #    station.py, not the primary mechanism.
    #
    # 3. THE ANSWER IS THE TRUTH. If the station cannot honour a
    #    command, it says Rejected. It never accepts and then quietly
    #    does nothing -- Track A's dispatcher records what it was told,
    #    and a lie here becomes a fabricated result in the E5 dataset.
    #
    # ON ACTION NAMES: OCPP 2.0.1 renamed 1.6's RemoteStopTransaction to
    # RequestStopTransaction. Both names appear in conversation, only one
    # appears on the wire, and the 1.6 spelling in agent/power.py's
    # docstring is a comment rather than a signature -- Contract 5 is
    # unaffected either way.

    def _command_log(self, action: str, accepted: bool, reason: str) -> None:
        """
        One line per inbound command, at INFO, always.

        These are the rarest and most consequential messages in a run --
        a handful across a whole experiment, each one changing physical
        state. They are never logged at DEBUG, because a run where the
        curtailment did not arrive must be distinguishable from a run
        where it arrived and was refused, using the default log level.
        """
        self.commands_received += 1
        if accepted:
            self.log.info("<- %s accepted (%s)", action, reason)
        else:
            self.log.warning("<- %s REJECTED: %s", action, reason)

    def _decide(
        self, action: str, decide: Any, *args: Any
    ) -> tuple[bool, str]:
        """
        Run one StationCommands method, converting any escape into a
        refusal rather than a CALLError. Rule 2 above.
        """
        try:
            accepted, reason = decide(*args)
        except Exception as exc:  # noqa: BLE001 - deliberate backstop
            self.log.exception(
                "handler for %s raised; answering Rejected rather than "
                "returning a CALLError, which would tell the CSMS this "
                "station is faulty", action,
            )
            accepted, reason = False, f"internal error: {type(exc).__name__}: {exc}"
        self._command_log(action, accepted, reason)
        return accepted, reason

    @staticmethod
    def _status_info(reason: str) -> dict[str, str]:
        """
        statusInfo for a refusal.

        reasonCode is capped at 20 characters by the OCPP schema and
        additionalInfo at 512 -- exceeding either is a schema violation
        that the library rejects locally, turning a polite refusal into
        the CALLError this file exists to avoid.
        """
        return {"reason_code": "Rejected", "additional_info": reason[:512]}

    # -- RequestStopTransaction ---------------------------------------------

    @on("RequestStopTransaction")
    async def on_request_stop_transaction(
        self, transaction_id: str | None = None, **kwargs: Any
    ):
        """
        Stop the transaction now. The operator pressed stop.

        Rejected when this station has no such transaction open, which
        is the honest answer and lets the CSMS tell a stale command from
        a station that ignored it.
        """
        accepted, reason = self._decide(
            "RequestStopTransaction",
            self.commands.handle_request_stop,
            transaction_id,
        )
        if accepted:
            return call_result.RequestStopTransaction(status="Accepted")
        return call_result.RequestStopTransaction(
            status="Rejected", status_info=self._status_info(reason)
        )

    # -- SetChargingProfile --------------------------------------------------

    @on("SetChargingProfile")
    async def on_set_charging_profile(
        self, evse_id: Any = None, charging_profile: Any = None, **kwargs: Any
    ):
        """
        Cap this station's power. THE experiment-critical command.

        E5's visible payload is the fleet's aggregate power moving on
        command. This handler is where that begins, and a limit of 0 is
        the curtailment case -- power to nothing, transaction still
        open, connector still reporting Occupied.
        """
        accepted, reason = self._decide(
            "SetChargingProfile",
            self.commands.handle_set_charging_profile,
            evse_id,
            charging_profile,
        )
        if accepted:
            return call_result.SetChargingProfile(status="Accepted")
        return call_result.SetChargingProfile(
            status="Rejected", status_info=self._status_info(reason)
        )

    # -- ClearChargingProfile -------------------------------------------------

    @on("ClearChargingProfile")
    async def on_clear_charging_profile(self, **kwargs: Any):
        """
        Remove a cap set earlier; go back to the station's own maximum.

        ClearChargingProfileStatus has no "Rejected" member -- it is
        Accepted or Unknown, where Unknown means "no matching profile
        was installed". A station that was never curtailed answering
        Unknown is correct and is not an error.
        """
        accepted, reason = self._decide(
            "ClearChargingProfile",
            self.commands.handle_clear_charging_profile,
            kwargs,
        )
        if accepted:
            return call_result.ClearChargingProfile(status="Accepted")
        return call_result.ClearChargingProfile(
            status="Unknown", status_info=self._status_info(reason)
        )

    # -- TriggerMessage --------------------------------------------------------

    @on("TriggerMessage")
    async def on_trigger_message(
        self, requested_message: str = "", evse: Any = None, **kwargs: Any
    ):
        """
        "Send me a <X> right now."

        Included in C3 because it is the cheapest possible probe during
        an E2 reconnection storm: Track A can ask a station that looks
        stalled for a StatusNotification and find out in one round trip
        whether it is alive, without waiting a heartbeat interval.

        Accepting means the message WILL be sent shortly, by the
        station's loop. It is not sent from inside this handler -- rule
        1. "NotImplemented" is the spec's word for a message this
        station will not produce on demand, and is not a failure.
        """
        accepted, reason = self._decide(
            "TriggerMessage",
            self.commands.handle_trigger_message,
            requested_message,
            evse,
        )
        if accepted:
            return call_result.TriggerMessage(status="Accepted")
        return call_result.TriggerMessage(
            status="NotImplemented", status_info=self._status_info(reason)
        )