"""
Migration orchestrator — the fleet-scale post-quantum migration engine.

Track B. Days 10-11. Implements Contract 4 (MigrationController).

WHAT MIGRATION MEANS HERE (Option B, application-layer PQC):
A station is "migrated" when its ML-DSA public key is enrolled in the
PQAuthenticator, so it can prove its identity by challenge-response
(crypto/pq_auth.py). The station keeps its classical certificate for TLS
transport; the post-quantum identity is layered on, not swapped in. This
is the coherent continuation of the Option B decision already merged:
there is no PQC X.509 to reissue, so migration is enrolment, not
certificate rotation.

GENERALIZED TRANSITION MACHINERY:
Every per-station change goes through _transition_station, which moves a
station from identity A to identity B with an overlap window. Enrolment
is one instance (no-PQC -> PQC-enrolled). Certificate rotation, when it
is added, is another instance (old key -> new key) and reuses the same
coroutine -- so rotation later is a sibling method, not a rewrite. This
was a deliberate design choice to keep "add rotation later" cheap.

THE OVERLAP-SAFETY INVARIANT:
Enrol the new identity BEFORE instructing the station to use it. During
that window both identities authenticate, so no in-flight transaction is
dropped (docs/limitations.md R1: the guarantee is no lost transaction,
not an unbroken socket). If the station never confirms, the enrolment is
rolled back, so a station is never left claiming a PQC identity it is not
actually using.

CONCURRENCY MODEL:
Contract 4's three methods are synchronous and return immediately. The
migration itself runs as a background asyncio task that updates shared
state those methods read. start_migration launches the task and returns
a migration id; get_migration_status reads a snapshot; rollback signals
the task. This matches Contract 4's stated intent exactly ("returns
immediately; the work proceeds in the background").

PROOF OF POSSESSION (added Day 12, before the A+B+C session):
Accepting InstallPQAuth only proves a station received a key. When the
orchestrator is built with challenge_message_factory and signature_parser,
each station is then sent a PQAuthChallenge straight after the install, and
is marked MIGRATED only if its ML-DSA signature verifies against the key
just enrolled. Without those two arguments the behaviour is exactly as
before (install accepted = migrated), so existing callers keep working.

OFFLINE STATIONS (added Day 12):
By default a station that is not connected fails its install and counts
against the wave's failure threshold. Built with skip_offline=True (and a
fleet that answers is_connected), an offline station is DEFERRED instead:
left PENDING, excluded from the threshold, and migratable by a later run.

LIVE CERTIFICATE ROTATION (Phase 5 of the Python 3.14 plan):
Built with rotation_driver (idmanager/rotation.py), each station's transition
is the original plan's Stage 5 instead of Option B enrolment: the station makes
its own ML-DSA-44 key, sends a CSR, receives an ML-DSA certificate, is
disconnected by the CSMS and must come back presenting the NEW certificate.
One CONNECTION_ATTEMPT (transition="cert_rotation") per station records it.
Everything else -- capability gate, offline deferral, waves, canary, the
failure threshold, the counters -- is the same code path. A wave that fails
its threshold reverts its rotated stations: their new serials are refused and
they fall back to their ECDSA certificates. Without rotation_driver the
orchestrator behaves exactly as before (Option B stays available, as the plan
requires, as the fallback).

WHAT IT DOES NOT DO:
It never writes per-command dispatch events -- csms/dispatch.py already
emits MESSAGE_SENT with dispatched=True for every command it sends
(Contract 3 belongs to Track A's CSMS). The orchestrator emits only the
migration-level events (MIGRATION_STARTED, WAVE_STARTED, WAVE_COMPLETED,
WAVE_ROLLED_BACK, MIGRATION_COMPLETED), plus one CONNECTION_ATTEMPT
(transition="pq_auth") per verified station when proof of possession is
wired, and "station_deferred" when skip_offline defers a station, through
the event_emitter Track A supplies. "station_deferred" and
"migration_failed" are not Contract 3 EventType members -- see the dev plan.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol

from crypto.identity import MigrationState
from crypto.provider import CryptoMode
from idmanager.api import (
    MigrationController,
    MigrationPhase,
    MigrationStatus,
    WavePhase,
    WaveStatus,
)

LOGGER = logging.getLogger("idmanager.orchestrator")

DEFAULT_FAILURE_THRESHOLD = 0.2
"""Fraction of a wave's eligible stations that may fail before the wave is
rolled back. 0.2 = a wave is abandoned if more than a fifth of the stations
that COULD migrate fail to. Incompatible stations are excluded from the
denominator -- they are skipped, not failures."""


# -- collaborator interfaces ------------------------------------------
#
# The orchestrator depends on these small Protocols, not on Track A's or
# Track C's concrete classes. This is what lets the rotation harness drive
# it with fakes, and what keeps Track B's module from importing csms/
# internals directly. The real CSMS passes its real objects; they satisfy
# these shapes structurally.


class DispatchLike(Protocol):
    """The one method the orchestrator needs from csms.dispatch.CommandDispatcher."""

    async def send(self, station_id: str, request: object, *, timeout_s: float | None = ...):
        ...


class AuthenticatorLike(Protocol):
    """The enrolment and challenge surface from crypto.pq_auth.PQAuthenticator.
    issue_challenge / verify_response are used only when proof of possession
    is wired (challenge_message_factory + signature_parser)."""

    def enrol(self, station_id: str, public_key: bytes) -> None: ...
    def unenrol(self, station_id: str) -> None: ...
    def issue_challenge(self, station_id: str) -> bytes: ...
    def verify_response(self, station_id: str, response_signature: bytes) -> bool: ...


class FleetLike(Protocol):
    """What the orchestrator needs to know about the fleet, satisfied by the
    CSMS FleetView. Reads only -- the orchestrator writes station migration
    state back through set_state, never by mutating fleet internals."""

    def migration_candidate_ids(self) -> list[str]: ...
    def supported_algorithms(self, station_id: str) -> list[str]: ...
    def set_state(self, station_id: str, state: MigrationState, wave: int | None) -> None: ...
    def mark_migrated_algorithm(self, station_id: str, algorithm: str) -> None: ...


# The station-side install message is provided by a factory so the orchestrator
# does not hard-code an OCPP message type Track A/C own. In enrolment mode this
# builds the message that tells a station to begin PQC challenge-response; the
# harness passes a stub. Returns any object dispatch.send() will accept.
InstallMessageFactory = Callable[[str, bytes], object]

# Produces a fresh (private, public) ML-DSA keypair for a station being
# enrolled. In production this is provider.generate_keypair; the harness may
# pass a deterministic fake.
KeypairFactory = Callable[[], tuple[bytes, bytes]]

# Builds the OCPP call that carries a challenge nonce to a station. In the
# live CSMS this is agent.pqc_messages.build_challenge_message -- injected,
# like the install factory, so idmanager/ never imports agent/.
ChallengeMessageFactory = Callable[[bytes], object]

# Pulls the station's signature bytes out of the dispatcher's response
# object (DispatchResult.response). In the live CSMS:
#     lambda response: agent.pqc_messages.parse_signature(response.data)
SignatureParser = Callable[[object], bytes]


class MigrationOrchestrator(MigrationController):
    """
    Implements Contract 4. One instance per CSMS process.

    Constructed with the collaborators it drives. Everything it needs from
    Track A (dispatch, fleet) and from Track B's own crypto (authenticator,
    keypair factory) is injected, so the orchestrator is testable in full
    isolation against fakes -- the rotation harness does exactly that.
    """

    def __init__(
        self,
        *,
        dispatcher: DispatchLike,
        authenticator: AuthenticatorLike,
        fleet: FleetLike,
        keypair_factory: KeypairFactory,
        install_message_factory: InstallMessageFactory,
        event_emitter: Callable[..., object] | None = None,
        failure_threshold: float = DEFAULT_FAILURE_THRESHOLD,
        dispatch_timeout_s: float | None = None,
        target_algorithm: str = "ML-DSA-44",
        challenge_message_factory: ChallengeMessageFactory | None = None,
        signature_parser: SignatureParser | None = None,
        skip_offline: bool = False,
        rotation_driver: object | None = None,
    ) -> None:
        if (challenge_message_factory is None) != (signature_parser is None):
            raise ValueError(
                "challenge_message_factory and signature_parser must be given "
                "together: one without the other cannot verify a station"
            )
        self._make_challenge_msg = challenge_message_factory
        self._parse_signature = signature_parser
        self._skip_offline = skip_offline
        self._dispatch = dispatcher
        self._auth = authenticator
        self._fleet = fleet
        self._make_keypair = keypair_factory
        self._make_install_msg = install_message_factory
        self._emit = event_emitter or (lambda *a, **k: None)
        self._threshold = failure_threshold
        self._dispatch_timeout_s = dispatch_timeout_s
        self._target_algorithm = target_algorithm
        self._rotation = rotation_driver
        self._new_serials: dict[str, str | None] = {}
        """station -> serial issued by rotation in this run, for wave rollback."""
        self._previous_serials: dict[str, str | None] = {}

        self._status: MigrationStatus | None = None
        self._task: asyncio.Task | None = None
        self._enrolled_this_run: dict[int, list[str]] = {}
        """wave_id -> station_ids enrolled in that wave, for rollback."""
        self._rollback_requested: set[int] = set()

    # -- Contract 4 surface (synchronous) -----------------------------

    def start_migration(
        self,
        wave_size: int,
        canary_count: int,
        target_mode: CryptoMode,
    ) -> str:
        if self._task is not None and not self._task.done():
            raise RuntimeError("a migration is already in progress")

        migration_id = uuid.uuid4().hex[:12]
        candidates = self._fleet.migration_candidate_ids()

        self._status = MigrationStatus(
            migration_id=migration_id,
            target_mode=target_mode,
            phase=MigrationPhase.CANARY,
            total_stations=len(candidates),
            pending=len(candidates),
            started_at=datetime.now(timezone.utc),
        )
        self._enrolled_this_run = {}
        self._rollback_requested = set()

        self._new_serials = {}
        self._previous_serials = {}
        self._emit("migration_started", migration_id=migration_id,
                   target_mode=target_mode, total=len(candidates),
                   wave_size=wave_size, canary_count=canary_count,
                   method="cert_rotation" if self._rotation is not None else "pq_enrolment")

        self._task = asyncio.ensure_future(
            self._run(candidates, wave_size, canary_count, target_mode)
        )
        return migration_id

    def rollback(self, wave_id: int) -> bool:
        if self._status is None:
            return False
        if wave_id not in self._enrolled_this_run:
            return False
        self._rollback_requested.add(wave_id)
        # Synchronous best-effort un-enrol so a manual rollback takes effect
        # immediately for callers polling status, even if the background task
        # is between waves.
        self._rollback_wave(wave_id)
        return True

    def get_migration_status(self) -> MigrationStatus:
        if self._status is None:
            return MigrationStatus(
                migration_id="",
                target_mode="classical",
                phase=MigrationPhase.IDLE,
            )
        return self._status

    # -- background migration -----------------------------------------

    async def _run(self, candidates, wave_size, canary_count, target_mode) -> None:
        try:
            canary = candidates[:canary_count]
            rest = candidates[canary_count:]

            wave_id = 0
            ok = await self._do_wave(wave_id, canary, target_mode, is_canary=True)
            if not ok:
                self._finish(MigrationPhase.ROLLED_BACK)
                return

            self._status.phase = MigrationPhase.RUNNING
            for i in range(0, len(rest), wave_size):
                wave_id += 1
                batch = rest[i:i + wave_size]
                ok = await self._do_wave(wave_id, batch, target_mode, is_canary=False)
                if not ok:
                    self._finish(MigrationPhase.ROLLED_BACK)
                    return

            self._finish(MigrationPhase.COMPLETED)
        except Exception as exc:  # noqa: BLE001 - a crash must not wedge status
            LOGGER.exception("migration task crashed")
            self._status.phase = MigrationPhase.FAILED
            self._status.completed_at = datetime.now(timezone.utc)
            self._emit("migration_failed", error=f"{type(exc).__name__}: {exc}")

    async def _do_wave(self, wave_id, station_ids, target_mode, *, is_canary) -> bool:
        """Transition one wave. Returns False if it exceeded the failure
        threshold (caller then rolls back and halts)."""
        wave = WaveStatus(
            wave_id=wave_id, is_canary=is_canary,
            station_ids=list(station_ids), phase=WavePhase.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        self._status.waves.append(wave)
        self._status.current_wave = wave_id
        self._status.total_waves = len(self._status.waves)
        self._enrolled_this_run[wave_id] = []
        self._emit("wave_started", wave_id=wave_id, is_canary=is_canary,
                   size=len(station_ids))

        outcomes = await asyncio.gather(*(
            self._transition_station(sid, wave_id, target_mode)
            for sid in station_ids
        ))

        migrated = sum(1 for o in outcomes if o == MigrationState.MIGRATED)
        failed = sum(1 for o in outcomes if o == MigrationState.ROLLED_BACK)
        incompatible = sum(1 for o in outcomes if o == MigrationState.INCOMPATIBLE)
        deferred = sum(1 for o in outcomes if o == MigrationState.PENDING)

        wave.migrated_count = migrated
        wave.failed_count = failed
        wave.completed_at = datetime.now(timezone.utc)

        # Every station that reached a terminal state leaves `pending`, and
        # lands in exactly one counter. A station that failed on its own was
        # set ROLLED_BACK by _transition_station, so it is counted here --
        # before Day 12 it was not, and a wave in which every station failed
        # individually left rolled_back at 0 and the counts short of
        # total_stations (reported by Track A, TrackA_Dev_Plan R6 §14).
        # Deferred (offline) stations stay pending.
        self._status.migrated += migrated
        self._status.rolled_back += failed
        self._status.incompatible += incompatible
        self._status.pending -= len(station_ids) - deferred

        eligible = len(station_ids) - incompatible - deferred
        wave_failed = eligible > 0 and (failed / eligible) > self._threshold

        if wave_failed:
            wave.phase = WavePhase.ROLLED_BACK
            self._rollback_wave(wave_id)
            self._emit("wave_rolled_back", wave_id=wave_id,
                       migrated=migrated, failed=failed, eligible=eligible,
                       deferred=deferred)
            return False

        wave.phase = WavePhase.COMPLETED
        self._emit("wave_completed", wave_id=wave_id,
                   migrated=migrated, failed=failed, incompatible=incompatible,
                   deferred=deferred)
        return True

    async def _transition_station(self, station_id, wave_id, target_mode) -> MigrationState:
        """
        Move one station from its current identity to PQC-enrolled, with the
        overlap-safety invariant.

        This is the generalized transition point. Enrolment is the instance
        built now; rotation reuses this coroutine with a different install
        message and a key it supplies rather than generates.
        """
        # Capability gate: a station whose firmware cannot do the target
        # algorithm is skipped, not failed. This models the heterogeneous
        # fleet and keeps it out of the failure-threshold denominator.
        supported = self._fleet.supported_algorithms(station_id)
        if target_mode == "pqc" and self._target_algorithm not in supported:
            self._fleet.set_state(station_id, MigrationState.INCOMPATIBLE, wave_id)
            return MigrationState.INCOMPATIBLE

        # Offline gate (opt-in): a station that is not connected cannot be
        # sent a key. Deferred = left PENDING, outside the threshold, and
        # never enrolled -- so there is nothing to undo.
        if self._skip_offline:
            is_connected = getattr(self._fleet, "is_connected", None)
            if callable(is_connected) and not is_connected(station_id):
                self._emit("station_deferred", station=station_id,
                           wave_id=wave_id, reason="not connected")
                return MigrationState.PENDING

        self._fleet.set_state(station_id, MigrationState.IN_PROGRESS, wave_id)

        if self._rotation is not None:
            return await self._rotate_station(station_id, wave_id)

        # Step 1: enrol the new identity FIRST -- overlap window opens.
        private_key, public_key = self._make_keypair()
        self._auth.enrol(station_id, public_key)
        self._enrolled_this_run[wave_id].append(station_id)

        # Step 2: instruct the station to begin using its PQC identity.
        install_msg = self._make_install_msg(station_id, private_key)
        result = await self._dispatch.send(
            station_id, install_msg, timeout_s=self._dispatch_timeout_s
        )
        if not getattr(result, "ok", False):
            return self._fail_station(station_id, wave_id)

        # Step 3 (when wired): the station must PROVE it holds the key by
        # signing a fresh challenge. Without this, "migrated" only means
        # "the station accepted a message".
        if self._make_challenge_msg is not None:
            verified, detail, duration_ms = await self._verify_possession(station_id)
            # Same event shape Track A uses for its identity check, so Track
            # C's E5 measure counts both. station_id travels as `station`
            # because the orchestrator's emitter writes events with no
            # top-level station (a wave is not one station).
            self._emit("connection_attempt", transition="pq_auth",
                       station=station_id, wave_id=wave_id,
                       result="success" if verified else "rejected",
                       detail=detail, duration_ms=duration_ms,
                       algorithm=self._target_algorithm)
            if not verified:
                return self._fail_station(station_id, wave_id)

        # Step 4: confirmed.
        self._fleet.set_state(station_id, MigrationState.MIGRATED, wave_id)
        self._fleet.mark_migrated_algorithm(station_id, self._target_algorithm)
        return MigrationState.MIGRATED

    async def _rotate_station(self, station_id: str, wave_id: int) -> MigrationState:
        """
        Live certificate rotation for one station (plan Phase 5). The overlap
        invariant holds by construction: the old ECDSA certificate stays valid
        and the station keeps it until the new one is confirmed in a real
        handshake, so there is never a moment with no working identity.
        """
        outcome = await self._rotation.rotate(station_id)
        self._emit("connection_attempt", transition="cert_rotation",
                   station=station_id, wave_id=wave_id,
                   result="success" if outcome.ok else "rejected",
                   detail=outcome.detail, step=outcome.step,
                   duration_ms=outcome.duration_ms,
                   algorithm=self._target_algorithm,
                   previous_serial=outcome.previous_serial,
                   new_serial=outcome.new_serial)
        if not outcome.ok:
            self._fleet.set_state(station_id, MigrationState.ROLLED_BACK, wave_id)
            return MigrationState.ROLLED_BACK

        self._enrolled_this_run[wave_id].append(station_id)
        self._new_serials[station_id] = outcome.new_serial
        self._previous_serials[station_id] = outcome.previous_serial
        record = getattr(self._fleet, "record_certificate", None)
        if callable(record):
            record(station_id, outcome.new_serial, outcome.previous_serial)
        self._fleet.set_state(station_id, MigrationState.MIGRATED, wave_id)
        self._fleet.mark_migrated_algorithm(station_id, self._target_algorithm)
        return MigrationState.MIGRATED

    async def _verify_possession(self, station_id: str) -> tuple[bool, str, float | None]:
        """
        Challenge a just-installed station and verify its ML-DSA signature
        against the key enrolled for it in Step 1.

        Returns (verified, detail, round_trip_ms). Never raises: a station
        that answers badly -- refuses, times out, sends garbage, or signs
        with the wrong key -- is a failed station, not a crashed wave.
        """
        nonce = self._auth.issue_challenge(station_id)
        result = await self._dispatch.send(
            station_id, self._make_challenge_msg(nonce),
            timeout_s=self._dispatch_timeout_s,
        )
        duration_ms = getattr(result, "duration_ms", None)

        if not getattr(result, "ok", False):
            return (False,
                    f"challenge not answered (outcome={getattr(result, 'outcome', None)}, "
                    f"status={getattr(result, 'status', None)})",
                    duration_ms)
        try:
            signature = self._parse_signature(getattr(result, "response", None))
        except Exception as exc:  # noqa: BLE001 - malformed answer = failed station
            return False, f"unreadable signature: {type(exc).__name__}: {exc}", duration_ms
        try:
            verified = self._auth.verify_response(station_id, signature)
        except Exception as exc:  # noqa: BLE001 - AuthError (expired / no challenge)
            return False, f"verification refused: {type(exc).__name__}: {exc}", duration_ms

        return (bool(verified),
                "signature verified" if verified else "signature did not verify",
                duration_ms)

    def _fail_station(self, station_id: str, wave_id: int) -> MigrationState:
        """Un-enrol so the station is never left half-migrated."""
        self._unenrol(station_id)
        if station_id in self._enrolled_this_run.get(wave_id, []):
            self._enrolled_this_run[wave_id].remove(station_id)
        self._fleet.set_state(station_id, MigrationState.ROLLED_BACK, wave_id)
        return MigrationState.ROLLED_BACK

    def _rollback_wave(self, wave_id: int) -> None:
        """Un-enrol every station enrolled in a wave and mark it rolled back."""
        for station_id in self._enrolled_this_run.get(wave_id, []):
            self._unenrol(station_id)
            self._fleet.set_state(station_id, MigrationState.ROLLED_BACK, wave_id)
            self._status.rolled_back += 1
            if self._status.migrated > 0:
                self._status.migrated -= 1
        self._enrolled_this_run[wave_id] = []

    def _unenrol(self, station_id: str) -> None:
        """Reverse a station's transition. Rotation: refuse the new
        certificate and disconnect, so the station falls back to its ECDSA
        one. Enrolment: un-enrol the key. PQAuthenticator.unenrol exists from
        Day 12; the fallback stays only for authenticators built before it."""
        if self._rotation is not None:
            self._rotation.revert_soon(station_id, self._new_serials.pop(station_id, None))
            record = getattr(self._fleet, "record_certificate", None)
            if callable(record) and station_id in self._previous_serials:
                record(station_id, self._previous_serials.pop(station_id), None)
            return
        unenrol = getattr(self._auth, "unenrol", None)
        if callable(unenrol):
            unenrol(station_id)
        else:
            enrolled = getattr(self._auth, "_enrolled", None)
            if isinstance(enrolled, dict):
                enrolled.pop(station_id, None)

    def _finish(self, phase: MigrationPhase) -> None:
        self._status.phase = phase
        self._status.completed_at = datetime.now(timezone.utc)
        self._status.current_wave = None
        if phase == MigrationPhase.COMPLETED:
            self._emit("migration_completed",
                       migrated=self._status.migrated,
                       incompatible=self._status.incompatible)