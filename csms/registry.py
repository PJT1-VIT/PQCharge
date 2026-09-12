"""
Session registry — who is connected, in what state, right now.

Track A (csms). Implements Contract 6's FleetView.

--------------------------------------------------------------------
This is the CSMS's memory of the fleet. Everything the dashboard shows,
everything Track B's orchestrator decides a migration wave against, and
the E2 recovery measurement all read from here.

Two dictionaries, deliberately separate:

  _sessions    live connections only. A station that disconnects is
               removed. Keyed by station_id.

  _identities  the Contract 2 record for every station the CSMS has
               ever heard of. Survives disconnection, because a
               station's certificate and migration state do not stop
               existing when its socket closes -- and a fleet-wide
               migration must still account for stations that are
               currently offline.

CONCURRENCY. The CSMS is a single asyncio event loop. Every method here
runs on that loop, so there is no lock and none is needed: no two of
these calls can interleave mid-statement. This is a real constraint,
not an accident -- if anything in csms/ ever moves to a thread pool,
this class needs revisiting first. Contract 3's EventLog takes its own
lock because it is written to with os.fsync and could legitimately be
called from elsewhere.
--------------------------------------------------------------------
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from crypto.identity import MigrationState, StationIdentity
from csms.events import EventLog, EventType, Outcome
from csms.fleet import ConnectionState, FleetSnapshot, FleetView, StationView


def _now() -> datetime:
    """Timezone-aware UTC. Used for display and ordering only."""
    return datetime.now(timezone.utc)


@dataclass
class StationSession:
    """
    One live connection.

    Holds the connection object itself, because Day 8-9 command dispatch
    (SetChargingProfile, RemoteStopTransaction) needs a route from a
    station_id back to the socket to push a command down it. That is the
    whole reason the registry cannot be replaced by a database table.
    """

    station_id: str

    connection: Any
    """The ocpp ChargePoint wrapper for this connection. Typed Any so
    that the registry does not import the ocpp library -- it has no
    business knowing what a ChargePoint is, only that it can hand one
    back to the dispatcher later."""

    connected_since: datetime
    connected_monotonic_ns: int
    """Monotonic reading at connection time. Every duration derived from
    this session is computed from monotonic differences, never from
    connected_since -- see Contract 3's note on NTP correction."""

    boot_accepted: bool = False
    """Set when a BootNotification is accepted. Half of the E2 recovery
    predicate; see Contract 6."""

    last_heartbeat_at: datetime | None = None
    ocpp_status: str | None = None
    charging_state: str | None = None
    active_transaction_id: str | None = None
    power_w: float | None = None
    energy_wh: float | None = None

    last_handshake_ms: float | None = None
    """Populated by Day 7 instrumentation. Carried on the session rather
    than recomputed at snapshot time so the dashboard's latency panel is
    a field read, not a log scan."""

    last_meter_at: datetime | None = None
    """The station's own timestamp on the most recent meter reading
    applied to live state.

    Exists to reject stale readings. A station that queued
    TransactionEvents while the CSMS was down replays them on
    reconnect, carrying timestamps minutes old. Without this guard a
    replayed backlog would overwrite live power and energy with
    historical values -- and aggregate_power_w, the number the whole E5
    demonstration turns on, would jump backwards on the dashboard at
    exactly the moment the fleet is being watched recover."""

    last_seq_no: int | None = None
    """seqNo of the last TransactionEvent seen for the active
    transaction. OCPP increments it per transaction, so a jump in the
    sequence is direct evidence that events were lost -- worth having
    during an E2 reconnection storm, and impossible to reconstruct
    afterwards."""

    bytes_tx: int = 0
    bytes_rx: int = 0
    """Day 7 instrumentation. Present now so that adding the counters
    later is wiring, not a schema change."""


class SessionRegistry(FleetView):
    """
    Live fleet state for one CSMS process.

    Constructed once by csms/server.py and handed to the handlers, to
    the HTTP surface, and (from Stage 5) to Track B's orchestrator.
    """

    def __init__(
        self,
        *,
        event_log: EventLog | None = None,
        crypto_mode: str = "classical",
        run_id: str = "",
        migration_controller: Any | None = None,
    ) -> None:
        self._sessions: dict[str, StationSession] = {}
        self._identities: dict[str, StationIdentity] = {}
        self._ever_connected: set[str] = set()

        self._log = event_log
        """Contract 3 writer, or None.

        Optional on purpose. The schedule puts instrumentation on Day 7,
        but retrofitting a log into every handler on Day 7 is how
        instrumentation bugs get written -- and section 14 warns those
        stay invisible until analysis. So the seam is built now and the
        timing fields are filled in on Day 7. Passing None makes the
        registry silent, which is what the unit tests want."""

        self._crypto_mode = crypto_mode
        self._run_id = run_id
        self._migration_controller = migration_controller

    # -- connection lifecycle ------------------------------------------

    def register(
        self,
        station_id: str,
        connection: Any,
        *,
        handshake_ms: float | None = None,
    ) -> StationSession:
        """
        Record a newly opened connection.

        If a session already exists for this station_id it is replaced
        and the old one is reported closed. This happens for real: a
        station whose network drops without a clean close leaves a stale
        session behind, and reconnects before the server has noticed.
        During an E2 reconnection storm, five hundred stations do this
        at once. Replacing rather than rejecting is what keeps the
        registry's count equal to the true connected count.
        """
        stale = self._sessions.pop(station_id, None)
        if stale is not None and self._log is not None:
            self._log.emit(
                EventType.CONNECTION_CLOSED,
                station_id,
                outcome=Outcome.FAILURE,
                reason="superseded_by_reconnect",
            )

        session = StationSession(
            station_id=station_id,
            connection=connection,
            connected_since=_now(),
            connected_monotonic_ns=time.monotonic_ns(),
            last_handshake_ms=handshake_ms,
        )
        self._sessions[station_id] = session
        self._ever_connected.add(station_id)
        self._ensure_identity(station_id)

        if self._log is not None:
            self._log.emit(
                EventType.CONNECTION_ESTABLISHED,
                station_id,
                handshake_ms=handshake_ms,
                outcome=Outcome.SUCCESS,
                superseded_stale_session=stale is not None,
            )
        return session

    def deregister(self, station_id: str, connection: Any) -> bool:
        """
        Record a closed connection.

        Takes the connection object and removes the session only if it
        is still the current one. Without that check there is a real
        race: a station reconnects, register() installs the new session,
        and only then does the old connection's handler finish unwinding
        and call deregister -- which would delete the live session and
        leave a connected station invisible to the dashboard and
        unreachable by command dispatch.

        Returns:
            True if a session was removed, False if this connection had
            already been superseded.
        """
        session = self._sessions.get(station_id)
        if session is None or session.connection is not connection:
            return False

        del self._sessions[station_id]
        if self._log is not None:
            self._log.emit(
                EventType.CONNECTION_CLOSED,
                station_id,
                outcome=Outcome.SUCCESS,
                session_duration_ms=(
                    (time.monotonic_ns() - session.connected_monotonic_ns) / 1e6
                ),
            )
        return True

    def get_session(self, station_id: str) -> StationSession | None:
        """The live session for a station, or None if not connected."""
        return self._sessions.get(station_id)

    @property
    def connected_ids(self) -> list[str]:
        """Station IDs with an open connection. Day 8-9 dispatch uses
        this to fan a command out across the fleet."""
        return sorted(self._sessions)

    # -- state updates, called from csms/handlers.py (Days 4-5) ---------

    def mark_boot_accepted(self, station_id: str) -> None:
        """Called when a BootNotification is accepted."""
        session = self._sessions.get(station_id)
        if session is not None:
            session.boot_accepted = True

    def record_heartbeat(self, station_id: str) -> None:
        session = self._sessions.get(station_id)
        if session is not None:
            session.last_heartbeat_at = _now()

    def record_status(self, station_id: str, status: str) -> None:
        """Connector status from StatusNotification, stored verbatim."""
        session = self._sessions.get(station_id)
        if session is not None:
            session.ocpp_status = status

    def record_transaction(
        self,
        station_id: str,
        *,
        transaction_id: str | None,
        charging_state: str | None = None,
    ) -> None:
        """
        Transaction started, updated or ended.

        transaction_id of None means the transaction has ended; the
        station keeps its last charging_state until it reports a new one.
        """
        session = self._sessions.get(station_id)
        if session is None:
            return
        session.active_transaction_id = transaction_id
        if charging_state is not None:
            session.charging_state = charging_state
        if transaction_id is None:
            session.power_w = 0.0
            session.last_meter_at = None
            session.last_seq_no = None

    def record_meter(
        self,
        station_id: str,
        *,
        power_w: float | None = None,
        energy_wh: float | None = None,
        reading_at: datetime | None = None,
    ) -> bool:
        """
        Apply meter values from a TransactionEvent to live state.

        Watts and watt-hours, matching Contract 5's units.

        Args:
            reading_at: the station's own timestamp for the reading. A
                reading older than the last one applied is REJECTED for
                live state -- see StationSession.last_meter_at. The
                caller still records it to the event log, so nothing is
                lost from the dataset; it simply does not claim to be
                the present.

        Returns:
            True if live state was updated, False if the reading was
            stale. The caller logs the difference, which is how a run
            can report how much offline replay actually occurred.
        """
        session = self._sessions.get(station_id)
        if session is None:
            return False

        if (
            reading_at is not None
            and session.last_meter_at is not None
            and reading_at < session.last_meter_at
        ):
            return False

        if power_w is not None:
            session.power_w = power_w
        if energy_wh is not None:
            session.energy_wh = energy_wh
        if reading_at is not None:
            session.last_meter_at = reading_at
        return True

    def record_sequence(self, station_id: str, seq_no: int) -> int:
        """
        Track a TransactionEvent's seqNo and report any gap.

        Returns:
            How many events appear to be missing before this one. 0 for
            an in-order event, for the first event of a transaction, or
            for a repeat -- a duplicate is not a loss.

        A non-zero return during E2 is evidence that the reconnection
        storm dropped messages, which is a finding rather than a bug to
        hide. Out-of-order and duplicate events are both possible when
        a station replays a queue, so only a forward jump counts.
        """
        session = self._sessions.get(station_id)
        if session is None:
            return 0
        previous = session.last_seq_no
        session.last_seq_no = seq_no
        if previous is None or seq_no <= previous:
            return 0
        return seq_no - previous - 1

    def reset_sequence(self, station_id: str) -> None:
        """Start a fresh sequence. Called when a transaction starts."""
        session = self._sessions.get(station_id)
        if session is not None:
            session.last_seq_no = None
            session.last_meter_at = None

    # -- Contract 6: FleetView read ------------------------------------

    def get_station(self, station_id: str) -> StationView | None:
        if station_id not in self._identities:
            return None
        return self._build_view(station_id)

    def list_stations(self) -> list[StationView]:
        return [self._build_view(sid) for sid in sorted(self._identities)]

    def snapshot(self) -> FleetSnapshot:
        """
        The whole fleet plus aggregates, from memory only.

        Called once a second per dashboard client, and during E2 while
        hundreds of stations are reconnecting, so it must stay a walk
        over two dictionaries. Nothing here touches SQLite.
        """
        views = self.list_stations()

        migration: dict[str, Any] | None = None
        if self._migration_controller is not None:
            migration = self._migration_controller.get_migration_status().to_dict()

        return FleetSnapshot(
            generated_at=_now(),
            run_id=self._run_id,
            crypto_mode=self._crypto_mode,
            total_stations=len(views),
            connected_count=sum(
                1 for v in views
                if v.connection_state == ConnectionState.CONNECTED.value
            ),
            booted_count=sum(1 for v in views if v.is_recovered),
            charging_count=sum(
                1 for v in views if v.active_transaction_id is not None
            ),
            aggregate_power_w=sum(v.power_w or 0.0 for v in views),
            stations=views,
            migration=migration,
        )

    def get_identity(self, station_id: str) -> StationIdentity | None:
        return self._identities.get(station_id)

    # -- Contract 6: FleetView write, called by Track B ----------------

    def set_identity(self, identity: StationIdentity) -> None:
        """
        Replace one station's Contract 2 record.

        Persistence to SQLite is added on Day 6 with csms/persistence.py;
        until then this is memory only, which is correct for Stage 1.
        """
        if identity.station_id not in self._identities:
            raise KeyError(
                f"unknown station {identity.station_id!r}; the CSMS has "
                f"never seen it and cannot hold an identity for it"
            )
        identity.last_updated = _now()
        self._identities[identity.station_id] = identity

    # -- provisioning ---------------------------------------------------

    def provision(self, identity: StationIdentity) -> None:
        """
        Add a station the CSMS has not yet met.

        Lets a fleet of N be configured before anything connects, so the
        dashboard shows N rows in NEVER_SEEN rather than an empty table
        filling up. Also how Track B seeds capability profiles for the
        heterogeneous fleet in Stage 6.
        """
        self._identities.setdefault(identity.station_id, identity)

    def _ensure_identity(self, station_id: str) -> StationIdentity:
        """
        Create a bare identity record on first contact.

        A station that connects without having been provisioned is still
        a station. current_algorithm is left empty rather than guessed:
        until Track B's provider reports one, the CSMS genuinely does
        not know, and an invented placeholder would end up in the
        results table.
        """
        identity = self._identities.get(station_id)
        if identity is None:
            identity = StationIdentity(
                station_id=station_id,
                current_algorithm="",
                migration_state=MigrationState.PENDING,
            )
            self._identities[station_id] = identity
        return identity

    # -- view construction ----------------------------------------------

    def _build_view(self, station_id: str) -> StationView:
        """Join the live session, if any, onto the Contract 2 record."""
        identity = self._identities[station_id]
        session = self._sessions.get(station_id)

        if session is not None:
            state = ConnectionState.CONNECTED
        elif station_id in self._ever_connected:
            state = ConnectionState.DISCONNECTED
        else:
            state = ConnectionState.NEVER_SEEN

        seconds_since_heartbeat: float | None = None
        if session is not None and session.last_heartbeat_at is not None:
            seconds_since_heartbeat = (
                _now() - session.last_heartbeat_at
            ).total_seconds()

        return StationView(
            station_id=station_id,
            connection_state=state.value,
            boot_accepted=bool(session and session.boot_accepted),
            connected_since=session.connected_since if session else None,
            last_heartbeat_at=session.last_heartbeat_at if session else None,
            seconds_since_heartbeat=seconds_since_heartbeat,
            last_handshake_ms=session.last_handshake_ms if session else None,
            ocpp_status=session.ocpp_status if session else None,
            charging_state=session.charging_state if session else None,
            active_transaction_id=(
                session.active_transaction_id if session else None
            ),
            power_w=session.power_w if session else None,
            energy_wh=session.energy_wh if session else None,
            current_algorithm=identity.current_algorithm or None,
            supported_algorithms=list(identity.supported_algorithms),
            certificate_serial=identity.certificate_serial,
            certificate_expiry=identity.certificate_expiry,
            previous_certificate_serial=identity.previous_certificate_serial,
            migration_wave=identity.migration_wave,
            migration_state=identity.migration_state.value,
        )
