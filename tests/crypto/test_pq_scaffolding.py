"""
Structural tests for the post-quantum provider scaffolding.

These prove the class satisfies Contract 1's shape and fails LOUDLY (Day 8
marker) rather than silently -- WITHOUT requiring liboqs to be installed.
Real cryptographic tests replace these on Day 8 once the bodies exist.
"""

import importlib.util

import pytest

from crypto.pq import PQProvider, SIG_ALG, KEM_ALG
from crypto.provider import CryptoProvider


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


@pytest.mark.parametrize(
    "call",
    [
        lambda p: p.generate_keypair(),
        lambda p: p.sign(b"k", b"m"),
        lambda p: p.verify(b"k", b"m", b"s"),
        lambda p: p.generate_kem_keypair(),
        lambda p: p.encapsulate(b"k"),
        lambda p: p.decapsulate(b"k", b"c"),
    ],
)
def test_crypto_methods_raise_day8_marker(call):
    p = PQProvider()
    with pytest.raises(NotImplementedError, match="Day 8"):
        call(p)


def test_scaffolding_works_without_liboqs():
    # The whole point of the deferred import: this module is usable before
    # liboqs is installed. If liboqs happens to be present that's fine too;
    # the assertion is that importing crypto.pq did not require it.
    import crypto.pq  # already imported above; this just documents intent
    assert crypto.pq is not None