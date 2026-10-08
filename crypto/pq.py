"""
Post-quantum cryptographic backend — ML-DSA-44 signatures, ML-KEM-768 key
establishment.

Implements Contract 1 (CryptoProvider) for mode "pqc".

BACKEND: pyca/cryptography >= 50 (OpenSSL), replacing quantcrypt 1.0.0.

History: Day 8 chose quantcrypt (prebuilt PQClean wheels) because liboqs-python
needed CMake + MSVC, and cryptography 46 had no ML-DSA. quantcrypt has no
working engine on Python 3.14 (PQAImportError), and cryptography 50 now ships
ML-DSA-44/65/87 and ML-KEM-768/1024 natively in its prebuilt wheels, on Windows,
macOS and Linux. Same NIST FIPS 203/204 parameter sets, so the public artifact
sizes are unchanged (ML-DSA-44 public key 1312 B, signature 2420 B; ML-KEM-768
public key 1184 B, ciphertext 1088 B, shared secret 32 B). The same library also
signs ML-DSA X.509 certificates, which quantcrypt could not.

PRIVATE KEY REPRESENTATION (changed from quantcrypt):
  A private key crosses Contract 1 as its FIPS 203/204 SEED:
    ML-DSA-44 private key = 32-byte seed  (quantcrypt: 2560-byte expanded key)
    ML-KEM-768 private key = 64-byte seed (quantcrypt: 2400-byte expanded key)
  The seed is the standard's own compact private-key form: the full key is
  re-derived from it deterministically, so nothing is lost. cryptography exposes
  only the seed (private_bytes_raw / from_seed_bytes). Effect outside this file:
  the InstallPQAuth payload and the E4 "private key" row shrink.

CONTRACT 1 ADAPTATIONS (callers see a uniform interface):

  1. Key order: cryptography returns key OBJECTS; this module returns
     (private_bytes, public_bytes), the Contract 1 order.

  2. verify(): cryptography RAISES InvalidSignature on a bad signature, and
     ValueError on malformed key bytes. Contract 1 requires verify() to RETURN
     False and never raise, because Track A treats it as an authentication
     decision. This module catches and returns False.

  3. encapsulate(): cryptography already returns (shared_secret, ciphertext),
     the Contract 1 order -- no swap needed (quantcrypt needed one).

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
    pyca/cryptography.

    Hybrid mode (classical + PQC together) is a separate concern and, if built,
    is its own subclass -- this class is pure PQC only, so a measurement tagged
    "pqc" is unambiguously the post-quantum algorithms alone.

    Stateless: every method rebuilds key objects from the bytes it is given,
    as Contract 1 requires (bytes in, bytes out).
    """

    def __init__(self, mode: CryptoMode = "pqc") -> None:
        if mode != "pqc":
            raise ValueError(f"PQProvider serves mode 'pqc', not {mode!r}")
        super().__init__(mode)
        # Imported here, not at module top level, so that importing this module
        # never fails on an older cryptography (< 50, no ML-DSA). Constructing
        # PQProvider() fails loudly instead -- csms/migration.py's startup probe
        # relies on exactly that to disable migration with a reason.
        from cryptography.hazmat.primitives.asymmetric import mldsa, mlkem

        self._mldsa = mldsa
        self._mlkem = mlkem

    @property
    def signature_algorithm(self) -> str:
        return SIG_ALG

    @property
    def kem_algorithm(self) -> str:
        return KEM_ALG

    # -- signatures (ML-DSA-44) ---------------------------------------

    def generate_keypair(self) -> tuple[bytes, bytes]:
        """
        Returns (private_key, public_key) as raw bytes:
        a 32-byte ML-DSA-44 seed and the 1312-byte public key.
        """
        key = self._mldsa.MLDSA44PrivateKey.generate()
        return key.private_bytes_raw(), key.public_key().public_bytes_raw()

    def sign(self, private_key: bytes, message: bytes) -> bytes:
        """Sign with the 32-byte seed. Returns the 2420-byte signature."""
        key = self._mldsa.MLDSA44PrivateKey.from_seed_bytes(private_key)
        return key.sign(message)

    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        """
        Returns True / False. cryptography raises InvalidSignature on a bad
        signature and ValueError on malformed key bytes; Contract 1 requires a
        bool, so every exception is caught here.
        """
        try:
            key = self._mldsa.MLDSA44PublicKey.from_public_bytes(public_key)
            key.verify(signature, message)
            return True
        except Exception:
            # InvalidSignature on a bad/forged signature, ValueError on a
            # malformed key -- all are "not valid", never a raised exception,
            # because Track A reads this as an authentication decision.
            return False

    # -- key establishment (ML-KEM-768) -------------------------------

    def generate_kem_keypair(self) -> tuple[bytes, bytes]:
        """
        Returns (private_key, public_key) as raw bytes:
        a 64-byte ML-KEM-768 seed and the 1184-byte public key.
        """
        key = self._mlkem.MLKEM768PrivateKey.generate()
        return key.private_bytes_raw(), key.public_key().public_bytes_raw()

    def encapsulate(self, public_key: bytes) -> tuple[bytes, bytes]:
        """
        Returns (shared_secret, ciphertext): 32 B and 1088 B.
        cryptography's encapsulate() already uses this order.
        The ciphertext is transmitted to the peer; the shared secret is not.
        """
        key = self._mlkem.MLKEM768PublicKey.from_public_bytes(public_key)
        shared_secret, ciphertext = key.encapsulate()
        return shared_secret, ciphertext

    def decapsulate(self, private_key: bytes, ciphertext: bytes) -> bytes:
        """Recover the shared secret. Identical to the value encapsulate()
        produced on the other side."""
        key = self._mlkem.MLKEM768PrivateKey.from_seed_bytes(private_key)
        return key.decapsulate(ciphertext)