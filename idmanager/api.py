"""
Contract 4 — Migration control API.

Provided by:  Track B (idmanager)
Called by:    Track C (status page, demo runner, experiment scripts)

FROZEN INTERFACE. Signatures agreed on Day 2.

Three operations: begin a migration, roll one wave back, ask what is
happening. Everything the orchestrator does is reachable through these.

start_migration() returns immediately with an identifier and does the
work in the background. A fleet migration takes minutes; a status page
that blocks for the duration is unusable, and the demo would appear
frozen at exactly the moment the panel is watching.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any

from crypto.provider import CryptoMode


class MigrationPhase(str, Enum):
    """Overall state of a migration run."""

    IDLE = "idle"
    """No migration has been started."""

    CANARY = "canary"
    """The canary group is migrating. No further wave begins until it
    reports clean."""

    RUNNING = "running"
    """Waves are progressing after a successful canary."""

    COMPLETED = "completed"
    """Every eligible station reached the target algorithm. Stations
    marked incompatible are excluded, not counted as failures."""

    ROLLED_BACK = "rolled_back"
    """A wave exceeded the failure threshold and was reverted. The
    migration is halted."""

    FAILED = "failed"
    """The orchestrator could not proceed -- distinct from a rollback,
    which is a controlled response to station failures."""


class WavePhase(str, Enum):
    """State of a single wave."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"


@dataclass
class WaveStatus:
    """One wave of a migration."""

    wave_id: int
    """Sequential. Wave 0 is always the canary group."""

    is_canary: bool
    station_ids: list[str] = field(default_factory=list)
    phase: WavePhase = WavePhase.QUEUED

    migrated_count: int = 0
    failed_count: int = 0
    """Stations whose rotation did not complete. Compared against the
    failure threshold to decide whether to roll this wave back."""

    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass
class MigrationStatus:
    """
    Snapshot of a migration. Returned by get_migration_status().

    Read by the status page on a poll and by the analysis scripts after
    a run, so every count needed for the results is present here rather
    than recomputed from the event log.
    """

    migration_id: str
    target_mode: CryptoMode
    phase: MigrationPhase

    total_stations: int = 0
    pending: int = 0
    in_progress: int = 0
    migrated: int = 0
    rolled_back: int = 0
    incompatible: int = 0
    """Counts by MigrationState. These sum to total_stations."""

    current_wave: int | None = None
    total_waves: int = 0
    waves: list[WaveStatus] = field(default_factory=list)

    started_at: datetime | None = None
    completed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """
        JSON-serialisable form for the status page.

        Round-tripped through json rather than returned from asdict()
        directly, so that nested enums and datetimes become plain
        strings. asdict() leaves enum members intact, which serialises
        correctly but compares wrongly.
        """
        return json.loads(json.dumps(asdict(self), default=str))

    @property
    def is_terminal(self) -> bool:
        """
        Whether the migration has stopped, successfully or otherwise.
        Poll loops use this as their exit condition.
        """
        return self.phase in (
            MigrationPhase.COMPLETED,
            MigrationPhase.ROLLED_BACK,
            MigrationPhase.FAILED,
        )


class MigrationController(ABC):
    """
    Control surface over the migration orchestrator.

    One instance per CSMS. Implementations live in idmanager/.
    """

    @abstractmethod
    def start_migration(
        self,
        wave_size: int,
        canary_count: int,
        target_mode: CryptoMode,
    ) -> str:
        """
        Begin migrating the fleet to target_mode.

        Args:
            wave_size: Stations per wave after the canary.
            canary_count: Stations in the canary group, migrated first
                and observed before any wave proceeds.
            target_mode: The cryptographic configuration to migrate to.

        Returns:
            migration_id, immediately. The work proceeds in the
            background; poll get_migration_status() to follow it.

        Raises:
            RuntimeError: if a migration is already in progress. Two
                concurrent migrations over one fleet would produce
                stations in indeterminate states.
        """

    @abstractmethod
    def rollback(self, wave_id: int) -> bool:
        """
        Revert one wave to its previous cryptographic identity.

        Called automatically by the orchestrator when a wave's failure
        count exceeds the threshold, and manually from the status page
        or the demo script.

        Args:
            wave_id: Which wave to revert. Wave 0 is the canary.

        Returns:
            True if the wave was reverted; False if wave_id is unknown
            or the wave was never started.
        """

    @abstractmethod
    def get_migration_status(self) -> MigrationStatus:
        """
        Current state of the migration.

        Safe to call at any time, including before any migration has
        started -- returns a status with phase IDLE rather than raising.
        Called on a poll by the status page, so it must be cheap and
        must not block on orchestrator work.
        """