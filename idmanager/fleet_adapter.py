"""
Adapter from Track A's FleetView (Contract 6) to the orchestrator's FleetLike.

The orchestrator depends on a small structural Protocol (FleetLike), not on
csms.registry.SessionRegistry directly. This adapter is the one place that
knows Track A's real method names and record shapes, so if Contract 6's
surface changes, only this file moves -- the orchestrator does not.

It also encapsulates the read-modify-write that Contract 6 requires:
set_identity replaces a whole StationIdentity, and every field in that record
is Track B's to write (Contract 6's ownership split), so the orchestrator
modifies the migration fields on the fetched record and writes it back. It
never touches a telemetry field, because telemetry lives on StationView /
StationState, not on StationIdentity.
"""

from __future__ import annotations

from crypto.identity import MigrationState, StationIdentity
from csms.fleet import FleetView


class FleetAdapter:
    """Presents csms FleetView as the orchestrator's FleetLike surface."""

    def __init__(self, fleet: FleetView) -> None:
        self._fleet = fleet

    def migration_candidate_ids(self) -> list[str]:
        """
        Every station the CSMS knows about, in stable order.

        list_stations() returns StationView objects ordered by station_id;
        we take their ids. Includes disconnected stations by design -- a
        fleet-wide migration must account for stations currently offline,
        and the orchestrator's per-station dispatch will simply fail for
        one that is not connected, which the wave logic already treats as
        a station failure rather than a crash.
        """
        return [v.station_id for v in self._fleet.list_stations()]

    def supported_algorithms(self, station_id: str) -> list[str]:
        """
        A station's declared capabilities, from its Contract 2 record.

        Returns [] if the station is unknown or has no declared
        capabilities. An empty list means the orchestrator will treat the
        station as incompatible with any PQC target -- which is correct:
        a station that never declared ML-DSA support must not be assumed
        to have it. Capability profiles are seeded via provision() /
        set_identity() before a migration runs (Stage 6 fleet setup).
        """
        identity = self._fleet.get_identity(station_id)
        if identity is None:
            return []
        return list(identity.supported_algorithms)

    def set_state(
        self,
        station_id: str,
        state: MigrationState,
        wave: int | None,
    ) -> None:
        """
        Write a station's migration state, preserving every other field.

        Read-modify-write of the whole StationIdentity, as Contract 6's
        set_identity requires. Only migration_state, migration_wave and
        current_algorithm are touched -- all Track B fields. Telemetry is
        not in this record and cannot be clobbered.

        Silently returns for an unknown station rather than raising: by
        the time the orchestrator writes state, it has already read the
        identity via supported_algorithms(); a station that vanished in
        between is a lost race, not a bug worth aborting a 500-station
        wave over.
        """
        identity = self._fleet.get_identity(station_id)
        if identity is None:
            return
        identity.migration_state = state
        identity.migration_wave = wave
        try:
            self._fleet.set_identity(identity)
        except KeyError:
            # Station deregistered between read and write -- lost race,
            # not an error. See docstring.
            return

    def mark_migrated_algorithm(self, station_id: str, algorithm: str) -> None:
        """
        Record that a station now authenticates with `algorithm`.

        Called by the orchestrator on a successful transition so the
        dashboard's current_algorithm column reflects the post-quantum
        identity. Separate from set_state because it is set only on
        success, not on every state transition.
        """
        identity = self._fleet.get_identity(station_id)
        if identity is None:
            return
        identity.current_algorithm = algorithm
        try:
            self._fleet.set_identity(identity)
        except KeyError:
            return