"""B-F5 (E4): real ML-DSA X.509 certificates and their sizes."""

import pytest

from crypto import pq_x509

pytestmark = pytest.mark.skipif(not pq_x509.real_certificates_available(),
                                reason="this cryptography cannot make ML-DSA certificates")


def test_the_certificates_are_real_and_signed_by_the_root():
    assert pq_x509.verify_chain(pq_x509.issue_chain("ML-DSA-44"))


def test_size_rows_have_the_e4_shape_and_grow_with_the_level():
    rows = pq_x509.size_rows()
    assert len(rows) == 12                                   # 3 levels x 4 artifacts
    for r in rows:
        assert set(r) == {"group", "label", "algorithm", "bytes", "pem_bytes",
                          "limit", "within_limit", "headroom"}
        assert r["within_limit"] == (r["pem_bytes"] <= r["limit"])
    station = {r["algorithm"]: r["pem_bytes"] for r in rows if r["label"] == "Station certificate"}
    assert station["ML-DSA-44"] < station["ML-DSA-65"] < station["ML-DSA-87"]
    # An ML-DSA-44 certificate carries a 1312-byte key and a 2420-byte signature.
    der44 = next(r["bytes"] for r in rows if r["algorithm"] == "ML-DSA-44"
                 and r["label"] == "Station certificate")
    assert der44 > 1312 + 2420


def test_computed_sizes_match_real_certificates_within_a_few_bytes():
    """L43 fallback: the computed rows agree with the real ones."""
    real = {(r["algorithm"], r["label"]): r for r in pq_x509.size_rows()}
    computed = {(r["algorithm"].replace(" (computed)", ""), r["label"]): r
                for r in pq_x509._computed_rows(pq_x509.LEVELS)}
    assert real.keys() == computed.keys()
    for key, r in real.items():
        c = computed[key]
        assert abs(c["bytes"] - r["bytes"]) <= 8, key
        assert abs(c["pem_bytes"] - r["pem_bytes"]) <= 12, key
        assert c["within_limit"] == r["within_limit"], key
