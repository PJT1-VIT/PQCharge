"""
OCPP 2.0.1 message handlers — station-initiated messages.

Track A (csms). Phase A2: BootNotification, Heartbeat, StatusNotification.

--------------------------------------------------------------------
WHAT THIS IS

One class, CSMSHandlers, subclassing the ocpp library's ChargePoint.
One instance per connected station, constructed by csms/server.py and
living for exactly as long as that connection.

The handlers do three things each, in this order:
  1. answer the station, so the protocol stays correct
  2. update the session registry, so the fleet view stays true
  3. emit to the Contract 3 event log, so the run stays measurable

Nothing here reaches for cryptography. Stage 1 is plain ws:// and
crypto/stub.py raises on every call until Day 7.

--------------------------------------------------------------------
ON OCPP FIELD NAMES

Every payload field and enum below is taken from the ocpp library's
v201 module as used in experiments/smoke_test.py, which is verified
working code in this repo -- not from memory. The context document's
standing rule is to never invent OCPP message or field names.

Two deliberate consequences:

  - Handlers take **kwargs. OCPP payloads carry optional fields this
    server does not use (custom_data among them), and a handler that
    rejects an unexpected key would fail against a conformant station
    that happens to send one. E6 tests exactly that.

  - connector_status is stored as the string the station sent, never
    converted to a local enum. The legal values belong to OCPP 2.0.1,
    and a second copy of them in csms/ is a second copy to drift.
--------------------------------------------------------------------

Phase A3 (Day 5) adds Authorize and TransactionEvent to this file.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ocpp.routing import on
from ocpp.v201 import ChargePoint as CpBase
from ocpp.v201 import call_result
from ocpp.v201.enums import RegistrationStatusEnumType

from csms.authorization import STATUS_ACCEPTED, AuthorizationPolicy
from csms.events import EventLog, EventType, Outcome
from csms.metering import parse_meter_values
from csms.registry import SessionRegistry

LOGGER = logging.getLogger("csms.handlers")

DEFAULT_HEARTBEAT_INTERVAL_S = 20
"""Seconds between heartbeats, handed to the station in the
BootNotification response.

Production charging networks use 300 s. That is useless here: a demo
cannot wait five minutes to show a station is alive, and E2's recovery
curve needs the fleet to report in on a timescale comparable to the
reconnection itself.

20 s is a measurement input, not a cosmetic choice. At 500 stations it
is 25 heartbeats per second arriving at the CSMS, each one an event
written to the log -- which is precisely the load the fsync experiment
on Day 6 exists to characterise. If that experiment changes the logging
strategy, revisit this number with it.
"""


def _now_iso() -> str:
    """Current time, ISO-8601 UTC, as OCPP response fields expect."""
    return datetime.now(timezone.utc).isoformat()


RESERVED_EMIT_KEYS: frozenset[str] = frozenset(
    {
        "event_type",
        "station_id",
        "handshake_ms",
        "bytes_tx",
        "bytes_rx",
        "outcome",
    }
)
"""Named parameters of EventLog.emit, which payload keys must not shadow.

Contract 3's emit() takes typed fields by name and sweeps everything else
into payload. Passing one of those names as a payload keyword raises
TypeError -- and the ocpp library turns any exception from a handler into
a CALLError back to the station. The station carries on, the connection
survives, and the ONLY casualty is the event that was never written.

That is the worst possible failure shape for this project: a silent hole
in the log, discovered during Stage 9 analysis when the numbers are
missing and the runs are over. Section 14 warns that instrumentation
errors stay invisible until analysis; this is exactly that.

So a colliding key is renamed rather than allowed to raise, and the
rename is logged. The event survives, and the mistake is visible.
"""


def _safe_payload(payload: dict[str, object]) -> dict[str, object]:
    """Rename any payload key that would shadow an emit() parameter."""
    clashes = RESERVED_EMIT_KEYS.intersection(payload)
    if not clashes:
        return payload
    LOGGER.warning(
        "event payload keys %s shadow EventLog.emit parameters; "
        "renaming with a payload_ prefix. Fix the caller.",
        sorted(clashes),
    )
    return {
        (f"payload_{k}" if k in RESERVED_EMIT_KEYS else k): v
        for k, v in payload.items()
    }


class CSMSHandlers(CpBase):
    """
    The CSMS side of one station connection.

    Holds references to the shared registry and event log rather than
    constructing its own, so that every connection writes into the same
    fleet view and the same run_id.
    """

    def __init__(
        self,
        station_id: str,
        connection,
        *,
        registry: SessionRegistry,
        event_log: EventLog,
        heartbeat_interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S,
        log_messages: bool = False,
        auth_policy: AuthorizationPolicy | None = None,
    ) -> None:
        super().__init__(station_id, connection)
        self.registry = registry
        self.event_log = event_log
        self.heartbeat_interval_s = heartbeat_interval_s
        self.log_messages = log_messages
        self.auth_policy = auth_policy or AuthorizationPolicy()

    # -- helpers --------------------------------------------------------

    def _log_message(self, action: str, **detail) -> None:
        """
        Emit a MESSAGE_RECEIVED event, if per-message logging is on.

        Off by default. Contract 3 defines MESSAGE_RECEIVED and
        MESSAGE_SENT, but emitting one per OCPP message multiplies log
        volume by the message rate: at 500 stations on a 20 s heartbeat
        that is 25 additional fsynced lines per second before any real
        traffic. E1 and E2 do not need per-message lines; debugging and
        E6 interoperability tracing do. Hence --log-messages.

        MESSAGE_SENT is not emitted here. Responses are one-to-one with
        the requests above, and the place to count outbound messages and
        their bytes is the Day 7 instrumentation wrapper, not a hand
        call in every handler.
        """
        if self.log_messages:
            self.event_log.emit(
                EventType.MESSAGE_RECEIVED,
                self.id,
                action=action,
                **_safe_payload(detail),
            )

    # -- BootNotification -----------------------------------------------

    @on("BootNotification")
    async def on_boot_notification(self, charging_station, reason, **kwargs):
        """
        A station announcing itself on connect.

        This is the moment the CSMS learns *who* is on the other end of
        a socket it already had open, which is why boot_accepted is
        tracked separately from connection_state and why the E2 recovery
        predicate requires both. A connected-but-unidentified station is
        not a recovered station.

        Always Accepted at Stage 1. OCPP also defines Pending and
        Rejected; neither buys anything before the capability registry
        exists, and rejecting stations now would mean debugging a
        rejection path with nothing to reject for.

        MUST CHANGE AT STAGE 6. Once Track B's capability registry is
        live, a station whose declared capabilities exclude the fleet's
        target algorithm has to be handled here -- marked INCOMPATIBLE
        per Contract 2 and skipped, rather than accepted into a
        migration wave it cannot complete. Tracked in
        claude/TrackA_Dev_Plan.md.
        """
        model = (charging_station or {}).get("model")
        vendor = (charging_station or {}).get("vendor_name")

        self._log_message("BootNotification", model=model, vendor_name=vendor)
        self.registry.mark_boot_accepted(self.id)

        self.event_log.emit(
            EventType.STATE_CHANGED,
            self.id,
            outcome=Outcome.SUCCESS,
            transition="booted",
            model=model,
            vendor_name=vendor,
            reason=reason,
            heartbeat_interval_s=self.heartbeat_interval_s,
        )
        LOGGER.info(
            "boot accepted: %s (%s / %s), interval=%ss",
            self.id, vendor, model, self.heartbeat_interval_s,
        )

        return call_result.BootNotification(
            current_time=_now_iso(),
            interval=self.heartbeat_interval_s,
            status=RegistrationStatusEnumType.accepted,
        )

    # -- Heartbeat -------------------------------------------------------

    @on("Heartbeat")
    async def on_heartbeat(self, **kwargs):
        """
        Keepalive at the interval issued in the BootNotification response.

        Deliberately does not emit an event of its own. A heartbeat that
        arrives on schedule is the absence of news, and at fleet scale
        logging it would dominate the event log while telling the
        analysis nothing. The registry timestamp is what the dashboard
        and the staleness view read; --log-messages turns the line on
        when someone is debugging.
        """
        self._log_message("Heartbeat")
        self.registry.record_heartbeat(self.id)
        return call_result.Heartbeat(current_time=_now_iso())

    # -- StatusNotification ----------------------------------------------

    @on("StatusNotification")
    async def on_status_notification(
        self, timestamp, connector_status, evse_id, connector_id, **kwargs
    ):
        """
        A physical connector state change. The CPS sensing path.

        Section 6 of the design document names this non-negotiable: it
        and TransactionEvent are how physical state reaches the server,
        and without them the project reads as network security rather
        than cyber-physical systems.

        STATE_CHANGED is emitted only when the status actually differs
        from the one held. Stations re-report their current status on
        reconnect, and in an E2 storm that is five hundred no-op
        notifications arriving at once; logging them as transitions
        would put five hundred phantom state changes into the data.

        MODELLING DECISION: one EVSE, one connector per station.
        StationView carries a single ocpp_status and Contract 5 gives
        the agent a single contactor, so the station's status is the
        connector's status. evse_id and connector_id are still recorded
        into the event payload, so nothing is lost if a multi-connector
        station is ever modelled. Recorded in docs/limitations.md.
        """
        session = self.registry.get_session(self.id)
        previous = session.ocpp_status if session else None

        self._log_message(
            "StatusNotification",
            connector_status=connector_status,
            evse_id=evse_id,
            connector_id=connector_id,
        )
        self.registry.record_status(self.id, connector_status)

        if connector_status != previous:
            self.event_log.emit(
                EventType.STATE_CHANGED,
                self.id,
                outcome=Outcome.SUCCESS,
                transition="connector_status",
                old=previous,
                new=connector_status,
                evse_id=evse_id,
                connector_id=connector_id,
                station_timestamp=timestamp,
            )
            LOGGER.info(
                "status: %s %s -> %s", self.id, previous, connector_status
            )

        return call_result.StatusNotification()

    # -- Authorize --------------------------------------------------------

    @on("Authorize")
    async def on_authorize(self, id_token, **kwargs):
        """
        A station asking whether a driver may charge.

        The decision itself lives in csms/authorization.py, so that
        changing the token list is an edit to a list rather than an
        edit to protocol logic.

        The outcome is always logged, accepted or not. A rejected
        authorisation is a result, not an error: E5 contrasts a
        rejection that is a policy decision against a rejection that is
        a failed signature, and both have to be visible in the same log
        to be compared.
        """
        token_id = (id_token or {}).get("id_token")
        token_type = (id_token or {}).get("type")

        self._log_message("Authorize", id_token=token_id, token_type=token_type)
        status, known = self.auth_policy.authorize(token_id)
        accepted = status == STATUS_ACCEPTED

        # STATE_CHANGED with a transition tag rather than a new EventType
        # member. Contract 3 says adding a member is safe, but it is still
        # an edit to a frozen contract and would need notifying both tracks
        # for something a payload field already expresses.
        self.event_log.emit(
            EventType.STATE_CHANGED,
            self.id,
            outcome=Outcome.SUCCESS if accepted else Outcome.REJECTED,
            transition="authorize",
            id_token=token_id,
            token_type=token_type,
            status=status,
            token_known=known,
        )
        LOGGER.info(
            "authorize: %s token=%s -> %s%s",
            self.id, token_id, status, "" if known else " (unknown token)",
        )

        return call_result.Authorize(id_token_info={"status": status})

    # -- TransactionEvent --------------------------------------------------

    @on("TransactionEvent")
    async def on_transaction_event(
        self, event_type, timestamp, trigger_reason, seq_no, transaction_info,
        **kwargs
    ):
        """
        A charging session starting, progressing or ending.

        The second CPS sensing path, and the one that carries the
        numbers: meter values arrive here, and they are what make
        aggregate_power_w non-zero -- the figure the fleet dashboard
        displays and the one E5's attack demonstration spikes.

        Three things happen beyond recording the event.

        SEQUENCE GAPS. OCPP increments seqNo per transaction. A forward
        jump means events were lost in transit, which during an E2
        reconnection storm is a measurement worth having rather than a
        fault worth hiding. Logged as a distinct event so the analysis
        can count it.

        STALE READINGS. A station that queued events while the CSMS was
        down replays them on reconnect with timestamps minutes old. The
        registry refuses to let those overwrite live power and energy;
        the event is still written to the log, it simply does not claim
        to be the present. The offline flag is recorded so a run can
        report how much replay actually occurred.

        UNRECOGNISED MEASURANDS. If a meter value's label is not one the
        server knows, it is logged BY NAME rather than dropped. This is
        the tripwire for a mismatch with Track C's agent: without it,
        power silently stays zero and the fault surfaces at demo
        rehearsal. See csms/metering.py.
        """
        info = transaction_info or {}
        transaction_id = info.get("transaction_id")
        charging_state = info.get("charging_state")
        offline = bool(kwargs.get("offline", False))
        event_name = str(event_type)

        self._log_message(
            "TransactionEvent",
            tx_event_type=event_name,
            transaction_id=transaction_id,
            seq_no=seq_no,
        )

        if event_name.lower().endswith("started"):
            self.registry.reset_sequence(self.id)

        missing = self.registry.record_sequence(self.id, int(seq_no))
        if missing:
            self.event_log.emit(
                EventType.TRANSACTION_UPDATED,
                self.id,
                outcome=Outcome.FAILURE,
                transition="sequence_gap",
                transaction_id=transaction_id,
                seq_no=seq_no,
                missing_events=missing,
            )
            LOGGER.warning(
                "sequence gap: %s transaction=%s missing %d event(s) before seq %s",
                self.id, transaction_id, missing, seq_no,
            )

        reading = parse_meter_values(kwargs.get("meter_value"))
        applied = self.registry.record_meter(
            self.id,
            power_w=reading.power_w,
            energy_wh=reading.energy_wh,
            reading_at=reading.reading_at,
        )

        if reading.unrecognised or reading.unknown_units:
            LOGGER.warning(
                "%s: unrecognised measurand(s) %s / unit(s) %s -- power and "
                "energy will stay empty until Track A and Track C agree these",
                self.id, reading.unrecognised, reading.unknown_units,
            )
            self.event_log.emit(
                EventType.TRANSACTION_UPDATED,
                self.id,
                outcome=Outcome.FAILURE,
                transition="unrecognised_meter_value",
                measurands=reading.unrecognised,
                units=reading.unknown_units,
                sample_count=reading.sample_count,
            )

        if event_name.lower().endswith("ended"):
            emitted = EventType.TRANSACTION_ENDED
            self.registry.record_transaction(
                self.id, transaction_id=None, charging_state=charging_state
            )
        elif event_name.lower().endswith("started"):
            emitted = EventType.TRANSACTION_STARTED
            self.registry.record_transaction(
                self.id,
                transaction_id=transaction_id,
                charging_state=charging_state,
            )
        else:
            emitted = EventType.TRANSACTION_UPDATED
            self.registry.record_transaction(
                self.id,
                transaction_id=transaction_id,
                charging_state=charging_state,
            )

        self.event_log.emit(
            emitted,
            self.id,
            outcome=Outcome.SUCCESS,
            transaction_id=transaction_id,
            tx_event_type=event_name,
            trigger_reason=str(trigger_reason),
            seq_no=seq_no,
            charging_state=charging_state,
            power_w=reading.power_w,
            energy_wh=reading.energy_wh,
            offline=offline,
            applied_to_live_state=applied,
            station_timestamp=timestamp,
        )
        LOGGER.info(
            "transaction %s: %s tx=%s seq=%s power=%sW energy=%sWh%s",
            event_name, self.id, transaction_id, seq_no,
            reading.power_w, reading.energy_wh,
            "" if applied else " [stale, not applied]",
        )

        return call_result.TransactionEvent()
