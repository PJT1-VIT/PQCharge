"""
Contract 2 — Station identity record.

Created/updated by:  Track B (idmanager)
Stored by:           Track A (csms, SQLite)
Displayed by:        Track C (status page, analysis)

FROZEN INTERFACE. Field names agreed on Day 2.

This record answers three questions about one charging station:
  - what cryptographic identity is it using right now
  - what could it use, if asked to change
  - where is it in the migration

Field names here are the field names in the database and on the status
page. Renaming one means changing three modules, so it is done by
agreement, not unilaterally.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class MigrationState(str, Enum):
    """
    Where a station sits in the migration.

    Inherits from str so that it serialises to a plain string in JSON
    and stores as TEXT in SQLite without conversion at the boundary.
    """

    PENDING = "pending"
    """Not yet migrated. Still on its original cryptographic identity."""

    IN_PROGRESS = "in_progress"
    """A new certificate has been issued; the station has not yet
    confirmed it is using it. Both certificates are valid during this
    window."""

    MIGRATED = "migrated"
    """Confirmed on the target algorithm. The previous certificate has
    been revoked."""

    ROLLED_BACK = "rolled_back"
    """Migration was attempted and reverted. The station is on its
    original identity and is excluded from further waves until
    manually re-enabled."""

    INCOMPATIBLE = "incompatible"
    """The station's declared capabilities do not include the target
    algorithm. It is skipped rather than failed -- this models the
    heterogeneous fleet, where older firmware cannot process ML-DSA."""


@dataclass
class StationIdentity:
    """
    The cryptographic identity of one charging station.

    One record per station. Track A holds these in its registry, keyed
    by station_id, and persists them to SQLite.
    """

    station_id: str
    """Matches the OCPP charging station identity used in the WebSocket
    path. The primary key everywhere in the system."""

    current_algorithm: str
    """Signature algorithm the station is authenticating with right
    now, as reported by CryptoProvider.signature_algorithm. Free-text
    for display and logging; never branched on."""

    supported_algorithms: list[str] = field(default_factory=list)
    """Everything this station's firmware can process. The migration
    orchestrator checks the target against this list before issuing;
    a station whose list excludes the target becomes INCOMPATIBLE
    rather than failing mid-rollout."""

    certificate_serial: str | None = None
    """Serial of the certificate currently in use. None before first
    issuance."""

    certificate_expiry: datetime | None = None
    """Expiry of the current certificate. Always timezone-aware UTC."""

    previous_certificate_serial: str | None = None
    """Serial of the certificate being replaced, retained through the
    overlapping-validity window so that either certificate
    authenticates the station during rotation. Cleared to None once
    the old certificate is revoked."""

    migration_wave: int | None = None
    """Which wave this station belongs to. 0 is the canary group.
    None means not yet assigned to any wave."""

    migration_state: MigrationState = MigrationState.PENDING
    """Current position in the migration."""

    last_updated: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    """When this record last changed. Timezone-aware UTC."""

    def to_dict(self) -> dict[str, Any]:
        """
        JSON-serialisable form, for storage, the status page and the
        event log. Datetimes become ISO-8601 strings; the enum becomes
        its string value.
        """
        d = asdict(self)
        d["migration_state"] = self.migration_state.value
        for key in ("certificate_expiry", "last_updated"):
            value = d.get(key)
            d[key] = value.isoformat() if isinstance(value, datetime) else None
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StationIdentity:
        """Rebuild a record from to_dict() output."""
        parsed = dict(data)
        parsed["migration_state"] = MigrationState(
            parsed.get("migration_state", MigrationState.PENDING.value)
        )
        for key in ("certificate_expiry", "last_updated"):
            value = parsed.get(key)
            parsed[key] = datetime.fromisoformat(value) if value else None
        if parsed.get("last_updated") is None:
            parsed["last_updated"] = datetime.now(timezone.utc)
        return cls(**parsed)

    def supports(self, algorithm: str) -> bool:
        """Whether this station can process the given algorithm."""
        return algorithm in self.supported_algorithms

    def is_rotating(self) -> bool:
        """
        Whether the station is inside an overlapping-validity window --
        two certificates valid at once. Track A's authentication path
        must accept either while this is true.
        """
        return (
            self.migration_state is MigrationState.IN_PROGRESS
            and self.previous_certificate_serial is not None
        )