"""
Contract 7, section 7.2 — the enrolment and challenge messages. Track C (tests). C-P1.

What these guard:
  * RequestPQEnrolment carries no key material, and round-trips.
  * The station's reply carries the PUBLIC key only. key_id is computed
    from the key, never trusted from outside. A reply containing a private
    key is refused (L04: key material must never reach the server or its
    logs).
  * PQAuthChallenge can name a key_id; a bad key_id is refused.
  * With real ML-DSA, a public key that went through the reply verifies
    a signature made with its private key, using Track B's own
    authenticator. The bytes survive the trip.

Pure tests run everywhere. The real-ML-DSA test skips without quantcrypt,
like test_pqc_integration.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

import pytest

from agent import pqc_messages as pqcm


def _reply_obj(key: bytes, **override) -> dict:
    obj = json.loads(pqcm.pack_enrolment_reply("ML-DSA-44", key))
    obj.update(override)
    return obj


# -- key_id ---------------------------------------------------------------------


def test_key_id_is_the_first_16_hex_of_sha256():
    key = os.urandom(1312)
    assert pqcm.key_id_for(key) == hashlib.sha256(key).hexdigest()[:16]
    assert len(pqcm.key_id_for(key)) == pqcm.KEY_ID_HEX_CHARS == 16


def test_key_id_differs_for_different_keys_and_is_stable():
    a, b = os.urandom(1312), os.urandom(1312)
    assert pqcm.key_id_for(a) == pqcm.key_id_for(bytearray(a))
    assert pqcm.key_id_for(a) != pqcm.key_id_for(b)


def test_key_id_of_an_empty_key_is_an_error():
    with pytest.raises(ValueError):
        pqcm.key_id_for(b"")


# -- RequestPQEnrolment ------------------------------------------------------------


def test_enrolment_request_carries_only_the_algorithm():
    msg = pqcm.build_enrolment_request("CP0001")
    assert msg.vendor_id == pqcm.PQC_VENDOR_ID == "pqcharge.pqc"
    assert msg.message_id == pqcm.MSG_REQUEST_ENROLMENT == "RequestPQEnrolment"
    assert json.loads(msg.data) == {"algorithm": "ML-DSA-44"}


def test_enrolment_request_round_trips_any_algorithm():
    msg = pqcm.build_enrolment_request("CP0001", algorithm="ML-DSA-65")
    assert pqcm.parse_enrolment_request(msg.data) == "ML-DSA-65"
    # The ocpp library may hand the station a dict instead of a string.
    assert pqcm.parse_enrolment_request(json.loads(msg.data)) == "ML-DSA-65"


@pytest.mark.parametrize("algorithm", ["", None, 44])
def test_enrolment_request_refuses_a_bad_algorithm(algorithm):
    with pytest.raises(ValueError):
        pqcm.build_enrolment_request("CP0001", algorithm=algorithm)


@pytest.mark.parametrize("data", ["not json", "[1, 2]", "{}", '{"algorithm": ""}', 42, None])
def test_a_malformed_enrolment_request_is_a_value_error(data):
    with pytest.raises(ValueError):
        pqcm.parse_enrolment_request(data)


# -- the station's reply ------------------------------------------------------------------


def test_reply_round_trips_the_public_key_and_its_key_id():
    public_key = os.urandom(1312)
    data = pqcm.pack_enrolment_reply("ML-DSA-44", public_key)
    obj = json.loads(data)
    assert set(obj) == {"algorithm", "public_key", "key_id"}
    assert "private_key" not in data

    algorithm, parsed, key_id = pqcm.parse_enrolment_reply(data)
    assert (algorithm, parsed) == ("ML-DSA-44", public_key)
    assert key_id == pqcm.key_id_for(public_key)
    # dict form too
    assert pqcm.parse_enrolment_reply(obj)[1] == public_key


def test_a_reply_containing_a_private_key_is_refused():
    obj = _reply_obj(os.urandom(1312), private_key=base64.b64encode(b"secret").decode())
    with pytest.raises(ValueError, match="private key"):
        pqcm.parse_enrolment_reply(json.dumps(obj))


def test_a_reply_whose_key_id_does_not_match_is_refused():
    obj = _reply_obj(os.urandom(1312), key_id="0" * 16)
    with pytest.raises(ValueError, match="does not match"):
        pqcm.parse_enrolment_reply(obj)


@pytest.mark.parametrize("change", [
    {"key_id": None},                       # missing key_id
    {"public_key": "@@not-base64@@"},       # bad base64
    {"public_key": ""},                     # empty key
    {"public_key": None},                   # missing key
    {"algorithm": ""},                      # missing algorithm
])
def test_a_malformed_reply_is_a_value_error(change):
    obj = _reply_obj(os.urandom(1312), **change)
    obj = {k: v for k, v in obj.items() if v is not None}
    with pytest.raises(ValueError):
        pqcm.parse_enrolment_reply(obj)


def test_pack_reply_refuses_a_bad_algorithm():
    with pytest.raises(ValueError):
        pqcm.pack_enrolment_reply("", os.urandom(32))


# -- PQAuthChallenge with key_id -----------------------------------------------------


def test_a_challenge_can_name_the_key_to_sign_with():
    key_id = pqcm.key_id_for(os.urandom(1312))
    nonce = os.urandom(32)
    msg = pqcm.build_challenge_message(nonce, key_id=key_id)
    parsed_nonce, payload = pqcm.parse_challenge_data(msg.data)
    assert parsed_nonce == nonce
    assert pqcm.challenge_key_id(payload) == key_id


def test_a_challenge_without_key_id_still_works():
    _nonce, payload = pqcm.parse_challenge_data(pqcm.build_challenge_message(os.urandom(32)).data)
    assert pqcm.challenge_key_id(payload) is None


@pytest.mark.parametrize("bad", ["short", "Z" * 16, "ABCDEF0123456789", 1234567890123456])
def test_a_challenge_with_a_bad_key_id_is_a_value_error(bad):
    with pytest.raises(ValueError):
        pqcm.challenge_key_id({"nonce": "AA==", "key_id": bad})


# -- the deprecated install message is untouched until section 7.8 ------------------------


def test_the_deprecated_install_message_still_round_trips():
    key = os.urandom(2560)
    algorithm, parsed = pqcm.parse_install_data(pqcm.build_install_message("CP0001", key).data)
    assert (algorithm, parsed) == ("ML-DSA-44", key)


# -- real ML-DSA: the key that crosses is the key that verifies ---------------------------


def test_a_real_public_key_survives_the_reply_and_verifies():
    pytest.importorskip("quantcrypt", reason="PQ backend; see requirements.txt")
    pq = pytest.importorskip("crypto.pq")
    pq_auth = pytest.importorskip("crypto.pq_auth")

    provider = pq.PQProvider()
    private_key, public_key = provider.generate_keypair()   # made on the "station"

    # station -> server: only the public key crosses
    algorithm, received, key_id = pqcm.parse_enrolment_reply(
        pqcm.pack_enrolment_reply("ML-DSA-44", public_key)
    )
    assert received == public_key and key_id == pqcm.key_id_for(public_key)

    # server enrols what it received, challenges with the key_id; the station signs
    auth = pq_auth.PQAuthenticator(provider)
    auth.enrol("CP0001", received)
    nonce = auth.issue_challenge("CP0001")
    challenge = pqcm.build_challenge_message(nonce, key_id=key_id)
    parsed_nonce, payload = pqcm.parse_challenge_data(challenge.data)
    assert pqcm.challenge_key_id(payload) == key_id

    signature = pq_auth.sign_challenge(provider, private_key, parsed_nonce)
    assert auth.verify_response("CP0001", pqcm.parse_signature(pqcm.pack_signature(signature))) is True
