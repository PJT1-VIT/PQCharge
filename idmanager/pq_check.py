"""
One post-quantum key check: challenge a station, verify its ML-DSA answer.

Track B. Added 2026-10-10 (B-P2). Shared by the two places that check a
station's key, so both use exactly the same logic:

  - idmanager/orchestrator.py  the migration check (trigger "migration"),
                               straight after a station's key is enrolled;
  - idmanager/boot_verifier.py the boot check (trigger "boot"), after every
                               accepted BootNotification (Contract 7 7.5).

The check:
    nonce = authenticator.issue_challenge(station_id)
    send PQAuthChallenge {nonce, key_id}  -> station signs with that key
    signature = signature_parser(response)
    authenticator.verify_response(station_id, signature, nonce=nonce)

The nonce is passed back to verify_response so that two checks for the same
station that overlap in time each verify against their OWN challenge
(crypto/pq_auth.py keeps several outstanding per station; L35).

It never raises for anything a station can do -- refuse, time out, send
garbage, sign with the wrong key -- nor for a dispatcher fault: each is a
failed check with a readable `detail`. asyncio.CancelledError still
propagates (it is not an Exception), so a cancelled task stays cancelled.

idmanager/ never imports agent/ or csms/: the message builder, the signature
parser and the dispatcher are all passed in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one key check."""

    verified: bool
    detail: str
    """Human-readable reason, written into the pq_auth event."""
    duration_ms: float | None
    """Round trip of the challenge as the dispatcher measured it, or None."""


async def challenge_and_verify(
    *,
    dispatcher: Any,
    authenticator: Any,
    station_id: str,
    make_challenge: Callable[..., object],
    parse_signature: Callable[[object], bytes],
    key_id: str | None = None,
    timeout_s: float | None = None,
    staged: bool = False,
) -> CheckResult:
    """
    Run one challenge-response check against the station's enrolled key.

    key_id (Contract 7 section 7.2) names which of the station's keys must
    sign. It is passed to make_challenge only when known, so a factory
    written as `lambda nonce: ...` keeps working on the deprecated
    InstallPQAuth path.
    """
    try:
        nonce = authenticator.issue_challenge(station_id)
        if key_id is None:
            challenge = make_challenge(nonce)
        else:
            challenge = make_challenge(nonce, key_id=key_id)
    except Exception as exc:  # noqa: BLE001 - a fault here = failed check
        return CheckResult(False, f"challenge not built: {type(exc).__name__}: {exc}", None)

    try:
        result = await dispatcher.send(station_id, challenge, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 - dispatcher fault = failed check
        return CheckResult(False, f"challenge not sent: {type(exc).__name__}: {exc}", None)

    duration_ms = getattr(result, "duration_ms", None)

    if not getattr(result, "ok", False):
        return CheckResult(
            False,
            f"challenge not answered (outcome={getattr(result, 'outcome', None)}, "
            f"status={getattr(result, 'status', None)})",
            duration_ms,
        )
    try:
        signature = parse_signature(getattr(result, "response", None))
    except Exception as exc:  # noqa: BLE001 - malformed answer = failed check
        return CheckResult(False, f"unreadable signature: {type(exc).__name__}: {exc}", duration_ms)
    try:
        if staged:
            # B-F2 rotation: verify against the NEW key held next to the old one.
            verified = authenticator.verify_response(station_id, signature, nonce=nonce, staged=True)
        else:
            verified = authenticator.verify_response(station_id, signature, nonce=nonce)
    except Exception as exc:  # noqa: BLE001 - AuthError (expired / unenrolled)
        return CheckResult(False, f"verification refused: {type(exc).__name__}: {exc}", duration_ms)

    return CheckResult(
        bool(verified),
        "signature verified" if verified else "signature did not verify",
        duration_ms,
    )