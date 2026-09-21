"""
The charging station — one agent's whole life.

Track C (agent). Phase C2. Entry point: python -m agent.station

--------------------------------------------------------------------
WHERE THIS FITS

    agent/config.py        settings          } imported, never imports
    agent/logging_setup.py logging           } anything of ours
    agent/messages.py      payload shapes
    agent/power.py         Contract 5, the physical model
          |
    agent/client.py        one CONNECTION's OCPP conversation
          |
    agent/station.py    <- you are here. One STATION. Outlives
                           connections. Owns config, power, the
                           transaction, and the session sequence.

The station/connection split is the load-bearing design decision in
Track C. An ocpp ChargePoint is welded to one WebSocket; when the
socket dies the object is finished. But a station is not finished --
it still has a configuration, a contactor that may be closed, a meter
reading, and possibly a transaction in progress. Those live here.

Phase C4 adds the reconnect loop to run(), below, and needs no change
to client.py at all. That is the point of the split.

--------------------------------------------------------------------
THE SESSION THIS PLAYS OUT

    connect
      -> BootNotification              adopt the server's interval
      -> StatusNotification Available
      -> Authorize                     stop here if refused
      -> StatusNotification Occupied
      -> TransactionEvent Started      seq 0, contactor closes
      -> TransactionEvent Updated      seq 1..n, real meter readings
      -> TransactionEvent Ended        seq n+1, contactor opens
      -> StatusNotification Available
    disconnect

Heartbeats run throughout, in the background, at the interval the
SERVER issued.

--------------------------------------------------------------------
TWO SAFETY RULES ENFORCED HERE

1. THE CONTACTOR IS ALWAYS OPENED ON THE WAY OUT. Every exit path --
   success, CALLError, dropped socket, Ctrl-C, an unexpected exception
   -- runs through a finally block that opens it. On simulated hardware
   this only affects a number; on the Raspberry Pi bench node in the
   Review 4 window it is a relay with current behind it, and a code
   path that leaves it closed on an error is a code path that leaves
   real current flowing after a crash. Building the habit now, while it
   costs nothing, is cheaper than retrofitting it into a file that has
   grown by then.

2. THE SEQUENCE COUNTER NEVER RESTARTS MID-TRANSACTION. Track A
   detects forward jumps in seq_no and logs them as evidence of message
   loss during a storm. That is a real finding worth having -- and a
   jump caused by our own counter resetting would be a fabricated one,
   indistinguishable at analysis time from genuine loss.
--------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import time
import uuid
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from agent import messages as msg
from agent.backoff import BackoffPolicy
from agent.offline_queue import OfflineQueue, QueuedEvent
from agent.charging_profile import (
    ProfileRejected,
    cleared_limit_w,
    parse_charging_profile,
)
from agent.client import CallFailed, StationClient, StationCommands
from agent.config import AgentConfig
from agent.logging_setup import configure_logging, get_logger
from agent.power import PowerInterface
from agent.pq_identity import PQIdentity
from agent import pqc_messages as pqc
from agent.simulated_power import SimulatedPower
from agent.state_machine import StationState, StationStateMachine

# Messages this station will re-send on demand when the CSMS sends a
# TriggerMessage. Anything else is answered NotImplemented, which is the
# spec's word for it and not an error.
#
# BootNotification is deliberately absent: re-booting mid-transaction
# would mean re-negotiating the heartbeat interval while a session is
# open, and the only caller that would want it is a CSMS trying to
# recover a station it thinks is stuck -- for which StatusNotification
# answers the question in one round trip without disturbing anything.
TRIGGERABLE = (
    "StatusNotification",
    "Heartbeat",
    "MeterValues",
)

# How many times to re-send BootNotification when the CSMS answers
# Pending. Today it never does -- csms/handlers.py always accepts --
# but from Stage 6 a station whose capabilities exclude the migration
# target must be held back, and Pending is how OCPP says "wait".
MAX_BOOT_ATTEMPTS = 3

# Cap on how long to wait between Pending retries, regardless of what
# interval the server issued. Without it, a server answering Pending
# with a 300-second interval would stall a test run for five minutes.
MAX_BOOT_RETRY_WAIT_S = 30.0

# How long one connection ATTEMPT may take before it is abandoned.
#
# websockets defaults open_timeout to 10 seconds. That is far too long
# here, and the reason is a platform difference measured on Windows
# during C4: when the fake CSMS closed its listener, a connect() to the
# dead port did not fail promptly -- it blocked for most of the outage
# before returning. On Linux the same connect is refused in
# microseconds.
#
# While a connect is in flight the station is NOT metering: _wait_offline
# only runs between attempts. So a slow-failing connect is a gap in the
# offline readings, and at the 10-second default a single hung attempt
# would swallow an entire short outage's worth of data. During E2, with
# five hundred agents, it would also serialise reconnection behind the
# OS's connect behaviour rather than behind the server's capacity --
# which is the thing being measured.
#
# Five seconds is comfortably longer than any healthy handshake,
# including a post-quantum one at Stage 8, and short enough that a hung
# attempt costs one backoff cycle rather than an outage.
#
# PHASE C5 MOVED THE VALUE INTO AgentConfig.connect_timeout_s, at Track
# A's request (their §9.5): under load the risk runs the other way, and
# a cap that is too LOW makes agents abandon attempts that would have
# succeeded -- recorded afterwards as failed recoveries. This constant
# is now only the default that field carries, kept here so the number
# and the reasoning stay in the same place.
CONNECT_TIMEOUT_S = 5.0


def describe_close(exc: BaseException) -> str:
    """
    Readable close code and reason from a ConnectionClosed.

    websockets deprecated ConnectionClosed.code and .reason in 13.1 in
    favour of the `rcvd` / `sent` Close objects. Reading them the old
    way still works but prints a DeprecationWarning on every dropped
    connection -- which during an E2 storm is hundreds of warnings
    burying the real output. This reads the new attributes and falls
    back, so it is correct on both sides of the change.
    """
    received = getattr(exc, "rcvd", None)
    sent = getattr(exc, "sent", None)
    close = received or sent
    if close is not None:
        return f"code={getattr(close, 'code', '?')} reason={getattr(close, 'reason', '')!r}"
    return repr(exc)


class ChargingStation(StationCommands):
    """
    One simulated charging station.

    Lives across connections. Created once by main(), or by the load
    generator in Phase C5 -- which will create five hundred of these,
    which is why nothing here reads sys.argv or configures logging:
    both are the entry point's job, done once per process.
    """

    def __init__(
        self,
        config: AgentConfig,
        power: PowerInterface | None = None,
    ) -> None:
        self.config = config
        self.log = get_logger(__name__, station_id=config.station_id)

        # Contract 5. Injectable so a test can pass a fake, and so the
        # Raspberry Pi's GPIOPower drops in later by changing one line
        # in main() rather than editing this class.
        self.power: PowerInterface = power or SimulatedPower(
            max_power_w=config.max_power_w
        )

        # -- transaction state, which outlives any one connection ------

        self.transaction_id: str | None = None
        """Set while a transaction is open. None otherwise. Track A's
        charging_count is a count of stations where this is not None."""

        self.seq_no = 0
        """Monotonic per transaction. See safety rule 2 above."""

        self.last_status: str | None = None
        """The last connector status actually sent. Used to avoid
        re-sending an identical one -- harmless to the server, which
        guards against it, but still a message on the wire, and at
        fleet scale those add up."""

        self.callerror_count = 0
        """Accumulated across connections, for the end-of-run summary."""

        self._heartbeat_task: asyncio.Task | None = None
        """The background keepalive for the CURRENT connection, held
        here so run_once()'s finally block can cancel it even when the
        session task was cancelled before it could clean up itself."""

        # ==============================================================
        # PHASE C3 — physical state and the actuation path
        # ==============================================================

        self.state = StationStateMachine(station_id=config.station_id)
        """
        The single source of truth for both OCPP state fields.

        STATION-scoped, like the power backend: a station whose socket
        dies mid-charge is still physically charging, and in Phase C4 it
        must resume reporting what it is actually doing rather than
        starting again from Available.
        """

        self._limit_w: float = config.max_power_w
        """
        The power cap currently in force, in watts.

        Distinct from what the power backend is set to. This is the
        OPERATOR'S limit -- from a charging profile, or the station's own
        maximum when none is installed. The backend is set to this only
        while actually charging, and to zero while suspended, so that
        lifting a suspension restores the right number without having to
        remember it somewhere else.
        """

        self._last_charging_state_sent: str | None = None
        """The charging_state carried by the last TransactionEvent. Used
        to decide whether the next one is a state change or an ordinary
        periodic reading -- see _next_trigger_reason()."""

        self._limit_changed = False
        """Set when a profile has been applied and the next meter event
        should carry ChargingRateChanged rather than MeterValuePeriodic.
        Track A reads trigger_reason, so this is how a curtailment is
        identifiable in their log rather than looking like an ordinary
        periodic sample that happened to read zero."""

        self._stop_requested = False
        self._stop_reason = ""
        """Set by handle_request_stop(). Read by the metering loop."""

        self._pending_triggers: list[str] = []
        """Messages a TriggerMessage command asked for, waiting to be
        sent by the loop rather than from inside the handler."""

        self._wake = asyncio.Event()
        """
        Wakes the metering loop early when a command arrives.

        Without it the loop sleeps up to meter_every_s -- five seconds by
        default -- before noticing a curtailment. Five seconds of a
        station still drawing full power after being told to stop is
        both physically wrong and, in E5's timeline, the difference
        between a command taking effect immediately and appearing to lag.

        Created here rather than in the loop so the OCPP handlers, which
        run before the loop starts on a reconnect, always have something
        to set.
        """

        self.commands_received = 0
        """Accumulated across connections, for the end-of-run summary."""

        self.connect_timeouts = 0
        """
        Attempts abandoned at --connect-timeout.

        NON-ZERO IS A FINDING, AND WHICH FINDING DEPENDS ON THE RUN. At
        N=1 it means the server stalled. At N=500 it much more likely
        means the cap is too low for a loaded machine, and every
        abandoned attempt would otherwise be counted as a station that
        failed to recover. harness/load_generator.py sums these across
        the fleet and says so in its summary, which is the number Track
        A asked for (their §9.5).
        """

        # -- TLS material, resolved once, at construction ---------------
        #
        # Deliberately eager. A station with a missing or unreadable
        # certificate fails HERE, before the fleet starts, with a
        # message naming the file -- rather than on its first dial,
        # where five hundred simultaneous identical failures look like a
        # server problem. Track A raises at start-up on their side for
        # the same reason.
        self._ssl_context = None
        if config.uses_tls:
            from agent.tls import build_station_context

            self._ssl_context = build_station_context(
                config.station_id,
                config.cert_dir,
                cert=config.cert,
                key=config.key,
                ca=config.ca,
                check_hostname=config.tls_check_hostname,
            )

        # ==============================================================
        # PHASE C4 — surviving an outage
        # ==============================================================

        self.backoff = BackoffPolicy.from_config(config)
        """How long to wait between connection attempts. See
        agent/backoff.py for why the jitter in here is the
        highest-risk line of code in Track C."""

        self.offline_queue = OfflineQueue(station_id=config.station_id)

        # Phase C8. This station's post-quantum identity: empty until the
        # migration orchestrator sends an InstallPQAuth. STATION-scoped,
        # so a station migrated before an E2 outage is still migrated
        # after it. quantcrypt is not touched until the first challenge.
        self.pq = PQIdentity(station_id=config.station_id)
        """
        Readings taken while the CSMS was unreachable.

        STATION-scoped, so it survives the very disconnection it exists
        to handle. Drained and replayed after the next accepted boot.
        """

        self._charge_deadline: float | None = None
        """
        monotonic() at which this transaction should end.

        Set when the transaction starts and NOT reset on reconnect. A
        session interrupted by a sixty-second outage still ends at the
        time it was always going to end -- it does not get sixty extra
        seconds of charging because the server went away, which would
        make every E2 run's energy totals depend on how long the outage
        was.
        """

        self._session_complete = False
        """
        True once the station has finished, successfully or not.

        run()'s retry loop needs to tell "the connection failed, try
        again" apart from "the session finished, stop". Without it a
        station with max_attempts=0 would charge, finish, disconnect,
        and immediately reconnect to charge again, forever.
        """

        self._session_ok = False
        """
        Whether that finish was a success.

        Separate from _session_complete because "stop retrying" and "it
        worked" are different facts, and run()'s return value becomes
        the process exit code. A station refused at BootNotification has
        finished -- there is nothing to retry -- but it has not
        succeeded, and a load generator counting successes in C5 needs
        to be able to tell those apart.
        """

        self._disconnected_at: float | None = None
        """monotonic() when the last connection ended, for measuring
        downtime across the gap."""

        self.connection_attempts = 0
        self.reconnections = 0
        self.total_downtime_s = 0.0
        """
        The E2 numbers, measured from the station's own side.

        Track A measures recovery server-side via
        StationView.is_recovered, which is the authoritative figure.
        These are the client's view, and they cover the part the server
        cannot see: attempts that never arrived. A station that tried
        eleven times before getting through is invisible in the server's
        log -- it only ever sees the twelfth.
        """

    # -- small helpers ------------------------------------------------------

    def _next_seq(self) -> int:
        """Hand out the next sequence number and advance."""
        seq, self.seq_no = self.seq_no, self.seq_no + 1
        return seq

    def _new_transaction_id(self) -> str:
        """
        A fresh transaction identifier.

        Random rather than sequential: five hundred stations starting
        transactions at once must not collide, and a station id prefix
        would leak identity into a field Track A treats as opaque.
        """
        return uuid.uuid4().hex[:12]

    async def _send_status_if_changed(
        self, client: StationClient, status: str
    ) -> None:
        """Send a connector status only when it is actually new."""
        if status == self.last_status:
            self.log.debug("status already %s; not re-sending", status)
            return
        await client.send_status(status)
        self.last_status = status

    async def _sync_status(self, client: StationClient) -> None:
        """
        Report whatever connector status the state machine currently says.

        The ONLY place StatusNotification is sent from, in Phase C3
        onwards. Before C3 the status was passed in by hand at each call
        site, which is exactly how connector_status and charging_state
        came to be able to disagree. Now there is one state and one
        place that reads it.
        """
        await self._send_status_if_changed(client, self.state.connector_status)

    # =====================================================================
    # ACTUATION — the state machine drives the physical model
    # =====================================================================

    def _apply_power_for_state(self) -> None:
        """
        Make the hardware match the state. Synchronous; no I/O.

        Two rules, and every physical decision in the agent follows from
        them:

            contactor closed  <=>  a transaction is open
            backend limit     ==   the operator's limit while CHARGING,
                                   zero in every other state

        Keeping the operator's limit in self._limit_w rather than in the
        backend is what makes a suspension reversible: lifting it
        restores the right number, with no separate "remembered limit"
        that can drift out of step.

        Note that the contactor STAYS CLOSED during SUSPENDED_EVSE. That
        is deliberate and it is what a real charger does -- the contactor
        is a mechanical part with a finite number of operations, and the
        transaction has not ended. Power goes to zero because the limit
        is zero, not because the circuit was broken. SimulatedPower
        computes its draw as min(limit, max), so a zero limit reads zero
        watts with the contactor still closed.
        """
        if self.state.in_transaction:
            if not self.power.is_closed():
                self.power.close_contactor()
                self.log.info("contactor closed")
        else:
            if self.power.is_closed():
                self.power.open_contactor()
                self.log.info("contactor opened")

        effective = self._limit_w if self.state.draws_power else 0.0
        if effective != self.power.get_power_limit():
            self.power.set_power_limit(effective)
            self.log.info(
                "power limit -> %.1fW (operator limit %.1fW, state %s)",
                effective, self._limit_w, self.state.state.value,
            )

    # =====================================================================
    # StationCommands — what the CSMS can make this station do
    # =====================================================================
    #
    # *** EVERY METHOD IN THIS SECTION IS SYNCHRONOUS AND SENDS NOTHING. ***
    #
    # They are called from inside agent/client.py's @on handlers, which
    # run inside the ocpp library's single receive loop. Awaiting an
    # outbound call() from here would deadlock the connection -- see the
    # long note on StationCommands in client.py.
    #
    # So each one: decides, changes state, actuates the hardware, sets
    # self._wake, and returns (accepted, reason). The metering loop wakes
    # immediately and sends whatever the new state requires.

    def handle_request_stop(self, transaction_id: str | None) -> tuple[bool, str]:
        """
        The operator pressed stop.

        Refused when there is no transaction, or when the id names a
        different one. Both refusals are honest answers that let the CSMS
        distinguish a stale command from a station ignoring it -- and
        both are reachable in practice, because a stop command sent
        during a reconnection storm may well arrive after the session it
        referred to has already ended.
        """
        if self.transaction_id is None:
            return False, "no transaction is in progress at this station"

        if transaction_id and transaction_id != self.transaction_id:
            return False, (
                f"transaction {transaction_id} is not the one in progress "
                f"({self.transaction_id})"
            )

        self._stop_requested = True
        self._stop_reason = "RequestStopTransaction"
        self._wake.set()
        return True, f"stopping transaction {self.transaction_id}"

    def handle_set_charging_profile(
        self, evse_id: Any, charging_profile: Any
    ) -> tuple[bool, str]:
        """
        Cap this station's power. The command E5 is built on.

        A limit of zero suspends the session without ending it:
        SUSPENDED_EVSE, power to nothing, connector still Occupied,
        transaction still open. Lifting it later resumes charging on the
        same transaction, with the energy counter carrying on from where
        it paused -- which is what makes the curtailment visible as a
        flat section in the energy curve rather than a gap in the data.
        """
        try:
            limit = parse_charging_profile(
                charging_profile,
                max_power_w=self.config.max_power_w,
                evse_id=evse_id,
                station_evse_id=msg.DEFAULT_EVSE_ID,
            )
        except ProfileRejected as exc:
            # Not an error on our side -- the command was malformed or
            # not for us. WARNING, not ERROR, and the reason goes back on
            # the wire so the operator sees it too.
            self.log.warning("charging profile rejected: %s", exc.reason)
            return False, exc.reason

        self.log.info("charging profile: %s", limit.describe())
        self._limit_w = limit.watts
        self._limit_changed = True

        # -- does this change the state, not just the number? ------------
        if self.state.in_transaction:
            if limit.is_curtailment and self.state.state is StationState.CHARGING:
                self.state.transition_to(
                    StationState.SUSPENDED_EVSE,
                    "charging profile set the limit to 0 W",
                )
            elif (
                not limit.is_curtailment
                and self.state.state is StationState.SUSPENDED_EVSE
            ):
                self.state.transition_to(
                    StationState.CHARGING,
                    f"charging profile raised the limit to {limit.watts:.0f} W",
                )

        self._apply_power_for_state()
        self._wake.set()
        return True, limit.describe()

    def handle_clear_charging_profile(self, criteria: dict) -> tuple[bool, str]:
        """
        Remove the cap; return to the station's own maximum.

        Answers Unknown -- not Rejected -- when no profile is installed,
        because that is what ClearChargingProfileStatus offers and it is
        the accurate word: there was nothing matching to clear. A station
        that was never curtailed saying so is correct behaviour, not a
        failure, and Track A's dispatcher should not treat it as one.

        The criteria (profile id, purpose, stack level) are logged but
        not matched against. This station holds at most one profile at a
        time, so there is nothing to select between; matching would be
        code that is never exercised and therefore never known to work.
        """
        if criteria:
            self.log.debug("clear criteria (not matched, one profile only): %s",
                           criteria)

        maximum = cleared_limit_w(self.config.max_power_w)
        if self._limit_w >= maximum:
            return False, "no charging profile is installed at this station"

        previous = self._limit_w
        self._limit_w = maximum
        self._limit_changed = True

        if (
            self.state.in_transaction
            and self.state.state is StationState.SUSPENDED_EVSE
        ):
            self.state.transition_to(
                StationState.CHARGING, "charging profile cleared"
            )

        self._apply_power_for_state()
        self._wake.set()
        return True, f"limit restored from {previous:.0f}W to {maximum:.0f}W"

    def handle_trigger_message(
        self, requested_message: str, evse: Any
    ) -> tuple[bool, str]:
        """
        Queue a message to be re-sent on the loop's next tick.

        Queued rather than sent, for the deadlock reason above. The delay
        is bounded by the wake event, so "shortly" means milliseconds,
        not up to a meter interval.
        """
        if requested_message not in TRIGGERABLE:
            return False, (
                f"{requested_message} cannot be triggered on this station "
                f"(available: {', '.join(TRIGGERABLE)})"
            )

        if (
            requested_message == "MeterValues"
            and not self.state.in_transaction
        ):
            return False, "no transaction is in progress, so there are no meter values"

        self._pending_triggers.append(requested_message)
        self._wake.set()
        return True, f"{requested_message} will be sent shortly"

    # -- post-quantum migration (Phase C8, Option B) ------------------------
    #
    # These are the station's half of Track B's migration. Both are
    # synchronous like every other StationCommands method, and both are
    # deliberately transaction-safe: installing a key stores bytes and
    # signing a nonce is pure CPU, so neither touches the contactor, the
    # meter or the state machine. A migration that lands mid-charge --
    # which, across a fleet, is most of them -- does not perturb the
    # session. That is the "rotate without losing a transaction"
    # guarantee, met by construction rather than by careful sequencing.

    def handle_install_pq_auth(self, data: Any) -> tuple[bool, str]:
        """
        Store the ML-DSA private key the orchestrator sent.

        This is what turns a classical station into a migrated one. A
        malformed payload is answered Rejected with the reason, never a
        crash -- the CSMS learns the key did not take, rather than seeing
        a fault.
        """
        try:
            algorithm, private_key = pqc.parse_install_data(data)
        except ValueError as exc:
            self.log.warning("InstallPQAuth rejected: %s", exc)
            return False, str(exc)

        self.pq.install(private_key, algorithm)
        return True, f"{algorithm} key installed; station migrated"

    def handle_pq_challenge(
        self, data: Any
    ) -> tuple[bool, str, bytes | None]:
        """
        Sign the server's nonce and hand back the signature.

        Rejected -- honestly -- when this station holds no key yet, which
        lets the CSMS tell "ignored the challenge" from "not migrated."
        """
        if not self.pq.is_migrated:
            return False, "station holds no PQC key (not migrated)", None

        try:
            nonce, _meta = pqc.parse_challenge_data(data)
            signature = self.pq.answer_challenge(nonce)
        except Exception as exc:  # noqa: BLE001 - reported, never a CALLError
            self.log.warning("PQAuthChallenge could not be answered: %s", exc)
            return False, f"could not sign challenge: {exc}", None

        return True, "challenge signed", signature

    # -- serving what the commands asked for -------------------------------

    async def _sleep_or_wake(self, seconds: float) -> None:
        """
        Sleep, but return the instant a command arrives.

        asyncio.sleep() would make every command wait out the rest of the
        meter interval. Racing it against the wake event turns a
        multi-second lag into a sub-millisecond one, which matters
        because E5 times how quickly the fleet's aggregate power responds
        to a curtailment -- and a lag introduced by our own polling
        interval would be reported as the cost of the command path.
        """
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return  # ordinary tick, nothing was waiting
        finally:
            # Consume the wake either way. Leaving it set would make the
            # next sleep return instantly and spin the loop.
            self._wake.clear()

    async def _serve_triggers(self, client: StationClient) -> None:
        """Send whatever TriggerMessage asked for, then forget it."""
        while self._pending_triggers:
            requested = self._pending_triggers.pop(0)
            self.log.info("serving TriggerMessage: %s", requested)

            if requested == "StatusNotification":
                # Forced, not "if changed" -- the CSMS asked for it
                # precisely because it does not know the current value.
                await client.send_status(self.state.connector_status)
                self.last_status = self.state.connector_status

            elif requested == "Heartbeat":
                await client.send_heartbeat()

            elif requested == "MeterValues":
                await self._send_meter_event(
                    client, trigger_reason=msg.TRIGGER_METER_PERIODIC
                )

    def _next_trigger_reason(self) -> str:
        """
        Why the next meter event is being sent.

        Track A stores trigger_reason verbatim, so this is what makes a
        curtailment identifiable in their event log. Without it, the
        moment a station's power drops to zero on command is
        indistinguishable from an ordinary periodic sample that happened
        to read zero -- and E5's whole finding is about a deliberate
        change being visible.

        Precedence: a state change outranks a rate change, which outranks
        the periodic tick. Only one reason fits in the field.
        """
        charging_state = self.state.charging_state or msg.CHARGING_STATE_IDLE

        if charging_state != self._last_charging_state_sent:
            return msg.TRIGGER_CHARGING_STATE_CHANGED

        if self._limit_changed:
            return msg.TRIGGER_CHARGING_RATE_CHANGED

        return msg.TRIGGER_METER_PERIODIC

    # -- boot, with the Pending case handled ---------------------------------

    async def _boot(self, client: StationClient) -> int | None:
        """
        Announce this station and return the heartbeat interval.

        Returns None when the station must not proceed: either the CSMS
        rejected it outright, or it stayed Pending for too long.

        UNLIKE tests/fixtures/fake_station.py, a Rejected boot stops the
        station here. That fixture ignores the status and opens a
        transaction anyway -- visible when running it against
        fake_csms.py with --fail-boot. A real charger does not charge
        after being refused registration, and from Stage 6 the refusal
        will be meaningful: a station whose capabilities exclude the
        migration target should be skipped, not quietly admitted into a
        wave it cannot complete.
        """
        for attempt in range(1, MAX_BOOT_ATTEMPTS + 1):
            result = await client.send_boot(
                reason=msg.BOOT_POWER_UP if attempt == 1 else msg.BOOT_RECONNECT
            )

            if result.accepted:
                return result.interval

            if result.pending:
                wait = min(float(result.interval), MAX_BOOT_RETRY_WAIT_S)
                self.log.warning(
                    "boot Pending (attempt %d/%d); retrying in %.0fs",
                    attempt, MAX_BOOT_ATTEMPTS, wait,
                )
                await asyncio.sleep(wait)
                continue

            self.log.error(
                "boot %s -- the CSMS refused this station; not charging",
                result.status,
            )
            return None

        self.log.error(
            "boot still Pending after %d attempts; giving up on this connection",
            MAX_BOOT_ATTEMPTS,
        )
        return None

    # -- the charging session -------------------------------------------------

    async def _charging_session(self, client: StationClient) -> None:
        """
        One driver plugging in, charging, and leaving.

        Split out from run_once() so that the connection lifecycle and
        the charging behaviour can be reasoned about -- and later
        replaced -- independently. Phase C3 puts the formal state
        machine in front of every transition here.
        """
        cfg = self.config

        # Every status now comes from the state machine. Nothing in this
        # method names a wire string.
        await self._sync_status(client)

        # -- the driver presents a card ----------------------------------
        accepted, status = await client.send_authorize(cfg.id_token)
        if not accepted:
            # A refusal is a result, not a failure. The station waits a
            # moment, as a real one would while the driver stares at the
            # screen, and then the session simply ends.
            self.log.info("session ends without charging (authorize: %s)", status)
            await asyncio.sleep(min(cfg.charge_for_s, 5.0))
            # Phase C4: this station's work IS done -- it asked, it was
            # refused, that is the answer. Without this flag run()'s
            # retry loop would reconnect and ask again forever, turning
            # one blocked card into a station that hammers the CSMS for
            # the length of the run.
            #
            # Successful, though: the station asked and got an answer.
            # A refused card is a working station, not a failed one.
            self._session_complete = True
            self._session_ok = True
            return

        # -- a car is plugged in -------------------------------------------
        self.state.transition_to(StationState.OCCUPIED, "vehicle plugged in")
        await self._sync_status(client)

        self.transaction_id = self._new_transaction_id()
        self.seq_no = 0
        self._last_charging_state_sent = None
        self._stop_requested = False
        self._stop_reason = ""
        self._pending_triggers.clear()
        self._wake.clear()

        # Energy must start from zero for this transaction, or the first
        # reading carries over the previous session's total and Track
        # A's registry sees a jump it cannot explain.
        self.power.reset_meter()

        # Anything still queued belongs to a transaction that is over.
        # Replaying it against this one would attach old readings to a
        # new transaction id -- which Track A would accept without
        # complaint, and which would be unfindable at analysis.
        self.offline_queue.clear()

        # Phase C4: on the station, not in a local, so that a session
        # interrupted by an outage resumes toward the SAME end time. A
        # local would restart the clock on reconnect and give every
        # interrupted session extra charging time proportional to the
        # outage -- which would put the outage length into the energy
        # totals E5 compares.
        self._charge_deadline = time.monotonic() + cfg.charge_for_s

        try:
            # -- current starts flowing -----------------------------------
            #
            # A profile that arrived BEFORE the transaction started is
            # already in self._limit_w, so a station curtailed to 0 W
            # while idle begins the session suspended rather than
            # charging for one tick and then dropping. This is reachable
            # in E5, where profiles are pushed across the fleet without
            # regard for which stations happen to be mid-session.
            if self._limit_w <= 0:
                self.state.transition_to(
                    StationState.SUSPENDED_EVSE,
                    "a 0 W profile was already in force when the session began",
                )
            else:
                self.state.transition_to(
                    StationState.CHARGING, "transaction started"
                )
            self._apply_power_for_state()

            await client.send_transaction_event(
                msg.TX_STARTED,
                self.transaction_id,
                self._next_seq(),
                self.power.read_power(),
                self.power.read_energy(),
                trigger_reason=msg.TRIGGER_AUTHORIZED,
                charging_state=self.state.charging_state
                or msg.CHARGING_STATE_CHARGING,
                token=cfg.id_token,
            )
            self._last_charging_state_sent = self.state.charging_state
            self._limit_changed = False

            await self._charge_until_deadline(client)

        finally:
            # Safety. See _charge_until_deadline for why this is
            # conditional in Phase C4 rather than unconditional.
            self._open_contactor_if_session_over()

    # -- resuming after an outage (Phase C4) -------------------------------

    async def _resume_transaction(self, client: StationClient) -> None:
        """
        Pick a transaction back up on a fresh connection.

        Reached when the socket died mid-charge and the station got back
        in. The car never stopped charging; from the driver's point of
        view nothing happened. What has to be rebuilt is the SERVER's
        picture, which is empty: csms/registry.py created a brand new
        StationSession on this connection, with no status, no
        transaction and no meter reading.

        So: re-announce the status, replay what was missed, then carry
        on to the same deadline.
        """
        self.reconnections += 1

        if self._disconnected_at is not None:
            downtime = time.monotonic() - self._disconnected_at
            self.total_downtime_s += downtime
            # THE E2 LINE. One per rejoin, with the measured gap. Track
            # A measures recovery server-side and that figure is
            # authoritative; this is the client's view, and it is the
            # only record of how long this particular station was away.
            self.log.info(
                "REJOINED after %.2fs offline (reconnection #%d, "
                "%d event(s) to replay)",
                downtime, self.reconnections, len(self.offline_queue),
            )
            self._disconnected_at = None

        # The server has no idea what this connector is doing -- it has
        # never seen a StatusNotification on this connection. last_status
        # was cleared in run_once() so this always sends.
        await self._sync_status(client)

        await self._replay_offline_events(client)

        try:
            if self._charge_deadline is None:
                # Should not happen: a transaction is open, so a
                # deadline was set. Treated as "time is up" rather than
                # charging forever, because an unbounded session in a
                # 500-agent run is a load generator that never finishes.
                self.log.error(
                    "resumed with a transaction but no deadline; ending it"
                )
                await self._end_transaction(client, msg.TRIGGER_STOP_AUTHORIZED)
                await self._finish_session(client)
                return

            if time.monotonic() >= self._charge_deadline:
                # The outage outlasted the session. The transaction ends
                # now, with the readings that were queued during it
                # already replayed above -- so the energy total is
                # complete even though the last stretch was reported
                # late.
                self.log.info(
                    "the outage outlasted this session; closing the "
                    "transaction on reconnect"
                )
                await self._end_transaction(client, msg.TRIGGER_STOP_AUTHORIZED)
                await self._finish_session(client)
                return

            await self._charge_until_deadline(client)

        finally:
            self._open_contactor_if_session_over()

    async def _replay_offline_events(self, client: StationClient) -> None:
        """
        Send everything that happened while the CSMS was away.

        Each event keeps ITS OWN TIMESTAMP and carries offline=True.
        Both matter, and the second one is not cosmetic: Track A's
        registry refuses to let a replayed reading overwrite newer live
        state, using the timestamp to decide. Replaying with now() would
        disable that guard silently. See agent/offline_queue.py.

        Replay happens BEFORE any new live event, so seq_no reaches the
        server in order. Track A logs a forward jump in seq_no as
        evidence of message loss during a storm -- a real finding worth
        having -- and events arriving out of order would manufacture one.
        """
        events = self.offline_queue.drain()
        if not events:
            return

        started = time.monotonic()
        for event in events:
            await client.send_transaction_event(
                event.event_type,
                event.transaction_id,
                event.seq_no,
                event.power_w,
                event.energy_wh,
                trigger_reason=event.trigger_reason,
                charging_state=event.charging_state,
                token=event.token,
                offline=True,
                timestamp=event.timestamp,
            )

        self.log.info(
            "replayed %d offline event(s) in %.0fms",
            len(events), (time.monotonic() - started) * 1000.0,
        )

    # -- the metering loop, shared by a fresh session and a resumed one -----

    async def _charge_until_deadline(self, client: StationClient) -> None:
        """
        Meter until the deadline, a stop command, or the socket dies.

        Deadline arithmetic rather than counting iterations: a slow
        server, a long CALLError timeout or an OS scheduling hiccup
        would each make a naive loop overshoot, and E1's handshake
        figures are compared against session durations.

        PHASE C3 CHANGED THE SLEEP to a race against the wake event, so
        an inbound command is acted on immediately.

        PHASE C4 SPLIT THIS OUT of _charging_session so that a resumed
        transaction runs exactly the same loop as a fresh one. Two
        copies of a metering loop is how a replayed session ends up
        subtly different from a normal one in ways that only show up in
        the analysis.
        """
        cfg = self.config
        assert self._charge_deadline is not None

        while True:
            remaining = self._charge_deadline - time.monotonic()
            if remaining <= 0:
                break

            await self._sleep_or_wake(min(cfg.meter_every_s, remaining))

            # 1. The operator may have pressed stop.
            if self._stop_requested:
                self.log.info("ending the session early: %s", self._stop_reason)
                break

            # 2. A command may have moved the connector status.
            await self._sync_status(client)

            # 3. A TriggerMessage may be waiting.
            await self._serve_triggers(client)

            # 4. The meter reading. Values come from Contract 5, not
            #    from a counter we increment ourselves, so a profile
            #    applied mid-session is reflected here automatically.
            await self._send_meter_event(
                client, trigger_reason=self._next_trigger_reason()
            )

        await self._end_transaction(
            client,
            msg.TRIGGER_REMOTE_STOP
            if self._stop_requested
            else msg.TRIGGER_STOP_AUTHORIZED,
        )
        await self._finish_session(client)

    async def _finish_session(self, client: StationClient) -> None:
        """
        The car leaves and the station goes back to Available.

        reset() rather than transition_to() because this runs on every
        exit path, including ones where the state machine is somewhere
        the transition table would not allow AVAILABLE from -- and
        "nothing is plugged in" is always a physically reachable truth.
        """
        self.state.reset("vehicle unplugged")
        await self._sync_status(client)
        self._session_complete = True
        self._session_ok = True

    def _open_contactor_if_session_over(self) -> None:
        """
        Safety rule 1, as Phase C4 changed it.

        *** READ THIS BEFORE "SIMPLIFYING" IT BACK. ***

        Up to C3 the rule was absolute: every exit path opened the
        contactor. C4 makes it conditional, and the condition is whether
        a TRANSACTION is still open -- not whether a connection is.

        The reason is physical. A station charging when the CSMS dies is
        still charging. The car is drawing current, the cable is live,
        and nothing about a server going away is a reason to interrupt
        it -- a real charger does not dump the driver's session because
        it lost its uplink. Opening the contactor here would mean every
        E2 run cut power to five hundred cars, and the energy curve
        would show the outage as a real loss of supply rather than a
        loss of reporting.

        So this opens the contactor when the transaction is over, and
        leaves it closed when the transaction is merely unreported. The
        absolute guarantee has not gone away -- it moved to run()'s
        finally, which is the point at which the STATION stops, and
        which is reached on Ctrl-C, on giving up, and on any unexpected
        exception.
        """
        if self.transaction_id is not None:
            self.log.debug(
                "leaving the contactor closed: transaction %s is still open "
                "(the car is still charging; only reporting stopped)",
                self.transaction_id,
            )
            return

        if self.power.is_closed():
            self.power.open_contactor()
            self.log.warning(
                "contactor opened on the way out of an unfinished session"
            )

    async def _send_meter_event(
        self, client: StationClient, *, trigger_reason: str
    ) -> None:
        """
        One TransactionEvent Updated carrying the current readings.

        Factored out of the loop because TriggerMessage MeterValues needs
        exactly the same message on demand, and two call sites building
        the same event by hand is how the seq_no discipline in safety
        rule 2 gets broken.
        """
        charging_state = self.state.charging_state or msg.CHARGING_STATE_IDLE

        await client.send_transaction_event(
            msg.TX_UPDATED,
            self.transaction_id or "",
            self._next_seq(),
            self.power.read_power(),
            self.power.read_energy(),
            trigger_reason=trigger_reason,
            charging_state=charging_state,
        )

        self._last_charging_state_sent = charging_state
        self._limit_changed = False

    async def _end_transaction(
        self, client: StationClient, trigger_reason: str
    ) -> None:
        """
        Close the transaction cleanly: stop current, then report.

        Order matters. The contactor opens first so the final meter
        reading reflects a station drawing nothing, which is what makes
        aggregate_power_w fall to zero on the dashboard at the moment
        the session ends rather than one poll later.
        """
        if self.transaction_id is None:
            return

        # The car is still plugged in; the transaction is what ended. The
        # state machine moving out of a transaction state is what opens
        # the contactor, via _apply_power_for_state -- there is no
        # separate "open the contactor" decision to get wrong.
        self.state.transition_to(StationState.OCCUPIED, f"transaction ended ({trigger_reason})")
        self._apply_power_for_state()

        await client.send_transaction_event(
            msg.TX_ENDED,
            self.transaction_id,
            self._next_seq(),
            self.power.read_power(),
            self.power.read_energy(),
            trigger_reason=trigger_reason,
            charging_state=msg.CHARGING_STATE_IDLE,
        )

        self.log.info(
            "transaction %s ended, %.1fWh delivered (%s)",
            self.transaction_id, self.power.read_energy(), trigger_reason,
        )
        self.transaction_id = None
        self._last_charging_state_sent = None

    # -- one connection ---------------------------------------------------------

    async def run_once(self) -> bool:
        """
        Connect, run one session, disconnect.

        Returns True if the session completed normally.

        The handshake is timed here rather than inside client.py
        because it is a property of the connection, not of any message.
        Track A measures the same thing server-side and writes it into
        the Contract 3 log as handshake_ms; this local figure is for the
        agent's own diagnostics and, from Phase C5, for the harness
        timing log -- the client's view of how long it waited, which the
        server cannot know for attempts that never arrived.
        """
        cfg = self.config
        started = time.monotonic()
        self.connection_attempts += 1

        # PHASE C4: the server has never seen this connection before, so
        # it holds no status for us. csms/registry.py creates a fresh
        # StationSession with ocpp_status=None on every connect.
        # Remembering what we sent on the PREVIOUS socket would suppress
        # the first StatusNotification of this one, and the station
        # would be connected and charging while the dashboard showed a
        # blank connector state for the rest of the run.
        self.last_status = None

        self.log.info(
            "connecting to %s (attempt %d)", cfg.ws_url, self.connection_attempts
        )

        # Built once per station in __init__, not per attempt: loading
        # and parsing certificate files five hundred times a second
        # during an E2 reconnection storm would add cost to precisely
        # the measurement the storm exists to take.
        connect_kwargs: dict[str, Any] = {
            "subprotocols": [cfg.subprotocol],
            "open_timeout": cfg.connect_timeout_s,
        }
        if self._ssl_context is not None:
            connect_kwargs["ssl"] = self._ssl_context
            if cfg.tls_server_name:
                # Overrides the name taken from the URL. Needed when the
                # station dials an IP but the server certificate carries
                # a .local SAN -- the Raspberry Pi case Track B verified.
                connect_kwargs["server_hostname"] = cfg.tls_server_name

        async with connect(cfg.ws_url, **connect_kwargs) as ws:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            self.log.info("connected in %.1fms", elapsed_ms)

            client = StationClient(
                cfg.station_id,
                ws,
                response_timeout=cfg.response_timeout_s,
                # Phase C3: this object is what answers server-initiated
                # commands. ChargingStation subclasses StationCommands,
                # so the four handle_* methods above are what the CSMS
                # actually reaches.
                commands=self,
            )

            # The ocpp library's receive loop. It MUST be running before
            # any call() is made: call() awaits a response that this
            # loop is what actually reads off the socket. Starting it
            # late means the first message hangs until its timeout.
            reader = asyncio.ensure_future(client.start())
            session = asyncio.ensure_future(self._connected_lifecycle(client))

            try:
                # ------------------------------------------------------
                # WHY THIS IS A RACE AND NOT A PLAIN AWAIT
                #
                # When the CSMS dies mid-message, the ocpp library's
                # call() is waiting on a response future that will never
                # resolve. Nothing tells it the socket has gone, so it
                # waits out the FULL response_timeout -- 30 seconds by
                # default -- before raising.
                #
                # Measured: a connection dropped four messages in took
                # exactly 30.03s to be noticed.
                #
                # During E2 that is catastrophic to the measurement. The
                # experiment kills the CSMS and times how long the fleet
                # takes to recover; every station would sit blind for up
                # to thirty seconds before even beginning to reconnect,
                # and that delay would be reported as the operational
                # cost of post-quantum cryptography. It is not. It is
                # our own client failing to notice a closed socket.
                #
                # The reader task DOES notice immediately: it is reading
                # the socket, so it finishes the moment the connection
                # closes. Racing the session against it turns a 30s
                # blind spot into a sub-millisecond one.
                # ------------------------------------------------------
                done, _pending = await asyncio.wait(
                    {reader, session},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if session in done:
                    # Normal path: the session finished on its own terms.
                    # .result() re-raises whatever it raised, so
                    # CallFailed and friends propagate to run().
                    return session.result()

                # The reader finished first, so the connection is gone.
                # Stop the session promptly; its finally blocks run and
                # open the contactor on the way out.
                self.log.warning(
                    "connection closed while the session was still running"
                )
                session.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await session

                # Surface whatever ended the reader -- usually
                # ConnectionClosed -- so run() can classify it.
                reader.result()
                return False

            finally:
                # Cancel background tasks before the socket closes,
                # otherwise a heartbeat fires into a closing connection
                # and raises noise that looks like a real fault.
                for task in (session, reader, self._heartbeat_task):
                    if task is not None and not task.done():
                        task.cancel()
                        with contextlib.suppress(
                            asyncio.CancelledError, Exception
                        ):
                            await task
                self._heartbeat_task = None

                self.callerror_count += client.callerror_count
                self.commands_received += client.commands_received
                self.log.info(
                    "session finished: %d messages sent, %d CALLErrors, "
                    "%d commands received, %d state transitions, final state %s",
                    client.messages_sent,
                    client.callerror_count,
                    client.commands_received,
                    self.state.transition_count,
                    self.state.describe(),
                )

    async def _connected_lifecycle(self, client: StationClient) -> bool:
        """
        Everything that happens while one connection is open.

        Split out of run_once() so it can be run as a task and raced
        against the reader -- see the long comment above. Keeping the
        boot, the heartbeat and the charging session together here means
        run_once() is purely about the connection's lifetime.
        """
        interval = await self._boot(client)
        if interval is None:
            # A refused boot is an answer, not a connection failure. The
            # station must not spin reconnecting to a CSMS that has
            # already said no -- from Stage 6 that refusal is
            # meaningful (capabilities excluding the migration target)
            # and retrying would put a station into a wave it cannot
            # complete.
            #
            # Finished but NOT successful: the station never charged.
            self._session_complete = True
            self._session_ok = False
            return False

        self._heartbeat_task = asyncio.ensure_future(
            client.heartbeat_loop(interval)
        )

        # PHASE C4: the fork. A transaction already open means this is a
        # reconnection into a session that never stopped happening
        # physically -- resume it. Otherwise it is a fresh arrival.
        if self.transaction_id is not None:
            await self._resume_transaction(client)
        else:
            await self._charging_session(client)
        return True

    # -- the station's whole life -------------------------------------------------

    async def run(self) -> bool:
        """
        Run this station until its work is done, reconnecting as needed.

        PHASE C4 REPLACED THE BODY OF THIS METHOD, exactly as the C2
        version said it would, and nothing in client.py changed --
        because run_once() already isolated one connection's lifetime.
        That was the point of the station/connection split.

        --------------------------------------------------------------
        THE LOOP

            attempt -> run_once()
              session finished?          -> return, we are done
              connection problem?        -> wait a JITTERED delay,
                                            metering into the offline
                                            queue while we wait, then
                                            attempt again
              out of attempts?           -> give up, cleanly

        E2 kills the CSMS on purpose, so ConnectionRefusedError here is
        a NORMAL condition and is logged at WARNING rather than ERROR.
        An agent that treated it as a fault would fill the log with
        hundreds of errors during the very measurement the experiment
        exists to take.

        --------------------------------------------------------------
        WHAT COUNTS AS "RETRY" AND WHAT DOES NOT

        Retryable -- the server is unreachable or went away:
            ConnectionRefusedError, ConnectionClosed, OSError,
            CallFailed, InvalidStatus.

        NOT retryable -- the server answered and said no:
            a rejected boot, a refused authorization. Those set
            _session_complete inside the lifecycle, and the loop stops.

        InvalidStatus is the awkward one. It covers both "the server is
        up but shedding load" (1013 -- retry, and during E2 this is
        expected) and "your subprotocol is wrong" (a configuration
        error that will never succeed). It is retried, because getting
        E2 wrong is expensive and a misconfigured subprotocol is loud in
        the log and obvious within two attempts. The ERROR line below
        says so explicitly rather than leaving someone to wonder why the
        agent is retrying something hopeless.
        """
        attempt = 0

        try:
            while True:
                attempt += 1

                if not self.backoff.should_retry(attempt):
                    self.log.error(
                        "giving up after %d attempt(s) -- the CSMS at %s never "
                        "became reachable. %d event(s) were never delivered.",
                        attempt - 1, self.config.ws_url, len(self.offline_queue),
                    )
                    return False

                try:
                    await self.run_once()

                except ConnectionRefusedError:
                    # Nothing listening. The headline E2 condition.
                    self.log.warning(
                        "connection refused by %s -- the CSMS is not listening",
                        self.config.ws_url,
                    )

                except InvalidStatus as exc:
                    # Retried; see the docstring for why, and for why
                    # this one is ERROR while the others are WARNING.
                    self.log.error(
                        "server refused the WebSocket upgrade: %s. If this "
                        "repeats, check the subprotocol (%s) and the URL path "
                        "-- retrying will not fix a configuration error.",
                        exc, self.config.subprotocol,
                    )

                except ConnectionClosed as exc:
                    self.log.warning(
                        "connection closed: %s", describe_close(exc)
                    )

                except TimeoutError:
                    # The connection attempt itself took too long -- see
                    # CONNECT_TIMEOUT_S. Its own clause (before OSError,
                    # which it subclasses from Python 3.11) because it
                    # means something different from a refusal: the
                    # server accepted the TCP connection and then did
                    # not complete the handshake, which during E2 is a
                    # server at capacity rather than a server that is
                    # down.
                    self.connect_timeouts += 1
                    self.log.warning(
                        "connection attempt to %s timed out after %.1fs "
                        "(--connect-timeout). If this repeats under load the "
                        "cap is too low, not the server too slow.",
                        self.config.ws_url, self.config.connect_timeout_s,
                    )

                except OSError as exc:
                    # DNS failure, network unreachable, connection reset.
                    # Distinct from "refused" and worth naming: during a
                    # storm these appear when the OS itself runs out of
                    # sockets, which is a finding about the harness
                    # rather than about the server.
                    self.log.warning(
                        "network error reaching %s: %s: %s",
                        self.config.ws_url, type(exc).__name__, exc,
                    )

                except CallFailed as exc:
                    # A message the session could not continue without.
                    # Already logged at ERROR inside client.py with the
                    # action and error code, so this records only the
                    # consequence.
                    self.log.error("session aborted: %s", exc)

                    if not exc.timeout:
                        # The server ANSWERED, with an error. Retrying
                        # would reconnect, send the same message, get
                        # the same CALLError and come straight back --
                        # a loop with no sleep in it, hammering a server
                        # that is working fine and simply refusing us.
                        # A timeout is the opposite case and does retry:
                        # no answer means overloaded or gone, which is
                        # exactly what backoff is for.
                        self.log.error(
                            "not retrying: the CSMS answered with an error "
                            "rather than failing to answer. Reconnecting "
                            "would produce the same error immediately."
                        )
                        self._session_complete = True
                        self._session_ok = False

                if self._session_complete:
                    return self._session_ok

                # -- the connection ended with work still to do --------
                if self._disconnected_at is None:
                    self._disconnected_at = time.monotonic()

                if not self.backoff.should_retry(attempt + 1):
                    self.log.error(
                        "giving up after %d attempt(s); %d event(s) undelivered",
                        attempt, len(self.offline_queue),
                    )
                    return False

                delay = self.backoff.delay_for(attempt)

                # THE LINE THAT MAKES A STORM AUDITABLE AFTERWARDS.
                # It logs the delay ACTUALLY SLEPT, not the ceiling. If
                # a run's recovery curve looks like a staircase, these
                # lines across five hundred agent logs are the evidence
                # that says whether the jitter was working -- and there
                # is no way to reconstruct them later.
                self.log.warning(
                    "reconnecting in %.2fs (attempt %d, ceiling %.2fs, %s)",
                    delay, attempt + 1, self.backoff.ceiling_for(attempt),
                    self.backoff.strategy,
                )

                await self._wait_offline(delay)

        except asyncio.CancelledError:
            # Ctrl-C, or the harness shutting this station down. Not an
            # error. Re-raised so the event loop unwinds properly, but
            # only after the finally below has opened the contactor.
            self.log.info("station cancelled")
            raise

        finally:
            # Safety rule 1, absolute form. _open_contactor_if_session_over
            # deliberately leaves current flowing across a reconnection,
            # because the car is still charging. THIS is the point at
            # which the station itself stops, and here the contactor
            # always opens -- on success, on giving up, on Ctrl-C, and
            # on any exception that escaped an inner handler.
            if self.power.is_closed():
                self.power.open_contactor()
                self.log.warning("contactor opened during station shutdown")

            if self.offline_queue.queued_total:
                self.log.info(self.offline_queue.describe())

    # -- what happens while nobody is listening ------------------------------

    async def _wait_offline(self, delay_s: float) -> None:
        """
        Sleep before the next connection attempt -- and keep metering.

        *** THE STATION DOES NOT STOP WHEN THE SERVER DOES. ***

        If a transaction is open, current is still flowing and energy is
        still accumulating for the whole of this delay. Those readings
        are real. Sleeping through them and reporting nothing would put
        a hole in the energy curve exactly as wide as the outage, and at
        Stage 9 that hole is indistinguishable from a station that
        genuinely stopped charging.

        So the wait is sliced at the normal meter interval and each
        slice produces a QueuedEvent with the reading taken at that
        moment, stamped with the time it was taken. They go out on the
        next connection, marked offline=True.

        seq_no keeps advancing through all of this. It must: Track A
        detects forward jumps as evidence of message loss during a
        storm, and a counter that paused during the outage and resumed
        afterwards would produce a perfectly contiguous sequence across
        a gap where events really were delayed -- hiding the very thing
        the experiment is looking at.
        """
        if delay_s <= 0:
            return

        if self.transaction_id is None:
            # Nothing charging, nothing to record. A plain sleep.
            await asyncio.sleep(delay_s)
            return

        cfg = self.config
        deadline = time.monotonic() + delay_s

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return

            await asyncio.sleep(min(cfg.meter_every_s, remaining))

            # The session's own end time still applies. A transaction
            # whose deadline passes during an outage stops ACCUMULATING
            # here and is closed on reconnect by _resume_transaction --
            # it does not keep charging until the server happens to come
            # back.
            #
            # Note that this stops the metering, NOT the wait. Returning
            # early here would skip the rest of the backoff delay, and
            # run() would immediately retry, fail, and come straight
            # back -- a hot spin against a server that is still down,
            # burning a core per agent for the length of the outage.
            # That is the opposite of backing off.
            if (
                self._charge_deadline is not None
                and time.monotonic() >= self._charge_deadline
            ):
                continue

            charging_state = self.state.charging_state or msg.CHARGING_STATE_IDLE
            self.offline_queue.append(
                QueuedEvent(
                    event_type=msg.TX_UPDATED,
                    transaction_id=self.transaction_id,
                    seq_no=self._next_seq(),
                    power_w=self.power.read_power(),
                    energy_wh=self.power.read_energy(),
                    trigger_reason=msg.TRIGGER_METER_PERIODIC,
                    charging_state=charging_state,
                    # Taken NOW, sent later. This is the whole point --
                    # see agent/offline_queue.py.
                    timestamp=msg.now_iso(),
                )
            )


# -- entry point --------------------------------------------------------------


async def main_async(config: AgentConfig) -> int:
    """Run one station and return a process exit code."""
    station = ChargingStation(config)
    ok = await station.run()

    # PHASE C4 SUMMARY. These four numbers are the station's own account
    # of the run, and they are the client-side half of E2: the server
    # can only see attempts that arrived.
    station.log.info(
        "run summary: %d connection attempt(s), %d reconnection(s), "
        "%d connect timeout(s), %.2fs total downtime, %d state transition(s)",
        station.connection_attempts,
        station.reconnections,
        station.connect_timeouts,
        station.total_downtime_s,
        station.state.transition_count,
    )
    if station.offline_queue.queued_total:
        station.log.info(station.offline_queue.describe())

    if station.offline_queue.dropped_total:
        station.log.error(
            "%d offline event(s) were DROPPED -- this station's contribution "
            "to the event log has gaps. Raise the queue cap or record this as "
            "a limitation of the run.",
            station.offline_queue.dropped_total,
        )

    if station.callerror_count:
        # Surfaced at the end because a CALLError does not stop a
        # session. A run that quietly received several has gaps in the
        # Contract 3 event log, and a number printed here is how anyone
        # finds out before Stage 9.
        station.log.error(
            "run finished with %d CALLError(s) -- the CSMS event log has "
            "gaps for this station", station.callerror_count,
        )

    return 0 if ok else 1


def main() -> None:
    """
    python -m agent.station --station-id CP001 --csms-url ws://localhost:9000

    Run as a module, not as a script. `python agent/station.py` fails,
    because direct execution puts agent/ on the import path instead of
    the repository root -- the same reason csms/server.py documents
    `python -m csms.server`.
    """
    parser = argparse.ArgumentParser(
        description="PQCharge station agent (Track C)",
    )
    AgentConfig.add_arguments(parser)
    config = AgentConfig.from_namespace(parser.parse_args())

    # The two foundation modules are wired together here, at the entry
    # point, and nowhere else. Neither imports the other -- see the
    # dependency note in both files.
    log = configure_logging(
        "agent",
        config.log_level,
        config.station_id,
        log_to_file=config.log_to_file,
        log_dir=config.log_dir,
        file_name=f"agent_{config.station_id}.log",
    )

    # One line recording exactly how this run was configured, so the
    # parameters are recoverable from the log rather than from memory of
    # which flags were typed. Track A does the same server-side by
    # writing its configuration into the SERVER_STARTED event.
    log.info("station starting: %s", config.describe_compact())

    # THE JITTER MUST BE IN THE LOG OF EVERY RUN. It is the difference
    # between a valid E2 measurement and an invalid one, and six weeks
    # later this line is the only record of which was used. Printed
    # before anything connects, so it survives a run that fails early.
    policy = BackoffPolicy.from_config(config)
    log.info("%s | ceilings: %s", policy.describe(), policy.preview())
    if policy.spread < 0.5:
        log.warning(
            "reconnect jitter is %.2f. For an E2 run this is LOW -- agents "
            "will retry in near-lockstep and the recovery curve will be "
            "shaped by our own thundering herd rather than by handshake "
            "cost. Use 1.0 unless you are deliberately demonstrating that.",
            policy.spread,
        )

    exit_code = 1
    try:
        exit_code = asyncio.run(main_async(config))
    except KeyboardInterrupt:
        # Ctrl-C unwinds through the finally blocks above, so the
        # contactor is opened and the transaction is not left dangling.
        log.info("interrupted")
        exit_code = 130

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()