"""
Tests for application-layer post-quantum authentication (Option B).

Covers the happy path and every rejection the security story depends on:
impersonation, replay, expiry, and non-enrolment. These are the assertions
behind E5's "migrated fleet rejects the attacker".
"""

import time

import pytest

from crypto.pq import PQProvider
from crypto.pq_auth import (
    PQAuthenticator,
    AuthError,
    sign_challenge,
    CHALLENGE_BYTES,
)


def _enrolled_authenticator(ttl_s=30.0):
    """An authenticator with one station 'CP001' enrolled, returning the
    authenticator, the provider, and the station's private key."""
    provider = PQProvider()
    auth = PQAuthenticator(provider, challenge_ttl_s=ttl_s)
    priv, pub = provider.generate_keypair()
    auth.enrol("CP001", pub)
    return auth, provider, priv


def test_legitimate_station_authenticates():
    auth, provider, priv = _enrolled_authenticator()
    challenge = auth.issue_challenge("CP001")
    assert len(challenge) == CHALLENGE_BYTES
    response = sign_challenge(provider, priv, challenge)
    assert auth.verify_response("CP001", response) is True


def test_impersonator_without_private_key_rejected():
    # The core E5 assertion: an attacker who is enrolled-as-nobody, or who holds
    # a different key, cannot produce a valid response for CP001.
    auth, provider, _ = _enrolled_authenticator()
    attacker_priv, _ = provider.generate_keypair()
    challenge = auth.issue_challenge("CP001")
    forged = sign_challenge(provider, attacker_priv, challenge)
    assert auth.verify_response("CP001", forged) is False


def test_replayed_response_rejected():
    # A response valid for one challenge must fail against the next.
    auth, provider, priv = _enrolled_authenticator()
    ch1 = auth.issue_challenge("CP001")
    resp1 = sign_challenge(provider, priv, ch1)
    assert auth.verify_response("CP001", resp1) is True
    # New challenge issued; the old response must not verify against it.
    auth.issue_challenge("CP001")
    assert auth.verify_response("CP001", resp1) is False


def test_challenge_is_single_use():
    # Even a valid response cannot be verified twice -- the challenge is
    # consumed on first use.
    auth, provider, priv = _enrolled_authenticator()
    ch = auth.issue_challenge("CP001")
    resp = sign_challenge(provider, priv, ch)
    assert auth.verify_response("CP001", resp) is True
    with pytest.raises(AuthError):
        auth.verify_response("CP001", resp)


def test_expired_challenge_raises():
    auth, provider, priv = _enrolled_authenticator(ttl_s=0.01)
    ch = auth.issue_challenge("CP001")
    resp = sign_challenge(provider, priv, ch)
    time.sleep(0.05)
    with pytest.raises(AuthError, match="expired"):
        auth.verify_response("CP001", resp)


def test_no_challenge_raises():
    auth, provider, priv = _enrolled_authenticator()
    resp = sign_challenge(provider, priv, b"x" * CHALLENGE_BYTES)
    with pytest.raises(AuthError, match="no outstanding challenge"):
        auth.verify_response("CP001", resp)


def test_unenrolled_station_raises():
    provider = PQProvider()
    auth = PQAuthenticator(provider)
    with pytest.raises(AuthError, match="not enrolled"):
        auth.verify_response("UNKNOWN", b"sig")


def test_fresh_challenges_differ():
    auth, _, _ = _enrolled_authenticator()
    c1 = auth.issue_challenge("CP001")
    c2 = auth.issue_challenge("CP001")
    assert c1 != c2


def test_algorithm_name_reported():
    auth, _, _ = _enrolled_authenticator()
    assert auth.algorithm == "ML-DSA-44"