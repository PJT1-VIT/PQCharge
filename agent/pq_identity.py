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

`is_migrated` is simply "do I hold a key yet." It survives reconnection
because it lives on the station, not the connection -- a station that
was migrated before an E2 outage is still migrated after it.

CONTRACT 7 (C-P2): THE STATION MAKES ITS OWN KEY

    enrol(algorithm)          RequestPQEnrolment: generate a key pair,
                              save it to the key file (agent/pq_keystore.py)
                              BEFORE returning, keep the old key as
                              "previous", return (public_key, key_id).
                              The private key never leaves this object
                              except into the key file.
    answer_challenge(nonce, key_id=None)
                              sign with the key the server names (current
                              or previous); no key_id = current.
    load()                    at station start: read the key file, if any,
                              and build the provider once -- so the first
                              challenge does not pay the ~300 ms library
                              load (L10). A station with no key file stays
                              lazy and never imports quantcrypt.
    install(private_key, alg) DEPRECATED InstallPQAuth path (Contract 7
                              section 7.8). Unchanged: memory only, no
                              key_id (the public key is unknown here).

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
from agent.pq_keystore import KeyRecord, PQKeyStore, StoredKey, now_utc


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
        key_store: PQKeyStore | None = None,
        supported_algorithms: list[str] | None = None,
    ) -> None:
        """
        Args:
            station_id: for the log prefix only.
            provider: an already-built provider. Injected by tests; a real
                station leaves it None and gets one lazily.
            provider_factory: overrides how the lazy provider is built.
                Defaults to constructing crypto.pq.PQProvider on first use.
            key_store: where enrolled keys are kept (Contract 7 section
                7.3). None = memory only (tests, and InstallPQAuth).
            supported_algorithms: the station's capability list
                (AgentConfig.supported_algorithms). If non-empty, an
                enrolment for an algorithm not on it is refused -- a
                legacy charger. Empty = whatever the provider signs with.
        """
        self.station_id = station_id
        self.log = get_logger(__name__, station_id=station_id)

        self._provider = provider
        self._provider_factory = provider_factory or _default_provider_factory

        self._private_key: bytes | None = None
        self._algorithm: str | None = None
        self._key_store = key_store
        self._supported = list(supported_algorithms or [])
        self._current: StoredKey | None = None
        self._previous: StoredKey | None = None
        """Contract 7 keys, made on this station. When set, _private_key
        mirrors _current.private_key so the deprecated path and the new
        one share is_migrated and answer_challenge."""

        self.installs = 0
        """How many keys this station has been given (1 = migrated once;
        more = rotated). Phase C6.1: reported in the tester diary so the
        analysis can compare the station's own view with the server's."""

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

    @property
    def key_id(self) -> str | None:
        """key_id of the current key; None if there is none, or if it came
        through the deprecated InstallPQAuth (public key unknown)."""
        return self._current.key_id if self._current is not None else None

    @property
    def previous_key_id(self) -> str | None:
        return self._previous.key_id if self._previous is not None else None

    @property
    def public_key(self) -> bytes | None:
        return self._current.public_key if self._current is not None else None

    # -- Contract 7: load at start, enrol on request -------------------------

    def load(self) -> bool:
        """
        Read this station's key file, if a key store is configured and a
        file exists. Returns True when a key was loaded.

        A loaded key means this station will sign challenges, so the
        provider is built NOW, at start, not on the first challenge (L10).
        """
        if self._key_store is None:
            return False
        record = self._key_store.load()
        if record is None:
            return False
        self._algorithm = record.algorithm
        self._current = record.current
        self._previous = record.previous
        self._private_key = record.current.private_key
        self._provider_or_build()
        self.log.info(
            "post-quantum key loaded (%s, key_id %s%s)",
            record.algorithm, record.current.key_id,
            f", previous {record.previous.key_id}" if record.previous else "",
        )
        return True

    def supports(self, algorithm: str) -> bool:
        """Whether this station will make a key for `algorithm`."""
        if self._supported and algorithm not in self._supported:
            return False
        return algorithm == getattr(self._provider_or_build(), "signature_algorithm", None)

    def enrol(self, algorithm: str) -> tuple[bytes, str]:
        """
        RequestPQEnrolment (Contract 7 section 7.2): make a NEW key pair,
        save it, and return (public_key, key_id). The private key stays here.

        Raises ValueError for an algorithm this station does not support;
        the handler answers Rejected. If saving fails the error propagates
        and NOTHING changes in memory either -- the station must never
        answer with a public key whose private half is not safely stored.
        """
        from agent.pqc_messages import key_id_for

        if not self.supports(algorithm):
            raise ValueError(f"this station does not support {algorithm!r}")

        private_key, public_key = self._provider_or_build().generate_keypair()
        new = StoredKey(key_id=key_id_for(public_key), public_key=bytes(public_key),
                        private_key=bytes(private_key), created_at=now_utc())
        previous = self._current

        if self._key_store is not None:
            self._key_store.save(KeyRecord(algorithm=algorithm, current=new, previous=previous))

        rotating = self._private_key is not None
        self._current, self._previous = new, previous
        self._private_key, self._algorithm = new.private_key, algorithm
        self.installs += 1
        self.log.info(
            "%s post-quantum key made on this station (%s, key_id %s)%s",
            "rotated:" if rotating else "first", algorithm, new.key_id,
            "" if self._key_store is not None else " -- memory only, not saved",
        )
        return new.public_key, new.key_id

    def _private_key_for(self, key_id: str | None) -> bytes:
        """The private key a challenge asks for (Contract 7 section 7.2)."""
        if key_id is None:
            if self._private_key is None:
                raise RuntimeError("cannot sign: this station holds no PQC key")
            return self._private_key
        for key in (self._current, self._previous):
            if key is not None and key.key_id == key_id:
                return key.private_key
        raise LookupError(f"this station holds no key with key_id {key_id!r}")

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
        # A server-made key replaces any station-made one; its public key
        # (and so its key_id) is unknown here. Not saved to disk: this path
        # is deprecated and disappears with InstallPQAuth (section 7.8).
        self._current = None
        self._previous = None
        self.installs += 1

        self.log.info(
            "%s ML-DSA key installed (%s, %d bytes) -- station is now migrated",
            "rotated" if rotating else "first",
            algorithm,
            len(self._private_key),
        )

    # -- the server challenges ---------------------------------------------

    def answer_challenge(self, nonce: bytes, key_id: str | None = None) -> bytes:
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

        Contract 7: `key_id` (from the challenge) picks the current or the
        previous key; None means the current one. An unknown key_id raises
        LookupError, which the handler answers as Rejected.
        """
        private_key = self._private_key_for(key_id)

        from crypto.pq_auth import sign_challenge

        signature = sign_challenge(
            self._provider_or_build(), private_key, nonce
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
            f"pqc: migrated ({self._algorithm}"
            f"{', key_id ' + self.key_id if self.key_id else ''}), "
            f"{self.challenges_signed} challenge(s) signed"
        )
