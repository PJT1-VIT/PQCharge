"""
Classical cryptographic backend — ECDSA P-256 signatures, ECDH key
establishment.

Implements Contract 1 (CryptoProvider) for mode "classical". This is
the baseline every post-quantum measurement is compared against, so it
is built first and fully, before liboqs enters the picture on Day 8.

Key material crosses the CryptoProvider boundary as raw bytes, DER-
encoded. DER is chosen over PEM because it is the compact form X.509
uses natively and the form the E4 size table measures; conversion to
PEM happens only at the OCPP transmission edge, in crypto/store.py.

ECDH is exposed through the encapsulate/decapsulate KEM interface even
though ECDH is not literally a KEM. The ephemeral-static construction
below is the standard way to present Diffie-Hellman as a KEM: the
"ciphertext" is an ephemeral public key, and the "shared secret" is the
HKDF-derived key. This lets classical and post-quantum modes sit behind
one interface and produces a classical key-exchange figure for E1.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from crypto.provider import CryptoProvider, CryptoMode

_CURVE = ec.SECP256R1()
_SHARED_SECRET_BYTES = 32
_HKDF_INFO = b"pqcharge-classical-ecdh-kem"


class ClassicalProvider(CryptoProvider):
    """ECDSA P-256 signatures and ECDH key establishment."""

    def __init__(self, mode: CryptoMode = "classical") -> None:
        if mode != "classical":
            raise ValueError(
                f"ClassicalProvider serves mode 'classical', not {mode!r}"
            )
        super().__init__(mode)

    @property
    def signature_algorithm(self) -> str:
        return "ECDSA-P256"

    @property
    def kem_algorithm(self) -> str:
        return "ECDH-P256"

    # -- signatures ---------------------------------------------------

    def generate_keypair(self) -> tuple[bytes, bytes]:
        private = ec.generate_private_key(_CURVE)
        priv_bytes = private.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_bytes = private.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return priv_bytes, pub_bytes

    def sign(self, private_key: bytes, message: bytes) -> bytes:
        private = serialization.load_der_private_key(private_key, password=None)
        if not isinstance(private, ec.EllipticCurvePrivateKey):
            raise ValueError("private_key is not an EC private key")
        return private.sign(message, ec.ECDSA(hashes.SHA256()))

    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        try:
            public = serialization.load_der_public_key(public_key)
            if not isinstance(public, ec.EllipticCurvePublicKey):
                return False
            public.verify(signature, message, ec.ECDSA(hashes.SHA256()))
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False

    # -- key establishment (ECDH presented as a KEM) ------------------

    def generate_kem_keypair(self) -> tuple[bytes, bytes]:
        # Same curve and encoding as signature keys, but a distinct
        # keypair: a station holds one for signing and one for exchange.
        private = ec.generate_private_key(_CURVE)
        priv_bytes = private.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_bytes = private.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return priv_bytes, pub_bytes

    def encapsulate(self, public_key: bytes) -> tuple[bytes, bytes]:
        peer_public = serialization.load_der_public_key(public_key)
        if not isinstance(peer_public, ec.EllipticCurvePublicKey):
            raise ValueError("public_key is not an EC public key")

        # Ephemeral keypair; its public part becomes the "ciphertext".
        ephemeral = ec.generate_private_key(_CURVE)
        shared_point = ephemeral.exchange(ec.ECDH(), peer_public)
        shared_secret = _derive(shared_point)

        ciphertext = ephemeral.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return shared_secret, ciphertext

    def decapsulate(self, private_key: bytes, ciphertext: bytes) -> bytes:
        private = serialization.load_der_private_key(private_key, password=None)
        if not isinstance(private, ec.EllipticCurvePrivateKey):
            raise ValueError("private_key is not an EC private key")

        ephemeral_public = serialization.load_der_public_key(ciphertext)
        if not isinstance(ephemeral_public, ec.EllipticCurvePublicKey):
            raise ValueError("ciphertext is not an EC ephemeral public key")

        shared_point = private.exchange(ec.ECDH(), ephemeral_public)
        return _derive(shared_point)


def _derive(shared_point: bytes) -> bytes:
    """
    Turn a raw ECDH shared point into a fixed-length key via HKDF.

    The raw x-coordinate is not used directly as a key -- HKDF-SHA256
    both fixes the length and removes the bias in the raw point, which
    is standard practice for ECDH key derivation.
    """
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_SHARED_SECRET_BYTES,
        salt=None,
        info=_HKDF_INFO,
    ).derive(shared_point)