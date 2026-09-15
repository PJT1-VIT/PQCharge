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

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from crypto.identity import MigrationState, StationIdentity
from csms.events import EventLog, EventType, Outcome
from csms.fleet import ConnectionState, FleetSnapshot, FleetView, StationView
from csms.persistence import NullStore


LOGGER = logging.getLogger("csms.registry")


def _now() -> datetime:
    """Timezone-aware UTC. Used for display and ordering only."""
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    """Rebuild a datetime stored as ISO-8601 text, tolerating a Z suffix.

    A value that cannot be parsed comes back as None rather than
    raising: a single unreadable timestamp in a restored row must not
    stop the CSMS from starting.
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class StationSession:
    """
    One live connection. Dies when the socket closes.

    Holds the connection object itself, because Day 8-9 command dispatch
    (SetChargingProfile, RequestStopTransaction) needs a route from a
    station_id back to the socket to push a command down it. That is the
    whole reason the registry cannot be replaced by a database table.

    ONLY facts that are genuinely about THIS connection live here. What
    the CSMS knows about the STATION lives in StationState below, and
    survives the socket closing. See that class for why.
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
    """Set when a BootNotification is accepted on THIS connection.

    Correctly per-connection, and deliberately not moved to
    StationState: a reconnected station has not identified itself until
    it boots again, and the E2 recovery predicate depends on that being
    true. Half of Contract 6's is_recovered."""

    last_heartbeat_at: datetime | None = None
    """Per-connection: a heartbeat proves this socket is alive."""

    last_handshake_ms: float | None = None
    """Populated by Day 7 instrumentation. Carried on the session rather
    than recomputed at snapshot time so the dashboard's latency panel is
    a field read, not a log scan."""

    bytes_tx: int = 0
    bytes_rx: int = 0
    """Day 7 instrumentation. Present now so that adding the counters
    later is wiring, not a schema change."""

    store_session_id: str = ""
    """Row identifier for this connection in the sessions table, minted
    by the store when the connection opened. Empty when persistence is
    off. Held so the close can find its own row without a lookup."""


@dataclass
class StationState:
    """
    What the CSMS knows about a STATION. Outlives any one connection.

    --------------------------------------------------------------------
    WHY THIS CLASS EXISTS -- two measured defects, one cause

    Until Day 6 all of these fields lived on StationSession, which is
    rebuilt from scratch on every connect. Track C measured both
    consequences against the running server:

      1. The status-change guard fired on EVERY reconnect. The handler
         compares the incoming connector status against the one held;
         after a reconnect the held value was None, so a station
         re-reporting "Available" looked like a transition into it. At
         500 stations that is 500 phantom state changes written into the
         dataset per E2 storm -- precisely what the guard was built to
         prevent.

      2. The stale-reading guard NEVER fired. Measured: 19 replayed
         readings applied, 0 rejected. last_meter_at was None on the
         fresh session, so the first replayed reading -- however old --
         had nothing to be compared against. That also made
         applied_to_live_state useless as a measurement of replay; the
         `offline` flag is what actually measures it.

    Both guards need to remember what came before. A per-connection
    record cannot, by construction. So the rule is now explicit:

        a fact about the STATION outlives the socket;
        a fact about the SOCKET dies with it.

    This is also what Day 6's SQLite layer persists. Nothing else in the
    registry needs to reach disk: a connection cannot survive a process
    restart anyway, but a station's last known state can and should.
    --------------------------------------------------------------------
    """

    station_id: str

    ocpp_status: str | None = None
    """Most recent connector status reported via StatusNotification,
    stored verbatim. Survives reconnection, so a station re-reporting
    its current status on a new socket is correctly seen as no change."""

    charging_state: str | None = None
    """Most recent charging state from TransactionEvent, stored
    verbatim. Track C's agent reports Charging, SuspendedEV,
    SuspendedEVSE and Idle here; SuspendedEVSE (curtailed by the CSMS)
    and SuspendedEV (the vehicle stopped) look identical on a dashboard
    and mean opposite things, so both reach the view unmodified."""

    active_transaction_id: str | None = None

    power_w: float | None = None
    """Last known instantaneous draw in watts.

    Retained after disconnection by decision, so the dashboard can show
    what a station was drawing when it vanished rather than a blank.
    StationView.power_is_stale marks it as not-current, and
    FleetSnapshot.aggregate_power_w counts only connected stations --
    a fleet total inflated by stations nobody can see would not survive
    a question about it."""

    energy_wh: float | None = None
    """Last known cumulative energy for the active transaction."""

    last_meter_at: datetime | None = None
    """The station's own timestamp on the newest reading applied.

    The stale-reading guard. A station that queued TransactionEvents
    while the CSMS was unreachable replays them on reconnect carrying
    timestamps minutes old; without this, a replayed backlog overwrites
    live power and energy with historical values. Now that it lives
    here, the guard survives the reconnect that used to reset it."""

    last_seq_no: int | None = None
    """seqNo of the last TransactionEvent seen for the active
    transaction. OCPP increments it per transaction, so a forward jump
    is direct evidence that events were lost -- worth having during an
    E2 reconnection storm, and impossible to reconstruct afterwards."""

    last_seen_at: datetime | None = None
    """When this station was last connected. Set on connect and again on
    disconnect, so the dashboard can render "7400 W, last seen 40 s ago"
    rather than presenting a stale figure as current."""


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
        store: Any | None = None,
    ) -> None:
        self._store = store if store is not None else NullStore()
        """Where station state reaches disk. A NullStore when persistence
        is off, so nothing in this class ever asks whether it has one."""

        self._sessions: dict[str, StationSession] = {}
        """Live connections only. Keyed by station_id, emptied on close."""

        self._states: dict[str, StationState] = {}
        """What the CSMS knows about each station. Survives reconnection,
        and from Day 6 survives a process restart via csms/persistence.py.
        See StationState for the two measured defects that made this
        separation necessary."""

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

    def load(self) -> int:
        """
        Restore the fleet from disk. Called once, at startup.

        This is what gives E2 its denominator. Without it a restarted
        CSMS reports total_stations = 0 and counts upwards, so it cannot
        say "47 of 500 recovered" -- it does not know there were 500.
        With it, every station the server ever knew reappears as
        disconnected and the recovery curve has something to recover
        towards.

        Returns:
            How many stations were restored.

        A station restored from disk is marked as having been seen
        before, so it reads as DISCONNECTED rather than NEVER_SEEN.
        Those mean different things: one is a station that has gone
        quiet, the other a station that has never arrived, and E2 cares
        about the difference.
        """
        for station_id, record in self._store.load_identities().items():
            try:
                self._identities[station_id] = StationIdentity.from_dict(record)
            except (TypeError, ValueError, KeyError):
                LOGGER.warning(
                    "stored identity for %s could not be rebuilt; ignoring",
                    station_id,
                )

        for station_id, fields in self._store.load_states().items():
            state = StationState(station_id=station_id)
            state.ocpp_status = fields.get("ocpp_status")
            state.charging_state = fields.get("charging_state")
            state.active_transaction_id = fields.get("active_transaction_id")
            state.power_w = fields.get("power_w")
            state.energy_wh = fields.get("energy_wh")
            state.last_meter_at = _parse_dt(fields.get("last_meter_at"))
            state.last_seq_no = fields.get("last_seq_no")
            state.last_seen_at = _parse_dt(fields.get("last_seen_at"))
            self._states[station_id] = state
            self._ensure_identity(station_id)
            if state.last_seen_at is not None:
                self._ever_connected.add(station_id)

        restored = len(self._identities)
        if restored:
            LOGGER.info("restored %d station(s) from persistence", restored)
        return restored

    def flush(self) -> int:
        """Push buffered writes to disk. Called on a timer by the server."""
        return self._store.flush()

    def close(self) -> None:
        """Flush and close the store. Called on clean shutdown."""
        self._store.close()

    def _persist_state(self, state: StationState) -> None:
        """Queue this station's state for the next flush.

        Buffered, not written: at 500 stations a disk round-trip per
        meter value would sit on the event loop serving the connections
        whose timings E2 is measuring. See csms/persistence.py.
        """
        self._store.save_state(state.station_id, asdict(state))

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

        session.store_session_id = self._store.open_session(
            station_id=station_id,
            connected_at=session.connected_since,
            handshake_ms=handshake_ms,
        )

        state = self._ensure_state(station_id)
        state.last_seen_at = session.connected_since
        self._persist_state(state)
        # Deliberately NOT reset. Everything the CSMS learned about this
        # station on its previous connection -- its connector status, its
        # last meter timestamp, its sequence position -- stays, because
        # that is what makes the status-change and stale-reading guards
        # work across a reconnect. See StationState.

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

        disconnected_at = _now()
        duration_ms = (
            time.monotonic_ns() - session.connected_monotonic_ns
        ) / 1e6
        self._store.close_session(
            session.store_session_id,
            disconnected_at=disconnected_at,
            duration_ms=duration_ms,
        )

        state = self._ensure_state(station_id)
        state.last_seen_at = disconnected_at
        self._persist_state(state)
        # Station state is NOT cleared. power_w in particular is retained
        # by decision, so the dashboard can show what a station was
        # drawing when it vanished; StationView.power_is_stale marks it
        # as no longer current and aggregate_power_w excludes it.

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

    def record_status(self, station_id: str, status: str) -> str | None:
        """
        Connector status from StatusNotification, stored verbatim.

        Returns:
            The status held BEFORE this one, or None if the CSMS has
            never had a status for this station. The caller uses it to
            decide whether this is a real transition worth logging.

        Returned rather than left for the handler to read off the
        session, because the value now lives on StationState and the
        handler should not need to know that. It is also what makes the
        reconnect case correct: a station re-reporting "Available" on a
        new socket returns "Available", not None, so it is no longer
        mistaken for a transition.
        """
        state = self._ensure_state(station_id)
        previous = state.ocpp_status
        state.ocpp_status = status
        self._persist_state(state)
        return previous

    def record_transaction(
        self,
        station_id: str,
        *,
        transaction_id: str | None,
        charging_state: str | None = None,
        id_token: str | None = None,
    ) -> None:
        """
        Transaction started, updated or ended.

        transaction_id of None means the transaction has ended; the
        station keeps its last charging_state until it reports a new one.

        Recorded on StationState, not the session, so a transaction that
        spans a reconnection is still the same transaction to the CSMS.
        """
        state = self._ensure_state(station_id)
        previous_transaction_id = state.active_transaction_id

        state.active_transaction_id = transaction_id
        if charging_state is not None:
            state.charging_state = charging_state

        if transaction_id is not None and transaction_id != previous_transaction_id:
            self._store.start_transaction(
                transaction_id=transaction_id,
                station_id=station_id,
                started_at=_now(),
                id_token=id_token,
            )
        elif transaction_id is None and previous_transaction_id is not None:
            self._store.end_transaction(
                transaction_id=previous_transaction_id,
                ended_at=_now(),
                energy_wh=state.energy_wh,
            )

        if transaction_id is None:
            state.power_w = 0.0
            state.last_meter_at = None
            state.last_seq_no = None

        self._persist_state(state)

    def record_meter(
        self,
        station_id: str,
        *,
        power_w: float | None = None,
        energy_wh: float | None = None,
        reading_at: datetime | None = None,
        seq_no: int | None = None,
        offline: bool = False,
    ) -> bool:
        """
        Apply meter values from a TransactionEvent to live state.

        Watts and watt-hours, matching Contract 5's units.

        Args:
            reading_at: the station's own timestamp for the reading. A
                reading older than the last one applied is REJECTED for
                live state -- see StationState.last_meter_at. The caller
                still records it to the event log, so nothing is lost
                from the dataset; it simply does not claim to be the
                present.

        Returns:
            True if live state was updated, False if the reading was
            stale. The caller logs the difference, which is how a run
            can report how much offline replay actually occurred.

        Now reads and writes StationState rather than the session. Track
        C measured 19 replayed readings applied and 0 rejected under the
        old arrangement, because a reconnect handed the guard a blank
        last_meter_at to compare against.
        """
        state = self._ensure_state(station_id)

        stale = (
            reading_at is not None
            and state.last_meter_at is not None
            and reading_at < state.last_meter_at
        )

        if not stale:
            if power_w is not None:
                state.power_w = power_w
            if energy_wh is not None:
                state.energy_wh = energy_wh
            if reading_at is not None:
                state.last_meter_at = reading_at
            self._persist_state(state)

        # Stored either way, flagged with whether it was applied. A
        # replayed reading the registry refused for live state is still
        # real history of that transaction, and dropping it would make
        # the database disagree with the event log about what the
        # station actually reported.
        self._store.add_meter_value(
            station_id=station_id,
            transaction_id=state.active_transaction_id,
            reading_at=reading_at,
            recorded_at=_now(),
            seq_no=seq_no,
            power_w=power_w,
            energy_wh=energy_wh,
            offline=offline,
            applied=not stale,
        )
        return not stale

    def record_sequence(self, station_id: str, seq_no: int) -> int:
        """
        Track a TransactionEvent's seqNo and report any gap.

        Returns:
            How many events appear to be missing before this one. 0 for
            an in-order event, for the first event of a transaction, or
            for a repeat -- a duplicate is not a loss.

        A non-zero return during E2 is evidence that messages were lost,
        which is a finding rather than a bug to hide. Out-of-order and
        duplicate events are both possible when a station replays a
        queue, so only a forward jump counts.

        WHERE the loss happened matters and the caller distinguishes it:
        a gap among replayed (offline=True) events means the agent's own
        bounded queue overflowed and dropped its oldest entries; a gap
        among live events means the message was lost in transit. Two
        different findings, so they are logged separately.

        Held on StationState so a transaction that spans a reconnection
        keeps its sequence position.
        """
        state = self._ensure_state(station_id)
        previous = state.last_seq_no
        state.last_seq_no = seq_no
        self._persist_state(state)
        if previous is None or seq_no <= previous:
            return 0
        return seq_no - previous - 1

    def reset_sequence(self, station_id: str) -> None:
        """Start a fresh sequence. Called when a transaction starts."""
        state = self._ensure_state(station_id)
        state.last_seq_no = None
        state.last_meter_at = None
        self._persist_state(state)

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

        # Both aggregates below count CONNECTED stations only, while each
        # station row still carries its last known figures. A station
        # retains power_w after disconnecting so the dashboard can show
        # what it was drawing when it vanished -- but a fleet total that
        # included stations nobody can currently see would be a number
        # that does not survive being asked about, and E5's whole claim
        # rests on aggregate_power_w meaning real present draw.
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
                1 for v in views
                if v.active_transaction_id is not None and not v.power_is_stale
            ),
            aggregate_power_w=sum(
                v.power_w or 0.0 for v in views if not v.power_is_stale
            ),
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
        self._store.save_identity(identity.station_id, identity.to_dict())

    # -- provisioning ---------------------------------------------------

    def provision(self, identity: StationIdentity) -> None:
        """
        Add a station the CSMS has not yet met.

        Lets a fleet of N be configured before anything connects, so the
        dashboard shows N rows in NEVER_SEEN rather than an empty table
        filling up. Also how Track B seeds capability profiles for the
        heterogeneous fleet in Stage 6.
        """
        if identity.station_id not in self._identities:
            self._identities[identity.station_id] = identity
            self._ensure_state(identity.station_id)
            self._store.save_identity(identity.station_id, identity.to_dict())

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
            self._store.save_identity(station_id, identity.to_dict())
        return identity

    def _ensure_state(self, station_id: str) -> StationState:
        """
        Fetch this station's persistent state, creating it on first use.

        Separate from _ensure_identity because the two have different
        owners: Contract 2's StationIdentity carries Track B's migration
        and certificate fields, StationState carries Track A's telemetry.
        The field-ownership split in Contract 6 is what lets both be
        written without a lock.
        """
        state = self._states.get(station_id)
        if state is None:
            state = StationState(station_id=station_id)
            self._states[station_id] = state
        return state

    # -- view construction ----------------------------------------------

    def _build_view(self, station_id: str) -> StationView:
        """
        Join three things into one dashboard row: the live session (if
        any), the station's persistent state, and the Contract 2 record.

        Connection fields come from the session and are None when the
        station is offline. Station fields come from StationState and
        persist -- which is what lets a disconnected row still show what
        the station was doing, flagged as not-current.
        """
        identity = self._identities[station_id]
        state = self._ensure_state(station_id)
        session = self._sessions.get(station_id)

        if session is not None:
            connection_state = ConnectionState.CONNECTED
        elif station_id in self._ever_connected:
            connection_state = ConnectionState.DISCONNECTED
        else:
            connection_state = ConnectionState.NEVER_SEEN

        seconds_since_heartbeat: float | None = None
        if session is not None and session.last_heartbeat_at is not None:
            seconds_since_heartbeat = (
                _now() - session.last_heartbeat_at
            ).total_seconds()

        return StationView(
            station_id=station_id,
            connection_state=connection_state.value,
            boot_accepted=bool(session and session.boot_accepted),
            connected_since=session.connected_since if session else None,
            last_seen_at=state.last_seen_at,
            last_heartbeat_at=session.last_heartbeat_at if session else None,
            seconds_since_heartbeat=seconds_since_heartbeat,
            last_handshake_ms=session.last_handshake_ms if session else None,
            ocpp_status=state.ocpp_status,
            charging_state=state.charging_state,
            active_transaction_id=state.active_transaction_id,
            power_w=state.power_w,
            energy_wh=state.energy_wh,
            power_is_stale=session is None,
            current_algorithm=identity.current_algorithm or None,
            supported_algorithms=list(identity.supported_algorithms),
            certificate_serial=identity.certificate_serial,
            certificate_expiry=identity.certificate_expiry,
            previous_certificate_serial=identity.previous_certificate_serial,
            migration_wave=identity.migration_wave,
            migration_state=identity.migration_state.value,
        )
