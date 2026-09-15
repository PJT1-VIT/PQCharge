"""
Post-quantum cryptographic backend — ML-DSA-44 signatures, ML-KEM-768 key
establishment.

Implements Contract 1 (CryptoProvider) for mode "pqc".

BACKEND: quantcrypt (PQClean precompiled binaries), NOT liboqs.
liboqs-python requires building the liboqs C library from source, which needs
CMake + an MSVC toolchain not present on the Track B Windows machine. quantcrypt
ships prebuilt PQClean wheels (quantcrypt==1.0.0 has a cp310 win_amd64 wheel),
installs with no compiler, and implements the same NIST FIPS 203/204 parameter
sets. Artifact sizes are standard-defined and therefore identical to liboqs
(verified on this machine: ML-DSA-44 sig 2420 B, ML-KEM-768 ct 1088 B). Because
everything sits behind Contract 1, the backend is swappable: final E1 *timing*
numbers may later be produced on a liboqs machine (the Pi, or Track A's Mac)
without changing this file's interface. Flagged to the team and recorded in
docs/limitations.md.

TWO PLACES THIS BACKEND DIVERGES FROM Contract 1 / ClassicalProvider, both
adapted here so callers see a uniform interface:

  1. quantcrypt keygen() returns (public, secret) -- PUBLIC FIRST. Contract 1
     and ClassicalProvider return (private, public). This module SWAPS the order
     so PQProvider.generate_keypair() returns (private, public) like every other
     provider. A caller must never see the quantcrypt order.

  2. quantcrypt verify() RAISES DSSVerifyFailedError on a bad signature. Contract
     1 requires verify() to RETURN False and never raise, because Track A treats
     it as an authentication decision. This module catches and returns False.

Key material crosses the Contract 1 boundary as raw bytes, exactly as
ClassicalProvider's does.
"""

from __future__ import annotations

from crypto.provider import CryptoProvider, CryptoMode

SIG_ALG = "ML-DSA-44"
KEM_ALG = "ML-KEM-768"


class PQProvider(CryptoProvider):
    """
    Pure post-quantum provider (mode "pqc"): ML-DSA-44 + ML-KEM-768 via
    quantcrypt/PQClean.

    Hybrid mode (classical + PQC together) is a separate concern and, if built,
    is its own subclass -- this class is pure PQC only, so a measurement tagged
    "pqc" is unambiguously the post-quantum algorithms alone.

    quantcrypt objects are constructed per call rather than held as instance
    state: the library's DSS/KEM objects are cheap to make and constructing
    fresh avoids any hidden per-object state leaking across the CryptoProvider's
    stateless-by-contract methods.
    """

    def __init__(self, mode: CryptoMode = "pqc") -> None:
        if mode != "pqc":
            raise ValueError(f"PQProvider serves mode 'pqc', not {mode!r}")
        super().__init__(mode)
        # Import here, not at module top level, so the rest of the package -- and
        # every test that does not exercise PQ crypto -- imports without
        # quantcrypt installed. Matches the deferred-import discipline the
        # scaffolding established.
        from quantcrypt.dss import MLDSA_44
        from quantcrypt.kem import MLKEM_768

        self._MLDSA_44 = MLDSA_44
        self._MLKEM_768 = MLKEM_768

    @property
    def signature_algorithm(self) -> str:
        return SIG_ALG

    @property
    def kem_algorithm(self) -> str:
        return KEM_ALG

    # -- signatures (ML-DSA-44) ---------------------------------------

    def generate_keypair(self) -> tuple[bytes, bytes]:
        """
        Returns (private_key, public_key) as raw bytes.

        NOTE: quantcrypt's keygen() returns (public, secret). We swap to
        Contract 1's (private, public) order so callers see the same shape as
        every other provider.
        """
        public_key, private_key = self._MLDSA_44().keygen()
        return private_key, public_key

    def sign(self, private_key: bytes, message: bytes) -> bytes:
        return self._MLDSA_44().sign(private_key, message)

    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        """
        Returns True / False. quantcrypt raises DSSVerifyFailedError on a bad
        signature; Contract 1 requires a bool, so the exception is caught here.
        Any malformed input also returns False rather than propagating.
        """
        try:
            return bool(self._MLDSA_44().verify(public_key, message, signature))
        except Exception:
            # DSSVerifyFailedError on a bad/forged signature, plus any parse
            # error on malformed key/signature bytes -- all are "not valid",
            # never a raised exception, because Track A reads this as an
            # authentication decision.
            return False

    # -- key establishment (ML-KEM-768) -------------------------------

    def generate_kem_keypair(self) -> tuple[bytes, bytes]:
        """
        Returns (private_key, public_key) as raw bytes -- swapped from
        quantcrypt's (public, secret) order, as generate_keypair() is.
        """
        public_key, private_key = self._MLKEM_768().keygen()
        return private_key, public_key

    def encapsulate(self, public_key: bytes) -> tuple[bytes, bytes]:
        """
        Returns (shared_secret, ciphertext).

        NOTE: quantcrypt's encaps() returns (ciphertext, shared_secret).
        Contract 1 (and ClassicalProvider) order it (shared_secret, ciphertext),
        so we swap. The ciphertext is transmitted to the peer; the shared secret
        is not.
        """
        ciphertext, shared_secret = self._MLKEM_768().encaps(public_key)
        return shared_secret, ciphertext

    def decapsulate(self, private_key: bytes, ciphertext: bytes) -> bytes:
        """Recover the shared secret. Identical to the value encapsulate()
        produced on the other side."""
        return self._MLKEM_768().decaps(private_key, ciphertext)