"""Tests for the classical ECDSA + ECDH provider."""

from crypto.classical import ClassicalProvider


def test_sign_verify_roundtrip():
    p = ClassicalProvider()
    priv, pub = p.generate_keypair()
    msg = b"boot notification payload"
    sig = p.sign(priv, msg)
    assert p.verify(pub, msg, sig) is True


def test_verify_rejects_tampered_message():
    p = ClassicalProvider()
    priv, pub = p.generate_keypair()
    sig = p.sign(priv, b"original")
    assert p.verify(pub, b"tampered", sig) is False


def test_verify_rejects_wrong_key():
    p = ClassicalProvider()
    priv1, _ = p.generate_keypair()
    _, pub2 = p.generate_keypair()
    sig = p.sign(priv1, b"msg")
    assert p.verify(pub2, b"msg", sig) is False


def test_verify_rejects_garbage_without_raising():
    p = ClassicalProvider()
    _, pub = p.generate_keypair()
    assert p.verify(pub, b"msg", b"not a signature") is False
    assert p.verify(b"not a key", b"msg", b"sig") is False


def test_kem_shared_secret_agrees():
    p = ClassicalProvider()
    priv, pub = p.generate_kem_keypair()
    shared_a, ciphertext = p.encapsulate(pub)
    shared_b = p.decapsulate(priv, ciphertext)
    assert shared_a == shared_b
    assert len(shared_a) == 32


def test_kem_distinct_exchanges_differ():
    p = ClassicalProvider()
    _, pub = p.generate_kem_keypair()
    secret1, _ = p.encapsulate(pub)
    secret2, _ = p.encapsulate(pub)
    # Different ephemeral keys -> different shared secrets.
    assert secret1 != secret2


def test_algorithm_names():
    p = ClassicalProvider()
    assert p.signature_algorithm == "ECDSA-P256"
    assert p.kem_algorithm == "ECDH-P256"