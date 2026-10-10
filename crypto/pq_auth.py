"""
Application-layer post-quantum authentication (Option B).

The chosen post-quantum path for PQCharge. Rather than putting an ML-DSA
signature inside an X.509 certificate -- which cryptography's CertificateBuilder
cannot do (no ML-DSA key type) and which would break E6 interoperability by
producing certificates no standard TLS stack can parse -- post-quantum identity
is proven at the application layer, in OCPP messages, over an ordinary TLS
channel.

The mechanism is challenge-response:
  1. The CSMS issues a fresh random challenge (nonce) to a connecting station.
  2. The station signs the challenge with its ML-DSA private key.
  3. The CSMS verifies the signature against the station's enrolled ML-DSA
     public key.

This directly defeats the impersonation finding Track A recorded (a station
presenting another station's valid certificate): possessing a certificate is not
possessing the private key, and only the private-key holder can sign the
challenge. It is also what makes E5 concrete -- the migrated fleet rejects an
adversary who cannot produce an ML-DSA signature.

Why the challenge must be fresh per attempt: a fixed challenge would let an
adversary who once observed a valid response replay it forever. A random 32-byte
nonce, issued per authentication and never reused, makes each response
single-use.

This module is backend-agnostic: it takes any CryptoProvider, so the same
authentication works in classical, hybrid or pqc mode. The provider decides
which algorithm signs; this module decides the protocol around it.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from crypto.provider import CryptoProvider

CHALLENGE_BYTES = 32
"""Nonce length. 32 bytes = 256 bits of entropy, far beyond any birthday-bound
concern for the number of authentications in a fleet's lifetime."""

MAX_OUTSTANDING_PER_STATION = 8
"""How many challenges one station may have outstanding at once (B-P2, L35).
More than one is needed because two checks can overlap for the same station
-- a boot check (Contract 7 section 7.5) while its migration check is still
in flight, e.g. a charger that reboots during its wave. 8 is far above any
real overlap; the cap only stops unbounded growth. When full, the OLDEST
outstanding challenge is dropped."""

DEFAULT_CHALLENGE_TTL_S = 30.0
"""How long an issued challenge remains valid. A station that cannot respond
within this window must request a new challenge. Bounds how long a captured
challenge is useful and stops unbounded growth of outstanding challenges."""


class AuthError(Exception):
    """Raised for protocol misuse (unknown or expired challenge). NOT raised for
    a merely invalid signature -- that returns a False verdict, because it is an
    authentication decision, not an error."""


@dataclass(frozen=True)
class Challenge:
    """One issued authentication challenge."""

    station_id: str
    nonce: bytes
    issued_at: float
    """Monotonic timestamp of issuance, for TTL expiry."""


class PQAuthenticator:
    """
    Server-side post-quantum authentication for the CSMS.

    Holds outstanding challenges and each station's enrolled public key. One
    instance per CSMS process. Not thread-safe in itself; everything runs on
    the CSMS's single asyncio event loop.

    OVERLAPPING CHECKS (B-P2, added 2026-10-10, L35):
    A station may have several challenges outstanding, each identified by
    its nonce, so a boot check and a migration check for the same station do
    not cancel each other. A caller that passes the nonce to verify_response
    verifies exactly that challenge. A caller that does not pass it gets the
    original behaviour: only the MOST RECENT challenge is accepted, and every
    older one for that station is discarded.
    """

    def __init__(
        self,
        provider: CryptoProvider,
        challenge_ttl_s: float = DEFAULT_CHALLENGE_TTL_S,
    ) -> None:
        self._provider = provider
        self._ttl_s = challenge_ttl_s
        self._enrolled: dict[str, bytes] = {}
        self._staged: dict[str, bytes] = {}
        """station_id -> a NEW public key being rotated in (B-F2, L26). The
        enrolled (old) key stays the station's identity until commit."""
        self._outstanding: dict[str, dict[bytes, Challenge]] = {}
        """station_id -> {nonce: Challenge}, oldest first (dicts keep
        insertion order)."""

    @property
    def algorithm(self) -> str:
        """The signature algorithm in force, for logging and results."""
        return self._provider.signature_algorithm

    def enrol(self, station_id: str, public_key: bytes) -> None:
        """
        Register a station's public key. In deployment this happens when the
        station's certificate is issued; here it is the CSMS learning which key
        to expect from which station.
        """
        self._enrolled[station_id] = public_key

    def unenrol(self, station_id: str) -> None:
        """
        Forget a station's public key and any challenge outstanding for it.

        Used by the migration orchestrator to undo an enrolment (a failed
        install, a rolled-back wave). Dropping the outstanding challenge as
        well matters: a response arriving after the un-enrolment must not
        find a live nonce waiting for it. Unknown station ids are ignored,
        so a rollback can call this without first checking.
        """
        self._enrolled.pop(station_id, None)
        self._outstanding.pop(station_id, None)
        self._staged.pop(station_id, None)

    # -- key rotation with an overlap window (B-F2, M5, L26) -----------------
    #
    #   stage_key(new)  -> the old key is STILL the station's identity: boot
    #                      checks and everything else verify against it;
    #   verify_response(..., staged=True) checks a challenge against the new key;
    #   commit_staged() -> the new key replaces the old one (via enrol(), so
    #                      Track A's PersistentPQAuthenticator saves it);
    #   discard_staged() -> the new key is forgotten, the old one is untouched.
    #
    # So at no moment is the station left without a working identity: that
    # is the overlap window the design document's rotation claim needs.

    def stage_key(self, station_id: str, new_public_key: bytes) -> None:
        """Hold a new key next to the enrolled one. Station must be enrolled."""
        if station_id not in self._enrolled:
            raise AuthError(f"station {station_id!r} is not enrolled; nothing to rotate")
        self._staged[station_id] = bytes(new_public_key)

    def staged_key(self, station_id: str) -> bytes | None:
        return self._staged.get(station_id)

    def commit_staged(self, station_id: str) -> bytes:
        """Make the staged key the station's key. Returns the OLD key."""
        new = self._staged.pop(station_id, None)
        if new is None:
            raise AuthError(f"no staged key for station {station_id!r}")
        old = self._enrolled[station_id]
        self.enrol(station_id, new)
        return old

    def discard_staged(self, station_id: str) -> None:
        """Forget the staged key; the enrolled key is untouched."""
        self._staged.pop(station_id, None)

    def public_key(self, station_id: str) -> bytes | None:
        """
        The station's enrolled public key, or None if it is not enrolled.

        Read-only. Added for B-P2: the boot verifier derives the key_id it
        names in the challenge (Contract 7 section 7.2) from this key, so
        the station signs with the key the server actually holds -- its
        current one, or its previous one if the server missed a rotation.
        """
        return self._enrolled.get(station_id)

    def is_enrolled(self, station_id: str) -> bool:
        return station_id in self._enrolled

    def issue_challenge(self, station_id: str) -> bytes:
        """
        Produce a fresh challenge for a station and remember it as outstanding.

        Earlier challenges for the same station stay outstanding (until they
        are verified, expire, or are pushed out by the per-station cap), so
        two overlapping checks each verify against their own nonce. Expired
        challenges are dropped here, so nothing lingers past its TTL. See
        the class docstring for callers that do not pass the nonce back.
        """
        now = time.monotonic()
        pending = self._outstanding.setdefault(station_id, {})
        for old_nonce in [n for n, c in pending.items()
                          if now - c.issued_at > self._ttl_s]:
            del pending[old_nonce]
        while len(pending) >= MAX_OUTSTANDING_PER_STATION:
            del pending[next(iter(pending))]

        challenge = Challenge(
            station_id=station_id,
            nonce=secrets.token_bytes(CHALLENGE_BYTES),
            issued_at=now,
        )
        pending[challenge.nonce] = challenge
        return challenge.nonce

    def verify_response(
        self,
        station_id: str,
        response_signature: bytes,
        nonce: bytes | None = None,
        *,
        staged: bool = False,
    ) -> bool:
        """
        Check a station's signed response to its outstanding challenge.

        Returns True only if: the station is enrolled, has an outstanding
        challenge that has not expired, and the signature verifies against its
        enrolled public key. Returns False for a bad signature. Raises AuthError
        only for protocol misuse -- no challenge outstanding, or it expired --
        because those are distinct from "the signature was wrong" and the CSMS
        handles them differently (reissue vs reject).

        The challenge is consumed on any verdict: a nonce is single-use whether
        the response was valid or not, so a failed attempt cannot be retried
        against the same nonce.

        nonce: which outstanding challenge this response answers (B-P2). Pass
        it whenever two checks may overlap for one station; only that
        challenge is consumed. Omitted: the most recent challenge is used and
        all older ones for the station are discarded (the original
        behaviour, kept for existing callers).
        """
        if station_id not in self._enrolled:
            raise AuthError(f"station {station_id!r} is not enrolled")

        pending = self._outstanding.get(station_id) or {}
        if nonce is None:
            challenge = pending[next(reversed(pending))] if pending else None
            # Consume it -- and, as before B-P2, every older one.
            self._outstanding.pop(station_id, None)
        else:
            # Consume only this challenge, before verifying, so it is
            # single-use on every path out of this method.
            challenge = pending.pop(bytes(nonce), None)
            if not pending:
                self._outstanding.pop(station_id, None)

        if challenge is None:
            raise AuthError(f"no outstanding challenge for station {station_id!r}")

        if time.monotonic() - challenge.issued_at > self._ttl_s:
            raise AuthError(f"challenge for station {station_id!r} has expired")

        key = self._enrolled[station_id]
        if staged:
            # B-F2: a rotation check is against the NEW (staged) key.
            key = self._staged.get(station_id)
            if key is None:
                raise AuthError(f"no staged key for station {station_id!r}")
        return self._provider.verify(key, challenge.nonce, response_signature)


def sign_challenge(
    provider: CryptoProvider, private_key: bytes, challenge: bytes
) -> bytes:
    """
    Station-side helper: sign a received challenge with the station's private
    key. Kept as a free function because the station side holds no state -- it
    receives a challenge and returns a signature.
    """
    return provider.sign(private_key, challenge)