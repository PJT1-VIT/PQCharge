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
    instance per CSMS process. Not thread-safe in itself; the CSMS serialises
    access per connection.
    """

    def __init__(
        self,
        provider: CryptoProvider,
        challenge_ttl_s: float = DEFAULT_CHALLENGE_TTL_S,
    ) -> None:
        self._provider = provider
        self._ttl_s = challenge_ttl_s
        self._enrolled: dict[str, bytes] = {}
        self._outstanding: dict[str, Challenge] = {}

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

    def is_enrolled(self, station_id: str) -> bool:
        return station_id in self._enrolled

    def issue_challenge(self, station_id: str) -> bytes:
        """
        Produce a fresh challenge for a station and remember it as outstanding.

        A previously outstanding challenge for the same station is replaced --
        only the most recent challenge is ever accepted, so a station cannot
        bank several and an old one cannot linger.
        """
        challenge = Challenge(
            station_id=station_id,
            nonce=secrets.token_bytes(CHALLENGE_BYTES),
            issued_at=time.monotonic(),
        )
        self._outstanding[station_id] = challenge
        return challenge.nonce

    def verify_response(
        self, station_id: str, response_signature: bytes
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
        """
        if station_id not in self._enrolled:
            raise AuthError(f"station {station_id!r} is not enrolled")

        challenge = self._outstanding.get(station_id)
        if challenge is None:
            raise AuthError(f"no outstanding challenge for station {station_id!r}")

        # Consume the challenge now, before verifying, so it is single-use on
        # every path out of this method.
        del self._outstanding[station_id]

        if time.monotonic() - challenge.issued_at > self._ttl_s:
            raise AuthError(f"challenge for station {station_id!r} has expired")

        return self._provider.verify(
            self._enrolled[station_id], challenge.nonce, response_signature
        )


def sign_challenge(
    provider: CryptoProvider, private_key: bytes, challenge: bytes
) -> bytes:
    """
    Station-side helper: sign a received challenge with the station's private
    key. Kept as a free function because the station side holds no state -- it
    receives a challenge and returns a signature.
    """
    return provider.sign(private_key, challenge)