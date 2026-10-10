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

def test_unenrol_removes_key_and_outstanding_challenge():
    # The orchestrator relies on this to undo a failed install. After
    # unenrol, the station is unknown again: a late response must raise,
    # not verify against a nonce that should no longer exist.
    auth, provider, priv = _enrolled_authenticator()
    challenge = auth.issue_challenge("CP001")
    response = sign_challenge(provider, priv, challenge)
    auth.unenrol("CP001")
    assert auth.is_enrolled("CP001") is False
    with pytest.raises(AuthError):
        auth.verify_response("CP001", response)


def test_unenrol_unknown_station_is_a_no_op():
    auth, _, _ = _enrolled_authenticator()
    auth.unenrol("NOPE")  # must not raise
    assert auth.is_enrolled("CP001") is True

# -- B-P2 (L35): several outstanding challenges per station ---------------


def test_overlapping_challenges_each_verify_with_their_nonce():
    # A boot check and a migration check for the same station overlap:
    # each must verify against its own nonce, in either order.
    auth, provider, priv = _enrolled_authenticator()
    n1 = auth.issue_challenge("CP001")
    n2 = auth.issue_challenge("CP001")
    r1 = sign_challenge(provider, priv, n1)
    r2 = sign_challenge(provider, priv, n2)
    assert auth.verify_response("CP001", r2, nonce=n2) is True
    assert auth.verify_response("CP001", r1, nonce=n1) is True


def test_a_nonce_is_still_single_use():
    auth, provider, priv = _enrolled_authenticator()
    n = auth.issue_challenge("CP001")
    r = sign_challenge(provider, priv, n)
    assert auth.verify_response("CP001", r, nonce=n) is True
    with pytest.raises(AuthError, match="no outstanding challenge"):
        auth.verify_response("CP001", r, nonce=n)


def test_a_response_for_one_nonce_fails_against_another():
    auth, provider, priv = _enrolled_authenticator()
    n1 = auth.issue_challenge("CP001")
    n2 = auth.issue_challenge("CP001")
    r1 = sign_challenge(provider, priv, n1)
    assert auth.verify_response("CP001", r1, nonce=n2) is False   # consumed n2
    assert auth.verify_response("CP001", r1, nonce=n1) is True


def test_unknown_nonce_raises():
    auth, provider, priv = _enrolled_authenticator()
    auth.issue_challenge("CP001")
    with pytest.raises(AuthError, match="no outstanding challenge"):
        auth.verify_response("CP001", b"sig", nonce=b"x" * CHALLENGE_BYTES)


def test_without_a_nonce_the_old_rule_holds_only_the_latest_counts():
    # Callers that do not pass the nonce keep the pre-B-P2 behaviour:
    # the most recent challenge is used and every older one is discarded.
    auth, provider, priv = _enrolled_authenticator()
    n1 = auth.issue_challenge("CP001")
    n2 = auth.issue_challenge("CP001")
    assert auth.verify_response("CP001", sign_challenge(provider, priv, n2)) is True
    with pytest.raises(AuthError, match="no outstanding challenge"):
        auth.verify_response("CP001", sign_challenge(provider, priv, n1))


def test_outstanding_challenges_are_capped_oldest_dropped():
    from crypto.pq_auth import MAX_OUTSTANDING_PER_STATION

    auth, provider, priv = _enrolled_authenticator()
    first = auth.issue_challenge("CP001")
    for _ in range(MAX_OUTSTANDING_PER_STATION):
        last = auth.issue_challenge("CP001")
    with pytest.raises(AuthError):
        auth.verify_response("CP001", sign_challenge(provider, priv, first), nonce=first)
    assert auth.verify_response("CP001", sign_challenge(provider, priv, last), nonce=last) is True


def test_expired_challenges_are_dropped_when_a_new_one_is_issued():
    auth, provider, priv = _enrolled_authenticator(ttl_s=0.01)
    old = auth.issue_challenge("CP001")
    time.sleep(0.05)
    auth.issue_challenge("CP001")
    with pytest.raises(AuthError, match="no outstanding challenge"):
        auth.verify_response("CP001", sign_challenge(provider, priv, old), nonce=old)


def test_public_key_is_readable_for_enrolled_stations_only():
    auth, _, _ = _enrolled_authenticator()
    assert auth.public_key("CP001") == auth._enrolled["CP001"]
    assert auth.public_key("CP999") is None
    auth.unenrol("CP001")
    assert auth.public_key("CP001") is None
