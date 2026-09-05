"""
Placeholder controller. Lets Track C build the status page and demo
runner before Track B's orchestrator exists (Days 3-12).

get_migration_status() returns a real IDLE status rather than raising,
because a status page that crashes when nothing is migrating is worse
than useless. The two mutating methods raise.

Delete once idmanager/orchestrator.py exists.
"""

from __future__ import annotations

from idmanager.api import (
    MigrationController,
    MigrationStatus,
    MigrationPhase,
)
from crypto.provider import CryptoMode


class StubController(MigrationController):
    """Satisfies the interface; orchestrates nothing."""

    def start_migration(
        self,
        wave_size: int,
        canary_count: int,
        target_mode: CryptoMode,
    ) -> str:
        raise NotImplementedError("Track B: orchestrator not implemented yet")

    def rollback(self, wave_id: int) -> bool:
        raise NotImplementedError("Track B: orchestrator not implemented yet")

    def get_migration_status(self) -> MigrationStatus:
        return MigrationStatus(
            migration_id="",
            target_mode="classical",
            phase=MigrationPhase.IDLE,
        )