"""
Minimal non-classical CryptoProvider stand-in, used only to test that
CertificateAuthority correctly refuses non-classical modes until Day 8.

Not a real backend. Delete once crypto/pq.py exists and real
post-quantum tests replace this.
"""

from __future__ import annotations

from crypto.provider import CryptoProvider, CryptoMode


class FakeNonClassicalProvider(CryptoProvider):
    def __init__(self) -> None:
        super().__init__("pqc")

    @property
    def signature_algorithm(self) -> str:
        return "fake-pqc-sig"

    @property
    def kem_algorithm(self) -> str:
        return "fake-pqc-kem"

    def generate_keypair(self):
        return b"fake-priv", b"fake-pub"

    def sign(self, private_key, message):
        return b"fake-sig"

    def verify(self, public_key, message, signature):
        return False

    def generate_kem_keypair(self):
        return b"fake-priv", b"fake-pub"

    def encapsulate(self, public_key):
        return b"fake-secret", b"fake-ct"

    def decapsulate(self, private_key, ciphertext):
        return b"fake-secret"