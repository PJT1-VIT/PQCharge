"""
The station's post-quantum identity — its ML-DSA key, and how it signs.

Track C (agent). Phase C8 — Track B integration.

--------------------------------------------------------------------
WHERE THIS FITS

    crypto/pq.py            Track B — PQProvider (ML-DSA-44 via quantcrypt)
    crypto/pq_auth.py       Track B — sign_challenge(), the shared signer
          |
    agent/pq_identity.py <- you are here. Holds this station's ML-DSA
          |                 private key and signs challenges with it.
    agent/station.py        owns ONE of these, per station, across
                            connections and across a migration.

--------------------------------------------------------------------
WHAT "MIGRATED" MEANS HERE

Under Option B, a station is "migrated to post-quantum" once it holds an
ML-DSA private key whose public half the CSMS has enrolled. This object
is that private key plus the one operation the station performs with it:
sign a challenge. Everything else about the station -- its TLS, its
X.509 client certificate, its OCPP messages -- is unchanged.

`is_migrated` is simply "do I hold a key yet." Before the orchestrator's
InstallPQAuth arrives it is False; after, True. It survives reconnection
because it lives on the station, not the connection -- a station that
was migrated before an E2 outage is still migrated after it.

--------------------------------------------------------------------
*** quantcrypt IS IMPORTED LAZILY, ON FIRST SIGN, NEVER AT IMPORT ***

Most stations in a run are classical and never receive a key. If this
module imported crypto/pq (and through it quantcrypt) at the top, every
one of five hundred agents would load the post-quantum backend just to
not use it -- and a teammate without the wheel installed could not
import the agent at all. So the provider is created on the first signing
call and cached. A classical station never touches quantcrypt.

This mirrors Track B's own discipline: crypto/pq.py defers its quantcrypt
import into __init__ for exactly this reason.
"""

from __future__ import annotations

from typing import Any, Callable

from agent.logging_setup import get_logger


def _default_provider_factory() -> Any:
    """
    Build a real PQProvider. Imported here, not at module top, so the
    cost and the dependency land only when a station actually signs.
    """
    from crypto.pq import PQProvider

    return PQProvider()


class PQIdentity:
    """
    One station's ML-DSA identity. STATION-scoped, like the power backend
    and the state machine: it outlives any single connection, because a
    migration that happened on one connection must still be in force on
    the next.

    Holds no asyncio and does no I/O, so it unit-tests with a fake
    provider and no event loop -- and a fake provider is also how a
    non-Windows machine without the quantcrypt wheel exercises the
    agent's install/challenge plumbing.
    """

    def __init__(
        self,
        station_id: str = "",
        *,
        provider: Any = None,
        provider_factory: Callable[[], Any] | None = None,
    ) -> None:
        """
        Args:
            station_id: for the log prefix only.
            provider: an already-built provider. Injected by tests; a real
                station leaves it None and gets one lazily.
            provider_factory: overrides how the lazy provider is built.
                Defaults to constructing crypto.pq.PQProvider on first use.
        """
        self.station_id = station_id
        self.log = get_logger(__name__, station_id=station_id)

        self._provider = provider
        self._provider_factory = provider_factory or _default_provider_factory

        self._private_key: bytes | None = None
        self._algorithm: str | None = None

        self.challenges_signed = 0
        """For the end-of-run summary and the latency measurement Track B
        asked for (their §8.6-b): how many challenges this station
        answered."""

    # -- reading -----------------------------------------------------------

    @property
    def is_migrated(self) -> bool:
        """Whether a post-quantum key has been installed on this station."""
        return self._private_key is not None

    @property
    def algorithm(self) -> str | None:
        return self._algorithm

    # -- the orchestrator installs a key -----------------------------------

    def install(self, private_key: bytes, algorithm: str) -> None:
        """
        Store the ML-DSA private key the orchestrator sent.

        This is the whole of what InstallPQAuth does on the station side.
        It touches no physical state -- no contactor, no meter, no state
        machine -- so it is safe to call in the middle of a charging
        transaction, which is exactly when a fleet migration will find
        most stations. That transaction-safety is not an accident to be
        preserved carefully; it is inherent, because installing a key is
        just storing bytes.

        Re-installing replaces the key. That is rotation: Track B's
        orchestrator rotates a station by enrolling a fresh key and
        sending a new InstallPQAuth, and the station simply holds the
        newest one. The station id (its identity) never changes -- only
        the key does -- which is the impersonation invariant from Track
        A's finding (a rotation changes the key, never the CN).
        """
        if not private_key:
            raise ValueError("refusing to install an empty private key")

        rotating = self._private_key is not None
        self._private_key = bytes(private_key)
        self._algorithm = algorithm

        self.log.info(
            "%s ML-DSA key installed (%s, %d bytes) -- station is now migrated",
            "rotated" if rotating else "first",
            algorithm,
            len(self._private_key),
        )

    # -- the server challenges ---------------------------------------------

    def answer_challenge(self, nonce: bytes) -> bytes:
        """
        Sign the server's nonce and return the ML-DSA signature.

        Synchronous by design. Signing is a CPU operation with no network
        in it, so unlike the actuation commands (which defer their reply
        to the metering loop to avoid the recv-loop deadlock), the
        challenge response IS produced here and returned directly in the
        DataTransfer reply. There is no outbound call to deadlock on.

        What gets signed is the nonce bytes, exactly as received.
        Confirmed against crypto/pq_auth.py:

            issue_challenge(station_id) -> bytes            # the nonce
            sign_challenge(provider, private_key, nonce)    # signs the nonce
            verify_response(station_id, signature)          # verify over nonce

        `sign_challenge`'s third argument is the nonce bytes, not a
        Challenge object -- the server keeps the Challenge record itself
        and verifies against its stored nonce. So there is nothing to
        reconstruct: the station signs the exact bytes it was handed, and
        the signature verifies as long as those bytes match. Using
        Track B's `sign_challenge` rather than calling `provider.sign`
        directly keeps the two sides sharing one definition of what is
        signed, so a future change on their side reaches the station.
        """
        if self._private_key is None:
            raise RuntimeError("cannot sign: this station holds no PQC key")

        from crypto.pq_auth import sign_challenge

        signature = sign_challenge(
            self._provider_or_build(), self._private_key, nonce
        )

        self.challenges_signed += 1
        self.log.info(
            "signed PQC challenge #%d (%d-byte signature)",
            self.challenges_signed, len(signature),
        )
        return signature

    # -- provider plumbing --------------------------------------------------

    def _provider_or_build(self) -> Any:
        if self._provider is None:
            self._provider = self._provider_factory()
        return self._provider

    def describe(self) -> str:
        """One line for the end-of-run summary."""
        if not self.is_migrated:
            return "pqc: classical (no key installed)"
        return (
            f"pqc: migrated ({self._algorithm}), "
            f"{self.challenges_signed} challenge(s) signed"
        )