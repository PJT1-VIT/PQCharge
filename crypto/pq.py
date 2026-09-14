"""
Post-quantum cryptographic backend — ML-DSA-44 signatures, ML-KEM-768 key
establishment, via liboqs.

SCAFFOLDING ONLY (Days 3-6). Every method raises NotImplementedError with a
message naming the Day 8 task. The class STRUCTURE is fixed now -- it subclasses
Contract 1 exactly as ClassicalProvider does, so Day 8 is filling in method
bodies against liboqs, not designing the interface. Nothing imports this yet;
it is inert.

DO NOT `pip install liboqs-python` to write this file. The import is deferred
into the methods (not at module top level) precisely so this scaffolding can be
committed, imported, and unit-tested for its structure WITHOUT liboqs present.
The install is a Day 8 task and is the one dependency that can fail on Windows.

Algorithm choices, fixed:
  - Signatures:        ML-DSA-44  (FIPS 204, liboqs name "ML-DSA-44")
  - Key establishment: ML-KEM-768 (FIPS 203, liboqs name "ML-KEM-768")
Changing them changes every E1 and E4 figure, so they are named here once.

DAY 8 CERTIFICATE-SIGNING NOTE: crypto/ca.py cannot sign an X.509 certificate
with an ML-DSA key through cryptography's CertificateBuilder -- this version has
no ml_dsa key type (verified). The two resolutions are recorded in ca.py's
_signing_key docstring and docs/limitations.md. This file provides sign()/verify()
over raw bytes regardless; whether those feed an X.509 cert or an
application-layer handshake is decided when the Day 8 signing path is chosen.
"""

from __future__ import annotations

from crypto.provider import CryptoProvider, CryptoMode

SIG_ALG = "ML-DSA-44"
KEM_ALG = "ML-KEM-768"

_DAY8 = "Track B Day 8: liboqs post-quantum backend not implemented yet"


class PQProvider(CryptoProvider):
    """
    Pure post-quantum provider (mode "pqc"): ML-DSA-44 + ML-KEM-768.

    Hybrid mode (classical + PQC together) is a separate concern and, if built,
    will be its own subclass -- this class is pure PQC only, so a measurement
    tagged "pqc" is unambiguously the post-quantum algorithms alone.
    """

    def __init__(self, mode: CryptoMode = "pqc") -> None:
        if mode != "pqc":
            raise ValueError(f"PQProvider serves mode 'pqc', not {mode!r}")
        super().__init__(mode)

    @property
    def signature_algorithm(self) -> str:
        return SIG_ALG

    @property
    def kem_algorithm(self) -> str:
        return KEM_ALG

    # -- signatures (ML-DSA-44) ---------------------------------------

    def generate_keypair(self) -> tuple[bytes, bytes]:
        raise NotImplementedError(_DAY8)

    def sign(self, private_key: bytes, message: bytes) -> bytes:
        raise NotImplementedError(_DAY8)

    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        raise NotImplementedError(_DAY8)

    # -- key establishment (ML-KEM-768) -------------------------------

    def generate_kem_keypair(self) -> tuple[bytes, bytes]:
        raise NotImplementedError(_DAY8)

    def encapsulate(self, public_key: bytes) -> tuple[bytes, bytes]:
        raise NotImplementedError(_DAY8)

    def decapsulate(self, private_key: bytes, ciphertext: bytes) -> bytes:
        raise NotImplementedError(_DAY8)