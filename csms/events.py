"""
Contract 3 — Structured event log schema.

Emitted by:   Track A (csms)
Consumed by:  Track C (analysis, status page)

FROZEN INTERFACE. Field names agreed on Day 2.

Every measured number in the results chapter is derived from this file.
Nothing is computed twice: the log is written once during a run and
read once during analysis, and the analysis never re-derives a value
the server already knew.

Format: JSON Lines -- one complete JSON object per line, append-only.
Chosen over CSV because payloads are nested and vary by event type, and
over a database because a run must survive the server being killed
mid-write (experiment E2 does exactly that).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator


class EventType(str, Enum):
    """
    Every event kind the server emits.

    Adding a member is safe. Renaming one invalidates every log file
    recorded before the change, so treat these strings as permanent
    once the first real run is recorded.
    """

    # -- connection lifecycle: the source of E1 and E2 --
    CONNECTION_ATTEMPT = "connection_attempt"
    CONNECTION_ESTABLISHED = "connection_established"
    CONNECTION_FAILED = "connection_failed"
    CONNECTION_CLOSED = "connection_closed"

    # -- OCPP protocol traffic --
    MESSAGE_RECEIVED = "message_received"
    MESSAGE_SENT = "message_sent"

    # -- charging session, the cyber-physical layer --
    TRANSACTION_STARTED = "transaction_started"
    TRANSACTION_UPDATED = "transaction_updated"
    TRANSACTION_ENDED = "transaction_ended"
    STATE_CHANGED = "state_changed"

    # -- certificate lifecycle: the source of rotation timings --
    CERTIFICATE_REQUESTED = "certificate_requested"
    CERTIFICATE_ISSUED = "certificate_issued"
    CERTIFICATE_INSTALLED = "certificate_installed"
    CERTIFICATE_REVOKED = "certificate_revoked"
    ROTATION_STARTED = "rotation_started"
    ROTATION_COMPLETED = "rotation_completed"
    ROTATION_FAILED = "rotation_failed"

    # -- migration orchestration --
    MIGRATION_STARTED = "migration_started"
    WAVE_STARTED = "wave_started"
    WAVE_COMPLETED = "wave_completed"
    WAVE_ROLLED_BACK = "wave_rolled_back"
    MIGRATION_COMPLETED = "migration_completed"

    # -- server lifecycle: the anchor points for E2 --
    SERVER_STARTED = "server_started"
    SERVER_STOPPING = "server_stopping"


class Outcome(str, Enum):
    """Result of an operation. Only meaningful on some event types."""

    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    REJECTED = "rejected"


@dataclass
class Event:
    """
    One line of the log.

    The first five fields appear on every event. The rest are optional
    and populated only where they apply -- a connection event carries
    timing and byte counts, a state change does not.
    """

    # -- always present --

    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    """Wall-clock time, ISO-8601 UTC. Used for ordering and for
    correlating against external observations. Never used to compute a
    duration -- see monotonic_ns."""

    monotonic_ns: int = field(default_factory=time.monotonic_ns)
    """Monotonic clock reading in nanoseconds. All durations are
    computed from differences of this field, never from timestamp,
    because wall-clock time can step backwards under NTP correction
    and a 200 ms handshake would then be recorded as negative."""

    event_type: str = EventType.MESSAGE_RECEIVED.value
    """One of EventType."""

    run_id: str = ""
    """Identifies one experimental run. Every event from a single
    invocation shares this value. Without it, a 10-node run and a
    50-node run appended to the same file are indistinguishable at
    analysis time."""

    crypto_mode: str = ""
    """classical, hybrid or pqc -- the configuration in force when this
    event occurred. This is the grouping variable for every comparison
    chart, so it appears on every event rather than being looked up."""

    # -- usually present --

    station_id: str | None = None
    """Which station this concerns. None for server-level events."""

    # -- connection events --

    handshake_ms: float | None = None
    """Duration from connection attempt to established, in
    milliseconds, derived from monotonic_ns. The primary measurement
    of experiment E1."""

    bytes_tx: int | None = None
    """Bytes sent by the server on this connection."""

    bytes_rx: int | None = None
    """Bytes received by the server on this connection."""

    outcome: str | None = None
    """One of Outcome, where the event represents a completed
    operation."""

    # -- free-form --

    payload: dict[str, Any] = field(default_factory=dict)
    """Event-specific detail: OCPP action name, certificate serial,
    wave number, state transition, error text. Kept separate from the
    typed fields above so that adding detail never changes the schema
    the analysis scripts depend on."""

    def to_json(self) -> str:
        """Serialise to a single line, no embedded newlines."""
        return json.dumps(asdict(self), separators=(",", ":"), default=str)

    @classmethod
    def from_json(cls, line: str) -> Event:
        """Parse one line back into an Event."""
        return cls(**json.loads(line))


class EventLog:
    """
    Append-only writer. One instance per server process.

    Thread-safe: the CSMS handles many connections concurrently and
    several may log at once. Without the lock, two writes interleave
    and produce a corrupt line that breaks the whole analysis run.
    """

    def __init__(
        self,
        path: str | Path = "logs/events.jsonl",
        run_id: str | None = None,
        crypto_mode: str = "classical",
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.crypto_mode = crypto_mode
        self._lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8")

    def emit(
        self,
        event_type: EventType | str,
        station_id: str | None = None,
        *,
        handshake_ms: float | None = None,
        bytes_tx: int | None = None,
        bytes_rx: int | None = None,
        outcome: Outcome | str | None = None,
        **payload: Any,
    ) -> Event:
        """
        Write one event.

        Any extra keyword arguments are collected into payload, so
        callers add detail without touching this signature:

            log.emit(EventType.STATE_CHANGED, "CP001",
                     old="Available", new="Charging")
        """
        event = Event(
            event_type=(
                event_type.value if isinstance(event_type, EventType) else event_type
            ),
            run_id=self.run_id,
            crypto_mode=self.crypto_mode,
            station_id=station_id,
            handshake_ms=handshake_ms,
            bytes_tx=bytes_tx,
            bytes_rx=bytes_rx,
            outcome=(outcome.value if isinstance(outcome, Outcome) else outcome),
            payload=payload,
        )
        line = event.to_json()
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())
        return event

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._fh.close()

    def __enter__(self) -> EventLog:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def read_events(path: str | Path) -> Iterator[Event]:
    """
    Stream events back from a log file.

    Malformed lines are skipped rather than raising: a run killed
    mid-write can leave one truncated final line, and losing the whole
    dataset to it would be absurd.
    """
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield Event.from_json(line)
            except (json.JSONDecodeError, TypeError):
                continue