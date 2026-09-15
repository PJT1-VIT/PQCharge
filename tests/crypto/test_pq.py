"""
Real cryptographic tests for the post-quantum provider (quantcrypt backend).

Replaces test_pq_scaffolding.py. These require quantcrypt installed; they are
the PQ counterpart of test_classical.py, and they specifically pin the three
Contract-1 adaptations (keypair order, verify-returns-bool, encapsulate order)
that quantcrypt's native API gets the other way round.
"""

import pytest

from crypto.pq import PQProvider, SIG_ALG, KEM_ALG
from crypto.provider import CryptoProvider

# ML-DSA-44 / ML-KEM-768 standard sizes (FIPS 204 / 203).
MLDSA44_SIG_LEN = 2420
MLKEM768_CT_LEN = 1088
KEM_SHARED_LEN = 32


def test_is_a_crypto_provider():
    assert issubclass(PQProvider, CryptoProvider)


def test_mode_and_algorithm_names():
    p = PQProvider()
    assert p.mode == "pqc"
    assert p.signature_algorithm == "ML-DSA-44" == SIG_ALG
    assert p.kem_algorithm == "ML-KEM-768" == KEM_ALG


def test_rejects_wrong_mode():
    with pytest.raises(ValueError):
        PQProvider("classical")


def test_sign_verify_roundtrip():
    p = PQProvider()
    priv, pub = p.generate_keypair()
    msg = b"boot notification payload"
    sig = p.sign(priv, msg)
    assert len(sig) == MLDSA44_SIG_LEN
    assert p.verify(pub, msg, sig) is True


def test_verify_rejects_tampered_message():
    # This is the critical adaptation: quantcrypt RAISES here; Contract 1
    # requires False. If this test raises instead of returning False, the
    # verify() wrapper is broken.
    p = PQProvider()
    priv, pub = p.generate_keypair()
    sig = p.sign(priv, b"original")
    assert p.verify(pub, b"tampered", sig) is False


def test_verify_rejects_wrong_key():
    p = PQProvider()
    priv1, _ = p.generate_keypair()
    _, pub2 = p.generate_keypair()
    sig = p.sign(priv1, b"msg")
    assert p.verify(pub2, b"msg", sig) is False


def test_verify_rejects_garbage_without_raising():
    p = PQProvider()
    _, pub = p.generate_keypair()
    assert p.verify(pub, b"msg", b"not a signature") is False
    assert p.verify(b"not a key", b"msg", b"sig") is False


def test_keypair_order_is_private_then_public():
    # quantcrypt returns (public, secret); Contract 1 is (private, public).
    # The ML-DSA-44 secret key is 2560 B and the public key is 1312 B, so we
    # can tell them apart by length and confirm the swap happened.
    p = PQProvider()
    priv, pub = p.generate_keypair()
    assert len(priv) == 2560, "first element must be the PRIVATE key"
    assert len(pub) == 1312, "second element must be the PUBLIC key"


def test_kem_shared_secret_agrees():
    p = PQProvider()
    priv, pub = p.generate_kem_keypair()
    shared_a, ciphertext = p.encapsulate(pub)
    shared_b = p.decapsulate(priv, ciphertext)
    assert shared_a == shared_b
    assert len(shared_a) == KEM_SHARED_LEN
    assert len(ciphertext) == MLKEM768_CT_LEN


def test_encapsulate_order_is_secret_then_ciphertext():
    # quantcrypt encaps() returns (ciphertext, shared_secret); Contract 1 is
    # (shared_secret, ciphertext). Shared secret is 32 B, ciphertext 1088 B.
    p = PQProvider()
    _, pub = p.generate_kem_keypair()
    first, second = p.encapsulate(pub)
    assert len(first) == KEM_SHARED_LEN, "first element must be the SHARED SECRET"
    assert len(second) == MLKEM768_CT_LEN, "second element must be the CIPHERTEXT"


def test_kem_distinct_exchanges_differ():
    p = PQProvider()
    _, pub = p.generate_kem_keypair()
    secret1, _ = p.encapsulate(pub)
    secret2, _ = p.encapsulate(pub)
    assert secret1 != secret2