"""
Contract 1 — Cryptographic provider abstraction.

Provided by:  Track B (crypto)
Consumed by:  Track A (csms), Track C (agent)

FROZEN INTERFACE. Signatures agreed on Day 2. Changing any signature
after this point requires notifying the other two tracks first.

--------------------------------------------------------------------
RULE: No module outside crypto/ may import liboqs, or name a specific
algorithm (ML-DSA, ML-KEM, ECDSA, X25519) anywhere in its source.

Every cryptographic operation in the system passes through this
interface. This is what makes the crypto-agility claim structural
rather than aspirational: switching the whole fleet between classical,
hybrid and post-quantum is a single change to the mode string.
--------------------------------------------------------------------

All keys, messages, signatures and ciphertexts cross this boundary as
raw bytes. The provider never exposes library-specific key objects,
because the classical backend (cryptography/OpenSSL) and the
post-quantum backend (liboqs) have incompatible object models. Bytes
are the only representation both can produce.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

CryptoMode = Literal["classical", "hybrid", "pqc"]

VALID_MODES: tuple[str, ...] = ("classical", "hybrid", "pqc")


class UnsupportedOperation(NotImplementedError):
    """
    Raised when an operation is not defined for the active mode.

    Example: a KEM encapsulation requested from a purely classical
    signature-only provider. This is raised rather than silently
    substituted, so that a measurement is never reported for an
    operation that did not actually occur.
    """


class CryptoProvider(ABC):
    """
    Uniform interface over one cryptographic configuration.

    A provider instance is bound to exactly one mode for its lifetime.
    To compare modes, instantiate several providers side by side --
    this is how experiment E1 collects classical, hybrid and
    post-quantum figures within a single run.

    Implementations live in crypto/. Nothing else constructs a backend
    directly; callers receive a provider and use it.
    """

    def __init__(self, mode: CryptoMode) -> None:
        if mode not in VALID_MODES:
            raise ValueError(
                f"unknown crypto mode {mode!r}; expected one of {VALID_MODES}"
            )
        self._mode: CryptoMode = mode

    @property
    def mode(self) -> CryptoMode:
        """The cryptographic configuration this provider was built with."""
        return self._mode

    @property
    @abstractmethod
    def signature_algorithm(self) -> str:
        """
        Human-readable name of the active signature algorithm, for logging
        and for the results tables. Never used to branch on behaviour.
        """

    @property
    @abstractmethod
    def kem_algorithm(self) -> str:
        """
        Human-readable name of the active key-establishment algorithm,
        for logging and results tables. Never used to branch on behaviour.
        """

    # -- signatures ---------------------------------------------------

    @abstractmethod
    def generate_keypair(self) -> tuple[bytes, bytes]:
        """
        Generate a fresh signature keypair.

        Returns:
            (private_key, public_key) as raw bytes.

        The encoding is the provider's own and is not interpreted by
        callers; a private key returned here is only ever passed back
        into sign() on a provider of the same mode.
        """

    @abstractmethod
    def sign(self, private_key: bytes, message: bytes) -> bytes:
        """
        Produce a signature over message.

        Returns:
            The signature as raw bytes.

        Guarantee: the result verifies under verify() with the public
        key matching private_key, on a provider of the same mode.
        """

    @abstractmethod
    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        """
        Check a signature.

        Returns:
            True if signature is valid for message under public_key,
            False otherwise.

        A malformed signature or key returns False; it does not raise.
        Callers treat this as an authentication decision, so an
        exception escaping here would be indistinguishable from a
        server fault.
        """

    # -- key establishment --------------------------------------------

    @abstractmethod
    def generate_kem_keypair(self) -> tuple[bytes, bytes]:
        """
        Generate a fresh key-establishment keypair.

        Returns:
            (private_key, public_key) as raw bytes.

        Separate from generate_keypair() because signature and KEM keys
        are distinct algorithms with distinct key material, and a single
        station holds both.

        Raises:
            UnsupportedOperation: if the mode has no KEM.
        """

    @abstractmethod
    def encapsulate(self, public_key: bytes) -> tuple[bytes, bytes]:
        """
        Establish a shared secret against a peer's KEM public key.

        Returns:
            (shared_secret, ciphertext) as raw bytes. The ciphertext is
            transmitted to the peer; the shared secret is not.

        Raises:
            UnsupportedOperation: if the mode has no KEM.
        """

    @abstractmethod
    def decapsulate(self, private_key: bytes, ciphertext: bytes) -> bytes:
        """
        Recover the shared secret from a peer's ciphertext.

        Returns:
            The shared secret as raw bytes, identical to the value the
            encapsulating side obtained.

        Raises:
            UnsupportedOperation: if the mode has no KEM.
        """