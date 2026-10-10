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

ENROLMENT BY PUBLIC KEY (Contract 7 section 7.4, B-P1, added 2026-10-09):
Built with enrolment_request_factory and public_key_parser (always given
together), the orchestrator no longer makes any key. It sends each station
a RequestPQEnrolment; the station makes its own ML-DSA key pair, keeps the
private half, and answers with {algorithm, public_key, key_id}. Only then is
the public key enrolled -- the server cannot enrol a key it does not have --
and the station is challenged with that key_id. The overlap guarantee is
unchanged: the station only USES the key when challenged, and the challenge
is sent only after enrolment. In this mode keypair_factory and
install_message_factory are not used and may be omitted, and proof of
possession (challenge_message_factory + signature_parser) is required: a
public key alone proves nothing about who holds the private half. Without
the two new arguments the old InstallPQAuth flow runs exactly as before,
until it is removed after the A+B+C re-test (Contract 7 section 7.8, B-P5).

OFFLINE STATIONS (added Day 12):
By default a station that is not connected fails its install and counts
against the wave's failure threshold. Built with skip_offline=True (and a
fleet that answers is_connected), an offline station is DEFERRED instead:
left PENDING, excluded from the threshold, and migratable by a later run.

WHAT IT DOES NOT DO:
It never writes per-command dispatch events -- csms/dispatch.py already
emits MESSAGE_SENT with dispatched=True for every command it sends
(Contract 3 belongs to Track A's CSMS). The orchestrator emits only the
migration-level events (MIGRATION_STARTED, WAVE_STARTED, WAVE_COMPLETED,
WAVE_ROLLED_BACK, MIGRATION_COMPLETED), plus one CONNECTION_ATTEMPT
(transition="pq_auth", trigger="migration") per checked station when proof
of possession is wired, one CERTIFICATE_INSTALLED (transition="pq_enrolled")
per station whose own public key was enrolled (Contract 7 section 7.6), and
"station_deferred" when skip_offline defers a station, through the
event_emitter Track A supplies. The orchestrator never puts "source" in a
payload: Track A's emitter (csms/migration.py orchestrator_emitter) already
writes source="orchestrator", and a second "source" keyword would raise
TypeError inside EventLog.emit. "station_deferred" and
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
from idmanager.pq_check import challenge_and_verify
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
    def verify_response(self, station_id: str, response_signature: bytes,
                        nonce: bytes | None = None) -> bool: ...


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

# Contract 7 section 7.4. Builds the RequestPQEnrolment call for a station.
# In the live CSMS: agent.pqc_messages.build_enrolment_request, which takes
# (station_id, *, algorithm=...). The orchestrator calls it as
# factory(station_id, algorithm=target_algorithm).
EnrolmentRequestFactory = Callable[..., object]

# Contract 7 section 7.4. Reads the station's enrolment answer out of the
# dispatcher's response object and returns (algorithm, public_key, key_id).
# Must raise on anything malformed -- a private key in the reply, a missing
# field, a key_id that does not match the key. In the live CSMS:
#     lambda response: agent.pqc_messages.parse_enrolment_reply(response.data)
PublicKeyParser = Callable[[object], tuple[str, bytes, str]]


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
        keypair_factory: KeypairFactory | None = None,
        install_message_factory: InstallMessageFactory | None = None,
        event_emitter: Callable[..., object] | None = None,
        failure_threshold: float = DEFAULT_FAILURE_THRESHOLD,
        dispatch_timeout_s: float | None = None,
        target_algorithm: str = "ML-DSA-44",
        challenge_message_factory: ChallengeMessageFactory | None = None,
        signature_parser: SignatureParser | None = None,
        skip_offline: bool = False,
        enrolment_request_factory: EnrolmentRequestFactory | None = None,
        public_key_parser: PublicKeyParser | None = None,
    ) -> None:
        if (challenge_message_factory is None) != (signature_parser is None):
            raise ValueError(
                "challenge_message_factory and signature_parser must be given "
                "together: one without the other cannot verify a station"
            )
        if (enrolment_request_factory is None) != (public_key_parser is None):
            raise ValueError(
                "enrolment_request_factory and public_key_parser must be given "
                "together (Contract 7 section 7.4): a request whose answer "
                "cannot be read enrols nothing"
            )
        if enrolment_request_factory is not None:
            if challenge_message_factory is None:
                raise ValueError(
                    "enrolment by public key needs challenge_message_factory "
                    "and signature_parser: a public key alone does not prove "
                    "the station holds the private half"
                )
        elif keypair_factory is None or install_message_factory is None:
            raise ValueError(
                "keypair_factory and install_message_factory are required "
                "unless enrolment_request_factory and public_key_parser are "
                "given (Contract 7 section 7.4)"
            )
        self._make_enrolment_req = enrolment_request_factory
        self._parse_public_key = public_key_parser
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

        self._emit("migration_started", migration_id=migration_id,
                   target_mode=target_mode, total=len(candidates),
                   wave_size=wave_size, canary_count=canary_count)

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

        if self._make_enrolment_req is not None:
            return await self._enrol_by_public_key(station_id, wave_id)

        # --- Deprecated InstallPQAuth flow (server makes the key). Kept
        # --- until the A+B+C re-test; removed in B-P5 (Contract 7 section 7.8).

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
            verified = await self._check_and_report(station_id, wave_id, key_id=None)
            if not verified:
                return self._fail_station(station_id, wave_id)

        # Step 4: confirmed.
        return self._mark_migrated(station_id, wave_id)

    async def _enrol_by_public_key(self, station_id: str, wave_id: int) -> MigrationState:
        """
        Contract 7 section 7.4: the station makes its own key pair.

            send RequestPQEnrolment -> station answers {algorithm, public_key, key_id}
            -> enrol(station_id, public_key) -> PQAuthChallenge {nonce, key_id}
            -> verify -> MIGRATED; any failure: un-enrol, ROLLED_BACK

        Never raises for a station's bad answer: a refusal, a timeout, an
        unreadable reply, a reply carrying a private key, a mismatched
        key_id or the wrong algorithm all make a failed station, never a
        crashed wave. Nothing is enrolled until the reply has been read
        and checked, so a failure before enrolment has nothing to undo
        (_fail_station's un-enrol is then a harmless no-op).
        """
        # Step 1: ask the station to make its key pair.
        request = self._make_enrolment_req(station_id, algorithm=self._target_algorithm)
        result = await self._dispatch.send(
            station_id, request, timeout_s=self._dispatch_timeout_s
        )
        if not getattr(result, "ok", False):
            LOGGER.warning(
                "%s: RequestPQEnrolment not accepted (outcome=%s, status=%s)",
                station_id, getattr(result, "outcome", None),
                getattr(result, "status", None),
            )
            return self._fail_station(station_id, wave_id)

        # Step 2: read and check the answer. The parser raises on anything
        # malformed, including a private key in the reply (L04) and a
        # key_id that does not match the public key.
        try:
            algorithm, public_key, key_id = self._parse_public_key(
                getattr(result, "response", None)
            )
        except Exception as exc:  # noqa: BLE001 - malformed answer = failed station
            LOGGER.warning("%s: unreadable enrolment reply: %s: %s",
                           station_id, type(exc).__name__, exc)
            return self._fail_station(station_id, wave_id)
        if algorithm != self._target_algorithm:
            LOGGER.warning("%s: enrolment reply is %r, expected %r",
                           station_id, algorithm, self._target_algorithm)
            return self._fail_station(station_id, wave_id)

        # Step 3: enrol the station's own public key -- overlap window opens.
        self._auth.enrol(station_id, public_key)
        self._enrolled_this_run[wave_id].append(station_id)
        self._emit("certificate_installed", transition="pq_enrolled",
                   station=station_id, key_id=key_id, algorithm=algorithm,
                   trigger="migration", wave_id=wave_id)

        # Step 4: the station must prove it holds the private half, signing
        # with the key it just reported (named by key_id).
        verified = await self._check_and_report(station_id, wave_id, key_id=key_id)
        if not verified:
            return self._fail_station(station_id, wave_id)

        # Step 5: confirmed.
        return self._mark_migrated(station_id, wave_id)

    async def _check_and_report(self, station_id: str, wave_id: int, *,
                                key_id: str | None) -> bool:
        """
        Run one migration key check and write its pq_auth line.

        Contract 7 section 7.6 shape: the same event Track A uses for its
        identity check, so Track C's E5 measure counts both. station_id
        travels as `station` because the orchestrator's emitter writes
        events with no top-level station (a wave is not one station).
        key_id is None on the deprecated InstallPQAuth path. "source" is
        added by Track A's emitter, not here (see the module docstring).
        """
        verified, detail, duration_ms = await self._verify_possession(
            station_id, key_id=key_id
        )
        self._emit("connection_attempt", transition="pq_auth",
                   station=station_id, wave_id=wave_id,
                   result="success" if verified else "rejected",
                   detail=detail, duration_ms=duration_ms,
                   algorithm=self._target_algorithm,
                   key_id=key_id, trigger="migration")
        return verified

    def _mark_migrated(self, station_id: str, wave_id: int) -> MigrationState:
        self._fleet.set_state(station_id, MigrationState.MIGRATED, wave_id)
        self._fleet.mark_migrated_algorithm(station_id, self._target_algorithm)
        return MigrationState.MIGRATED

    async def _verify_possession(
        self, station_id: str, *, key_id: str | None = None
    ) -> tuple[bool, str, float | None]:
        """
        Challenge a station and verify its ML-DSA signature against the key
        enrolled for it.

        key_id (Contract 7 section 7.2) names which of the station's keys
        must sign. It is passed to the challenge factory only when known,
        so a factory written as `lambda nonce: ...` keeps working on the
        deprecated path.

        Returns (verified, detail, round_trip_ms). Never raises: a station
        that answers badly -- refuses, times out, sends garbage, or signs
        with the wrong key -- is a failed station, not a crashed wave.

        Since B-P2 the check itself lives in idmanager/pq_check.py, shared
        with the boot verifier, so both checks behave identically.
        """
        result = await challenge_and_verify(
            dispatcher=self._dispatch,
            authenticator=self._auth,
            station_id=station_id,
            make_challenge=self._make_challenge_msg,
            parse_signature=self._parse_signature,
            key_id=key_id,
            timeout_s=self._dispatch_timeout_s,
        )
        return result.verified, result.detail, result.duration_ms

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
        """Reverse an enrolment. PQAuthenticator.unenrol exists from Day 12;
        the fallback stays only for authenticators built before it."""
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