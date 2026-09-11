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

from csms.events import EventLog, EventType, Outcome
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
    ) -> None:
        super().__init__(station_id, connection)
        self.registry = registry
        self.event_log = event_log
        self.heartbeat_interval_s = heartbeat_interval_s
        self.log_messages = log_messages

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
                EventType.MESSAGE_RECEIVED, self.id, action=action, **detail
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
