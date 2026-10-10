"""
Boot verifier -- the post-quantum key check after every accepted boot.

Track B. B-P2, added 2026-10-10. Implements Contract 7 section 7.5 (2) and
the boot half of section 7.6.

WHAT IT DOES
In `--mode hybrid`, Track A registers verify_on_boot on the registry's boot
hook (registry.add_boot_listener, A-P1), so it runs after every
BootNotification that was accepted AND answered, on its own asyncio task:

  - station NOT enrolled (no ML-DSA public key on the server): it is still
    classical. No check, no event. Returns True.
  - station enrolled: send PQAuthChallenge naming the enrolled key's key_id,
    verify the ML-DSA signature, write one pq_auth event, and return the
    verdict.

Returns False -- never raises -- for every failure: no answer within the
dispatch timeout, Rejected, an unreadable signature, a signature that does
not verify, or a fault inside the verifier itself (fail closed: a check that
could not be completed is not a pass).

WHAT IT DOES NOT DO (Track A's side, A-P3)
  - close the connection with code 1008 on False, and log connection_closed
    with payload.reason = "pq_auth_failed";
  - mark the station's connection pq_verified on True. Track A's
    orchestrator_emitter(..., on_pq_auth_success=registry.mark_pq_verified)
    already does this from the success event (L34);
  - decide whether to register it at all (only in --mode hybrid).

EVENT (Contract 7 section 7.6)
    connection_attempt  {transition: "pq_auth", station, result, detail,
                         duration_ms, algorithm, key_id, trigger: "boot"}
`source` is NOT in the payload (L31): the emitter Track A builds for the
verifier -- orchestrator_emitter(event_log, source="boot_verifier") -- adds
it. There is no wave_id: a boot check belongs to no wave.

WHY key_id
The charger keeps its current AND previous key (Contract 7 section 7.3) and
signs with the one the challenge names. The verifier names the key the
SERVER holds, so a server that missed the charger's last rotation still
verifies. A charger holding neither key answers Rejected -> False.

idmanager/ never imports agent/ or csms/: the challenge builder, the
signature parser and key_id_for are passed in (in the live CSMS, all three
come from agent/pqc_messages.py).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Protocol

from idmanager.pq_check import CheckResult, challenge_and_verify

LOGGER = logging.getLogger("idmanager.boot_verifier")


class BootAuthenticatorLike(Protocol):
    """What the verifier needs from crypto.pq_auth.PQAuthenticator."""

    @property
    def algorithm(self) -> str: ...
    def is_enrolled(self, station_id: str) -> bool: ...
    def public_key(self, station_id: str) -> bytes | None: ...
    def issue_challenge(self, station_id: str) -> bytes: ...
    def verify_response(self, station_id: str, response_signature: bytes,
                        nonce: bytes | None = None) -> bool: ...


class BootVerifier:
    """
    The key check run after every accepted boot (Contract 7 section 7.5).

    One instance per CSMS process. Built by Track A in A-P3 with the same
    collaborators the orchestrator gets:

        verifier = BootVerifier(
            dispatcher=dispatcher,
            authenticator=setup.authenticator,
            challenge_message_factory=build_challenge_message,
            signature_parser=lambda r: parse_signature(r.data),
            key_id_for=key_id_for,
            event_emitter=orchestrator_emitter(
                event_log, source="boot_verifier",
                on_pq_auth_success=registry.mark_pq_verified),
            dispatch_timeout_s=dispatcher.timeout_s,
        )
        # then, in A-P3, a listener that awaits verifier.verify_on_boot(id)
        # and closes the connection with 1008 when it returns False.
    """

    def __init__(
        self,
        *,
        dispatcher: Any,
        authenticator: BootAuthenticatorLike,
        challenge_message_factory: Callable[..., object],
        signature_parser: Callable[[object], bytes],
        key_id_for: Callable[[bytes], str],
        event_emitter: Callable[..., object] | None = None,
        dispatch_timeout_s: float | None = None,
    ) -> None:
        self._dispatch = dispatcher
        self._auth = authenticator
        self._make_challenge_msg = challenge_message_factory
        self._parse_signature = signature_parser
        self._key_id_for = key_id_for
        self._emit = event_emitter or (lambda *a, **k: None)
        self._dispatch_timeout_s = dispatch_timeout_s

    async def verify_on_boot(self, station_id: str) -> bool:
        """
        Contract 7 section 7.5 (2). True = pass, or not enrolled (no check).
        False = the check failed; Track A closes the connection (1008).
        Never raises.
        """
        try:
            if not self._auth.is_enrolled(station_id):
                return True
            public_key = self._auth.public_key(station_id)
            if public_key is None:
                # Un-enrolled between the two calls (a rollback raced the
                # boot): the station is classical again.
                return True
            key_id = self._key_id_for(public_key)
            algorithm = self._auth.algorithm
        except Exception as exc:  # noqa: BLE001 - fail closed, never raise
            LOGGER.exception("%s: boot check could not start", station_id)
            self._report(station_id, CheckResult(
                False, f"internal error: {type(exc).__name__}: {exc}", None),
                key_id=None, algorithm=None)
            return False

        result = await challenge_and_verify(
            dispatcher=self._dispatch,
            authenticator=self._auth,
            station_id=station_id,
            make_challenge=self._make_challenge_msg,
            parse_signature=self._parse_signature,
            key_id=key_id,
            timeout_s=self._dispatch_timeout_s,
        )
        if not result.verified:
            LOGGER.warning("%s: boot key check failed: %s", station_id, result.detail)
        self._report(station_id, result, key_id=key_id, algorithm=algorithm)
        return result.verified

    def _report(self, station_id: str, result: CheckResult, *,
                key_id: str | None, algorithm: str | None) -> None:
        """Write the pq_auth line. An emitter fault never changes the verdict."""
        try:
            self._emit("connection_attempt", transition="pq_auth",
                       station=station_id,
                       result="success" if result.verified else "rejected",
                       detail=result.detail, duration_ms=result.duration_ms,
                       algorithm=algorithm, key_id=key_id, trigger="boot")
        except Exception:  # noqa: BLE001 - never lose the verdict over a log line
            LOGGER.exception("%s: could not write the boot pq_auth event", station_id)