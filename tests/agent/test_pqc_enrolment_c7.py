"""
Contract 7 end to end, on the charger side, with REAL ML-DSA. Track C (tests). C-P2.

Through the agent's real DataTransfer handler (agent/client.py) and the
station's real handlers (agent/station.py), against Track B's real
crypto/pq.py and crypto/pq_auth.py. Only the socket is missing.

The decisive tests:
  * test_enrolment_then_challenge_verifies_with_the_servers_authenticator
        RequestPQEnrolment -> the station's public key -> Track B's
        PQAuthenticator enrols it -> PQAuthChallenge with key_id ->
        verify_response is True. The private key never crossed.
  * test_a_restarted_charger_still_proves_its_identity
        L01: a NEW ChargingStation on the same key folder answers the
        server's challenge without being enrolled again.

Skips without quantcrypt, like test_pqc_integration.py.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("quantcrypt", reason="PQ backend; see requirements.txt")
pq = pytest.importorskip("crypto.pq", reason="Track B crypto/pq.py")
pq_auth = pytest.importorskip("crypto.pq_auth", reason="Track B crypto/pq_auth.py")

from agent import pqc_messages as pqcm  # noqa: E402
from agent.client import StationClient  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.station import ChargingStation  # noqa: E402


def station_with_keys(tmp_path, station_id="CP0001", **kw):
    st = ChargingStation(AgentConfig(station_id=station_id, csms_url="ws://localhost:9000",
                                     pq_key_dir=str(tmp_path), **kw))
    return st, StationClient(station_id, connection=None, commands=st)


async def send(client, message):
    return await client.on_data_transfer(
        vendor_id=message.vendor_id, message_id=message.message_id, data=message.data
    )


@pytest.mark.asyncio
async def test_enrolment_then_challenge_verifies_with_the_servers_authenticator(tmp_path):
    station, client = station_with_keys(tmp_path)
    auth = pq_auth.PQAuthenticator(pq.PQProvider())

    reply = await send(client, pqcm.build_enrolment_request("CP0001"))
    assert reply.status == pqcm.STATUS_ACCEPTED
    assert "private_key" not in reply.data

    algorithm, public_key, key_id = pqcm.parse_enrolment_reply(reply.data)
    assert (algorithm, key_id) == ("ML-DSA-44", station.pq.key_id)
    assert len(public_key) == 1312                           # ML-DSA-44 public key
    auth.enrol("CP0001", public_key)

    nonce = auth.issue_challenge("CP0001")
    answer = await send(client, pqcm.build_challenge_message(nonce, key_id=key_id))
    assert answer.status == pqcm.STATUS_ACCEPTED
    assert auth.verify_response("CP0001", pqcm.parse_signature(answer.data)) is True


@pytest.mark.asyncio
async def test_a_restarted_charger_still_proves_its_identity(tmp_path):
    first, client = station_with_keys(tmp_path)
    reply = await send(client, pqcm.build_enrolment_request("CP0001"))
    _alg, public_key, key_id = pqcm.parse_enrolment_reply(reply.data)

    auth = pq_auth.PQAuthenticator(pq.PQProvider())          # server keeps only the public key
    auth.enrol("CP0001", public_key)

    restarted, client2 = station_with_keys(tmp_path)         # new process, same disk
    assert restarted.pq.is_migrated and restarted.pq.key_id == key_id

    nonce = auth.issue_challenge("CP0001")
    answer = await send(client2, pqcm.build_challenge_message(nonce, key_id=key_id))
    assert auth.verify_response("CP0001", pqcm.parse_signature(answer.data)) is True


@pytest.mark.asyncio
async def test_after_rotation_the_old_key_id_still_answers_and_the_new_one_verifies(tmp_path):
    station, client = station_with_keys(tmp_path)
    auth = pq_auth.PQAuthenticator(pq.PQProvider())

    _a, old_pub, old_id = pqcm.parse_enrolment_reply((await send(client, pqcm.build_enrolment_request("CP0001"))).data)
    _a, new_pub, new_id = pqcm.parse_enrolment_reply((await send(client, pqcm.build_enrolment_request("CP0001"))).data)
    assert old_id != new_id

    for pub, kid in ((old_pub, old_id), (new_pub, new_id)):
        auth.enrol("CP0001", pub)
        nonce = auth.issue_challenge("CP0001")
        answer = await send(client, pqcm.build_challenge_message(nonce, key_id=kid))
        assert auth.verify_response("CP0001", pqcm.parse_signature(answer.data)) is True


@pytest.mark.asyncio
async def test_a_station_signing_with_the_wrong_key_fails_verification(tmp_path):
    """E5's property, Contract 7 edition: a different charger's own key does not pass."""
    honest, honest_client = station_with_keys(tmp_path / "a", "CP0001")
    impostor, impostor_client = station_with_keys(tmp_path / "b", "CP0001")
    auth = pq_auth.PQAuthenticator(pq.PQProvider())

    _a, pub, _k = pqcm.parse_enrolment_reply((await send(honest_client, pqcm.build_enrolment_request("CP0001"))).data)
    await send(impostor_client, pqcm.build_enrolment_request("CP0001"))
    auth.enrol("CP0001", pub)

    nonce = auth.issue_challenge("CP0001")
    answer = await send(impostor_client, pqcm.build_challenge_message(nonce))
    assert auth.verify_response("CP0001", pqcm.parse_signature(answer.data)) is False


@pytest.mark.asyncio
async def test_refusals_are_rejected_answers_never_callerrors(tmp_path):
    legacy, legacy_client = station_with_keys(tmp_path / "l", supported_algorithms=["ECDSA-P256"])
    r = await send(legacy_client, pqcm.build_enrolment_request("CP0001"))
    assert r.status == pqcm.STATUS_REJECTED

    _station, client = station_with_keys(tmp_path / "s")
    bad = pqcm.build_enrolment_request("CP0001")
    r = await client.on_data_transfer(vendor_id=bad.vendor_id, message_id=bad.message_id, data="{not json")
    assert r.status == pqcm.STATUS_REJECTED

    r = await send(client, pqcm.build_enrolment_request("CP0001", algorithm="ML-DSA-87"))
    assert r.status == pqcm.STATUS_REJECTED

    # a challenge naming a key this station never had
    await send(client, pqcm.build_enrolment_request("CP0001"))
    r = await send(client, pqcm.build_challenge_message(b"\x01" * 32, key_id="0" * 16))
    assert r.status == pqcm.STATUS_REJECTED


@pytest.mark.asyncio
async def test_the_saved_file_holds_the_key_the_reply_announced(tmp_path):
    _station, client = station_with_keys(tmp_path)
    _a, public_key, key_id = pqcm.parse_enrolment_reply((await send(client, pqcm.build_enrolment_request("CP0001"))).data)
    on_disk = json.loads((tmp_path / "CP0001.json").read_text())
    assert on_disk["current"]["key_id"] == key_id == pqcm.key_id_for(public_key)


def test_the_load_generator_warms_the_provider_before_spawning():
    from harness.load_generator import warm_up_pq_provider

    ms = warm_up_pq_provider()
    assert ms is not None and ms >= 0
