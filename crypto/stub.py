"""
Placeholder provider. Lets Track A and Track C import and call the
interface before Track B's real backends exist (Days 3-9).

Every method raises. It never returns fake data -- a stub that returns
plausible-looking bytes is a stub someone forgets is a stub, and the
failure then appears as a mysterious verification error days later
rather than as an obvious NotImplementedError here.

Delete this file once crypto/classical.py and crypto/pq.py exist.
"""

from __future__ import annotations

from crypto.provider import CryptoProvider, CryptoMode


class StubProvider(CryptoProvider):
    """Satisfies the interface; implements nothing."""

    def __init__(self, mode: CryptoMode = "classical") -> None:
        super().__init__(mode)

    @property
    def signature_algorithm(self) -> str:
        return "stub-sig"

    @property
    def kem_algorithm(self) -> str:
        return "stub-kem"

    def generate_keypair(self) -> tuple[bytes, bytes]:
        raise NotImplementedError("Track B: crypto backend not implemented yet")

    def sign(self, private_key: bytes, message: bytes) -> bytes:
        raise NotImplementedError("Track B: crypto backend not implemented yet")

    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        raise NotImplementedError("Track B: crypto backend not implemented yet")

    def generate_kem_keypair(self) -> tuple[bytes, bytes]:
        raise NotImplementedError("Track B: crypto backend not implemented yet")

    def encapsulate(self, public_key: bytes) -> tuple[bytes, bytes]:
        raise NotImplementedError("Track B: crypto backend not implemented yet")

    def decapsulate(self, private_key: bytes, ciphertext: bytes) -> bytes:
        raise NotImplementedError("Track B: crypto backend not implemented yet")