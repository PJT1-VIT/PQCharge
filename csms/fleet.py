"""
Contract 6 — Fleet state and control surface.

Provided by:  Track A (csms)
Consumed by:  Track C (dashboard, demo runner, experiment scripts)
              Track B (migration orchestrator, in-process)

PROPOSED — merge to main before Tracks B and C build against it.
Contracts 1-5 were frozen on Day 2. This one was missed: Contract 3 is
an append-only log and Contract 2 defines a record shape, but nothing
defined how anything asks the CSMS what is true *right now*. Track C
surfaced the gap while planning the dashboard.

--------------------------------------------------------------------
WHY THIS EXISTS

The CSMS is the only component that knows live fleet state. Stations
connect to it; it holds the open WebSocket connections; the session
registry lives in its memory. Nothing else can see any of that.

Contract 3 answers "what happened." This answers "what is true now."
Deriving the second from the first means replaying an append-only log
of hundreds of thousands of lines on every dashboard refresh, which is
not a design, it is an accident waiting for E2.
--------------------------------------------------------------------

TWO SURFACES, ONE SOURCE OF TRUTH

  In-process (FleetView, below)
      The migration orchestrator runs INSIDE the CSMS process, because
      certificate issuance travels over OCPP messages on live station
      connections that only the CSMS owns. It therefore calls this
      interface directly -- no serialisation, no network hop.

  Over HTTP (endpoint table, below)
      The dashboard is a genuinely separate process, and must be: E2
      kills the CSMS and measures fleet recovery. A dashboard living
      inside the CSMS dies with it and shows nothing at exactly the
      moment being measured.

Both read the same registry. The HTTP layer is a serialisation of the
Python layer, never a second store.

--------------------------------------------------------------------
WRITE OWNERSHIP -- read this before touching a StationIdentity

Two tracks write to one record, so ownership is split by field group
rather than by lock. There is no field either track may write.

  Track A (csms) owns:   connection_state, ocpp_status, charging_state,
                         power_w, energy_wh, active_transaction_id,
                         boot_accepted, last_heartbeat_at,
                         connected_since, last_handshake_ms
  Track B (idmanager)    migration_state, migration_wave,
  owns:                  certificate_serial, certificate_expiry,
                         previous_certificate_serial,
                         current_algorithm, supported_algorithms

Track A never writes a migration field. Track B never writes a
telemetry field. Single writer per field, so no lock is needed on the
record and no update can be lost to a race.
--------------------------------------------------------------------

HTTP SURFACE

Served on the same host/port as the OCPP WebSocket endpoint, via the
websockets library's process_request hook -- no second server, no extra
dependency, no extra port. Paths under /api/ are answered as HTTP;
everything else is treated as an OCPP WebSocket upgrade.

  GET  /api/health              Liveness. {"ok": true, "run_id": ...}.
                                Used by E2 to detect the moment the
                                restarted CSMS becomes reachable.

  GET  /api/fleet               FleetSnapshot.to_dict(). The dashboard's
                                one-second poll. Serves from memory only.

  GET  /api/fleet/{station_id}  StationView.to_dict(), or 404.

  GET  /api/migration           Contract 4 MigrationStatus.to_dict(),
                                forwarded from the in-process controller.

  POST /api/migration/start     ?wave_size=&canary_count=&target_mode=
                                -> {"migration_id": str}
                                409 if a migration is already running.
                                501 while Track B ships only the stub.

  POST /api/migration/rollback  ?wave_id=
                                -> {"reverted": bool}
                                501 while Track B ships only the stub.

Control parameters are query parameters, not JSON request bodies. This
is not a style choice: the websockets library's process_request hook is
handed the request line and headers only, and never reads a request
body. Accepting JSON bodies would mean running a second HTTP server on
a second port purely to receive two integers. Query parameters keep the
whole control surface on one port with no additional dependency, at the
cost of being mildly unRESTful on two endpoints.

No authentication. Binds to localhost only. Recorded in
docs/limitations.md, not hidden: this is an operator-side control
surface on a research prototype, and an examiner asking about it should
get a straight answer rather than a defence.

The dashboard page is served from the CSMS as a static file so that
browser same-origin rules apply and no CORS configuration is needed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any

from crypto.identity import MigrationState, StationIdentity


class ConnectionState(str, Enum):
    """
    Liveness of one station's link to the CSMS.

    Inherits from str so it serialises to a plain string in JSON,
    matching the convention set by Contract 2's MigrationState.
    """

    CONNECTED = "connected"
    """Socket is open. Says nothing about whether the station has
    identified itself -- see StationView.boot_accepted."""

    DISCONNECTED = "disconnected"
    """Known station, previously seen, currently not connected. This is
    the state the whole fleet occupies for the duration of an E2 run."""

    NEVER_SEEN = "never_seen"
    """Provisioned in the station table but has never connected. Keeps
    a configured fleet size meaningful before anything boots."""


@dataclass
class StationView:
    """
    One row of the fleet table: everything known about one station at
    one instant.

    A flattened join of live connection state (Track A, in memory) and
    the Contract 2 identity record (Track B's fields, persisted). Kept
    flat deliberately -- the dashboard renders a table, and a nested
    shape would push the join into JavaScript.
    """

    station_id: str
    """Contract 2's primary key. The same string used in the WebSocket
    connection path."""

    # -- connection, owned by Track A ----------------------------------

    connection_state: str = ConnectionState.NEVER_SEEN.value
    """One of ConnectionState."""

    boot_accepted: bool = False
    """Whether this station has completed a BootNotification that the
    CSMS accepted on its current connection.

    Distinct from connection_state on purpose. A socket being open does
    not mean the CSMS knows who is on the other end. The agreed
    definition of 'recovered' for E2 is connection_state == connected
    AND boot_accepted -- the first instant at which the station is
    genuinely back in the fleet rather than merely reachable."""

    connected_since: datetime | None = None
    """When the current connection opened. None when not connected.
    Timezone-aware UTC."""

    last_heartbeat_at: datetime | None = None
    """Timestamp of the most recent Heartbeat. Timezone-aware UTC."""

    seconds_since_heartbeat: float | None = None
    """Computed at snapshot time so the dashboard does not do clock
    arithmetic against a server in another timezone."""

    last_handshake_ms: float | None = None
    """Duration of this station's most recent connection handshake, as
    logged to Contract 3's handshake_ms. Repeated here so the
    dashboard's latency panel does not have to parse the event log
    live."""

    # -- physical and session state, owned by Track A ------------------

    ocpp_status: str | None = None
    """Most recent connector status reported via StatusNotification,
    passed through verbatim.

    This contract deliberately does not enumerate the legal values. They
    are defined by OCPP 2.0.1 and supplied by the ocpp library's own
    enums; restating them here would create a second, divergent copy of
    the specification."""

    charging_state: str | None = None
    """Most recent charging state reported via TransactionEvent, passed
    through verbatim. Same reasoning as ocpp_status."""

    active_transaction_id: str | None = None
    """Transaction currently in progress, or None."""

    power_w: float | None = None
    """Instantaneous power draw in watts, from the most recent meter
    value. Watts, matching Contract 5."""

    energy_wh: float | None = None
    """Cumulative energy for the active transaction, in watt-hours.
    Matching Contract 5."""

    # -- cryptographic identity, owned by Track B ----------------------

    current_algorithm: str | None = None
    """Display string for the signature algorithm this station is
    authenticating with, as reported by the crypto provider. Free text
    for the table and the report. Never branched on -- see Contract 1's
    rule that no module outside crypto/ names an algorithm."""

    supported_algorithms: list[str] = field(default_factory=list)
    """Contract 2's capability list. Shown so that a station skipped as
    incompatible during a migration is visibly skipped rather than
    silently absent."""

    certificate_serial: str | None = None
    certificate_expiry: datetime | None = None
    previous_certificate_serial: str | None = None
    """Contract 2's certificate fields. previous_certificate_serial is
    non-None exactly during an overlapping-validity window, which is
    what makes mid-session rotation visible on the dashboard as it
    happens -- the single hardest result in the project, and the one to
    be able to point at during the demo."""

    migration_wave: int | None = None
    migration_state: str = MigrationState.PENDING.value
    """Contract 2's MigrationState value."""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form. Datetimes become ISO-8601 strings."""
        d = asdict(self)
        for key in ("connected_since", "last_heartbeat_at", "certificate_expiry"):
            value = d.get(key)
            d[key] = value.isoformat() if isinstance(value, datetime) else None
        return d

    @property
    def is_recovered(self) -> bool:
        """
        The E2 recovery predicate, defined once, in code, before the
        experiment runs.

        Section 13.1 of the design document is explicit that deciding
        what 'recovered' means during analysis silently invalidates the
        comparison across all twelve runs. Putting it here means the
        dashboard, the load generator and the analysis scripts cannot
        disagree about it.
        """
        return (
            self.connection_state == ConnectionState.CONNECTED.value
            and self.boot_accepted
        )


@dataclass
class FleetSnapshot:
    """
    The whole fleet at one instant. One HTTP GET, one dashboard redraw.

    Aggregates are computed server-side rather than in the browser
    because the same numbers are read by the demo runner and the
    analysis scripts, and three implementations of one sum is three
    chances to disagree.
    """

    generated_at: datetime
    run_id: str
    """Contract 3's run_id for the currently running CSMS. Lets the
    dashboard notice a server restart -- the run_id changes -- which is
    the visible marker of the E2 kill."""

    crypto_mode: str
    """The mode the server is running in: the grouping variable for
    every comparison chart."""

    total_stations: int = 0
    connected_count: int = 0
    booted_count: int = 0
    charging_count: int = 0

    aggregate_power_w: float = 0.0
    """Sum of power_w across all stations drawing power.

    The headline number of the E5 attack demonstration: a forged
    identity issues a fleet-wide charging profile and this figure spikes
    on screen. The cyber-to-physical consequence, in one value."""

    stations: list[StationView] = field(default_factory=list)

    migration: dict[str, Any] | None = None
    """Contract 4's MigrationStatus.to_dict(), embedded so the dashboard
    makes one request per refresh rather than two that can disagree with
    each other. None if no migration controller is attached."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "run_id": self.run_id,
            "crypto_mode": self.crypto_mode,
            "total_stations": self.total_stations,
            "connected_count": self.connected_count,
            "booted_count": self.booted_count,
            "charging_count": self.charging_count,
            "aggregate_power_w": self.aggregate_power_w,
            "stations": [s.to_dict() for s in self.stations],
            "migration": self.migration,
        }


class FleetView(ABC):
    """
    Read and write access to live fleet state, for code running inside
    the CSMS process.

    Implemented by Track A over the session registry. Track B's
    migration orchestrator holds one of these; it is the orchestrator's
    only route to per-station state, and it replaces any temptation to
    reach into the registry's internals or open the SQLite file
    alongside the server.

    Sending OCPP messages down to a station is NOT part of this
    contract. That is command dispatch (csms/dispatch.py, Days 8-9) and
    belongs in its own surface.
    """

    # -- read ----------------------------------------------------------

    @abstractmethod
    def get_station(self, station_id: str) -> StationView | None:
        """One station's current view, or None if unknown."""

    @abstractmethod
    def list_stations(self) -> list[StationView]:
        """
        Every known station, connected or not, ordered by station_id so
        that the dashboard table does not reshuffle on every refresh.
        """

    @abstractmethod
    def snapshot(self) -> FleetSnapshot:
        """
        The whole fleet plus aggregates.

        Must be cheap and must not block -- it is called once per second
        per dashboard client, and during E2 it is called while five
        hundred stations are reconnecting. Served from in-memory state;
        it never touches SQLite on this path.
        """

    @abstractmethod
    def get_identity(self, station_id: str) -> StationIdentity | None:
        """
        The persisted Contract 2 record for one station.

        Track B's orchestrator reads this to decide eligibility: whether
        a station's supported_algorithms include the migration target,
        and what its current migration_state is.
        """

    # -- write ---------------------------------------------------------

    @abstractmethod
    def set_identity(self, identity: StationIdentity) -> None:
        """
        Replace one station's Contract 2 record, in memory and in SQLite.

        Called by Track B's orchestrator when it issues a certificate,
        confirms installation, revokes, or rolls a station back. Track A
        persists the record and emits the corresponding Contract 3
        event, so the migration appears in the log without the
        orchestrator having to log anything itself.

        Whole-record replacement rather than a partial update: the field
        ownership split at the top of this module means the orchestrator
        never holds a stale copy of a field it does not own, and a
        partial-update API would invite exactly that.

        Raises:
            KeyError: if station_id is unknown. A migration that writes
                to a station the CSMS has never heard of is a bug, not
                a condition to absorb silently.
        """
